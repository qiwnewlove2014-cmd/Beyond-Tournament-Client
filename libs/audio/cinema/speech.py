"""A voice coming out of a cabinet's room.

The megaphone already knows how to get a voice out of a set of speakers: every
listener plays the talker's frames at the map's PA speakers with the PA's own
delays, filters and gain. A cinema cabinet is the same job with better numbers
-- the speakers the builder placed around that one cabinet, the room's own
distance ramp (8 m full, 60 m silent), the map's level for each speaker, the
wall standing between, and the trim that speaker carries.

So a talker standing inside a room's reach is heard *from that room* instead of
from the map's PA: the room replaces the PA for that voice, which also means a
map with a cabinet but no PA speakers at all -- where megaphone speech used to
be silent everywhere -- now carries a voice through the cabinet.

Three things this deliberately does not do:

    * it does not touch the PA path. A talker with no room around them keeps
      byte-identical PA behaviour, and so does every listener who turned this
      off, on a map the feature never reached;
    * it does not push the voice into the room's *frame queue*. That queue runs
      80-240 ms deep plus the trims, and a voice has no beat that could take
      that back out -- the same reason a live note is played at the speakers
      rather than queued (see ``live.py``);
    * it does not play the talker's own voice back at the room *from their
      packet*. A client only ever spawns copies of the frames it received, and
      the Server never echoes a broadcast to its sender, so a listener hears
      each voice once. The talker's own monitor is a separate leg
      (:func:`feed_local`, built from their own microphone) for the talker
      standing where no PA can be heard at all.

Every number comes from the room itself (``LiveRoomRouter.terms_for_plan``) so
one speaker is exactly as loud for a voice as it is for the band and for the
song: the same distance ramp, the same per-speaker level and aim, the same wall
filters (`listener.occlusion_filter`), and the same two EFX sends -- the room's
reverb zone and the cabinet's EQ (`room_environment`), which is what makes a
voice come *out of that room* instead of out of a dry booth.
"""

import time
from collections import deque
from contextlib import suppress
from math import sqrt

from .layout import ROOM_RADIUS
from .listener import occlusion_filter, restore_filter

# The listener's own choice, like ``cinema_live_instruments``: it is how *you*
# hear a voice, it takes nothing from anybody (no source of anyone else's is
# touched and no packet changes), and it is on until someone in the Music Bot
# menu turns it off. The switch behind it is on that menu, not in Options, and
# the audio path never asks who you are -- only what you chose.
OPTION_ENABLED = "cinema_speech"
DEFAULT_ENABLED = True

# One voice frame is 20 ms of mono 48 kHz PCM, the same frame the PA plays.
FRAME_MS = 20.0
SAMPLE_RATE = 48000
# REAL frames a speaker is given before it starts. The PA keeps a much bigger
# cushion because its frames cross the network; this leg's frames only cross
# the audio worker's own 20 ms timer, but a speaker handed one frame at a time
# goes dry the first time that timer is a millisecond late -- heard as clicks
# in the voice. Frames, never silence: the room has audio to wait for.
START_FRAMES = 3
# A speaker's trim is an installer's alignment offset. For a voice it is worth
# honouring up to a point and no further: past ~60 ms a talker hears the room
# answering them late, which reads as lag rather than as depth.
TRIM_LIMIT_MS = 60.0
# Nobody has been heard for this long: let the speakers go. The megaphone play
# out already drops a stream after a second of silence; this is the same clock,
# so a leg cannot outlive the voice it was built for.
RELEASE_AFTER_S = 1.0
# The megaphone's own cap on simultaneous talkers. A leg is 1 source per room
# speaker (no ground-reflection clone, unlike the PA), so this is cheaper than
# the path it replaces.
MAX_TALKERS = 8
# The cabinet's own slider, because this is that cabinet's system: one volume
# moves the song, the band and the voice coming out of it together.
CATEGORY = "jukebox"
BUFFER_POOL_LIMIT = 64
# The room's two EFX sends, the same two indices the room's own speakers carry
# for the song (see ``bank.CinemaSpeakerBank.update_output``): 0 is the reverb
# zone's slot, 1 the cabinet's EQ. Same index, same slot, same filter, so there
# is one room in the room.
REVERB_SEND = 0
EQ_SEND = 1


def speech_enabled():
    """Whether this client plays a voice through the room the talker is in."""
    try:
        from ... import options
        saved = options.get(OPTION_ENABLED)
        if saved is None:
            return DEFAULT_ENABLED
        return bool(saved)
    except Exception:
        # A missing settings back end must not silence a room: the default is
        # what a player would have had anyway.
        return DEFAULT_ENABLED


def set_speech_enabled(enabled):
    """Record the listener's choice. The menu line and the audio share a key."""
    try:
        from ... import options
        options.set(OPTION_ENABLED, bool(enabled))
    except Exception:
        return False
    return True


def talker_position(gameplay, sender_id):
    """Where the talker stands, from their entity.

    A megaphone frame carries the *sender's voice channel id* as its first
    byte, and ``gameplay.voice_channels`` maps that id to the entity the
    server put it on -- so the room a voice comes out of is the room the
    person speaking is standing in, resolved locally and needing no packet.
    """
    try:
        entity = getattr(gameplay, "voice_channels", {}).get(sender_id)
    except Exception:
        return None
    if entity is None:
        return None
    try:
        return (float(entity.x), float(entity.y), float(entity.z))
    except Exception:
        return None


def local_position(gameplay):
    """Where the LOCAL player stands, for their own monitor's room.

    The player's own entity first (that is the body the server knows about),
    then the listener's ears, so a client with no entity yet still answers.
    """
    player = getattr(gameplay, "player", None)
    try:
        return (float(player.x), float(player.y), float(player.z))
    except Exception:
        pass
    try:
        position = getattr(getattr(gameplay, "game", None), "audio_mngr", None).position
    except Exception:
        return None
    if position is None:
        return None
    try:
        return tuple(float(value) for value in position)
    except Exception:
        return None


def _router(game):
    from .live import router_for
    router = router_for(getattr(game, "audio_mngr", None))
    if router.game is None:
        router.game = game
    return router


def room_target(game, gameplay, sender_id):
    """``(cabinet_id, plan)`` for the room this talker stands in, or None.

    Cheap and safe on any thread: it reads one entity position and asks the
    same router the live instruments ask (which re-resolves at most once a
    second). It does no OpenAL work at all, which is what lets the audio
    worker decide PA-or-room per frame without touching the audio thread's
    objects.

    The talker has to be *in* the room, not merely nearest to it: the router
    answers "the cabinet closest to this point", and a speaker set is the
    room's reach from its own cabinet (``ROOM_RADIUS``). Somebody standing
    out in the map with one cabinet far behind them is on the map's PA -- the
    room's speakers are all out of earshot of every listener anyway, so
    routing them there would silence an announcement that the PA would have
    carried everywhere.
    """
    if game is None or gameplay is None:
        return None
    return _target_at(game, talker_position(gameplay, sender_id))


def local_room_target(game, gameplay):
    """``(cabinet_id, plan)`` for the room the LOCAL player is standing in.

    The same question as :func:`room_target`, asked about this client's own
    body: a player standing in a hall hears their own broadcast out of that
    hall's speakers, exactly like everyone else in it.
    """
    if game is None or gameplay is None:
        return None
    return _target_at(game, local_position(gameplay))


def local_room_available(game, gameplay):
    """Whether a room could carry this player's own voice right now.

    Used by the PA Test Mode gate: on a map with a cabinet and no PA
    speakers, the room *is* the public address system, so the O key has
    something to test instead of refusing with "no PA speakers available".
    """
    try:
        return local_room_target(game, gameplay) is not None
    except Exception:
        return False


def _target_at(game, position):
    """The room a point stands inside, or None (see :func:`room_target`)."""
    if position is None or game is None or not speech_enabled():
        return None
    try:
        target = _router(game).route_for(position)
    except Exception:
        return None
    if target is None or not _inside_room(target[1], position):
        return None
    return target


def _inside_room(plan, position):
    """Whether a point stands inside a resolved room's own reach."""
    anchor = getattr(getattr(plan, "placement", None), "anchor", None)
    if anchor is None:
        return True
    try:
        gap = sqrt(sum((float(anchor[index]) - float(position[index])) ** 2
                       for index in range(3)))
    except Exception:
        return True
    return gap <= ROOM_RADIUS


def routed(game, gameplay, sender_id):
    """Whether this talker's voice belongs to a room (no OpenAL, any thread)."""
    try:
        return room_target(game, gameplay, sender_id) is not None
    except Exception:
        return False


def rooms_for(game):
    """The registry of voices this client is playing through rooms.

    One per client, held by the audio manager with the audio it plays into, so
    a relogin or a map change takes the sources with it instead of leaving
    them on a dead map.
    """
    audio = getattr(game, "audio_mngr", None)
    if audio is None:
        return None
    holder = getattr(audio, "cinema_speech", None)
    if holder is None:
        holder = SpeechRooms(game)
        try:
            audio.cinema_speech = holder
        except Exception:
            return None
    holder.game = game
    return holder


def room_environment(game, gameplay, cabinet_id, plan):
    """``(reverb_slot, eq_slot)`` for a cabinet's room: what the song gets.

    A voice out of a room should sound like *that room*, not like it was played
    into the room from a dry booth, so it goes through the same two sends the
    room's own speakers use for the song. Both are properties of the cabinet,
    not of a playing stream, so they are derived the way the jukebox derives
    them -- the reverb zone the cabinet stands in, and the cabinet's own EQ
    preset -- which also means they exist with nothing playing at all (a band
    in a hall with no song).

    **No reverb zone around the cabinet is no reverb send**: a voice is never
    handed an invented room, it is played dry, which is what a plain map has
    always sounded like. Either slot may be None, and every caller has to treat
    None as "clear that send" rather than "keep the last one".
    """
    anchor = _room_anchor(game, cabinet_id, plan)
    return (_zone_reverb(gameplay, anchor),
            _cabinet_eq(gameplay, cabinet_id))


def _room_anchor(game, cabinet_id, plan):
    """Where the cabinet this room belongs to stands, from the plan or the map."""
    placement = getattr(plan, "placement", None)
    anchor = getattr(placement, "anchor", None)
    if anchor is not None:
        try:
            return tuple(float(value) for value in anchor)
        except (TypeError, ValueError):
            pass
    try:
        from .plugin import cabinet_anchor
        return cabinet_anchor(game, cabinet_id)
    except Exception:
        return None


def _zone_reverb(gameplay, anchor):
    """The reverb zone standing where the cabinet is, or None for a dry spot."""
    if anchor is None:
        return None
    lookup = getattr(getattr(gameplay, "map", None), "get_reverb_at", None)
    if not callable(lookup):
        return None
    try:
        zone = lookup(*anchor)
    except Exception:
        return None
    if zone is None:
        return None
    return getattr(zone, "reverb", None)


def _cabinet_eq(gameplay, cabinet_id):
    """The cabinet's own EQ slot, or None (normal profile, or no jukebox).

    The cabinet owns one EQ, so a voice and the song coming out of the same box
    are shaped by the same box. ``_get_eq_slot`` is the jukebox's own (cached)
    resolver, asked rather than re-implemented: a preset the player picked must
    not be able to mean two different things.
    """
    player = getattr(gameplay, "jukebox_player", None)
    resolver = getattr(player, "_get_eq_slot", None)
    if not callable(resolver):
        return None
    profiles = getattr(player, "eq_profiles", None)
    profile = "normal"
    if isinstance(profiles, dict):
        profile = str(profiles.get(cabinet_id, "normal") or "normal")
    stored = getattr(player, "eq_values", None)
    values = stored.get(cabinet_id) if isinstance(stored, dict) else None
    try:
        return resolver(profile, cabinet_id, values)
    except Exception:
        return None


def feed(game, gameplay, sender_id, packet):
    """Play one voice frame at the room's speakers. MAIN THREAD ONLY.

    Returns False when there is no room for this talker -- no cabinet on the
    map, a cabinet the map set to ``off``, a speaker set that never resolved,
    or a listener who turned this off. The caller then keeps the PA path, so
    the fallback is the behaviour that shipped, not a new one.
    """
    return _feed_at(game, gameplay, sender_id, packet,
                    room_target(game, gameplay, sender_id),
                    talker_position(gameplay, sender_id), monitor=False)


def feed_local(game, gameplay, key, packet):
    """The OWNER's own broadcast, at the room they are standing in.

    MAIN THREAD ONLY. Same room, same speakers and same numbers as everyone
    else hears this player through, so a performer standing in the hall hears
    their own voice from the hall instead of from a PA that (on a map with a
    cabinet and no PA speakers) does not exist at all.

    ``monitor=True``: the installer's per-speaker trims are skipped. They are
    an alignment offset for the people standing out there, not something the
    performer should hear their own line through (the same rule the PA
    monitor follows, see ``voice_chat.queue_and_delay_frame``).
    """
    return _feed_at(game, gameplay, key, packet,
                    local_room_target(game, gameplay),
                    local_position(gameplay), monitor=True)


def _feed_at(game, gameplay, key, packet, target, position, monitor):
    if target is None:
        # The room can also go away mid-sentence (a talker walks out of it, a
        # builder deletes the last speaker, the cabinet is switched to off, or
        # this listener turned the switch off): release the speakers and let
        # the PA take it back.
        drop(game, key)
        return False
    holder = rooms_for(game)
    if holder is None or position is None:
        drop(game, key)
        return False
    holder.sweep()
    return holder.leg(key, monitor=monitor).feed(packet, position, target, gameplay)


def drop(game, sender_id):
    """Stop playing this talker's voice through a room. MAIN THREAD ONLY.

    Destroying OpenAL sources is the one thing here that must never happen on
    the audio worker: it is called from the play-out thread through the audio
    inbox, exactly like the frame feed.
    """
    holder = getattr(getattr(game, "audio_mngr", None), "cinema_speech", None)
    if holder is None:
        return
    holder.drop(sender_id)


def release_all(game):
    """Hand every room back -- used when the map or the audio goes away."""
    holder = getattr(getattr(game, "audio_mngr", None), "cinema_speech", None)
    if holder is None:
        return
    for sender_id in list(holder.legs):
        holder.drop(sender_id)


def describe(game):
    """One line per voice currently coming out of a room, for menus and logs."""
    holder = getattr(getattr(game, "audio_mngr", None), "cinema_speech", None)
    if holder is None:
        return []
    return [f"{sender_id}: jukebox {leg.cabinet_id} "
            f"({len(leg.sources)} speaker(s))"
            for sender_id, leg in holder.legs.items()]


def _trim_frames(delay_ms):
    """A speaker's trim, in whole 20 ms frames (the frame is what we hold)."""
    try:
        trimmed = min(TRIM_LIMIT_MS, max(0.0, float(delay_ms)))
    except Exception:
        return 0
    return int(round(trimmed / FRAME_MS))


def _volume(game):
    """The global PA volume, times the cabinet's own category.

    The same two numbers the PA path answers to (``megaphone_volume``) and the
    same category the room's song uses, so a listener who turns that cabinet
    down hears the voice come down with the song instead of a voice at full
    volume over a quiet cabinet.
    """
    global_volume = 1.0
    try:
        from ... import options
        global_volume = float(options.get("megaphone_volume", 100)) / 100.0
    except Exception:
        pass
    category = 1.0
    try:
        category = getattr(game.audio_mngr, "volume_categories", {}).get(
            CATEGORY, [100])[0] / 100.0
    except Exception:
        pass
    return max(0.0, global_volume) * max(0.0, category)


class SpeechRooms:
    """Every voice this client is currently playing through a room."""

    def __init__(self, game=None):
        self.game = game
        self.legs = {}
        # Two wall filters and one buffer pool, shared by every talker: a voice
        # is ~50 buffers a second per speaker and there is no reason for each
        # leg to build its own.
        self.filters = {}
        self.pool = []

    def leg(self, sender_id, monitor=False):
        leg = self.legs.get(sender_id)
        if leg is None:
            while len(self.legs) >= MAX_TALKERS:
                oldest = min(self.legs.values(), key=lambda item: item.last_feed)
                self.drop(oldest.sender_id)
            leg = RoomSpeechLeg(self, sender_id, monitor=monitor)
            self.legs[sender_id] = leg
        return leg

    def drop(self, sender_id):
        leg = self.legs.pop(sender_id, None)
        if leg is not None:
            leg.release()

    def sweep(self):
        """Let go of a talker nobody has heard from in a second."""
        now = time.monotonic()
        for sender_id in [sid for sid, leg in self.legs.items()
                          if now - leg.last_feed > RELEASE_AFTER_S]:
            self.drop(sender_id)

    def take_buffer(self):
        while self.pool:
            buffer = self.pool.pop()
            if buffer is not None:
                return buffer
        try:
            return self.game.audio_mngr.context.gen_buffer()
        except Exception:
            return None

    def recycle(self, buffers):
        for buffer in buffers or ():
            if buffer is None or len(self.pool) >= BUFFER_POOL_LIMIT:
                continue
            self.pool.append(buffer)


class RoomSpeechLeg:
    """One talker's voice, at every speaker of the room they stand in.

    Its sources are flat (``rolloff_factor = 0``): the room's own ramp already
    shaped this voice, and letting OpenAL attenuate it again would fade the
    same speaker twice -- the same reason a live note is spawned flat.
    """

    # How often the room's reverb and EQ are re-read. The room refreshes its
    # own environment once a second (``JukeboxPlayer.sync_reverb``), and both
    # are properties of the cabinet rather than of a frame: asking per frame
    # would also re-run the jukebox's EQ resolver on every one of them, which
    # for a custom profile means re-writing its parameters fifty times a
    # second to no effect. A change is heard on the next refresh either way.
    ENVIRONMENT_INTERVAL_S = 1.0

    def __init__(self, holder, sender_id, monitor=False):
        self.holder = holder
        self.sender_id = sender_id
        # True for the owner's own monitor (see :func:`feed_local`): the
        # installer's trims are skipped, the room's geometry is not.
        self.monitor = bool(monitor)
        self.gameplay = None
        self.cabinet_id = None
        self.plan = None
        self.signature = None
        self.sources = {}     # slot -> OpenAL source
        self.fresh = {}       # slot -> frames queued since a stopped source's last drain
        self.hold = {}        # slot -> frames waiting for that speaker's trim
        self.holds = {}       # slot -> how many frames that speaker holds
        self.tiers = {}       # slot -> the wall tier last applied
        self.environments = {}  # slot -> the (reverb, EQ) pair last applied
        self._environment_value = (None, None)
        self._environment_at = 0.0
        self.last_feed = 0.0

    # --------------------------------------------------------------- feeding
    def feed(self, packet, position, target, gameplay):
        """One frame of this talker, at every speaker of their room."""
        self.gameplay = gameplay
        self.last_feed = time.monotonic()
        self._follow(target)
        if not self.sources:
            return False
        self._shape()
        for slot, source in list(self.sources.items()):
            waiting = self.hold.get(slot)
            if waiting is None:
                continue
            waiting.append(packet)
            if len(waiting) <= self.holds.get(slot, 0):
                # This speaker carries a trim: it plays the frame that many
                # frames later, so speech keeps the room's alignment the way
                # the song does. The room's first frame is never held back by
                # waiting for audio nobody will send (the limit is whole
                # frames, and `holds` is derived from the trim itself).
                continue
            self._publish(slot, source, waiting.popleft())
        return True

    def _follow(self, target):
        """Re-shape for the room the talker is in now, at most once a second.

        A resolve that finds nothing keeps the room that is already playing: a
        builder deleting one speaker mid-sentence must not cut the voice off
        (the same rule the playing room follows for the song).
        """
        cabinet_id, plan = target
        signature = _signature(plan)
        if self.plan is not None and cabinet_id == self.cabinet_id \
                and signature == self.signature:
            return
        self._reshape(cabinet_id, plan, signature)

    def _reshape(self, cabinet_id, plan, signature):
        specs = _specs(plan)
        if not specs:
            return
        self.cabinet_id = cabinet_id
        self.plan = plan
        self.signature = signature
        # A different room is a different reverb: read the new one at once
        # rather than serving the old room's for up to a refresh interval.
        self._environment_at = 0.0
        for slot in list(self.sources):
            if slot not in specs:
                self._retire(slot)
        for slot, spec in specs.items():
            if slot in self.sources:
                continue
            source = self._new_source(spec)
            if source is None:
                continue
            self.sources[slot] = source
            self.hold[slot] = deque()
            self.holds[slot] = (0 if self.monitor
                                else _trim_frames(getattr(spec, "delay_ms", 0.0)))
            self.tiers[slot] = None       # force the wall to be measured
            self.environments[slot] = None  # and the room's reverb to be sent
        if not self.sources:
            # Nothing could be created (a dead context, or a room with no
            # speakers left): do not claim to be following it, so the next
            # frame tries again instead of feeding a stale plan.
            self.plan = None
            self.signature = None

    def _new_source(self, spec):
        audio = getattr(self.holder.game, "audio_mngr", None)
        try:
            source = audio.context.gen_source()
        except Exception:
            return None
        try:
            source.position = tuple(float(value) for value in spec.position)
            # Flat at the speaker: the room's ramp already shaped the voice.
            source.rolloff_factor = 0.0
            source.reference_distance = 1.0
            source.max_distance = 100000.0
            source.gain = 0.0
        except Exception:
            pass
        return source

    def _shape(self):
        """Per-speaker gain, aim, distance and wall, from the room's numbers.

        A speaker the terms no longer mention is *out of reach* (the room's own
        ramp already decided that), so it is silenced rather than left at the
        last gain it happened to have: a listener who walks out of the hall
        must stop hearing the voice, and a frozen gain would keep playing it at
        whatever level the last position gave.
        """
        terms = self._terms()
        environment = self._environment()
        game = self.holder.game
        audio = getattr(game, "audio_mngr", None)
        volume = _volume(game)
        for slot, source in self.sources.items():
            term = terms.get(slot)
            if term is None:
                with suppress(Exception):
                    source.gain = 0.0
                continue
            _slot, _spot, gain, _delay, tier = term
            with suppress(Exception):
                source.gain = max(0.0, volume * gain)
            if (tier == self.tiers.get(slot)
                    and environment == self.environments.get(slot)):
                continue
            self.tiers[slot] = tier
            self.environments[slot] = environment
            filt = occlusion_filter(audio, tier, self.holder.filters)
            with suppress(Exception):
                if filt is not None:
                    source.direct_filter = filt
                else:
                    restore_filter(source, audio)
            self._send_environment(source, environment, filt)

    def _environment(self):
        """The room's reverb and EQ (see :func:`room_environment`).

        Re-read on a timer rather than remembered forever: a player who changes
        the cabinet's EQ, or a reload that moves the reverb zone the cabinet
        stands in, has to reach a voice that is already playing. Never
        *invented*, either: a room with no reverb zone yields None, which the
        caller sends as "clear that send" and the voice stays dry.
        """
        now = time.monotonic()
        if now - self._environment_at < self.ENVIRONMENT_INTERVAL_S:
            return self._environment_value
        self._environment_at = now
        try:
            value = room_environment(self.holder.game, self.gameplay,
                                     self.cabinet_id, self.plan)
        except Exception:
            value = (None, None)
        self._environment_value = value
        return value

    def _send_environment(self, source, environment, filt):
        """Route one speaker's voice through the room's reverb and its EQ.

        The same sends the room's own speakers carry for the song, on the same
        slots and with the wall filter as the send filter, so a voice behind a
        wall is muffled *inside the reverb* exactly as much as it is dry (the
        room's filters are shared, see ``listener.occlusion_filter``).

        A slot that is None is sent as None, which *clears* the send: the room
        the voice was in may have stopped having a reverb, and a voice left
        ringing in a room that no longer has one is a bug you hear once and
        then never trust the speaker again.
        """
        audio = getattr(self.holder.game, "audio_mngr", None)
        efx = getattr(audio, "efx", None)
        if efx is None:
            return
        reverb_slot, eq_slot = environment
        for index, slot in ((REVERB_SEND, reverb_slot), (EQ_SEND, eq_slot)):
            with suppress(Exception):
                efx.send(source, index, slot, filter=filt)

    def _terms(self):
        if self.plan is None:
            return {}
        game = self.holder.game
        listener = getattr(getattr(game, "audio_mngr", None), "position", None)
        provider = getattr(getattr(self.gameplay, "jukebox_player", None),
                           "occlusion_tier", None)
        try:
            terms = _router(game).terms_for_plan(self.plan, listener, provider)
        except Exception:
            return {}
        return {term[0]: term for term in terms}

    def _publish(self, slot, source, frame):
        """Queue one frame on one speaker and make sure it is playing.

        A STOPPED or INITIAL source reports **every** buffer it holds as
        *processed*: OpenAL counts a buffer as done the moment its source is
        not playing it. Recycling on that number before queueing the next
        frame therefore hands back the frame that was just queued, and the
        queue can never reach ``START_FRAMES`` -- so a speaker that had ever
        played (the first word of the previous sentence, the end of the last
        burst) never started again, and the next thing said through the room
        was silent until the leg was swept and its sources rebuilt. Only a
        *playing* source's finished buffers are recycled here; while a stopped
        one is being filled, what is left over from the previous burst goes
        first and this burst's own frames stay.
        """
        try:
            import cyal
            state = source.state
            queued = int(source.buffers_queued)
        except Exception:
            return
        if state in (cyal.SourceState.PLAYING, cyal.SourceState.PAUSED):
            try:
                if source.buffers_processed > 0:
                    self.holder.recycle(source.unqueue_buffers())
            except Exception:
                return
            self.fresh[slot] = 0
        else:
            # A stopped speaker's leftover queue is finished audio; the frames
            # this burst has queued are not (and cannot be told apart by the
            # driver's own count). `max=` keeps the unqueue to the leftovers.
            stale = queued - self.fresh.get(slot, 0)
            if stale > 0:
                try:
                    self.holder.recycle(source.unqueue_buffers(max=stale))
                except Exception:
                    return
        buffer = self.holder.take_buffer()
        if buffer is None:
            return
        try:
            buffer.set_data(frame, sample_rate=SAMPLE_RATE,
                            format=cyal.BufferFormat.MONO16)
            source.queue_buffers(buffer)
        except Exception:
            self.holder.recycle([buffer])
            return
        try:
            import cyal
            if source.state in (cyal.SourceState.STOPPED, cyal.SourceState.INITIAL):
                self.fresh[slot] = self.fresh.get(slot, 0) + 1
                if source.buffers_queued < START_FRAMES:
                    # Give it real frames to start on (see START_FRAMES).
                    return
                source.play()
                self.fresh[slot] = 0
            else:
                self.fresh[slot] = 0
        except Exception:
            pass

    def _retire(self, slot):
        source = self.sources.pop(slot, None)
        self.fresh.pop(slot, None)
        self.hold.pop(slot, None)
        self.holds.pop(slot, None)
        self.tiers.pop(slot, None)
        self.environments.pop(slot, None)
        if source is not None:
            self._destroy(source)

    def release(self):
        """Hand the room back: stop every speaker and return their buffers."""
        for slot in list(self.sources):
            self._retire(slot)
        self.plan = None
        self.signature = None
        self.cabinet_id = None

    def _destroy(self, source):
        with suppress(Exception):
            source.stop()
        self._drain(source)
        with suppress(Exception):
            source.destroy()

    def _drain(self, source):
        """Return whatever OpenAL has finished with, bounded and never looped.

        ``unqueue_buffers`` only ever hands back *processed* buffers, so a
        queue whose remaining buffers are still counted as queued would spin
        here forever -- on the main thread. Ask at most as many times as the
        queue holds and stop the moment nothing comes back.
        """
        with suppress(Exception):
            for _ in range(int(source.buffers_queued) + 1):
                taken = source.unqueue_buffers()
                if not taken:
                    break
                self.holder.recycle(taken)


def _specs(plan):
    """``{slot: spec}`` for a resolved plan, or ``{}`` when it has none."""
    placement = getattr(plan, "placement", None)
    specs = {}
    for slot in getattr(placement, "slots", ()):
        specs[slot] = placement.speakers[slot].spec
    return specs


def _signature(plan):
    """What makes this room *this* room, for reuse without rebuilding.

    Position, level, aim and trim per slot: a builder placing or moving a
    speaker mid-sentence is followed, while a re-resolve that returns the same
    room keeps the speakers that are already playing (rebuilding them would
    drop a queue's worth of voice).
    """
    placement = getattr(plan, "placement", None)
    parts = []
    for slot in getattr(placement, "slots", ()):
        spec = placement.speakers[slot].spec
        parts.append((slot, tuple(spec.position), float(spec.level),
                      float(getattr(spec, "delay_ms", 0.0)),
                      getattr(spec, "aim_yaw", None)))
    return tuple(parts)
