"""A song one player plays into a cabinet's room, heard by everyone there.

The Music Bot is the Jukebox's sibling: one player picks a track, it is
decoded on their own client and -- while the bot broadcasts -- the Server
relays those Opus frames to every player on the map. Listeners have always
played them out of a *speaker on the sender's back*, because that is where the
relay frames were aimed: the receiving entity's own ``music_source``, a 3D
source 5 m loud and 50 m silent.

So when the sender routed their bot into a cabinet's room (Music Bot menu ->
``Cinema Speakers:``), only their own client knew. That routing is a local
output choice, like the other listening switches, which meant the room was
heard by the person testing it and by nobody else -- and everyone else heard
either a distant speaker on the sender's back, or, with the bot in its default
private mode, nothing at all.

This module closes that gap without the Jukebox's relay being touched:

    sender                                listener
    ------                                --------
    music bot -> the cabinet's room       the same Opus frames arrive
    announce the cabinet  ------------->  note_routing(channel, cabinet)
                                          route(channel) -> resolve that room
                                          from *this* client's own map, put the
                                          frames through a bank keyed
                                          ``musicpeer:<channel>``

Three things stay exactly as they were. The sender's own copy of the room is
their client's business, the plain 3D feed is untouched for anybody who never
sees an announcement, and a Party Sync session -- which shares the bot with its
guests privately -- is never placed in a shared room.

Both halves of the decision are the sender's: *which* cabinet, and *that* the
song belongs in a room at all. The listener's own switch (``cinema_speakers``)
still wins over it, a listener whose map cannot resolve that room falls back to
the plain feed, and the cabinet's own ``cinema_mode`` stays a decision about
the cabinet's *own* playback rather than a veto over somebody else's stream.
"""

import time

from ...deferred_log import log_deferred as log_line
from .layout import ROOM_MAX_DISTANCE, ROOM_REFERENCE_DISTANCE
from .plugin import (acquire_bank, cabinet_anchor, preview_room,
                     release_renderer, room_diagnosis, rooms_enabled)

# The event the sender announces its routing on, and the attribute the holder
# lives on. One name each, asked rather than copied by every caller.
ANNOUNCE_EVENT = "music_bot_cinema"
ROOMS_ATTRIBUTE = "cinema_peer_music"

# How often the playing room re-resolves the map under it: a builder placing
# or deleting a speaker mid-song, or moving the cabinet, is followed without
# anything being stopped or restarted (see CinemaSpeakerBank.reconfigure).
RESHAPE_INTERVAL = 1.0

# No frames for this long and the room is handed back: the sender stopped, left
# the map, or lost their connection, and a room nobody feeds is a room holding
# a set of OpenAL sources for nothing. The megaphone play-out drops a stream on
# the same clock, so nothing outlives the audio it was built for.
STALE_AFTER = 1.0

# A routing whose room cannot be resolved (the cabinet was deleted, the map
# reloaded without its speakers) is retried at this cadence rather than on
# every frame: resolving a room pairs up speakers and measures the map, and
# fifty attempts a second would spend a frame's budget on a room that is not
# there.
RETRY_AFTER = 2.0

# The volume category a peer's song answers to: the Music slider, because that
# is what a Music Bot stream is -- the room is only where it comes out.
CATEGORY = "music"


def rooms_for(game, *, create=False):
    """The holder for this game's peer rooms, or None when there is none yet."""
    if game is None:
        return None
    holder = getattr(game, ROOMS_ATTRIBUTE, None)
    if holder is None and create:
        holder = PeerRoomHost(game)
        try:
            setattr(game, ROOMS_ATTRIBUTE, holder)
        except Exception:
            return holder
    return holder


def note_routing(game, channel, cabinet):
    """Record which cabinet's room a peer is playing their bot through.

    Called from the Server's own announcement (the sender repeats it while it
    streams, so a missed packet heals by itself and a listener who joined
    mid-song picks it up shortly after). An empty cabinet means "plain feed
    again", which is how the sender turning the routing off is heard at once
    instead of at the end of the song.
    """
    holder = rooms_for(game, create=True)
    if holder is None:
        return None
    return holder.note_routing(channel, cabinet)


def routing_for(game, channel):
    """The cabinet this peer announced, or None for the plain 3D feed."""
    holder = rooms_for(game)
    return None if holder is None else holder.routing(channel)


def route(game, channel, entity=None):
    """The room this peer's frames should play through, or None for the feed.

    This is the whole decision, in one call, for the receive path: it is cheap
    when nothing is routed (two dict lookups), and it hands back the same feed
    for every frame of a song so a room is built once rather than per frame.
    """
    holder = rooms_for(game)
    if holder is None:
        return None
    return holder.route(channel, entity)


def forget(game, channel):
    """Stop playing one peer through a room (its routing went away)."""
    holder = rooms_for(game)
    if holder is None:
        return
    holder.forget(channel)


def release_all(game):
    """Hand every peer room back, and forget every routing.

    A map reload replaces the speakers (their positions are the old map's) and
    the voice channels with them, so both halves of this are stale at once.
    """
    holder = rooms_for(game)
    if holder is None:
        return 0
    return holder.release_all()


def describe(game):
    """One line per peer currently heard through a room, for diagnostics."""
    holder = rooms_for(game)
    return [] if holder is None else holder.describe()


class PeerRoomHost:
    """Every song this client is playing through a cabinet's room.

    One feed per peer, keyed by the sender's voice channel -- the only thing
    the relay packets and the announcement both carry, and the same key the
    receiving entity itself is looked up by.
    """

    def __init__(self, game=None):
        self.game = game
        self.feeds = {}
        # channel -> cabinet, as announced. Kept apart from the feeds because
        # a routing whose room cannot be resolved has to stay remembered: the
        # room may well exist in a second, when the map comes back.
        self.routings = {}
        self._failed_at = {}
        # The last room problem already said out loud, per channel: a routing
        # whose room cannot be built is retried, and saying the same thing
        # every two seconds buries the one line that explains the silence.
        self._reported = {}
        self._reshaped_at = 0.0

    # ------------------------------------------------------------- routings
    def note_routing(self, channel, cabinet):
        try:
            channel = int(channel)
        except (TypeError, ValueError):
            return None
        cabinet = str(cabinet or "").strip() or None
        previous = self.routings.get(channel)
        if cabinet is None:
            self.routings.pop(channel, None)
            self.forget(channel)
            if previous is not None:
                log_line(f"[Cinema] music bot {channel} is back on the plain feed")
            return None
        if cabinet == previous and channel in self.feeds:
            return self.feeds.get(channel)
        self.routings[channel] = cabinet
        # A re-announcement of the same routing keeps the room that is playing.
        if cabinet != previous:
            self.forget(channel)
            # Said once per change: this is what proves the announcement
            # reached this client at all (a listener with no line here is
            # hearing the plain feed, and the next line will say why).
            log_line(f"[Cinema] music bot {channel} is routed to jukebox {cabinet}")
        return None

    def routing(self, channel):
        try:
            return self.routings.get(int(channel))
        except (TypeError, ValueError):
            return None

    # ---------------------------------------------------------------- rooms
    def route(self, channel, entity=None):
        """The feed for this peer's channel, building its room on first use."""
        try:
            channel = int(channel)
        except (TypeError, ValueError):
            return None
        self.sweep()
        cabinet = self.routings.get(channel)
        if not cabinet:
            # Nothing routed here for this peer: the room built for it goes
            # back now, so the sender turning it off is heard on the next
            # frame instead of at the end of the song.
            self.forget(channel)
            return None
        if not rooms_enabled():
            # This listener asked for the plain feed. Said out loud because a
            # listener who hears a song from the sender's back while the
            # sender's own menu says "theatre" has no other way to know that
            # the difference is their own switch.
            self._report(channel, "this client's Cinema rooms switch is off")
            self.forget(channel)
            return None
        if getattr(entity, "_party_sync_direct", False):
            # A Party Sync guest is hearing the host privately, on purpose.
            self.forget(channel)
            return None
        feed = self.feeds.get(channel)
        if feed is not None and feed.cabinet_id == cabinet and feed.active:
            self._follow(channel, feed)
            return feed
        if feed is not None:
            self.forget(channel)
        now = time.monotonic()
        if now - self._failed_at.get(channel, 0.0) < RETRY_AFTER:
            return None
        feed = self._build(channel, cabinet)
        if feed is None:
            self._failed_at[channel] = now
            return None
        self._failed_at.pop(channel, None)
        self.feeds[channel] = feed
        return feed

    def _build(self, channel, cabinet_id):
        """Resolve the room from this client's own map and take its speakers.

        Everything here is this listener's own view of the map, and a listener
        who hears nothing has no other way to find out why (the sender's menu
        only describes *their* client). So a room that cannot be built says so
        once, in the same words the jukebox uses for the same failure, and a
        room that is built says which one it is.
        """
        anchor = cabinet_anchor(self.game, cabinet_id)
        if anchor is None:
            self._report(channel, f"jukebox {cabinet_id} is not on this map")
            return None
        try:
            plan = preview_room(self.game, anchor, room_id=cabinet_id)
        except Exception as ex:
            self._report(channel, f"resolving jukebox {cabinet_id} failed: {ex}")
            return None
        if plan is None:
            try:
                why = room_diagnosis(self.game, anchor, room_id=cabinet_id)
            except Exception:
                why = "the speakers around it do not make a usable room"
            self._report(channel, f"no room around jukebox {cabinet_id} ({why})")
            return None
        bank = self._take_bank(channel, cabinet_id, anchor, plan)
        if bank is None:
            self._report(channel, f"jukebox {cabinet_id} has no speakers to play through")
            return None
        self._reported.pop(channel, None)
        log_line(f"[Cinema] music bot {channel} -> jukebox {cabinet_id} "
                 f"({plan.profile}, {len(bank.sources)} speaker(s))")
        for warning in plan.warnings:
            log_line(f"[Cinema] music bot {channel}: {warning}")
        return PeerRoomFeed(self.game, channel, cabinet_id, plan, bank,
                            anchor=anchor)

    def _report(self, channel, reason):
        """Say why a peer's room is not playing, once per distinct reason."""
        if self._reported.get(channel) == reason:
            return
        self._reported[channel] = reason
        log_line(f"[Cinema] music bot {channel}: {reason}")

    def _take_bank(self, channel, cabinet_id, anchor, plan):
        """Acquire the room's OpenAL speakers, or None when the map has none.

        The room reuses the cabinet's own shape (its profile, its speakers,
        its trims) because a listener in that hall should hear one room, not
        two: the song a cabinet plays and the song a peer sends both come out
        of the same speakers, shaped the same way.
        """
        reverb_slot, eq_slot = self._environment(cabinet_id, plan)
        try:
            return acquire_bank(
                self.game, self._key(channel), anchor,
                profile=plan.profile, specs=plan.specs, placement=plan.placement,
                fill=plan.fill,
                # The room's own scale, not the plain feed's 50 m: this is the
                # room the map describes, heard from its back row.
                reference_distance=ROOM_REFERENCE_DISTANCE,
                max_distance=ROOM_MAX_DISTANCE,
                occlusion_provider=self._occlusion,
                reverb_slot=reverb_slot,
                eq_slot=eq_slot,
                category=CATEGORY,
            )
        except Exception:
            return None

    def _environment(self, cabinet_id, plan):
        """``(reverb_slot, eq_slot)`` for the cabinet, as the song gets them.

        Derived from the cabinet rather than read off a playing bank: the
        room's acoustics are a property of the cabinet, so they are there with
        no song playing at all. A cabinet standing in no reverb zone reports
        None, which is what keeps a plain map sounding dry.
        """
        try:
            from .speech import room_environment
            return room_environment(self.game, getattr(self.game, "gameplay", None),
                                    cabinet_id, plan)
        except Exception:
            return (None, None)

    def _occlusion(self, position, listener, max_distance):
        """Wall occlusion measured from each speaker's own spot.

        Without a provider a room is heard as if the map had no walls at all.
        The jukebox player's ray caches tile results, so this is cheap per
        speaker; its own fallback is used on a map with no jukebox player.
        """
        gameplay = getattr(self.game, "gameplay", None)
        provider = getattr(getattr(gameplay, "jukebox_player", None),
                           "occlusion_tier", None)
        if not callable(provider):
            from ...jukebox import wall_occlusion_tier
            map_obj = getattr(gameplay, "map", None)
            provider = (lambda pos, lis, _max: wall_occlusion_tier(map_obj, pos, lis))
        try:
            return int(provider(position, listener, max_distance))
        except Exception:
            return 0

    @staticmethod
    def _key(channel):
        """This listener's own bank for a peer's stream.

        Keyed away from the cabinet's own room (``musicpeer:`` vs the cabinet
        id, or the sender's ``musicbot:<id>``): both can be playing at once,
        and whoever stops first must not take the other's speakers down.
        """
        return f"musicpeer:{int(channel)}"

    # -------------------------------------------------------------- upkeep
    def _follow(self, channel, feed):
        """Keep the playing room the map's room, at most once a second."""
        now = time.monotonic()
        if now - self._reshaped_at < RESHAPE_INTERVAL:
            return
        self._reshaped_at = now
        for other, playing in list(self.feeds.items()):
            anchor = cabinet_anchor(self.game, playing.cabinet_id)
            if anchor is None:
                # The cabinet is gone from the map. The stream keeps playing
                # in the room it already has -- the next frame re-decides --
                # rather than being cut off mid-song.
                continue
            try:
                plan = preview_room(self.game, anchor, room_id=playing.cabinet_id)
            except Exception:
                plan = None
            if plan is None:
                continue
            if plan.placement is not None and playing.matches(plan):
                continue
            # Re-acquiring the same key is a re-shape in place: the speakers
            # that did not change keep their queues, one that just appeared is
            # filled from the frames the room still holds and joins on the
            # current beat, and one that was deleted is stopped.
            bank = self._take_bank(other, playing.cabinet_id, anchor, plan)
            if bank is not None:
                playing.adopt(plan, bank)

    def sweep(self, now=None):
        """Let go of a peer nobody has heard from, and of a room that went dry."""
        now = time.monotonic() if now is None else now
        for channel, feed in list(self.feeds.items()):
            if now - feed.last_push > STALE_AFTER:
                self.forget(channel)

    def forget(self, channel):
        feed = self.feeds.pop(channel, None)
        if feed is not None:
            feed.stop()
        return feed

    def release_all(self):
        count = len(self.feeds)
        for channel in list(self.feeds):
            self.forget(channel)
        self.routings.clear()
        self._failed_at.clear()
        self._reported.clear()
        return count

    def describe(self):
        lines = []
        for channel, feed in self.feeds.items():
            lines.append(f"{channel}: jukebox {feed.cabinet_id} "
                         f"({feed.plan.profile}, {len(feed.bank.sources)} speaker(s))")
        return lines

    def __len__(self):
        return len(self.feeds)

    def __repr__(self):
        return (f"PeerRoomHost(rooms={len(self.feeds)}, "
                f"routings={len(self.routings)})")


class PeerRoomFeed:
    """One peer's decoded frames, playing out of a cabinet's room.

    The room owns the sources (one per speaker), so this object is what the
    receiving stream hands its PCM to instead of the entity's own source --
    exactly the shape the Music Bot itself feeds when the sender is the one
    routed into a room.
    """

    def __init__(self, game, channel, cabinet_id, plan, bank, anchor=None):
        self.game = game
        self.channel = int(channel)
        self.cabinet_id = str(cabinet_id)
        self.plan = plan
        self.bank = bank
        self.anchor = anchor
        self.last_push = time.monotonic()
        self._epoch = None
        self._started = False
        self._started_at = None

    # -------------------------------------------------------------- quality
    def matches(self, plan):
        """Whether a freshly resolved plan is still the room being played."""
        return self._signature(self.plan) == self._signature(plan)

    @staticmethod
    def _signature(plan):
        placement = getattr(plan, "placement", None)
        parts = []
        for slot in getattr(placement, "slots", ()) or ():
            spec = placement.speakers[slot].spec
            parts.append((slot, tuple(spec.position), float(spec.level),
                          float(getattr(spec, "delay_ms", 0.0)),
                          getattr(spec, "aim_yaw", None)))
        return (str(getattr(plan, "profile", "")), tuple(parts))

    def adopt(self, plan, bank):
        """Follow a re-shaped room: the same speakers, holding this stream."""
        self.plan = plan
        self.bank = bank

    @property
    def active(self):
        """False once the room this feed was playing into has gone away."""
        bank = self.bank
        if bank is None or getattr(bank, "_stopped", False):
            return False
        return bool(getattr(bank, "sources", ()))

    # -------------------------------------------------------------- feeding
    def push(self, pcm, *, stereo=False, epoch=None):
        """Hand one decoded frame to the room; True when the room took it."""
        self.last_push = time.monotonic()
        if epoch is not None:
            if self._epoch is not None and int(epoch) != self._epoch:
                # A new broadcast (a new track, a seek) re-forms the room: the
                # frames it still holds belong to the one before it, and
                # nothing can be taken back out of an OpenAL queue.
                self._restart()
            self._epoch = int(epoch)
        # What the speakers have finished with has to come back before the
        # next frame is queued: each pool holds a handful of buffers, so a
        # room that never reclaims refuses every frame after its first pool's
        # worth -- a quarter of a second of song and then silence. The bank
        # owns its per-speaker pools, so the bank owns the reclamation.
        #
        # Only once the room is actually *playing*, though: OpenAL reports a
        # source that is not playing as having processed everything it holds
        # (the same disclosure the speech leg is built around), so reclaiming
        # while the pre-buffer is still filling hands back the very frames the
        # room is waiting on. The queue can then never reach
        # ``wanted_for_start`` and the room stays silent for the whole song.
        if self._started:
            if self._room_playing():
                try:
                    self.bank.reclaim()
                except Exception:
                    pass
            else:
                # It started and then stopped on its own (a device hiccup, an
                # underrun): hand it the frames the room still holds and start
                # every speaker again rather than queuing behind a queue
                # nobody is playing.
                try:
                    self.bank.start_playback()
                except Exception:
                    pass
        left, right = split_frame(pcm, stereo)
        if not self.bank.queue_frame(left, right):
            return False
        if not self._started:
            try:
                ready = self.bank.queued_frames() >= self.bank.wanted_for_start()
            except Exception:
                ready = True
            if ready:
                try:
                    self.bank.start_playback()
                except Exception:
                    pass
                self._started = True
                self._started_at = time.monotonic()
        try:
            self.bank.update_output()
        except Exception:
            pass
        return True

    def _room_playing(self):
        """True while every speaker of the room is actually playing.

        A bank that cannot answer is treated as playing, so a room that works
        is never starved of its reclaimed buffers by a failed query.
        """
        try:
            return bool(self.bank.playing())
        except Exception:
            return True

    def _restart(self):
        """Empty this room's queues, keeping the speakers themselves."""
        reset = getattr(self.bank, "_reset_output", None)
        if callable(reset):
            try:
                reset()
            except Exception:
                pass
        self._started = False
        self._started_at = None

    def latency_s(self):
        """How far behind the live edge the room's audio is, in seconds.

        What a remote instrument note has to wait out to land on the beat the
        listener actually hears: the room's own queue, read from the room
        rather than assumed (the same measurement the jukebox path feeds to
        jam-note sync). The installer's trims are deliberately not in here:
        the room plays each speaker's own trim, and a note is spawned at that
        speaker with it (``live.route_to_room``), so counting the deepest trim
        as well would put every speaker -- untrimmed ones included -- behind
        the song by it.
        """
        try:
            return self.bank.buffered_ms() / 1000.0
        except Exception:
            return 0.0

    @property
    def playing(self):
        return self._started

    @property
    def started_at(self):
        """When this room began playing the stream, or None before it did."""
        return self._started_at

    def stop(self):
        """Hand the room back; the sources go with it.

        A room is only *silenced* by its own ``stop`` -- deleting its OpenAL
        sources is whoever put them there, which is this feed. Skipping that
        leaves a bank whose every source has been deleted and which the next
        stream is then handed (the silent-room-then-dead-names report the
        jukebox path already hit): the order here is stop, delete, forget,
        release.
        """
        bank = self.bank
        self.bank = None
        if bank is None:
            return
        try:
            bank.stop()
        except Exception:
            pass
        for source in getattr(bank, "sources", ()) or ():
            _delete_source(source)
        try:
            bank.forget_sources()
        except Exception:
            pass
        try:
            release_renderer(self.game, f"musicpeer:{self.channel}", bank)
        except Exception:
            pass

    def __repr__(self):
        return (f"PeerRoomFeed(channel={self.channel}, cabinet={self.cabinet_id!r}, "
                f"playing={self._started})")


def _delete_source(source):
    """Stop, drain and delete one OpenAL source this feed created."""
    if source is None:
        return
    try:
        source.stop()
        limit = 64
        while getattr(source, "buffers_processed", 0) > 0 and limit > 0:
            source.unqueue_buffers()
            limit -= 1
        limit = 64
        while getattr(source, "buffers_queued", 0) > 0 and limit > 0:
            source.unqueue_buffers()
            limit -= 1
        source.delete()
    except Exception:
        pass


def split_frame(pcm, stereo):
    """``(left, right)`` bytes for one decoded frame.

    A Music Bot broadcast is mono (one Opus channel, the proven public path),
    so both sides of the room are the same bytes and no work is done at all.
    A Party Sync session carries true stereo, and a frame that reaches here in
    that shape keeps its two sides rather than being downmixed.
    """
    if not stereo:
        return pcm, pcm
    data = bytes(pcm)
    try:
        import audioop
        return (audioop.tomono(data, 2, 1.0, 0.0),
                audioop.tomono(data, 2, 0.0, 1.0))
    except Exception:
        left = bytearray()
        right = bytearray()
        for index in range(0, len(data) - 3, 4):
            left += data[index:index + 2]
            right += data[index + 2:index + 4]
        return bytes(left), bytes(right)
