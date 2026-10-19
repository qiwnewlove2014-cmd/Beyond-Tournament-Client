"""The cabinet's ears: one JukeboxPlayer owns every cabinet's output.

Decoding, distance attenuation, the EQ, the cinema room hand-off and the relay receiver's
lifecycle all live here, because they share one thing a reader must hold in mind at once:
what is audible right now and where the song's position stamp says it should be. What
watches that output once a frame -- whether it is still playing at all, and the ladder that
repairs it -- is `recovery.py`, which this class inherits."""

import contextlib
from collections import OrderedDict
from math import trunc
import threading
import time

from ..audio.cinema import (CINEMA_AUTO, CINEMA_OFF, ROOM_REFERENCE_DISTANCE,
                            acquire_bank, cabinet_anchor, cinema_room,
                            release_renderer)
from ..audio_diagnostics import probe as audio_probe
from ..audio_health import probe as audio_health
from ..deferred_log import log_deferred as log_line

from .constants import (CINEMA_PROFILES, OCCLUSION_CLEAR, OCCLUSION_FULL,
                        OCCLUSION_LIGHT, _CINEMA_SILENT)
from .media_cache import JukeboxMediaCache
from .recovery import RecoveryWatchdog
from .relay import JukeboxRelayReceiver
from .routes import RelayRoutes


def wall_occlusion_tier(cur_map, src_pos, listener):
    # Classify wall thickness between two points for discrete-filter sites: wraps
    # map.occlusion_tier() (which counts wall tiles along the ray, so a single pillar
    # only lightly muffles) with a legacy boolean fallback for stub/foreign maps.
    if cur_map is None or src_pos is None or listener is None:
        return OCCLUSION_CLEAR
    tfn = getattr(cur_map, "occlusion_tier", None)
    if tfn is not None:
        try:
            return int(tfn(src_pos, listener))
        except Exception:
            return OCCLUSION_CLEAR
    try:
        if cur_map.valid_straight_path(src_pos, listener) is False:
            return OCCLUSION_FULL
    except Exception:
        pass
    return OCCLUSION_CLEAR


class JukeboxPlayer(RecoveryWatchdog, RelayRoutes):
    # Plays one song per jukebox, anchored at the jukebox's 3D position: each jukebox id
    # gets its own OpenAL source + ffmpeg streamer thread. The watchdog -- update() and
    # the relay recovery ladder it runs -- is the base class in recovery.py.

    def __init__(self, game):
        self.game = game
        self.players = {}  # jukebox_id -> {"source", "secondary_source", "streamer", "title", "url"}
        # Only URL/header metadata survives map changes, never audio sources
        # or a playback position. Eight short-lived entries per player owner.
        self._media_cache = JukeboxMediaCache()
        self.volume = 65
        self._lock = threading.Lock()
        self.relay_routes = {}
        # Clocks whose output was replaced while a jam note was still waiting on it (see
        # _keep_waiting_notes): update pumps only the entry's *current* streamer, so these
        # are pumped here until they have nothing left, and dropped when a map change takes
        # the song with it.
        self._orphan_clocks = []
        # Frames buffered for relay routes whose jukebox_play event is still sitting on the
        # deferred game queue (see pend_relay_route): this is what keeps the first fraction
        # of a song -- its intro -- from being dropped between the network thread and the
        # main loop.
        self._relay_pending = {}
        self._control_serial = 0
        # Jukebox ids awaiting a post-reload play confirmation (see
        # mark_pending_map_change). Confirmed players are kept playing
        # seamlessly; unconfirmed ones are stopped after a short grace period.
        self._pending_map_change = set()
        self._pending_map_change_serial = None
        self._last_recovery_request_at = 0.0
        # Consecutive relay recovery failures per jukebox + sticky direct-playback deadlines
        # (see update() -- the cure for "sound dies until a full relogin": map changes keep
        # the same ENet peer, so only a fresh connection or local direct playback restores
        # audio).
        self._relay_fail_counts = {}
        self._direct_fallback_until = {}
        self._stall_unsticks = {}
        self.eq_profiles = {}
        self.eq_values = {}
        self.eq_slots = {}
        self.custom_eq_slots = {}
        self.cabinet_volumes = {}
        # The cinema profile each cabinet's room actually resolved to, for
        # diagnostics. The *authority* on what a cabinet should play is the
        # server's own hint (below) plus the map's speakers.
        self.cinema_profiles = {}
        # The mode the map has for each cabinet (auto / off / a profile id), as last told by
        # the server. Kept locally because a mid-song map refresh re-resolves the room
        # without a fresh play event, and because the cabinet menu has to answer before
        # another song starts.
        self._cinema_modes = {}
        # Each cabinet's own reach, in metres (``cinema_radius``): how far its
        # room takes speakers in and how far from a listener it is heard. One
        # number, two names -- see ``audio.cinema.plugin.cabinet_reach``.
        self._cinema_reaches = {}
        # Placement warnings already written to the log, per cabinet.
        self._cinema_warned = {}
        # When the playing rooms last re-resolved their shape (see
        # refresh_cinema_rooms): a builder placing a speaker mid-song must not
        # have to wait for the next song to hear it.
        self._cinema_refresh_at = 0.0
        self._occlusion_filter = None
        self._light_occlusion_filter = None

        self._occlusion_cache = OrderedDict()
        self._occlusion_lock = threading.Lock()
        self._retired_relays = []
        # Direct tails retired at song advance: the streamer keeps playing to
        # its natural EOF while the next song resolves and holds for its own
        # lead-in (see _retire_or_stop).
        self._retiring_direct = []

    def occlusion_tier(self, box_pos, listener, max_distance=40.0):
        # Skip inaudible rays and briefly reuse exact tile-ray results. The main relay pump
        # and the legacy direct worker may both query this; only the small cache is locked,
        # never the map scan or any audio operation.
        if listener is None or box_pos is None:
            return OCCLUSION_CLEAR
        if sum((listener[i] - box_pos[i]) ** 2 for i in range(3)) >= (max_distance + 2.5) ** 2:
            return OCCLUSION_CLEAR  # Both stereo channels are out of range.
        gp = getattr(self.game, "gameplay", None)
        cur_map = getattr(gp, "map", None)
        if cur_map is None:
            return OCCLUSION_CLEAR
        tiles = getattr(cur_map, "tile_list", ())
        key = (id(cur_map), id(tiles), len(tiles),
               tuple(trunc(value) for value in box_pos),
               tuple(trunc(value) for value in listener))
        now = time.monotonic()
        with self._occlusion_lock:
            cached = self._occlusion_cache.get(key)
            if cached is not None and now - cached[0] < 0.15:
                self._occlusion_cache.move_to_end(key)
                return cached[1]
        tier = wall_occlusion_tier(cur_map, box_pos, listener)
        with self._occlusion_lock:
            # Retain identity owners until eviction: otherwise a rebuilt tile
            # list can immediately reuse an old id and inherit its wall tier.
            self._occlusion_cache[key] = (now, tier, cur_map, tiles)
            self._occlusion_cache.move_to_end(key)
            while len(self._occlusion_cache) > 64:
                self._occlusion_cache.popitem(last=False)
        return tier

    def get_occlusion_filter(self):
        """Lazy-create and return a shared Lowpass filter for wall occlusion muffling."""
        if self._occlusion_filter is None:
            audio = getattr(self.game, "audio_mngr", None)
            if audio is not None and hasattr(audio, "gen_filter"):
                try:
                    self._occlusion_filter = audio.gen_filter(
                        "LOWPASS",
                        ("GAINHF", 0.05),
                        ("GAIN", 0.22),
                    )
                except Exception:
                    self._occlusion_filter = None
        return self._occlusion_filter

    def get_light_occlusion_filter(self):
        # Lazy-create a gentle Lowpass for PARTIALLY occluded playback: a thin obstacle (a
        # lone pillar tile between cabinet and listener) should only slightly dull the song,
        # unlike the heavy full-wall filter above.
        if self._light_occlusion_filter is None:
            audio = getattr(self.game, "audio_mngr", None)
            if audio is not None and hasattr(audio, "gen_filter"):
                try:
                    self._light_occlusion_filter = audio.gen_filter(
                        "LOWPASS",
                        ("GAINHF", 0.45),
                        ("GAIN", 0.75),
                    )
                except Exception:
                    self._light_occlusion_filter = None
        return self._light_occlusion_filter

    EQ_PRESETS = {
        "bass_boost": (
            ("low_gain", 7.0),
            ("low_cutoff", 260.0),
            ("mid1_gain", 0.9),
            ("high_gain", 1.0),
            ("high_cutoff", 4000.0),
        ),
    }

    @staticmethod
    def _normalize_eq_values(values):
        values = values if isinstance(values, dict) else {}
        normalized = {}
        for band in ("bass", "mid", "treble"):
            try:
                value = int(values.get(band, 50))
            except (TypeError, ValueError):
                value = 50
            normalized[band] = max(0, min(100, value))
        return normalized

    @classmethod
    def _custom_eq_parameters(cls, values):
        # Map accessible 0-100 sliders to safe OpenAL EQUALIZER gains. 50 is unity/flat; the
        # exponential curve gives equal perceptual room above and below unity while staying
        # inside the EFX 0.126-7.943 range. Both mid filters move together so the single Mid
        # slider covers vocals and instruments instead of one narrow centre frequency.
        values = cls._normalize_eq_values(values)

        def gain(value):
            return 7.0 ** ((value - 50.0) / 50.0)

        mid_gain = gain(values["mid"])
        return (
            ("low_gain", gain(values["bass"])),
            ("low_cutoff", 260.0),
            ("mid1_gain", mid_gain),
            ("mid1_center", 500.0),
            ("mid1_width", 1.0),
            ("mid2_gain", mid_gain),
            ("mid2_center", 3000.0),
            ("mid2_width", 1.0),
            ("high_gain", gain(values["treble"])),
            ("high_cutoff", 4000.0),
        )

    @audio_probe.measured("jukebox.eq")
    def _get_eq_slot(self, profile, jukebox_id=None, eq_values=None):
        # Get or update an OpenAL Hardware Equalizer slot. Presets share a cached slot; a
        # custom profile owns one slot per active cabinet and mutates its effect in place on
        # every slider tick, avoiding EFX slot leaks and keeping changes audible in real time.
        profile = str(profile or "normal").lower()
        if profile != "custom" and profile not in self.EQ_PRESETS:
            return None
        audio = getattr(self.game, "audio_mngr", None)
        if audio is None or getattr(audio, "efx", None) is None or not hasattr(audio, "gen_effect"):
            return None
        if profile == "custom":
            if not jukebox_id:
                return None
            params = self._custom_eq_parameters(eq_values)
            slot = self.custom_eq_slots.get(jukebox_id)
            if slot is None:
                try:
                    slot = audio.gen_effect(
                        "EQUALIZER", *params,
                        hold=("cabinet_eq", f"custom:{jukebox_id}", None),
                    )
                except Exception:
                    slot = None
                self.custom_eq_slots[jukebox_id] = slot
            elif slot is not None:
                effect = getattr(slot, "effect", None)
                if effect is not None:
                    for param in params:
                        try:
                            effect.set(*param)
                        except Exception:
                            pass
                    try:
                        # EFX implementations may snapshot parameters when an
                        # effect is attached; reattach the same object so the
                        # in-place edits become audible without a new slot.
                        slot.effect = effect
                    except Exception:
                        pass
            return slot
        if profile not in self.eq_slots:
            try:
                params = self.EQ_PRESETS[profile]
                self.eq_slots[profile] = audio.gen_effect(
                    "EQUALIZER", *params,
                    hold=("cabinet_eq", f"preset:{profile}", None),
                )
            except Exception:
                self.eq_slots[profile] = None
        return self.eq_slots.get(profile)

    def set_eq_profile(self, jukebox_id, profile, eq_values=None):
        """Update EQ profile for a specific jukebox and re-apply EFX sends in real-time."""
        profile = str(profile or "normal").lower()
        if profile not in ("normal", "bass_boost", "custom"):
            profile = "normal"
        previous_profile = self.eq_profiles.get(jukebox_id, "normal")
        normalized_values = self._normalize_eq_values(eq_values)
        self.eq_profiles[jukebox_id] = profile
        if profile == "custom":
            self.eq_values[jukebox_id] = normalized_values
        else:
            self.eq_values.pop(jukebox_id, None)
        audio = getattr(self.game, "audio_mngr", None)
        if audio is None or getattr(audio, "efx", None) is None:
            return
        slot = self._get_eq_slot(profile, jukebox_id, normalized_values)
        with self._lock:
            entry = self.players.get(jukebox_id)
            if entry is not None:
                streamer = entry.get("streamer")
                if streamer is not None:
                    streamer.eq_slot = slot
                filt = None
                gp = getattr(self.game, "gameplay", None)
                cur_map = getattr(gp, "map", None) if gp is not None else None
                listener = getattr(audio, "position", None)
                src = entry.get("source")
                if cur_map is not None and listener is not None and src is not None and hasattr(cur_map, "valid_straight_path"):
                    pos = src.position
                    tier = self.occlusion_tier((pos[0] + 2.5, pos[1], pos[2]), listener)
                    if tier == OCCLUSION_FULL:
                        filt = self.get_occlusion_filter()
                    elif tier == OCCLUSION_LIGHT:
                        filt = self.get_light_occlusion_filter()
                bank = entry.get("cinema")
                if bank is not None:
                    # Every speaker in the room carries the cabinet's EQ, not
                    # just the front pair; the room re-sends it on its own
                    # next refresh.
                    bank.set_eq_slot(slot)
                    bank.touch_environment()
                    bank.update_output()
                else:
                    for key in ("source", "secondary_source"):
                        src = entry.get(key)
                        if src is not None:
                            try:
                                audio.efx.send(src, 1, slot, filter=filt)
                            except Exception:
                                pass
        if previous_profile == "custom" and profile != "custom":
            old_slot = self.custom_eq_slots.pop(jukebox_id, None)
            if old_slot is not None and hasattr(audio, "release_effect_slot"):
                # A replaced relay may still be fading on the audio owner.
                # Detach its send before lending this slot to another sound.
                for receiver in self._retired_relays:
                    if receiver.eq_slot is old_slot:
                        receiver.eq_slot = None
                        for source in (receiver.source_l, receiver.source_r):
                            with contextlib.suppress(Exception):
                                audio.efx.send(source, 1, None)
                try:
                    audio.release_effect_slot(old_slot)
                except Exception:
                    pass

    def set_cabinet_volume(self, jukebox_id, volume):
        """Set server-synchronized master volume for a specific jukebox cabinet (0-100)."""
        vol = max(0, min(100, int(volume)))
        self.cabinet_volumes[jukebox_id] = vol
        with self._lock:
            p = self.players.get(jukebox_id)
            if p is not None:
                streamer = p.get("streamer")
                if streamer is not None and hasattr(streamer, "set_cabinet_volume"):
                    streamer.set_cabinet_volume(vol)
                # The room is told directly as well: its streamer may be a
                # placeholder with no sources yet, and the room must still
                # carry the cabinet's own volume.
                bank = p.get("cinema")
                if bank is not None:
                    bank.set_cabinet_volume(vol)

    # The stereo pair every cabinet plays through: two mono sources this far
    # either side of the cabinet, full volume inside the reference distance and
    # silent at the maximum (the same arithmetic piano and drums use for their
    # 3D stereo). They are class constants because four places read them -- the
    # source placement, the initial wall check and both streamers -- and a pair
    # whose numbers disagreed would fade at a different distance than it muffles.
    PAIR_OFFSET = 2.5
    PAIR_REFERENCE_DISTANCE = 8.0
    PAIR_MAX_DISTANCE = 40.0

    # Anchored direct playback runs one AudioStreamer.DIRECT_LEAD_IN_S behind the
    # server's audioStartedAt, so the next song's jukebox_play lands while the old song
    # still has its final seconds queued locally. A retired tail may run at most the
    # lead-in plus this margin before the sweep stops it.
    DIRECT_RETIRE_TAIL_MARGIN_S = 5.0
    # Full parse_map reloads can cross the ordered map channel and the misc
    # response channel.  The observed state reply can arrive just over two
    # seconds later, so do not destroy the healthy receiver prematurely.
    MAP_RELOAD_CONFIRM_TIMEOUT = 4.0
    @property
    def control_serial(self):
        with self._lock:
            return self._control_serial

    def set_volume(self, volume):
        """Set local volume level for all jukeboxes (0-100)."""
        self.volume = max(0, min(100, int(volume)))
        with self._lock:
            for p in self.players.values():
                streamer = p.get("streamer")
                if streamer is not None and hasattr(streamer, "set_volume"):
                    streamer.set_volume(self.volume)
                bank = p.get("cinema")
                if bank is not None:
                    bank.set_volume(self.volume)

    def _acquire_cinema(self, jukebox_id, x, y, z, volume, kwargs, keep=None):
        # Build this cabinet's cinema room, or None to play the plain pair. Every failure path
        # (feature off, no room possible, refused sources) returns None. ``keep`` is the room already
        # audible: re-acquiring one passes its trims through, or every refresh would reset the
        # cabinet's volume to 100 and drop the reverb and EQ the song started with.
        try:
            # The map may mark a cabinet (an enforced profile) or surround it with speakers.
            # Either alone is enough to make a room, an explicit "off" refuses one, and with none
            # of them this returns None so the plain two-source playback below runs as it always has.
            mode = self._cinema_mode(jukebox_id, kwargs)
            if mode == CINEMA_OFF:
                return None
            # The reach arrives with the play event too, and it has to be in
            # the cache before the room is resolved: it decides which speakers
            # the room takes in, not only how far it is heard.
            self._cinema_reach(jukebox_id, kwargs)
            requested = None if mode == CINEMA_AUTO else mode
            plan = cinema_room(self.game, jukebox_id, (float(x), float(y), float(z)),
                               requested)
            if plan is None:
                return None
            if getattr(plan, "silent", False) is True:
                # Custom mode, and the map has no speaker for this cabinet: the
                # mode says "only the speakers I placed", and silence is that
                # answer. The callers must not fall back to the plain pair.
                # ``is True`` and not merely truthy: a plan-shaped stand-in that
                # answers to anything must never silence a cabinet.
                log_line(f"[Jukebox] cinema room {jukebox_id}: custom mode "
                         f"with no speakers placed; the cabinet is silent")
                return _CINEMA_SILENT
            self.cinema_profiles[jukebox_id] = plan.profile
            # Map mistakes are worth reporting once, not once per song: a room
            # that is resolved the same way again has nothing new to say.
            warnings = plan.warnings
            if warnings and self._cinema_warned.get(jukebox_id) != warnings:
                self._cinema_warned[jukebox_id] = warnings
                for warning in warnings:
                    log_line(f"[Jukebox] cinema room {jukebox_id}: {warning}")
            return acquire_bank(
                self.game,
                jukebox_id,
                (float(x), float(y), float(z)),
                profile=plan.profile,
                specs=plan.specs or kwargs.get("cinema_speakers"),
                placement=plan.placement,
                # A room read off the map is only the speakers the map has.
                fill=plan.fill,
                volume=volume,
                cabinet_volume=(keep.cabinet_volume * 100.0 if keep is not None
                                else self.cabinet_volumes.get(jukebox_id, 100)),
                # The ROOM's own scale, not the plain pair's 8/40: a room is heard from the back row. The
                # reach is the cabinet's own - the very number its speakers were claimed with.
                reference_distance=ROOM_REFERENCE_DISTANCE,
                max_distance=plan.reach,
                occlusion_provider=self.occlusion_tier,
                reverb_slot=keep.reverb_slot if keep is not None else None,
                eq_slot=keep.eq_slot if keep is not None else None,
            )
        except Exception as ex:
            from .. import logger
            logger.log_exception(ex, f"JukeboxPlayer cinema room ({jukebox_id})")
            log_line(f"[Jukebox] cinema room unavailable for {jukebox_id}: {ex}")
            return None

    # How often a playing room re-resolves the map it is in. A builder who
    # places a speaker sees it join within this long, and the cost is one
    # resolve per playing room (a handful of distance checks).
    CINEMA_REFRESH_INTERVAL = 1.0

    def cinema_mode(self, jukebox_id):
        """The mode this client will play a cabinet in (auto when unknown)."""
        return self._cinema_modes.get(jukebox_id, CINEMA_AUTO)

    def set_local_cinema_mode(self, jukebox_id, mode):
        # Echo a mode change locally so the cabinet menu answers immediately; the Server owns the
        # setting. What makes a PLAYING song change output is the server's re-offer.
        self._cinema_modes[jukebox_id] = str(mode or "").strip().lower() or CINEMA_AUTO

    def cinema_reach(self, jukebox_id):
        # This cabinet's own reach in metres, or None when the map has none. None is a real answer
        # ("the map never said"), so the default belongs to the reader (plugin.cabinet_reach).
        value = self._cinema_reaches.get(jukebox_id)
        return None if value is None else float(value)

    def set_local_cinema_reach(self, jukebox_id, reach):
        # Echo a reach change locally so the cabinet menu answers immediately. Same rule as
        # the mode: the Server owns the number (written into the map and sent to everybody)
        # and the re-offer that follows a change is what re-shapes a playing room; this only
        # makes the menu behind the prompt read the value that was just typed.
        try:
            self._cinema_reaches[jukebox_id] = float(reach)
        except (TypeError, ValueError):
            self._cinema_reaches.pop(jukebox_id, None)

    def _cinema_reach(self, jukebox_id, kwargs=None):
        # The reach the map gives this cabinet, cached from what arrived. It travels with
        # every play event and with the cabinet state, exactly as the mode does. A value that
        # is not a number is not a reach: it is forgotten rather than obeyed, so a bad packet
        # cannot stretch a room across the map -- and a map that says nothing keeps the default.
        kwargs = kwargs or {}
        if "cinema_reach" not in kwargs and "cinema_radius" not in kwargs:
            return self.cinema_reach(jukebox_id)
        raw = kwargs.get("cinema_reach", kwargs.get("cinema_radius"))
        if raw is None:
            return self.cinema_reach(jukebox_id)
        try:
            reach = float(raw)
        except (TypeError, ValueError):
            return self.cinema_reach(jukebox_id)
        if reach <= 0:
            return self.cinema_reach(jukebox_id)
        self._cinema_reaches[jukebox_id] = reach
        return reach

    def _cinema_mode(self, jukebox_id, kwargs=None):
        # What the map says this cabinet plays through, right now. The mode arrives with every
        # play event (and with the cabinet state, so a menu can read it with nothing playing).
        # An unrecognised value reads as ``auto`` rather than as a profile: a cabinet whose
        # mode nobody understands must behave like every other cabinet, not go quiet.
        kwargs = kwargs or {}
        raw = kwargs.get("cinema_mode")
        if raw is None:
            raw = kwargs.get("cinema_profile")
        if raw is None:
            return self.cinema_mode(jukebox_id)
        mode = str(raw).strip().lower() or CINEMA_AUTO
        if mode not in CINEMA_PROFILES and mode not in (CINEMA_AUTO, CINEMA_OFF):
            mode = CINEMA_AUTO
        self._cinema_modes[jukebox_id] = mode
        return mode

    def refresh_cinema_rooms(self, now=None):
        # Let every playing room follow the map under it: re-resolved at most once a second and
        # re-shaped IN PLACE (CinemaSpeakerBank.reconfigure), so untouched speakers keep playing. A
        # map edit that removes the room never yanks the audio out from under a song.
        now = time.monotonic() if now is None else now
        if now - self._cinema_refresh_at < self.CINEMA_REFRESH_INTERVAL:
            return 0
        self._cinema_refresh_at = now
        with self._lock:
            rooms = [
                (jukebox_id, entry.get("cinema"), entry.get("cinema_mode"))
                for jukebox_id, entry in self.players.items()
                if entry.get("cinema") is not None
            ]
        refreshed = 0
        for jukebox_id, expected, running_mode in rooms:
            anchor = cabinet_anchor(self.game, jukebox_id)
            if anchor is None:
                continue
            try:
                # Re-resolved exactly as the play did, minus the fallback to the profile the map resolved
                # last time: a cabinet that only ever had a map room must not be handed a synthetic ring
                # because its speakers were deleted mid-song. The mode is the one this output was BUILT with.
                options = {"cinema_mode": running_mode or self.cinema_mode(jukebox_id),
                           "cinema_reach": self.cinema_reach(jukebox_id)}
                bank = self._acquire_cinema(
                    jukebox_id, anchor[0], anchor[1], anchor[2],
                    self.volume, options, keep=expected,
                )
            except Exception as ex:
                from .. import logger
                logger.log_exception(ex, f"JukeboxPlayer cinema room refresh ({jukebox_id})")
                continue
            if bank is None:
                # Room no longer resolvable (or the feature was turned off):
                # keep the room that is already audible rather than cutting
                # the song off mid-verse.
                continue
            if bank is not expected:
                # The room that was playing no longer exists (its bank was stopped), so the stream
                # holding it is already silent: the stall watchdog owns that rebuild. Never swap a
                # bank out from under a live stream -- the bank is what the transport feeds.
                log_line(f"[Jukebox] cinema room {jukebox_id} was lost; awaiting recovery")
                continue
            refreshed += 1
        return refreshed

    # How long a room asked for while a song plays is given to take it over before it is
    # handed back: a stream that never feeds a frame (a paused song, a dead decode) must
    # not hold a room's speakers and effect slots forever (see _sweep_cinema_swaps).
    CINEMA_SWAP_TIMEOUT = 10.0

    def _retune_output(self, entry, jukebox_id, x, y, z, title, kwargs, volume):
        # Apply a cabinet's new cinema mode to a song that is already playing: a room is re-shaped in
        # place, a room hands its front pair over to become the plain pair, and a plain pair asks a
        # room to take over and waits for its pre-buffer - no new decode and no refill, which is the
        # pause a mode pick used to cost. False when it cannot be done in place.
        streamer = entry.get("streamer")
        if streamer is None:
            return False
        mode = self._cinema_mode(jukebox_id, kwargs)
        self._cinema_reach(jukebox_id, kwargs)
        try:
            pending = entry.get("cinema_pending")
            running = entry.get("cinema")
            if mode == CINEMA_OFF:
                if pending is not None:
                    self._cancel_pending_room(entry, jukebox_id, streamer, pending)
                if running is None:
                    with self._lock:
                        entry["cinema_mode"] = mode
                    return True
                return self._hand_song_to_pair(entry, streamer, jukebox_id,
                                                x, y, z, mode, volume)
            if running is None:
                if pending is None:
                    return self._hand_song_to_room(entry, streamer, jukebox_id,
                                                   x, y, z, mode, kwargs, volume)
                keep = pending
            else:
                keep = running
            reshaped = self._acquire_cinema(jukebox_id, x, y, z, volume, kwargs, keep=keep)
            if reshaped is _CINEMA_SILENT:
                # The mode became custom and no speaker is placed for it: this
                # output goes away rather than becoming the plain pair. The next
                # map write re-offers the song, and whatever room then resolves
                # is what plays it.
                self._retire_or_stop(jukebox_id)
                return True
            if reshaped is None:
                # The mode names a room and the map no longer resolves one. A room that is playing is
                # not yanked out from under the song for that: the cabinet keeps what it has (its mode
                # still applies from the next song).
                if running is None:
                    with self._lock:
                        entry["cinema_mode"] = mode
                    return True
                return self._hand_song_to_pair(entry, streamer, jukebox_id,
                                                x, y, z, mode, volume)
            if reshaped is not keep:
                return False
            if pending is not None:
                # A room that was still waiting to take the song over is
                # re-shaped instead of taken back and built again.
                streamer.request_room(reshaped)
                with self._lock:
                    entry["cinema_pending"] = reshaped
                    entry["cinema_pending_at"] = time.monotonic()
                return True
            with self._lock:
                entry["cinema_mode"] = mode
            log_line(f"[Jukebox] cinema room {jukebox_id}: mode {mode} "
                     f"applied to the playing song")
            return True
        except Exception as ex:
            from .. import logger
            logger.log_exception(ex, f"JukeboxPlayer cinema retune ({jukebox_id})")
            return False

    def _hand_song_to_pair(self, entry, streamer, jukebox_id, x, y, z, mode, volume):
        # Hand the playing song from its room to the cabinet's own pair: the room's front pair IS the
        # plain pair and comes out with the frames the room was about to play still queued, so the
        # song keeps its content instant. A crossed or trimmed front pair is refused -> False.
        bank = entry.get("cinema")
        if bank is None:
            return False
        pair = bank.detach_primary_pair()
        if pair is None:
            return False
        src_l, src_r = pair
        base_gain = max(0.0, min(1.0, volume / 100.0))
        offset, ref, maxd = 2.5, 8.0, 40.0
        for src, sx in ((src_l, float(x) - offset), (src_r, float(x) + offset)):
            with contextlib.suppress(Exception):
                src.position = (sx, float(y), float(z))
                src.rolloff_factor = 0.0
                src.reference_distance = maxd
                src.max_distance = maxd
                src.spatialize = True
                src.direct_channels = False
                src.gain = base_gain
        streamer.switch_to_pair((src_l, src_r, ref, maxd))
        with self._lock:
            entry["cinema"] = None
            entry["cinema_mode"] = mode
            entry["source"] = src_l
            entry["secondary_source"] = src_r
        self._retire_room(jukebox_id, bank)
        log_line(f"[Jukebox] cinema room {jukebox_id}: the playing song is the "
                 f"cabinet's own pair now (mode {mode})")
        return True

    def _retire_room(self, jukebox_id, bank):
        # Stop a room that is no longer this song's output and give it back. Its speakers stop
        # where they stand (the pair that took over already holds the frames the room was
        # about to play), and the room goes back to the host so its effect slots are free for
        # whatever comes next.
        with contextlib.suppress(Exception):
            bank.stop()
        self._release_sources(list(bank.sources))
        self._release_cinema(jukebox_id, {"cinema": bank})

    def _hand_song_to_room(self, entry, streamer, jukebox_id, x, y, z, mode, kwargs, volume):
        # Ask a room to take over a song that is playing plainly: it needs its pre-buffer and the
        # frames the pair is about to play, so it is *requested* and the streamer commits it on the
        # frame it can (AudioStreamer.request_room); _sweep_cinema_swaps finishes the bookkeeping.
        request = getattr(streamer, "request_room", None)
        if not callable(request):
            return False
        bank = self._acquire_cinema(jukebox_id, x, y, z, volume, kwargs)
        if bank is None:
            return False
        request(bank)
        with self._lock:
            entry["cinema_pending"] = bank
            entry["cinema_pending_at"] = time.monotonic()
        log_line(f"[Jukebox] cinema room {jukebox_id}: room {mode} taking the "
                 f"playing song over")
        return True

    def _cancel_pending_room(self, entry, jukebox_id, streamer, bank):
        """Give back a room that was waiting to take a playing song over."""
        cancel = getattr(streamer, "cancel_room", None)
        if callable(cancel):
            with contextlib.suppress(Exception):
                cancel(bank)
        with self._lock:
            if entry.get("cinema_pending") is bank:
                entry["cinema_pending"] = None
                entry["cinema_pending_at"] = None
        self._retire_room(jukebox_id, bank)

    def _sweep_cinema_swaps(self, now):
        # Finish or give back a room that was asked for while a song plays: the entry's room becomes
        # the one that is playing and the plain pair's sources are deleted - or a room that never got
        # its window is handed back and the song plays plainly (its mode still applies next song).
        with self._lock:
            pending = [(jukebox_id, entry) for jukebox_id, entry in self.players.items()
                       if entry.get("cinema_pending") is not None]
        for jukebox_id, entry in pending:
            bank = entry.get("cinema_pending")
            streamer = entry.get("streamer")
            if getattr(streamer, "cinema", None) is bank:
                pair = (entry.get("source"), entry.get("secondary_source"))
                mode = self.cinema_mode(jukebox_id)
                with self._lock:
                    entry["cinema"] = bank
                    entry["cinema_pending"] = None
                    entry["cinema_pending_at"] = None
                    entry["cinema_mode"] = mode
                    entry["source"] = getattr(bank, "primary_source", None)
                    entry["secondary_source"] = getattr(bank, "secondary_source", None)
                self._release_sources(pair)
                log_line(f"[Jukebox] cinema room {jukebox_id}: room took the "
                         f"playing song over (mode {mode})")
                continue
            if getattr(streamer, "swap_room_failed", False):
                with contextlib.suppress(Exception):
                    streamer.swap_room_failed = False
                with self._lock:
                    entry["cinema_pending"] = None
                    entry["cinema_pending_at"] = None
                self._retire_room(jukebox_id, bank)
                log_line(f"[Jukebox] cinema room {jukebox_id}: the playing stream "
                         f"could not take the room; it plays plainly until the next song")
                continue
            asked_at = float(entry.get("cinema_pending_at") or now)
            if now - asked_at >= self.CINEMA_SWAP_TIMEOUT:
                self._cancel_pending_room(entry, jukebox_id, streamer, bank)
                log_line(f"[Jukebox] cinema room {jukebox_id}: the playing stream "
                         f"never took the room; it plays plainly until the next song")


    @audio_probe.measured("jukebox.start", trigger=True)
    def play(self, jukebox_id, x, y, z, title, url, duration, volume=None, start_offset=0.0,
             playback_id=None, transport="direct", relay_id=None, stream_epoch=None,
             http_headers=None, room_lead_in_s=None, join_playing_room=False,
             received_at=None, canonical_url=None, **_kwargs):
        """Start (or seamlessly continue) the song for one cabinet.

        This method is the order the phases run in and nothing else. Each phase
        may finish the call on its own, which is why the ones that can say so
        return a value (`True`, `None`) and this method returns on it.
        """
        # Start (or seamlessly continue) the song for one jukebox at (x, y, z).
        # Stereo-spatial like piano/drums: the STEREO stream is split into two MONO sources
        # placed just left/right of the jukebox, so you hear a real stereo image up close that
        # collapses to mono and fades with distance.
        if not url and transport == "direct":
            log_line(f"[Jukebox] play({jukebox_id}) skipped: no url")
            return

        transport, room_lead_in_s, join_playing_room = self._accepted_transport(
            jukebox_id, transport, relay_id, stream_epoch,
            room_lead_in_s, join_playing_room)
        effective_volume, playback_key, anchor_at, play_params = self._play_identity(
            volume, url, received_at, playback_id, x, y, z, title, duration,
            start_offset, http_headers, canonical_url)
        if self._continue_or_retune(jukebox_id, playback_key, transport, relay_id,
                                    stream_epoch, _kwargs, x, y, z, title,
                                    effective_volume):
            return
        if self._relay_pending_keeps_a_live_stream(jukebox_id, playback_key, transport, title):
            return
        self._retire_or_stop(jukebox_id)
        if self._relay_pending_placeholder(jukebox_id, transport, title, url, playback_key,
                                           play_params, start_offset, anchor_at, playback_id):
            return
        output = self._open_output(jukebox_id, x, y, z, effective_volume, _kwargs)
        if output is None:
            return
        src_l, src_r, bank, audio = output
        filt, reverb = self._initial_filters(x, y, z, src_l, src_r)
        slot, cab_vol = self._cabinet_output(jukebox_id, _kwargs, audio, src_l, src_r,
                                             bank, filt, reverb)
        streamer = self._start_streamer(
            jukebox_id, transport, relay_id, stream_epoch, x, y, z, url,
            src_l, src_r, effective_volume, start_offset, anchor_at, http_headers,
            canonical_url, play_params, room_lead_in_s, join_playing_room,
            bank, reverb, slot, cab_vol)
        if streamer is None:
            return
        self._record_playback(jukebox_id, transport, relay_id, stream_epoch, title, url,
                              playback_key, play_params, start_offset, anchor_at,
                              src_l, src_r, bank, streamer, x, y, z, playback_id)

    def _accepted_transport(self, jukebox_id, transport, relay_id, stream_epoch,
                            room_lead_in_s, join_playing_room):
        """The transport this offer is played on, after the emergency fallback.

        A connection that chronically loses the relay channel plays locally, and
        an offer that arrives as relay for such a cabinet is answered as direct.
        Such a machine plays direct only because its own channel is lost while
        the room still hears the server relay, so it anchors with zero lead-in
        and joins the playing room at its position -- otherwise its jam notes
        land ~3.5 s off the beat with no lag report to fix it.
        """
        # Emergency direct fallback: when this connection chronically loses the unreliable
        # relay channel, play locally over HTTP instead of waiting for relay frames that never
        # arrive. Sticky until the deadline (or a map change) so later songs stay audible too.
        if transport in ("relay", "relay_pending"):
            deadline = self._direct_fallback_until.get(jukebox_id)
            if deadline is not None and time.monotonic() < deadline:
                if relay_id is not None and stream_epoch is not None:
                    try:
                        with self._lock:
                            self._relay_pending.pop((int(relay_id), int(stream_epoch)), None)
                    except (TypeError, ValueError):
                        pass
                transport = "direct"
                # This machine plays direct ONLY because its own relay channel is chronically lost, while the
                # room still hears the server relay: anchor with zero lead-in and JOIN the playing room at its
                # position, or its jam notes land ~3.5 s off the beat with no lag report to fix it.
                room_lead_in_s = 0.0
                join_playing_room = True
        return transport, room_lead_in_s, join_playing_room

    def _play_identity(self, volume, url, received_at, playback_id, x, y, z, title,
                       duration, start_offset, http_headers, canonical_url):
        """What this offer is, and the one instant it is anchored on.

        The anchor is the play event's ARRIVAL on the network thread
        (jukebox_play stamps it there because play() itself is deferred to the
        main thread), else now: the streamer's, entry's and replay path's anchors
        must all be that same number. play_params is kept so the emergency direct
        fallback can re-start this exact song from its current wall-clock
        position without another server event.
        """
        effective_volume = self.volume if volume is None else volume
        playback_key = ("id", int(playback_id)) if playback_id is not None else ("url", url)
        # The one instant this playback is anchored on: the play event's ARRIVAL on the network thread
        # (jukebox_play stamps it there because play() itself is deferred to the main thread), else
        # now. The streamer's, entry's and replay path's anchors must all be that same number.
        anchor_at = float(received_at) if received_at is not None else time.monotonic()
        # Kept so the emergency direct fallback can re-start this exact song
        # from its current wall-clock position without another server event.
        play_params = {
            "x": float(x), "y": float(y), "z": float(z),
            "title": title, "url": url, "duration": int(duration or 0),
            "start_offset": float(start_offset or 0.0),
            "http_headers": http_headers,
            # The page this song IS, sent beside the signed URL: direct playback rebuilds its own stream
            # on every resume and seek, and the signed URL it was handed can have expired by then.
            # Retrying a stale URL never helps; re-resolving the page does (AudioStreamer.canonical_url).
            "canonical_url": canonical_url,
            "received_at": anchor_at,
        }
        return effective_volume, playback_key, anchor_at, play_params

    def _continue_or_retune(self, jukebox_id, playback_key, transport, relay_id,
                            stream_epoch, _kwargs, x, y, z, title, effective_volume):
        """True when what is already playing answers this offer.

        A play event for this cabinet also confirms it is alive after a map
        reload, so the pending-stop mark goes. Two sync routes must never cancel
        the same in-flight playback, so identical offers are idempotent... unless
        the cinema mode changed: a change of mode is a change of *output*, and the
        server re-offers the same song when the map it wrote is reloaded. Treating
        that as continuity was why picking a mode from the cabinet menu appeared
        to do nothing -- the song kept playing through the old output until the
        next track.
        """
        # A re-offer that carries a different cinema mode is not a new playback: the song, its
        # transport and its position are all the same and only the *output* changed. It is
        # applied to the playing output below (outside this lock: resolving a room reads the
        # map) instead of tearing a decode down.
        retune = None
        with self._lock:
            self._control_serial += 1
            # A play event for this jukebox confirms it is still alive after a
            # map reload — drop the pending-stop mark so playback continues
            # seamlessly instead of being torn down and rebuilt every reload.
            self._pending_map_change.discard(jukebox_id)
            existing = self.players.get(jukebox_id)
            same_relay_identity = True
            if existing is not None and transport == "relay":
                incoming_key = (
                    (int(relay_id), int(stream_epoch))
                    if relay_id is not None and stream_epoch is not None
                    else None
                )
                # A server restart of the relay worker (new identity, same song/playback) needs the old
                # receiver torn down and the new route registered, or every frame from the new worker is
                # dropped and only clients that were already connected go silent.
                same_relay_identity = existing.get("relay_key") == incoming_key
            # A change of cinema mode is a change of *output*, and the server re-offers this same
            # song when the map it wrote is reloaded. That re-offer is not continuity -- treating it
            # as continuity was why picking a mode from the cabinet menu appeared to do nothing:
            # the song kept playing through the old output until the next track.
            mode_changed = (
                existing is not None
                and existing.get("cinema_mode")
                != self._cinema_mode(jukebox_id, _kwargs)
            )
            if (mode_changed and existing is not None
                    and existing.get("playback_key") == playback_key
                    and existing.get("transport") == transport
                    and same_relay_identity):
                retune = existing
            if (
                existing is not None
                and existing.get("playback_key") == playback_key
                and existing.get("transport") == transport
                and same_relay_identity
                and not mode_changed
            ):
                # Idempotent even while yt-dlp/ffmpeg is still resolving. Two map
                # sync routes must never cancel the same in-flight playback.
                src_l = existing.get("source")
                src_r = existing.get("secondary_source")
                current_streamer = existing.get("streamer")
                if getattr(current_streamer, "main_thread_audio", False) is True:
                    current_streamer.box_pos = (float(x), float(y), float(z))
                offset = 2.5
                if (existing.get("cinema") is None
                        and src_l is not None and src_r is not None):
                    # Never re-aim a cinema room's speakers from the plain
                    # pair offset: the bank owns their positions.
                    try:
                        for src, sx in ((src_l, float(x) - offset), (src_r, float(x) + offset)):
                            src.position = (sx, float(y), float(z))
                    except Exception:
                        pass
                log_line(f"[Jukebox] play({jukebox_id}) seamless continuity for {title!r}")
                return True

        if retune is not None and self._retune_output(
                retune, jukebox_id, x, y, z, title, _kwargs, effective_volume):
            return True
        return False

    def _open_output(self, jukebox_id, x, y, z, effective_volume, _kwargs):
        """The sources this cabinet's song comes out of, or None when it is silent.

        A cabinet in a cinema room hands its song to the room's own speakers (the
        bank created, positioned and spatialised one source per speaker);
        otherwise this is the two-source stereo pair, split just left and right
        of the cabinet so the image collapses to mono and fades with distance. A
        cabinet in custom mode with no speaker placed gets no source at all: the
        song is silent here until the next map write re-offers it.
        """
        audio = getattr(self.game, "audio_mngr", None)
        if audio is None:
            log_line(f"[Jukebox] play({jukebox_id}) skipped: no audio_mngr")
            return None
        src_l = src_r = None
        base_gain = max(0.0, min(1.0, effective_volume / 100.0))
        # Cinema room (opt-in, per cabinet). While it is off -- the shipped
        # default -- this is None and the block below is the same two-source
        # playback it has always been.
        bank = self._acquire_cinema(jukebox_id, x, y, z, effective_volume, _kwargs)
        if bank is _CINEMA_SILENT:
            # Nothing placed for a custom cabinet: no sources at all, so the
            # song is silent here. The next map write re-offers it, and the
            # room it then resolves is what plays.
            log_line(f"[Jukebox] play({jukebox_id}) silent: custom mode, "
                     f"no speakers placed")
            return None
        if bank is not None:
            # The bank already created, positioned and spatialised one source
            # per speaker around the cabinet.
            src_l, src_r = bank.primary_source, bank.secondary_source
        else:
            try:
                src_l = audio_probe.call("jukebox.gen_source", audio.context.gen_source)
                src_r = audio_probe.call("jukebox.gen_source", audio.context.gen_source)
            except Exception:
                self._release_sources((src_l, src_r))
                log_line(f"[Jukebox] play({jukebox_id}) failed: gen_source error")
                return None
            try:
                with audio_probe.span("jukebox.source_setup"):
                    for src, sx in ((src_l, float(x) - self.PAIR_OFFSET),
                                    (src_r, float(x) + self.PAIR_OFFSET)):
                        src.position = (sx, float(y), float(z))
                        src.rolloff_factor = 0.0   # linear fade handled per-frame
                        src.reference_distance = self.PAIR_MAX_DISTANCE
                        src.max_distance = self.PAIR_MAX_DISTANCE
                        src.spatialize = True
                        src.direct_channels = False
                        src.gain = base_gain
            except Exception:
                pass
        # A song that starts while the listener is underwater inherits the
        # active global water filter (camera.py pushes it onto
        # audio_mngr.filter) so it is muffled from its first frame.
        try:
            active = getattr(audio, "filter", None)
            if active and active[-1] is not None:
                src_l.direct_filter = active[-1]
                src_r.direct_filter = active[-1]
        except Exception:
            pass
        return src_l, src_r, bank, audio

    def _initial_filters(self, x, y, z, src_l, src_r):
        """The wall and the water a song starts inside: its first filter and reverb.

        A song that starts while the listener is underwater inherits the active
        global water filter (camera.py pushes it onto audio_mngr.filter) so it is
        muffled from its first frame, and the cabinet's wall occlusion is resolved
        once here rather than on the first frame, which would be heard unfiltered.
        """
        # Initial wall occlusion check:
        filt = None
        try:
            gameplay = getattr(self.game, "gameplay", None)
            cur_map = getattr(gameplay, "map", None) if gameplay is not None else None
            audio = getattr(self.game, "audio_mngr", None)
            listener = getattr(audio, "position", None) if audio is not None else None
            if cur_map is not None and listener is not None and hasattr(cur_map, "valid_straight_path"):
                tier = audio_probe.call("jukebox.initial_wall", self.occlusion_tier,
                                        (float(x), float(y), float(z)), listener,
                                        self.PAIR_MAX_DISTANCE)
                if tier != OCCLUSION_CLEAR:
                    filt = (
                        self.get_light_occlusion_filter() if tier == OCCLUSION_LIGHT
                        else self.get_occlusion_filter()
                    )
                    if filt is not None:
                        src_l.direct_filter = filt
                        src_r.direct_filter = filt
        except Exception:
            pass
        # Room reverb: like piano/drums, the song picks up the reverb zone of
        # the place the jukebox stands in (none if the spot has no reverb).
        reverb = None
        try:
            if gameplay is not None and getattr(gameplay, "map", None) is not None:
                reverb_zone = audio_probe.call("jukebox.reverb_lookup", gameplay.map.get_reverb_at,
                                               float(x), float(y), float(z))
                reverb = reverb_zone.reverb if reverb_zone and hasattr(reverb_zone, "reverb") else None
                if reverb is not None and getattr(audio, "efx", None) is not None:
                    audio_probe.call("jukebox.efx", audio.efx.send, src_l, 0, reverb, filter=filt)
                    audio_probe.call("jukebox.efx", audio.efx.send, src_r, 0, reverb, filter=filt)
        except Exception:
            pass
        return filt, reverb

    def _cabinet_output(self, jukebox_id, _kwargs, audio, src_l, src_r, bank, filt, reverb):
        """The EQ slot and cabinet volume this playback runs with.

        The profile is the one the offer named, else the cabinet's cached one
        (Bass Boost, Horn Speaker, a custom curve from the sliders). A room
        carries the cabinet's reverb and EQ on every one of its speakers, not just
        the pair of them.
        """
        # Jukebox Equalizer profile (e.g. Bass Boost / Horn Speaker per-jukebox):
        slot = None
        try:
            eq_profile = str(_kwargs.get("eq_profile") or self.eq_profiles.get(jukebox_id, "normal")).lower()
            eq_values = self._normalize_eq_values(
                _kwargs.get("eq_values") or self.eq_values.get(jukebox_id)
            )
            self.eq_profiles[jukebox_id] = eq_profile
            if eq_profile == "custom":
                self.eq_values[jukebox_id] = eq_values
            else:
                self.eq_values.pop(jukebox_id, None)
            slot = self._get_eq_slot(eq_profile, jukebox_id, eq_values)
            if slot is not None and getattr(audio, "efx", None) is not None:
                audio_probe.call("jukebox.efx", audio.efx.send, src_l, 1, slot, filter=filt)
                audio_probe.call("jukebox.efx", audio.efx.send, src_r, 1, slot, filter=filt)
            if bank is not None:
                # The room carries the cabinet's reverb and EQ on every one of
                # its speakers, not just the pair of them.
                bank.set_reverb(reverb)
                bank.set_eq_slot(slot)
        except Exception:
            pass
        cab_vol = _kwargs.get("cabinet_volume")
        if cab_vol is None:
            cab_vol = self.cabinet_volumes.get(jukebox_id, 100)
        try:
            cab_vol = int(cab_vol)
        except Exception:
            cab_vol = 100
        self.cabinet_volumes[jukebox_id] = cab_vol
        return slot, cab_vol

    def _start_streamer(self, jukebox_id, transport, relay_id, stream_epoch, x, y, z, url,
                        src_l, src_r, effective_volume, start_offset, anchor_at,
                        http_headers, canonical_url, play_params, room_lead_in_s,
                        join_playing_room, bank, reverb, slot, cab_vol):
        """Launch the relay receiver or the direct streamer, or None if it failed.

        A relay offer must carry its identity. A direct one rebuilds its own stream
        on every resume and seek, so it is handed the page the song IS beside the
        signed URL -- the URL it was given can have expired by then, and retrying
        a stale URL never helps. On failure the sources are deleted here rather
        than left to the watchdog, and the exception is logged as an error, not as
        a failure to load the song.
        """
        streamer = None
        try:
            if transport == "relay":
                if relay_id is None or stream_epoch is None:
                    raise ValueError("missing relay identity")
                audio_probe.event("jukebox.relay")
                streamer = audio_probe.call("jukebox.receiver_create", JukeboxRelayReceiver,
                    self.game, src_l, src_r, effective_volume,
                    relay_id, stream_epoch, self.PAIR_REFERENCE_DISTANCE,
                    self.PAIR_MAX_DISTANCE,
                    box_pos=(float(x), float(y), float(z)), player=self,
                    reverb_slot=reverb, eq_slot=slot,
                    cabinet_volume=cab_vol, cinema=bank,
                )
            else:
                from .. import music_bot as mb
                audio_probe.event("jukebox.direct")
                streamer = audio_probe.call("jukebox.direct_create", mb.AudioStreamer,
                    self.game, url, src_l, volume=effective_volume, bot=None,
                    channels=2,
                    spatial_pair=None if bank is not None else (
                        src_l, src_r, self.PAIR_REFERENCE_DISTANCE,
                        self.PAIR_MAX_DISTANCE),
                    cinema=bank,
                    start_offset=start_offset,
                    start_offset_received_at=anchor_at,
                    http_headers=http_headers,
                    canonical_url=canonical_url,
                    timeline_anchor=play_params["duration"] > 0,
                    media_cache=self._media_cache if play_params["duration"] > 0 else None,
                    room_lead_in_s=room_lead_in_s,
                    join_playing_room=join_playing_room,
                )
                streamer.reverb_slot = reverb
                streamer.eq_slot = slot
                streamer.jukebox_player = self
                if hasattr(streamer, "set_cabinet_volume"):
                    streamer.set_cabinet_volume(cab_vol)
            audio_probe.call("jukebox.thread_start", streamer.start)
        except Exception as ex:
            if getattr(streamer, "main_thread_audio", False) is True:
                streamer.stop()
            for src in (src_l, src_r):
                try:
                    src.delete()
                except Exception:
                    pass
            from .. import logger
            logger.log_exception(ex, f"JukeboxPlayer.play({jukebox_id}) start streamer")
            log_line(f"[Jukebox] play({jukebox_id}) failed to start streamer: {ex}")
            return None
        return streamer

    def _record_playback(self, jukebox_id, transport, relay_id, stream_epoch, title, url,
                         playback_key, play_params, start_offset, anchor_at,
                         src_l, src_r, bank, streamer, x, y, z, playback_id):
        """Write the entry every reader works from, then say what is playing.

        The entry carries the song's own position (not the streamer's, which is
        replaced mid-song) and the mode this output was built for, so a re-offer
        that carries a different mode is a request to re-tune. A relay route is
        registered under the same lock as the frames it takes over: a live frame
        on the network thread must not slip in first and make the sequence gate
        reject the buffered intro frames.
        """
        with self._lock:
            self.players[jukebox_id] = {
                "source": src_l,
                "secondary_source": src_r,
                "cinema": bank,
                # The mode this output was built for. A re-offer of the same
                # song that carries a different mode is a request to re-tune,
                # not seamless continuity.
                "cinema_mode": self.cinema_mode(jukebox_id),
                "streamer": streamer,
                "title": title,
                "url": url,
                "transport": transport,
                "playback_key": playback_key,
                "relay_key": (int(relay_id), int(stream_epoch)) if transport == "relay" else None,
                "created_at": time.monotonic(),
                "play_params": play_params,
                # The song's own position, on the entry rather than on the
                # streamer: a streamer is replaced mid-song and the song's
                # position does not move with it (see ``_song_position_keys``).
                **self._song_position_keys(start_offset, anchor_at),
            }
            if transport == "relay":
                self.relay_routes[(int(relay_id), int(stream_epoch))] = streamer
                # Take over any frames buffered while this receiver was being created and flush them
                # UNDER THE SAME LOCK: a live frame on the network thread must not slip in first and
                # make the sequence gate reject the buffered intro frames.
                pending = self._relay_pending.pop((int(relay_id), int(stream_epoch)), None)
                if pending is not None:
                    for seq, payload, flags in pending["frames"]:
                        streamer.receive(seq, payload, flags)

        log_line(
            f"[Jukebox] playing {title!r} transport={transport} "
            f"playback={playback_id!r} relay={relay_id!r}/{stream_epoch!r} "
            f"at ({x}, {y}, {z}) offset={start_offset:.1f}s url={url[:60]!r}"
        )
    @staticmethod
    def _song_position_keys(start_offset, anchor_at):
        # The song's own position, as the two entry keys that carry it: start_offset (the position the
        # Server named) and anchor_at (the monotonic instant that event reached this machine), so the
        # position at any later moment needs no wall clock of either machine to be true. Both ends of
        # a live note are aimed with it (EventHandeler._audible_song_position_ms reads exactly these;
        # Gameplay._attach_jukebox_sender_lag stamps the answer on every note). They belong on the
        # playback ENTRY, not the streamer: a streamer is replaced mid-song while the position does
        # not move with it, and a relay_pending entry has no streamer yet while notes can already be
        # played along. anchor_at MUST be the arrival (taken on the network thread), never play().
        return {"start_offset": float(start_offset or 0.0),
                "start_offset_received_at": anchor_at}

    def _fade_out_sources(self, sources, streamer=None, duration=0.5, cleanup=None):
        # Fade active sources to 0 gain, then clean them up; ``cleanup`` runs exactly once, after the
        # sources are gone. A cinema room's sources are deleted HERE rather than by stop(), so this is
        # the only place that can hand the room back - a skipped hand-back left the host holding a
        # bank with every source deleted, and the NEXT song was handed those dead names.
        def finished():
            if callable(cleanup):
                try:
                    cleanup()
                except Exception:
                    pass

        valid_sources = [s for s in sources if s is not None]
        if getattr(streamer, "main_thread_audio", False) is True:
            # Relay fades share the main audio pump: no timer thread touches
            # OpenAL, no join, and the old receiver cannot revive a new song.
            self._retired_relays.append(streamer)
            def finish():
                if streamer in self._retired_relays:
                    self._retired_relays.remove(streamer)
                self._release_sources(valid_sources)
                finished()
            streamer.retire(duration=duration, cleanup_callback=finish)
            return
        if not valid_sources:
            if streamer is not None:
                try:
                    if hasattr(streamer, "stop"):
                        streamer.stop()
                    else:
                        streamer.running = False
                except Exception:
                    pass
            finished()
            return

        def _fade_worker():
            try:
                start_gains = [float(getattr(s, 'gain', 1.0) or 0.0) for s in valid_sources]
                steps = 10
                step_sleep = duration / steps
                for i in range(steps):
                    fraction = (steps - 1 - i) / steps
                    for idx, s in enumerate(valid_sources):
                        try:
                            s.gain = max(0.0, start_gains[idx] * fraction)
                        except Exception:
                            pass
                    time.sleep(step_sleep)
            except Exception:
                pass
            finally:
                if streamer is not None:
                    try:
                        if hasattr(streamer, "stop"):
                            streamer.stop()
                        else:
                            streamer.running = False
                    except Exception:
                        pass
                audio = getattr(self.game, "audio_mngr", None)
                for s in valid_sources:
                    try:
                        with contextlib.suppress(Exception):
                            del s.direct_filter
                        if audio is not None and getattr(audio, "efx", None) is not None:
                            try:
                                audio.efx.send(s, 0, None)
                                audio.efx.send(s, 1, None)
                            except Exception:
                                pass
                        s.stop()
                        drain_limit = 64
                        while s.buffers_processed > 0 and drain_limit > 0:
                            s.unqueue_buffers()
                            drain_limit -= 1
                        s.delete()
                    except Exception:
                        pass
                finished()

        import threading
        threading.Thread(target=_fade_worker, daemon=True).start()

    def _release_sources(self, sources):
        """Detach effects, then delete independently of drain failures (owner)."""
        audio = getattr(self.game, "audio_mngr", None)
        for source in sources:
            if source is None:
                continue
            with contextlib.suppress(Exception):
                del source.direct_filter
            if getattr(audio, "efx", None) is not None:
                for index in (0, 1):
                    with contextlib.suppress(Exception):
                        audio.efx.send(source, index, None)
            with contextlib.suppress(Exception):
                source.stop()
                for _ in range(64):
                    if source.buffers_processed <= 0:
                        break
                    source.unqueue_buffers()
            with contextlib.suppress(Exception):
                source.delete()

    def _entry_sources(self, entry):
        """Every OpenAL source an entry owns (2 plain, N in a cinema room)."""
        if not entry:
            return []
        stored = entry.get("sources")
        if isinstance(stored, (list, tuple)):
            return [source for source in stored if source is not None]
        banks = [bank for bank in (entry.get("cinema"), entry.get("cinema_pending"))
                 if bank is not None]
        if banks:
            sources, seen = [], set()
            for bank in banks:
                for source in bank.sources:
                    if id(source) not in seen:
                        seen.add(id(source))
                        sources.append(source)
            if entry.get("cinema_pending") is not None:
                # A room waiting to take the song over does not own the pair
                # that is still playing it: both are this entry's to release.
                for source in (entry.get("source"), entry.get("secondary_source")):
                    if source is not None and id(source) not in seen:
                        seen.add(id(source))
                        sources.append(source)
            return sources
        return [source for source in (entry.get("source"), entry.get("secondary_source"))
                if source is not None]

    def _release_cinema(self, jukebox_id, entry):
        # Drop a cabinet's cinema room once its sources were deleted. Released after the
        # sources so the bank can never keep a deleted OpenAL name, and its per-speaker
        # buffers are returned with it.
        banks = [bank for bank in ((entry or {}).get("cinema"),
                                   (entry or {}).get("cinema_pending"))
                 if bank is not None]
        for bank in banks:
            # Releasing by name, not by key: a retired room lingers half a
            # second while the song that replaced it already owns the key.
            release_renderer(self.game, jukebox_id, bank)
            bank.forget_sources()

    @audio_probe.measured("jukebox.stop")
    def stop(self, jukebox_id, playback_id=None, fade=False):
        """Stop the song for one jukebox and free its audio source."""
        with self._lock:
            existing = self.players.get(jukebox_id)
            if existing is not None and playback_id is not None:
                try:
                    expected_key = ("id", int(playback_id))
                except (TypeError, ValueError):
                    return False
                if existing.get("playback_key") != expected_key:
                    return False
            player = self.players.pop(jukebox_id, None)
        if not player:
            return False
        log_line(f"[Jukebox] stop({jukebox_id}) title={player.get('title')!r}")
        streamer = player.get("streamer")
        # A jam note waiting on this output's clock must still sound: the
        # streamer goes, the clock it was holding the note on does not
        # (``_keep_waiting_notes``).
        self._keep_waiting_notes(streamer)
        relay_key = player.get("relay_key")
        if relay_key is not None:
            with self._lock:
                self.relay_routes.pop(relay_key, None)
                self._relay_pending.pop(relay_key, None)
        if fade:
            bank = player.get("cinema")
            if bank is not None:
                # The direct fade worker writes source gains the room's own frame-by-frame refresh would
                # overwrite, so the room fades itself over the same duration instead. Either way it is marked
                # as leaving, or the replacement song was handed the room being torn down ("cinema room lost").
                bank.retire(duration=0.5,
                            ramp=getattr(streamer, "main_thread_audio", False) is not True)
            # The fade worker deletes the room's sources, so it is also what has to give the room
            # back: releasing it here would strand the sources it has not deleted yet, and
            # releasing it never left the host handing the recycled bank's dead sources to the
            # next song.
            self._fade_out_sources(self._entry_sources(player), streamer=streamer,
                                   duration=0.5,
                                   cleanup=lambda: self._release_cinema(jukebox_id, player))
            return True
        if getattr(streamer, "main_thread_audio", False) is True:
            streamer.stop()
            self._release_sources(self._entry_sources(player))
            self._release_cinema(jukebox_id, player)
            return True
        try:
            if streamer is not None:
                if isinstance(streamer, JukeboxRelayReceiver):
                    streamer.stop()
                else:
                    if hasattr(streamer, "stop"):
                        streamer.stop()
                    else:
                        streamer.running = False
                    if hasattr(streamer, "join") and threading.current_thread() is not streamer:
                        streamer.join(timeout=1.0)
        except Exception:
            pass
        for source in self._entry_sources(player):
            if source is not None:
                try:
                    with contextlib.suppress(Exception):
                        del source.direct_filter
                    audio = getattr(self.game, "audio_mngr", None)
                    if audio is not None and getattr(audio, "efx", None) is not None:
                        try:
                            audio.efx.send(source, 0, None)
                            audio.efx.send(source, 1, None)
                        except Exception:
                            pass
                    source.stop()
                    try:
                        drain_limit = 64
                        while source.buffers_processed > 0 and drain_limit > 0:
                            source.unqueue_buffers()
                            drain_limit -= 1
                    except Exception:
                        pass
                    source.delete()
                except Exception:
                    pass
        self._release_cinema(jukebox_id, player)
        return True

    def stop_all(self):
        # Stop every jukebox (map change / disconnect). Clears the pending map-change marks
        # too, so any in-flight sweep from an earlier reload can never act on a jukebox
        # created after this teardown.
        with self._lock:
            self._pending_map_change = set()
            self._pending_map_change_serial = None
            # The mode and the reach are properties of a cabinet *on a map*. Element ids are
            # strings written into the map file, so a copy of a map carries the same ids: left
            # behind, a stale entry would answer for the wrong hall on the next map until a play
            # event corrected it.
            self._cinema_modes.clear()
            self._cinema_reaches.clear()
            ids = list(self.players.keys())
        for jukebox_id in ids:
            self.stop(jukebox_id)
        # A map change takes the songs with it: a note waiting on a replaced output belongs to
        # a beat that is not on this map any more, so it is dropped rather than fired into the
        # next one -- the same choice a cinema room makes for what it is holding when it goes away.
        self._drop_orphan_clocks()
        for receiver in list(self._retired_relays):
            self._drop_waiting_notes(receiver)
            receiver.stop()  # Finishes its owner-thread source cleanup too.
        self._stop_retiring_direct()
        with self._occlusion_lock:
            self._occlusion_cache.clear()
        audio = getattr(self.game, "audio_mngr", None)
        slots = list(self.custom_eq_slots.values())
        self.custom_eq_slots.clear()
        self.eq_values.clear()
        if audio is not None and hasattr(audio, "release_effect_slot"):
            for slot in slots:
                if slot is not None:
                    try:
                        audio.release_effect_slot(slot)
                    except Exception:
                        pass

    def _keep_waiting_notes(self, streamer):
        # Remember an output's clock if a jam note is still waiting on it: a note waits on the clock
        # of the output that measured it, and a streamer replaced under it left a note nothing ever
        # fired or dropped - not late, simply not heard. Kept here and pumped by update until empty;
        # stop_all drops them instead (a map change is not a hiccup), and a clock that was never asked
        # for a wait is not kept at all (pair_clock is made on first use).
        clock = getattr(streamer, "pair_clock", None)
        if clock is None:
            return False
        try:
            if not clock.pending_waits():
                return False
        except Exception:
            return False
        with self._lock:
            if any(existing is clock for existing in self._orphan_clocks):
                return False
            self._orphan_clocks.append(clock)
        return True

    @staticmethod
    def _drop_waiting_notes(streamer):
        """Throw away what an output that is going was still holding."""
        clock = getattr(streamer, "pair_clock", None)
        if clock is None:
            return False
        try:
            clock.drop_waits()
        except Exception:
            return False
        return True

    def _drop_orphan_clocks(self):
        """Drop the notes kept from replaced outputs (a map change)."""
        with self._lock:
            clocks = list(self._orphan_clocks)
            self._orphan_clocks[:] = []
        for clock in clocks:
            try:
                clock.drop_waits()
            except Exception:
                pass

    def _pump_orphan_clocks(self):
        # Fire the notes a replaced output was still holding (one frame). Pumped next to the
        # entries' own clocks, on the game thread, because firing a note can spawn sources. A
        # clock with nothing left to fire is forgotten.
        with self._lock:
            clocks = list(self._orphan_clocks)
        keep = []
        for clock in clocks:
            try:
                clock.pump_waits()
                if clock.pending_waits():
                    keep.append(clock)
            except Exception:
                keep.append(clock)      # an unreadable clock is not a verdict
        with self._lock:
            self._orphan_clocks[:] = keep

    def _retire_or_stop(self, jukebox_id):
        # Song advance: let a nearly-finished direct song play out its tail. Anchored playback runs one
        # lead-in behind the server's audioStartedAt, so the next jukebox_play lands while the old song
        # still has its final seconds queued; stopping on the packet cut every song's ending short.
        from .. import music_bot as mb
        budget = mb.AudioStreamer.DIRECT_LEAD_IN_S + self.DIRECT_RETIRE_TAIL_MARGIN_S
        with self._lock:
            waiting_on = (self.players.get(jukebox_id) or {}).get("streamer")
        # Outside the lock: ``_keep_waiting_notes`` takes it as well.
        self._keep_waiting_notes(waiting_on)
        with self._lock:
            existing = self.players.get(jukebox_id)
            if existing is not None:
                streamer = existing.get("streamer")
                duration = float((existing.get("play_params") or {}).get("duration") or 0)
                position = None
                content_position = getattr(streamer, "content_position", None)
                if duration > 0 and callable(content_position):
                    try:
                        position = float(content_position())
                    except Exception:
                        position = None
                remaining = duration - position if position is not None else None
                if (
                    existing.get("transport") == "direct"
                    and streamer is not None
                    and hasattr(streamer, "is_alive") and streamer.is_alive()
                    and not getattr(streamer, "failure_reason", None)
                    and remaining is not None and 0.0 < remaining <= budget
                ):
                    self.players.pop(jukebox_id, None)
                    streamer.reverb_slot = None
                    bank = existing.get("cinema")
                    if bank is not None:
                        bank.set_reverb(None)
                    self._retiring_direct.append({
                        "id": jukebox_id,
                        "streamer": streamer,
                        "source": existing.get("source"),
                        "secondary_source": existing.get("secondary_source"),
                        "cinema": bank,
                        "sources": self._entry_sources(existing),
                        "deadline": time.monotonic() + remaining + 2.0,
                    })
                    log_line(
                        f"[Jukebox] retire({jukebox_id}): letting "
                        f"{existing.get('title')!r} finish its last {remaining:.1f}s"
                    )
                    return
        self.stop(jukebox_id, fade=True)

    def _sweep_retiring_direct(self):
        """Release retired direct tails once they drain (or time out)."""
        now = time.monotonic()
        finished = []
        with self._lock:
            still = []
            for entry in self._retiring_direct:
                streamer = entry.get("streamer")
                done = (
                    not hasattr(streamer, "is_alive")
                    or not streamer.is_alive()
                    or now >= float(entry.get("deadline") or now)
                )
                if done:
                    finished.append(entry)
                else:
                    still.append(entry)
            self._retiring_direct[:] = still
        for entry in finished:
            streamer = entry.get("streamer")
            try:
                if streamer is not None and hasattr(streamer, "stop"):
                    streamer.stop()
            except Exception:
                pass
            self._release_sources(self._entry_sources(entry))
            self._release_cinema(entry.get("id"), entry)
            log_line(f"[Jukebox] retire({entry.get('id')}): tail finished")

    def _stop_retiring_direct(self):
        """Hard-stop every retired tail (disconnect / full teardown)."""
        with self._lock:
            entries = list(self._retiring_direct)
            self._retiring_direct.clear()
        for entry in entries:
            streamer = entry.get("streamer")
            try:
                if streamer is not None and hasattr(streamer, "stop"):
                    streamer.stop()
            except Exception:
                pass
            self._release_sources(self._entry_sources(entry))
            self._release_cinema(entry.get("id"), entry)

    def _refresh_cinema(self, now):
        """Keep every playing room the shape the map has now.

        A speaker placed or deleted while a song plays joins (or leaves) the room
        the listener is hearing, without the song being restarted; a room asked
        for while a song plays is committed by the stream itself, and this
        finishes (or gives back) what it asked for. Both read the map, so both are
        allowed to fail this frame -- the next one tries again.
        """
        # A speaker placed or deleted while a song plays joins (or leaves) the
        # room the listener is hearing, without the song being restarted.
        try:
            self.refresh_cinema_rooms(now)
        except Exception:
            pass
        # A room asked for while a song plays is committed by the stream
        # itself; this finishes (or gives back) what it asked for.
        try:
            self._sweep_cinema_swaps(now)
        except Exception:
            pass

    def _pump_note_clocks(self):
        """Fire the notes waiting on a clock, on this thread.

        A live note waits on the clock of what the listener is actually hearing
        (jukebox_clock.py) and this is the thread it has to fire on -- a room's
        queue or a plain pair, both pumped here once per gameplay frame, because
        firing a note can spawn sources. Clocks whose output was replaced mid-wait
        are pumped too (_pump_orphan_clocks), or a replaced streamer would strand
        the note that was aimed at it.
        """
        # A live note waits on the clock of what the listener is actually hearing (jukebox_clock.py)
        # and this is the thread it has to fire on - a room's queue or a plain pair, both pumped here
        # once per gameplay frame, because firing a note can spawn sources.
        try:
            for entry in list(self.players.values()):
                if not isinstance(entry, dict):
                    continue
                room = entry.get("cinema")
                if room is not None:
                    room.pump_waits()
                clock = getattr(entry.get("streamer"), "pair_clock", None)
                if clock is not None:
                    clock.pump_waits()
            self._pump_orphan_clocks()
        except Exception:
            pass

    def stop_all_if_serial(self, serial):
        """Ignore a delayed map-cleanup after a newer play control has arrived."""
        with self._lock:
            unchanged = self._control_serial == serial
        if unchanged:
            self.stop_all()
        return unchanged

    def mark_pending_map_change(self, serial):
        # Mark every active player as awaiting a post-reload play event: the server re-broadcasts
        # jukebox_play for songs still playing and those clear the mark via play(). Any player still
        # unconfirmed after a short grace period is a song that truly ended or a jukebox gone from the
        # map - stop it so no ghost audio lingers.
        import threading as _threading
        with self._lock:
            # This mark is queued from CHANNEL_MAP. A newer jukebox_play may
            # already have reached the game queue through another ENet channel;
            # never let the stale reload mark claim that newly confirmed stream.
            if self._control_serial != serial:
                return False
            pending = set(self.players.keys())
            self._pending_map_change = pending
            self._pending_map_change_serial = serial
            # A fresh map is a fresh chance for the relay channel: give the
            # server relay another try after the emergency direct fallback.
            self._direct_fallback_until.clear()
            self._relay_fail_counts.clear()
            self._stall_unsticks.clear()
            if pending:
                log_line(
                    f"[Jukebox] map reload: {len(pending)} jukebox(es) awaiting "
                    f"play confirmation"
                )
        if not pending:
            return True

        def sweep():
            time.sleep(self.MAP_RELOAD_CONFIRM_TIMEOUT)
            with self._lock:
                if self._pending_map_change_serial != serial:
                    return  # a newer reload's mark superseded this one
                doomed = list(self._pending_map_change)
            if not doomed:
                return

            def do_stop():
                for jid in doomed:
                    with self._lock:
                        # A late play() re-confirmed it — keep playing.
                        if jid not in self._pending_map_change:
                            continue
                        self._pending_map_change.discard(jid)
                    self.stop(jid)

            if self.game is not None:
                self.game.put(do_stop)

        _threading.Thread(target=sweep, daemon=True).start()
        return True

    def sync_reverb(self):
        """Re-syncs environment Reverb EFX for all active jukebox streams after map reloads."""
        with self._occlusion_lock:
            self._occlusion_cache.clear()
        gameplay = getattr(self.game, "gameplay", None)
        if gameplay is None or getattr(gameplay, "map", None) is None:
            return not bool(self.players)
        audio = getattr(self.game, "audio_mngr", None)
        if audio is None:
            return not bool(self.players)
        healthy = True
        with self._lock:
            for jid, p in self.players.items():
                bank = p.get("cinema")
                if bank is not None:
                    # A room takes its reverb from the cabinet but keeps its
                    # occlusion per speaker, so one shared ray from here
                    # would be the wrong answer; the room refreshes itself.
                    try:
                        anchor = bank.anchor
                        reverb_zone = gameplay.map.get_reverb_at(anchor[0], anchor[1], anchor[2])
                        reverb = reverb_zone.reverb if reverb_zone and hasattr(reverb_zone, "reverb") else None
                        streamer = p.get("streamer")
                        if streamer is not None:
                            streamer.reverb_slot = reverb
                        bank.set_reverb(reverb)
                        bank.touch_environment()
                        bank.update_output()
                    except Exception as error:
                        audio_health.note_swallowed("jukebox.sync_reverb.cinema",
                                                   type(error).__name__)
                        healthy = False
                    continue
                src_l = p.get("source")
                src_r = p.get("secondary_source")
                if src_l is None or src_r is None:
                    healthy = False
                    continue
                try:
                    pos = src_l.position
                    reverb_zone = gameplay.map.get_reverb_at(pos[0] + 2.5, pos[1], pos[2])
                    reverb = reverb_zone.reverb if reverb_zone and hasattr(reverb_zone, "reverb") else None
                    streamer = p.get("streamer")
                    if streamer is not None:
                        streamer.reverb_slot = reverb
                    filt = None
                    listener = getattr(audio, "position", None)
                    if listener is not None and hasattr(gameplay.map, "valid_straight_path"):
                        tier = self.occlusion_tier((pos[0] + 2.5, pos[1], pos[2]), listener)
                        if tier != OCCLUSION_CLEAR:
                            filt = (
                                self.get_light_occlusion_filter() if tier == OCCLUSION_LIGHT
                                else self.get_occlusion_filter()
                            )
                    audio.efx.send(src_l, 0, reverb, filter=filt)
                    audio.efx.send(src_r, 0, reverb, filter=filt)
                except Exception as error:
                    audio_health.note_swallowed("jukebox.sync_reverb.stereo",
                                               type(error).__name__)
                    healthy = False
        return healthy

    def detach_reverb(self):
        """Detach active and fading streams before map slots return to the pool."""
        audio = getattr(self.game, "audio_mngr", None)
        with self._lock:
            sources = [
                source
                for player in self.players.values()
                for source in self._entry_sources(player)
                if source is not None
            ]
            for player in self.players.values():
                streamer = player.get("streamer")
                if streamer is not None:
                    streamer.reverb_slot = None
                bank = player.get("cinema")
                if bank is not None:
                    bank.set_reverb(None)
        # Retired relay streams remain audible for the crossfade, but must not
        # retain or reattach a slot after the old map returns it to the pool.
        for receiver in tuple(self._retired_relays):
            receiver.reverb_slot = None
            sources.extend(source for source in (receiver.source_l, receiver.source_r)
                           if source is not None)
            cinema = getattr(receiver, "cinema", None)
            if cinema is not None:
                cinema.set_reverb(None)
                sources.extend(cinema.sources)
        # Retired direct tails (song-advance crossfade) follow the same rule.
        for entry in tuple(self._retiring_direct):
            streamer = entry.get("streamer")
            if streamer is not None:
                streamer.reverb_slot = None
            bank = entry.get("cinema")
            if bank is not None:
                bank.set_reverb(None)
            sources.extend(source for source in self._entry_sources(entry)
                           if source is not None)
        if audio is None or not hasattr(audio, "efx"):
            return
        for source in sources:
            try:
                audio.efx.send(source, 0, None)
            except Exception:
                pass
