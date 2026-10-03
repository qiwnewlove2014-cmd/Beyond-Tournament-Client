"""Cinema speakers - the Music Bot's half (extracted from controller.py).

A cabinet with speakers around it is heard as a room; this is the Music Bot
playing through one. With a cabinet selected the bot feeds that jukebox's room
instead of the listener's ears, through the same `CinemaSpeakerBank` the
cabinet's own playback uses - one room per cabinet, whoever is feeding it.

**The state stays on `MapMusicBot`; the behaviour moved here.** The tests, the
streamer's upload gate (`cinema_force_upload`) and `MapMusicBot`'s own call
sites read `cinema_target`, `cinema_bank` (+ `_key` / `_cabinet`),
`_cinema_announced[_at]` and `_cinema_reshape_at` straight off the bot, so
those attributes are still the bot's, and the two intervals
(`CINEMA_RESHAPE_INTERVAL`, `CINEMA_ANNOUNCE_INTERVAL`) are still declared
there -- this class reads and writes them through its owner, the way
`DrumHandler` writes `gameplay.drum_mode`. The pass-throughs below are the
whole of what this handler needs from the bot, kept in one block so the
coupling is greppable; everything after them is the room's own code, moved
across unchanged.

Usage from MapMusicBot::

    self._cinema_handler = CinemaHandler(self)   # or built on first use
    self._cinema().set_cinema_target("j1")

Rules, the five output paths and every trap: .agents/skills/cinema_speaker_system/.
"""

import time

from .. import options
from ..speech import speak
from ..audio.cinema import (ROOM_REFERENCE_DISTANCE,
                            acquire_bank as cinema_acquire_bank,
                            live_instruments_enabled, plan_reach,
                            preview_room,
                            release_renderer as cinema_release,
                            room_diagnosis as cinema_diagnosis, rooms_enabled,
                            set_enabled as cinema_set_enabled,
                            set_live_instruments, set_rooms_enabled,
                            set_speech_enabled as set_cinema_speech,
                            speech_enabled as cinema_speech_enabled)
from ..audio.cinema import pan as cinema_pan
from ..audio.cinema import peer as cinema_peer


class CinemaHandler:
    """The Music Bot's cinema-room output (see the module docstring)."""

    def __init__(self, bot):
        self._bot = bot          # back-reference to MapMusicBot

    # ------------------------------------------------------------------
    # the whole of what this handler needs from its bot
    # ------------------------------------------------------------------

    # Callbacks into the bot.
    def _find_gameplay(self):
        return self._bot._find_gameplay()

    def _delete_source(self, src):
        return self._bot._delete_source(src)

    def _show_mode_menu(self):
        return self._bot._show_mode_menu()

    def _seek_restart(self, position):
        return self._bot._seek_restart(position)

    def track_position(self):
        return self._bot.track_position()

    # Values read (or, below, written) through the bot.
    @property
    def game(self):
        return self._bot.game

    @property
    def volume(self):
        return self._bot.volume

    @property
    def streamer(self):
        return self._bot.streamer

    @property
    def playing(self):
        return self._bot.playing

    # State the bot owns: every one of these is read there by a test or by the
    # class itself, so the attribute keeps living on the bot.
    @property
    def cinema_target(self):
        return self._bot.cinema_target

    @cinema_target.setter
    def cinema_target(self, value):
        self._bot.cinema_target = value

    @property
    def cinema_bank(self):
        return self._bot.cinema_bank

    @cinema_bank.setter
    def cinema_bank(self, value):
        self._bot.cinema_bank = value

    @property
    def cinema_bank_key(self):
        return self._bot.cinema_bank_key

    @cinema_bank_key.setter
    def cinema_bank_key(self, value):
        self._bot.cinema_bank_key = value

    @property
    def cinema_cabinet(self):
        return self._bot.cinema_cabinet

    @cinema_cabinet.setter
    def cinema_cabinet(self, value):
        self._bot.cinema_cabinet = value

    @property
    def instruments_cinema(self):
        return self._bot.instruments_cinema

    @instruments_cinema.setter
    def instruments_cinema(self, value):
        self._bot.instruments_cinema = value

    @property
    def _cinema_announced(self):
        return self._bot._cinema_announced

    @_cinema_announced.setter
    def _cinema_announced(self, value):
        self._bot._cinema_announced = value

    @property
    def _cinema_announced_at(self):
        return self._bot._cinema_announced_at

    @_cinema_announced_at.setter
    def _cinema_announced_at(self, value):
        self._bot._cinema_announced_at = value

    @property
    def _cinema_reshape_at(self):
        return self._bot._cinema_reshape_at

    @_cinema_reshape_at.setter
    def _cinema_reshape_at(self, value):
        self._bot._cinema_reshape_at = value

    # ------------------------------------------------------------------
    # the room (moved from controller.py unchanged)
    # ------------------------------------------------------------------

    def cinema_cabinets(self):
        """``(id, anchor, room)`` for every jukebox on the current map.

                    ``room`` is None for a cabinet with no speakers resolved around it, so the menu can
                    say which are ready. The room is only *previewed*, never acquired: listing what the
                    map has must not turn the feature on.
                    """
        gp = self._find_gameplay()
        map_obj = getattr(gp, "map", None) if gp else None
        cabinets = []
        for zone in list(getattr(map_obj, "jukebox_list", None) or ()):
            try:
                anchor = tuple(float(value) for value in zone.center)
            except Exception:
                continue
            cabinet_id = str(getattr(zone, "id", ""))
            try:
                room = preview_room(self.game, anchor, room_id=cabinet_id)
            except Exception:
                room = None
            cabinets.append((cabinet_id, anchor, room))
        return cabinets

    @staticmethod
    def _cinema_key(cabinet_id):
        """This bot feeds a cabinet under its own key.

                    The room's speakers are the cabinet's, but the registry entry is separate so a song
                    playing on that jukebox at the same time cannot share (and tear down) this bank.
                    """
        return f"musicbot:{cabinet_id}"

    def cinema_target_label(self):
        """Menu text for the current routing choice."""
        if not getattr(self, "cinema_target", None):
            return "Cinema Speakers: OFF (play at my ears)"
        for cabinet_id, anchor, room in self.cinema_cabinets():
            if cabinet_id == self.cinema_target:
                where = room.summary() if room is not None else self._cinema_problem(anchor)
                return f"Cinema Speakers: jukebox {cabinet_id} ({where})"
        return f"Cinema Speakers: jukebox {self.cinema_target} (not on this map)"

    def cinema_routing_allowed(self):
        """Whether this account may route the bot into a cabinet's room.

                    Server-owned: Developer/Contributor only - feeding a room takes the cabinet's speakers
                    over, so lower staff get a plain Music Bot with no Cinema Speakers line at all.
                    """
        gp = self._find_gameplay()
        return bool(getattr(gp, "can_use_cinema_speakers", False))

    def cinema_active_target(self):
        """The cabinet this bot may feed right now, or None for normal playback.

                    A choice saved before a rank change is not acted on: the routing drops back to the
                    listener's ears rather than through a room whose menu line is no longer visible.
                    """
        if not self.cinema_routing_allowed():
            return None
        return getattr(self, "cinema_target", None)

    def cinema_listening_allowed(self):
        """Whether this account gets the listener-side cinema switches.

                    Two lines ask this - the rooms switch and the live-instrument routing - and both are
                    about how *this* listener hears a cabinet, both taking nothing from anybody, so the
                    rule is the Music Bot's own access rule, asked rather than copied. The *song* routing
                    above stays Developer/Contributor. The Server is accepted too.
                    """
        gp = self._find_gameplay()
        if gp is None:
            return False
        if getattr(gp, "can_use_cinema_speakers", False):
            return True
        can_use = getattr(gp, "_can_use_music_bot", None)
        if callable(can_use):
            return bool(can_use())
        return bool(getattr(gp, "can_use_music_bot", False)
                    or getattr(gp, "is_staff", False)
                    or getattr(gp, "is_builder", False)
                    or getattr(gp, "is_technician", False))

    def instruments_cinema_active(self):
        """Whether this client plays live instruments through the room.

                    The listener's own choice, like ``cinema_speakers``: the performer's notes arrive with
                    their world position and every client decides for itself whether to also play them at
                    the speakers, so no packet changes and two listeners in one hall can disagree. Open to
                    everybody and on by default.
                    """
        return live_instruments_enabled()

    def instruments_cinema_label(self):
        """Menu text for the live-instrument routing choice."""
        if not live_instruments_enabled():
            return "Instruments: OFF (played where they stand)"
        return "Instruments: through the nearest cabinet's room"

    def toggle_instruments_cinema(self):
        """Send (or stop sending) live instruments into the room.

                    Turning it off never needs a bank handed back: the notes were one sample per speaker,
                    never in the room's buffers, so the next note is simply not spawned there.
                    """
        self.instruments_cinema = not live_instruments_enabled()
        set_live_instruments(self.instruments_cinema)
        if self.instruments_cinema:
            speak("Instruments through the cabinet speakers.")
        else:
            speak("Instruments played where they stand.")

    def speech_cinema_label(self):
        """Menu text for whether a voice comes out of the room it is spoken in.

                    The third listening switch, next to songs and instruments: a talker inside a cabinet's
                    room is heard from that room's speakers instead of the map's PA.
                    """
        if not cinema_speech_enabled():
            return "Speech: through the map's PA speakers"
        return "Speech: through the nearest cabinet's room"

    def toggle_speech_cinema(self):
        """Hear a voice from the room it is spoken in, or from the map's PA.

                    Nothing has to be handed back when it changes: the next frame is fed by the other path,
                    and the room that was playing the voice is released by the frame that no longer routes
                    to it.
                    """
        enabled = not cinema_speech_enabled()
        set_cinema_speech(enabled)
        if enabled:
            speak("Speech comes from the cabinet's room.")
        else:
            speak("Speech uses the map's PA speakers.")

    def cinema_rooms_label(self):
        """Menu text for whether songs come out of the rooms around cabinets.

                    A listening choice like the two lines next to it (it used to be a paragraph in
                    Options). Which room a single cabinet uses is still the map's decision; off is the
                    plain two-source stereo for this listener.
                    """
        if not rooms_enabled():
            return "Cinema rooms: OFF (every jukebox plays its own stereo here)"
        return "Cinema rooms: ON (songs come from the speakers around a cabinet)"

    def toggle_cinema_rooms(self):
        """Hear a jukebox through its room, or as the plain two-source stereo."""
        enabled = not rooms_enabled()
        set_rooms_enabled(self.game, enabled)
        if enabled:
            speak("Songs come from the rooms around cabinets.")
        else:
            speak("Every jukebox plays its own stereo.")

    def own_sound_label(self):
        """Read-only line: where staff have put *your* voice.

                    A pan is a staff decision relayed to the map and the owner is told once when it lands;
                    after that it can only be *heard*. This answers it the way the pan menu answers, and
                    it changes nothing - never a veto.
                    """
        gp = self._find_gameplay()
        if gp is None:
            return "Your sound: unknown here"
        return cinema_pan.own_label(gp)

    def announce_own_sound(self):
        """Say where your own voice is heard, and what decides what you hear.

                    The two halves are said apart on purpose: other players hear the staff-chosen room,
                    while the owner hears it according to their own ``Cinema rooms``/``Instruments``/
                    ``Speech`` switches. Same sentence the announcement uses.
                    """
        gp = self._find_gameplay()
        if gp is None:
            speak("Your sound is not known here.")
            return
        report = cinema_pan.own_report(gp)
        if report is None:
            speak("No staff pan is moving your voice: other players hear you "
                  "from where you stand.")
            return
        speak(report)

    def _cinema_problem(self, anchor, cabinet_id=None):
        """Why this cabinet's speakers cannot be used, in the resolver's words.

                    "No speakers found" leaves a tester standing in front of four of them with no idea
                    what is wrong; the reason a room was refused is knowable, so say it instead.
                    """
        try:
            return cinema_diagnosis(self.game, anchor, room_id=cabinet_id)
        except Exception as exc:
            return f"speakers unusable ({exc})"

    def _cinema_map_help(self):
        """What this map is missing for a room, for the empty-cabinet menu."""
        from ..audio.cinema import map_speakers
        try:
            speakers = len(map_speakers(self.game))
        except Exception:
            speakers = 0
        if speakers:
            return (f"This map has {speakers} cinema speaker(s) but no Jukebox to "
                    f"anchor them. Add one: Builder menu (F8) -> Musical "
                    f"Instrument -> Jukebox.")
        return ("This map has no Jukebox to build a cinema room around. Add one: "
                "Builder menu (F8) -> Musical Instrument -> Jukebox.")

    def cinema_force_upload(self):
        """Whether routing to a room means this bot must upload at all.

                    A room is a place other people stand in, so a song played only into the sender's own
                    copy would leave the venue silent. The Broadcast switch itself is untouched: this only
                    ORs into the upload gate. (A question, not a value: the bot's own
                    ``cinema_force_upload`` is the property the streamer's upload gate reads.)
                    """
        return bool(self.cinema_active_target())

    def announce_cinema_target(self, force=False):
        """Tell the map which cabinet's room this bot is playing through.

                    Listeners cannot work this out themselves - the room is a choice made on this client.
                    Announced when it changes and repeated at :data:`CINEMA_ANNOUNCE_INTERVAL`, which is
                    what lets a mid-song joiner hear the song from the room.
                    """
        target = self.cinema_active_target()
        # Read defensively: a bot object built by a test (or restored by an
        # older build) may never have been through __init__. Announcing is
        # never worth an exception on a path a menu click runs.
        announced = getattr(self, "_cinema_announced", None)
        now = time.monotonic()
        if not force and target == announced:
            if target is None:
                return False
            last = getattr(self, "_cinema_announced_at", 0.0)
            if now - last < self._bot.CINEMA_ANNOUNCE_INTERVAL:
                return False
        network = getattr(self.game, "network", None)
        if network is None:
            return False
        from .. import consts
        network.send(consts.CHANNEL_MISC, cinema_peer.ANNOUNCE_EVENT,
                     {"cabinet": str(target or "")})
        self._cinema_announced = target
        self._cinema_announced_at = now
        return True

    def set_cinema_target(self, cabinet_id):
        """Route (or un-route) this bot through a cabinet's speaker room.

                    Applying it restarts the current track the same way a seek does, so the change is
                    heard immediately instead of at the next song.
                    """
        cabinet_id = str(cabinet_id or "") or None
        self.cinema_target = cabinet_id
        options.set("music_bot_cinema_target", cabinet_id or "")
        self._release_cinema_bank()
        room = None
        if cabinet_id is not None:
            # Selecting a room is an explicit request for the feature, so the
            # local master switch cannot silently swallow it.
            cinema_set_enabled(self.game, True)
            for other_id, anchor, other_room in self.cinema_cabinets():
                if other_id == cabinet_id:
                    room = other_room
                    break
        if cabinet_id is None:
            speak("Cinema speakers off. Music plays at your ears again.")
        elif room is None:
            anchor = None
            for other_id, other_anchor, _room in self.cinema_cabinets():
                if other_id == cabinet_id:
                    anchor = other_anchor
                    break
            reason = (self._cinema_problem(anchor, cabinet_id) if anchor is not None
                      else "that jukebox is not on this map")
            speak(f"Jukebox {cabinet_id} cannot play through its speakers: {reason}. "
                  f"Music will play at your ears.")
        else:
            speak(f"Music now plays through the speakers around jukebox {cabinet_id}: "
                  f"{room.summary()}. Everyone in the room hears it from those "
                  f"speakers.")
        # Both directions move the running stream, not just the one that turns
        # the room on. Turning it off deletes the room's sources, which are
        # what the live streamer is feeding.
        self._restart_in_new_output()
        # And both directions are announced: the listeners are playing this
        # song out of a room they were told about, so a change that is not
        # announced leaves them on the old one until the next song.
        self.announce_cinema_target(force=True)

    def _restart_in_new_output(self):
        """Hand a playing track to whichever output now owns the stream.

                    Switching between the room and the ears changes which OpenAL sources carry the audio,
                    so the running stream is restarted into the new one (the seek path). Without it the
                    bot falls silent or writes into deleted sources.
                    """
        if not getattr(self, "playing", False):
            return
        try:
            self._seek_restart(self.track_position() or 0.0)
        except Exception:
            pass

    def _open_cinema_menu(self):
        """Pick the cabinet whose room this bot should play through."""
        from .. import menu as menu_mod, menus
        gp = self._find_gameplay()
        if gp is None:
            return
        if not self.cinema_routing_allowed():
            return

        def go_back():
            gp.pop_last_substate()
            self._show_mode_menu()

        m = menu_mod.Menu(self.game, "Cinema Speakers", parrent=gp)
        items = []
        if self.cinema_target:
            items.append(("Turn Off (play at my ears)",
                          lambda: (self.set_cinema_target(None), go_back())))
        cabinets = self.cinema_cabinets()
        if not cabinets:
            items.append(("No jukebox on this map", lambda: speak(
                self._cinema_map_help())))
        for cabinet_id, anchor, room in cabinets:
            position = f"({anchor[0]:.0f}, {anchor[1]:.0f}, {anchor[2]:.0f})"
            if room is None:
                label = (f"Jukebox {cabinet_id} {position} - "
                         f"{self._cinema_problem(anchor, cabinet_id)}")
            else:
                label = f"Jukebox {cabinet_id} {position} - {room.summary()}"
            picked = cabinet_id == self.cinema_target
            if picked:
                label = f"* {label} (playing here)"
            items.append((label, (lambda cid=cabinet_id: (
                self.set_cinema_target(cid), go_back()))))
        items.append(("Cancel", lambda: gp.pop_last_substate()))
        m.add_items(items)
        menus.set_default_sounds(m)
        gp.add_substate(m)

    def _acquire_cinema_room(self, anchor, room):
        """Take (or re-shape) the room this bot feeds for a resolved cabinet.

                    Called when a track starts and once a second while it plays: the host hands back the
                    SAME bank and re-shapes it in place, so the running stream keeps feeding it.
                    """
        key = self._cinema_key(self.cinema_target)
        bank = cinema_acquire_bank(
            self.game, key, anchor,
            profile=room.profile, specs=room.specs, placement=room.placement,
            fill=room.fill,
            volume=self.volume, cabinet_volume=100,
            # A room is heard from the back row; the bot's own ear source keeps
            # its own falloff and is not part of this. The reach is the
            # cabinet's own, so a bot feeding a hall is heard as far as the
            # hall's own songs are -- one room, not two.
            reference_distance=ROOM_REFERENCE_DISTANCE,
            max_distance=plan_reach(room),
            occlusion_provider=self._cinema_occlusion,
            # The bot's own output answers to the Music slider, not the
            # Jukebox one: the room is only where it comes out.
            category="music",
        )
        if bank is None:
            return None
        self.cinema_bank = bank
        self.cinema_cabinet = self.cinema_target
        self.cinema_bank_key = key
        return bank

    def _cinema_occlusion(self, position, listener, max_distance):
        """Wall occlusion for the room, measured from each speaker's own spot.

                    Without it a speaker in the next room plays as loudly as one beside the listener. The
                    jukebox player's ray already caches tile results, so this is cheap per speaker.
                    """
        gp = self._find_gameplay()
        if gp is None:
            return 0
        provider = getattr(getattr(gp, "jukebox_player", None),
                           "occlusion_tier", None)
        if not callable(provider):
            from ..jukebox import wall_occlusion_tier
            map_obj = getattr(gp, "map", None)
            provider = (lambda pos, lis, _max: wall_occlusion_tier(map_obj, pos, lis))
        try:
            return int(provider(position, listener, max_distance))
        except Exception:
            return 0

    def _ensure_cinema_bank(self):
        """The room this bot should feed right now, or None for normal playback."""
        target = self.cinema_active_target()
        if not target:
            return None
        for attempt in (0, 1):
            for cabinet_id, anchor, room in self.cinema_cabinets():
                if cabinet_id != target or room is None:
                    continue
                bank = self.cinema_bank
                if bank is not None and not bank._stopped and bank.sources:
                    return bank
                return self._acquire_cinema_room(anchor, room)
            if attempt == 0:
                # A target restored from settings on a fresh client starts
                # before anything has turned the feature on; selecting a room
                # is itself the request, so honour it.
                cinema_set_enabled(self.game, True)
        return None

    def _release_cinema_bank(self):
        """Silence and delete this bot's room, then hand it back."""
        bank = getattr(self, "cinema_bank", None)
        key = getattr(self, "cinema_bank_key", None)
        self.cinema_bank = None
        self.cinema_cabinet = None
        self.cinema_bank_key = None
        if bank is None:
            return
        try:
            bank.stop()
        except Exception:
            pass
        for source in getattr(bank, "sources", ()) or ():
            self._delete_source(source)
        # Released after the sources, so the bank can never keep a deleted
        # OpenAL name, and its per-speaker buffers go back with it.
        try:
            bank.forget_sources()
        except Exception:
            pass
        if key:
            try:
                cinema_release(self.game, key)
            except Exception:
                pass

    def _update_cinema_output(self):
        """Per-frame room upkeep: gains, map changes, recovery from a lost room.

                    A jukebox stop, map change or map reload can take the shared bank away; the bot then
                    rebuilds it around the stream that is already playing rather than going silent.
                    """
        if not self.cinema_active_target():
            if getattr(self, "cinema_bank", None) is not None:
                self._release_cinema_bank()
            return
        bank = self.cinema_bank
        if bank is None or bank._stopped or not bank.sources:
            self._release_cinema_bank()
            bank = self._ensure_cinema_bank()
            streamer = self.streamer
            if bank is None or streamer is None:
                return
            streamer.cinema = bank
            streamer.source = getattr(bank, "primary_source", None)
        self._follow_cinema_map(bank)
        try:
            bank.update_output()
        except Exception:
            pass

    def _follow_cinema_map(self, bank):
        """Let the playing room follow the map under it.

                    A builder placing or deleting a speaker mid-song gets the change without toggling the
                    routing off and on (which restarts the track): the room is re-resolved at most once a
                    second and re-shaped IN PLACE (``CinemaSpeakerBank.reconfigure``), so the speakers
                    that did not change keep playing.
                    """
        now = time.monotonic()
        if now - getattr(self, "_cinema_reshape_at", 0.0) < self._bot.CINEMA_RESHAPE_INTERVAL:
            return
        self._cinema_reshape_at = now
        cabinet = getattr(self, "cinema_cabinet", None)
        if not cabinet:
            return
        for cabinet_id, anchor, room in self.cinema_cabinets():
            if cabinet_id != cabinet:
                continue
            if room is None:
                # An edit that removed the room must never yank the audio out
                # from under a song that is already playing: keep the room
                # that is audible and let the next track re-decide.
                return
            self._acquire_cinema_room(anchor, room)
            return
