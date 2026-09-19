"""Live instruments coming out of a cabinet's room.

A room is built to play a *song*: one stream of frames, distributed to the
speakers standing around the cabinet. A live performance is not a stream, it
is a note struck at an instant -- and pushing it into the room's frame queue
would put it a whole queue behind the beat the performer is hearing (the
queue's own depth, 80-240 ms, plus the speaker trims the room holds). A
drummer following the song would then be heard late by exactly the amount the
room is buffered, which is the one thing note sync cannot take back out.

So a live note is played *at the room's speakers* instead: one short sample
per speaker, placed in the world like the megaphone's PA does for a voice with
the same note, shaped by the numbers the room itself uses (its distance ramp,
the map's per-speaker level and aim, the wall standing between) and timed by
the note's own scheduler with each speaker's trim added.

Two things this deliberately does NOT do: it does not turn the feature on for
someone who opted out or for a cabinet the map set to ``off``, and it does not
need a room that is already playing -- the speakers come from the map, so a
band playing in a hall with no song on reaches it, and a cabinet as plain as it
ever was stays plain.
"""

import time
from math import sqrt

from ...deferred_log import log_deferred as log_line
from .crossover import FULL_RANGE, crossover_hz, mark_of
from .listener import (distance_gain, speaker_aim_gain, tone_openness,
                       TONE_NEUTRAL)
from .pan import DEFAULT_DIRECTION, apply_direction
from .plugin import (CINEMA_AUTO, CINEMA_OFF, cabinet_anchors, cabinet_reach,
                     preview_room, room_diagnosis)
from .profiles import get_profile
from .layout import (ROOM_MAX_DISTANCE, ROOM_RADIUS, ROOM_REFERENCE_DISTANCE)

# Player preference: whether live instruments are also played at the speakers
# of the cabinet the performer stands at. It is a *listening* choice (like the
# cinema_speakers option) and not a broadcast: every client decides for itself,
# so two people in one hall can disagree without either being wrong. On by
# default for the same reason cinema_speakers is -- it can only ever matter on
# a map that gave a cabinet a room, so a plain jukebox is unaffected either
# way.
#
# It is on for *everybody*, with no rank attached: a live note takes nothing
# from anybody (the note is one sample per speaker on this listener's own
# client, never a turn at a cabinet), while feeding a room with a *song* does
# -- which is why that routing stays Developer/Contributor. The switch itself
# is not listed in Options: it is a listening choice, so it sits with the other
# listening switches in the Music Bot menu, and it is on until someone there
# turns it off.
OPTION_ENABLED = "cinema_live_instruments"
# The same choice as it was saved while the Music Bot menu was the only place
# to make it. Read so a preference set back then still counts.
LEGACY_OPTION = "music_bot_instruments_cinema"
DEFAULT_ENABLED = True

# How long a resolved "which room is this performer in" answer is trusted.
# The performer can walk, and a builder can place a speaker, mid-song; a
# second is the same cadence the playing room re-resolves at.
REFRESH_INTERVAL = 1.0

# A performer who has not moved further than this is not resolved again
# within the interval above: a drummer standing still costs nothing.
STILL_METRES = 1.0

# How long one "how loud is each speaker from where the listener stands"
# answer is reused. A drum roll is twenty notes a second and every note needs
# a distance, an aim and a wall ray per speaker, all of it on the main thread
# before the sample is spawned -- and that tail is exactly the latency a live
# band feels. Both the listener and the performer move at walking speed, so a
# quarter of a second cannot be heard. A listener who has walked further than
# ``TERMS_METRES``, or turned up in a different place, is measured again at
# once, so stepping behind a wall muffles the band when it happens.
TERMS_INTERVAL = 0.25
TERMS_METRES = 0.5

# How often this client may say that the room carrying a band is out of *its*
# reach. A note is played twenty times a second and the ramp edge is crossed at
# walking speed, so without a floor this would be a line every half metre.
REACH_REPORT_INTERVAL = 5.0

# Which half of a stereo sample a speaker plays: ``"l"``, ``"r"`` or None.
# A room splits a *song* between its speakers (front_l is the left channel and
# nothing else, front_r the right), and a live instrument is played at those
# same speakers -- so it is split the same way, or the kit has no left and
# right and a tom panned left arrives from every speaker of the room at once.
CHANNEL_LEFT = "l"
CHANNEL_RIGHT = "r"

# How close to a whole channel a slot's weights have to be before it counts as
# one side. Deliberately tight: only the screen-wall pair carries a whole
# channel. The sides carry L-R (the *difference*, which is what widens the
# room) and the rears lean across to the opposite channel, so those are a mix
# no single sample half could stand in for -- handing a rear-left speaker the
# right channel would be inventing an image the profile does not define.
CHANNEL_EPSILON = 0.05


def slot_channel(weights):
    """``'l'``/``'r'`` when a profile gives this slot one whole channel, else None.

    ``weights`` is the ``(left, right)`` pair the room's profile mixes into
    that speaker for the *song*. A front pair that is exactly one channel is
    the room's own definition of its stereo image, so a note belongs there --
    at the very same gain the song gets at that speaker, which is what keeps
    one speaker as loud for the band as for the song. Anything else (a centre
    speaker, a side wall, a rear pair, a mono spread) returns None and keeps
    the whole note, exactly as every speaker used to.
    """
    if not weights or len(weights) < 2:
        return None
    try:
        gain_l = float(weights[0])
        gain_r = float(weights[1])
    except (TypeError, ValueError):
        return None
    if gain_l >= 1.0 - CHANNEL_EPSILON and abs(gain_r) <= CHANNEL_EPSILON:
        return CHANNEL_LEFT
    if gain_r >= 1.0 - CHANNEL_EPSILON and abs(gain_l) <= CHANNEL_EPSILON:
        return CHANNEL_RIGHT
    return None


def plan_channels(plan):
    """``{slot: 'l'|'r'}`` for the room's own profile, empty when it has none.

    Read from the profile the room resolved rather than from the slot's name:
    the profile is the very thing that divides the song, so the band divides
    the same way by construction. An unknown profile simply splits nothing.
    """
    try:
        profile = get_profile(getattr(plan, "profile", None))
    except Exception:
        return {}
    channels = {}
    for slot, weights in getattr(profile, "weights", {}).items():
        channel = slot_channel(weights)
        if channel is not None:
            channels[slot] = channel
    return channels


def live_instruments_enabled():
    """Whether this client plays live instruments through a cabinet's rooms.

    Read live rather than cached on an object: the Music Bot menu's own line
    edits exactly this setting, and a value cached at construction would go
    stale the moment that line is used. Both instruments ask this one
    function, so a piano and a kit in the same hall can never answer
    differently about how the same listener hears them.
    """
    try:
        from ... import options
        saved = options.get(OPTION_ENABLED)
        if saved is None:
            saved = options.get(LEGACY_OPTION)
        if saved is None:
            return DEFAULT_ENABLED
        return bool(saved)
    except Exception:
        # A missing settings back end must never stop a note from playing: the
        # default is what a player would have had anyway.
        return DEFAULT_ENABLED


# Whether the switch's state has already been said out loud (None = not yet
# read, True = it is on, False = its being off has been reported). Only the
# *off* case is worth a line, and only once per time it goes off.
_gate_reported = None


def note_reaches_a_room(game=None):
    """Whether a live note is played at a cabinet's speakers on this client.

    Everything that plays a note asks this rather than the raw options, because
    the one outcome that silences a band with no other trace - this listener's
    own switch being off - would otherwise look exactly like a room that would
    not resolve: everyone else hears the band out of the room, this client
    hears the plain instrument, and nothing anywhere says why.

    **This switch is the whole answer, and no staff pan overrides it** (see
    ``pan.py``): a pan chooses *which* room a band belongs to, never whether
    *you* hear one. It is the band's own line and nothing else -- ``Cinema
    rooms:`` belongs to the jukebox's songs and ``Speech:`` to voices, so
    turning one of those off never silently takes the band with it (one
    listening choice per shape of sound is the whole model a player is asked to
    keep). ``game`` is taken for the callers that have one to hand; nothing in
    here needs a map.

    The option is read live (the Music Bot menu's line edits it); only a
    *change* of reason is reported, so a band of four costs one line.
    """
    global _gate_reported
    if not live_instruments_enabled():
        reason = "this client's Instruments switch is off"
    else:
        _gate_reported = None
        return True
    if _gate_reported != reason:
        _gate_reported = reason
        log_line(f"[Cinema] live instruments: {reason}")
    return False


def set_live_instruments(enabled):
    """Record the listener's choice; the menu line and the instruments share it.

    One place owns the key so the menu cannot save under a name the playback
    path does not read (the two drifting apart is silent: the line looks like
    it works and nothing changes).
    """
    try:
        from ... import options
        options.set(OPTION_ENABLED, bool(enabled))
    except Exception:
        return False
    return True


def _distance(first, second):
    return sqrt(sum((float(first[i]) - float(second[i])) ** 2 for i in range(3)))


def _as_point(position):
    """A 3-tuple of floats, or None when there is no point to measure from."""
    if position is None:
        return None
    try:
        return (float(position[0]), float(position[1]), float(position[2]))
    except Exception:
        return None


def _same_point(first, second):
    """Whether two points are the same place, with "no point" its own place."""
    if first is None or second is None:
        return first is None and second is None
    return _distance(first, second) < 0.01


def plan_reach(plan, fallback=None):
    """The reach of a resolved room, or the module default when it has none.

    A room carries its own number (``RoomPlan.reach``: the cabinet's
    ``cinema_radius``, or the room's default), and everything that has to know
    how far this room reaches asks it here -- membership, audibility and the
    distance a performer has to stand within to be "in" it are one number, so
    they are read from one place.
    """
    default = float(ROOM_MAX_DISTANCE if fallback is None else fallback)
    try:
        value = float(getattr(plan, "reach", None))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def inside_room(plan, position, *, radius=None):
    """Whether a point stands inside a resolved room's own reach.

    The measurement the room hands its speakers out by -- the cabinet's reach
    around it is where a speaker may stand to belong to the room -- asked
    about a person instead: a room is somewhere somebody can be *in*, and the
    cabinet nearest a performer across the map is not their room. The voice
    path has always required this (a talker out in the map keeps the PA); the
    live-instrument path requires it too now, because its answer is no longer
    taken per listener, and "the nearest cabinet is mine" would otherwise send
    a band playing at the far end of a map into a room nobody can hear.

    A plan with no anchor answers True: there is nothing to measure against,
    and refusing would silence a room that resolves fine everywhere else.
    """
    anchor = getattr(getattr(plan, "placement", None), "anchor", None)
    if anchor is None or position is None:
        return True
    try:
        gap = _distance(anchor, position)
    except Exception:
        return True
    return gap <= plan_reach(plan, radius)


def _provider_key(provider):
    """A stable name for a wall provider: a bound method is a new object
    every time it is read off its owner, so the owner and the function are
    what identify it (a cached answer must never be served for a different
    provider -- that is a test's fake provider, or another map's walls)."""
    if provider is None:
        return None
    owner = getattr(provider, "__self__", None)
    if owner is not None:
        return (id(owner), getattr(provider, "__func__", None))
    return provider


def cabinet_mode(game, cabinet_id):
    """The map's own decision for a cabinet (``auto`` when nothing says)."""
    gameplay = getattr(game, "gameplay", None)
    player = getattr(gameplay, "jukebox_player", None)
    getter = getattr(player, "cinema_mode", None)
    if not callable(getter):
        return CINEMA_AUTO
    try:
        return getter(cabinet_id)
    except Exception:
        return CINEMA_AUTO


class LiveRoomRouter:
    """Which room a live performance comes out of, and how loud at each speaker.

    ``route_for`` answers "the room nearest this point"; ``speaker_terms``
    turns that into one term per speaker. Both are cheap and both are cached,
    because a drummer can call this twenty times a second.
    """

    def __init__(self, game=None, *, interval=REFRESH_INTERVAL):
        self.game = game
        self.interval = float(interval)
        self._cache = None    # (stamp, point, result)
        # (stamp, performer point, listener point, wall provider, the room's
        # two distances, terms) for the notes of one burst -- TERMS_INTERVAL.
        self._terms = None
        # The last answer already said out loud, so a note every few
        # milliseconds does not repeat it: this is the only place a listener
        # can find out which room their instruments are (or are not) coming
        # out of, because the performer's menu only describes the performer.
        self._reported = None
        # The last answer about whether the room carrying a band is audible
        # *here*: a listener the room does not reach hears nothing of a note
        # that belongs to it, and that silence is worth one line (see
        # ``report_reach``).
        self._reach = None

    def route_for(self, position):
        """``(cabinet_id, plan)`` for the room ``position`` stands in, or None.

        A cabinet the map set to ``off`` is not a target: the map's decision
        about that cabinet outranks anything a performer does, exactly as it
        does for playback, and a cabinet with no speakers around it resolves to
        no room at all instead of a synthetic ring nobody placed. Neither is a
        cabinet further away than the room's own reach (``inside_room``):
        somebody out in the map is not in the nearest room to them, and the
        answer must not depend on who is listening.
        """
        if position is None or self.game is None:
            return None
        point = (float(position[0]), float(position[1]), float(position[2]))
        now = time.monotonic()
        cached = self._cache
        if cached is not None:
            stamp, last_point, result = cached
            if now - stamp < self.interval and _distance(last_point, point) < STILL_METRES:
                return result
        result = self._resolve(point)
        self._announce(result, point)
        self._cache = (now, point, result)
        return result

    def _announce(self, result, point):
        """Say which room live notes come out of, once per change (see __init__)."""
        if result is not None:
            cabinet_id, plan = result
            said = reason = f"-> jukebox {cabinet_id} ({plan.profile})"
        else:
            reason = f": not in a room ({self.why_not(point)})"
            said = reason
        if said == self._reported:
            return
        self._reported = said
        log_line(f"[Cinema] live instruments {reason}")

    def report_reach(self, cabinet_id, terms):
        """Say it when a room carries a band and this listener hears none of it.

        A note that belongs to a room is not played at the instrument any more
        (``note_goes_to_a_room``), so a listener the room's own ramp does not
        reach hears *nothing* of it -- and silence is the one outcome of a room
        that leaves no trace of its own. This is that trace, said on the client
        that is deaf to it: the performer's menu and log only ever describe the
        performer's client, so nobody else can report it.

        Said once per change of state, and no more often than
        ``REACH_REPORT_INTERVAL``: the ramp edge is crossed at walking speed and
        a note arrives twenty times a second, so without a floor this would be
        a line every half metre. Only the deaf state is floored -- hearing the
        band again is recorded at once (it needs no announcement) so that
        walking back out of the room is reported instead of being swallowed.
        """
        now = time.monotonic()
        state = (str(cabinet_id), bool(terms))
        last = self._reach
        if last is not None and last[0] == state:
            return
        if terms:
            self._reach = (state, now)
            return
        if last is not None and now - last[1] < REACH_REPORT_INTERVAL:
            return          # said recently: the next note tries again
        self._reach = (state, now)
        log_line(f"[Cinema] live instruments -> jukebox {cabinet_id}: out "
                 f"of reach here (this listener hears nothing of it)")

    def _resolve(self, point):
        best = None
        for cabinet_id, anchor in cabinet_anchors(self.game):
            gap = _distance(anchor, point)
            if best is None or gap < best[2]:
                best = (cabinet_id, anchor, gap)
        if best is None:
            return None
        cabinet_id, anchor, gap = best
        if cabinet_mode(self.game, cabinet_id) == CINEMA_OFF:
            return None
        # The same reach the room takes its speakers in at: a performer 80 m
        # from a cabinet whose room reaches 90 m is inside it, and one 80 m
        # from a cabinet that reaches 20 m is not -- so "the room I am
        # standing in" follows the cabinet's own size instead of the default.
        if gap > cabinet_reach(self.game, cabinet_id):
            return None
        plan = preview_room(self.game, anchor, room_id=cabinet_id)
        if plan is None:
            return None
        return (cabinet_id, plan)

    def room_by_id(self, cabinet_id):
        """``(cabinet_id, plan)`` for the cabinet with this id, or None.

        The same resolution ``route_for`` does, asked about one named cabinet
        instead of the one nearest a point -- a staff pan names its destination
        (see ``pan.py``), and the two questions have to agree: a cabinet the
        map set to ``off``, or one that is not on this map at all, resolves to
        nothing here too, so a stale pan puts the voice back on the map's PA
        rather than into a room nobody placed.
        """
        key = str(cabinet_id or "").strip()
        if not key or self.game is None:
            return None
        for found_id, anchor in cabinet_anchors(self.game):
            if str(found_id) != key:
                continue
            if cabinet_mode(self.game, found_id) == CINEMA_OFF:
                return None
            plan = preview_room(self.game, anchor, room_id=found_id)
            if plan is None:
                return None
            return (found_id, plan)
        return None

    def speaker_terms(self, position, listener=None, occlusion_provider=None,
                      reference_distance=ROOM_REFERENCE_DISTANCE,
                      max_distance=None,
                      direction=DEFAULT_DIRECTION):
        """One term per speaker of the room near ``position``.

        Each term is ``(slot, world position, gain, delay_ms, wall tier,
        channel, tone, crossover)``. ``gain`` folds in the map's own level for
        that speaker, the room's distance ramp at the listener and the
        speaker's aim; the tier is the same wall measurement the room applies
        to the song (0 clear, 1 light, 2 heavy) so a note behind a wall is
        muffled rather than silenced; ``channel`` is the half of a stereo
        sample that speaker carries (``'l'``/``'r'``), or None when it plays
        the whole note; ``tone`` is the map's own voicing for that speaker
        (1.0 = as it was placed) so a note comes out of a dulled speaker as
        dull as the song does; and ``crossover`` is that speaker's signed
        crossover mark (``FULL_RANGE`` for the speakers every map had before
        it), so a bass cabinet plays the band's notes as bass and a tweeter
        plays only their top -- the last shape of sound the room carries that
        used to come out of a crossed speaker whole. An empty list means
        "nothing to play into", never an error.
        """
        target = self.route_for(position)
        if target is None:
            return []
        if max_distance is None:
            # The room's own reach, never the module constant: a note is heard
            # exactly as far as the room carrying it (see ``RoomPlan.reach``).
            max_distance = plan_reach(target[1])
        point = _as_point(position)
        heard = _as_point(listener)
        provider = _provider_key(occlusion_provider)
        shape = (reference_distance, max_distance, direction)
        now = time.monotonic()
        cached = self._terms
        if cached is not None:
            stamp, last_point, last_heard, last_provider, last_shape, terms = cached
            if (now - stamp < TERMS_INTERVAL
                    and _distance(last_point, point) < TERMS_METRES
                    and _same_point(last_heard, heard)
                    and last_provider == provider
                    and last_shape == shape):
                return list(terms)
        terms = self.terms_for_plan(target[1], listener, occlusion_provider,
                                   reference_distance, max_distance,
                                   direction=direction)
        self._terms = (now, point, heard, provider, shape, terms)
        return list(terms)

    def terms_for_plan(self, plan, listener=None, occlusion_provider=None,
                       reference_distance=ROOM_REFERENCE_DISTANCE,
                       max_distance=None,
                       direction=DEFAULT_DIRECTION):
        """The same terms, for a caller that already resolved the room.

        A legacy of the split: the note path resolves per strike, but a voice
        is a *stream* that owns the speakers it is playing into, so it has to
        shape itself against the plan it is following rather than re-answer
        "which room is this" fifty times a second. Same numbers either way, so
        one speaker is exactly as loud for speech as it is for the band.

        Without an explicit ``max_distance`` the room's own reach is used, so
        the song, the band and a voice are one room rather than three.
        """
        if max_distance is None:
            max_distance = plan_reach(plan)
        placement = getattr(plan, "placement", None)
        channels = plan_channels(plan)
        terms = []
        for slot in getattr(placement, "slots", ()):
            spec = placement.speakers[slot].spec
            spot = spec.position
            gain = (float(spec.level)
                    * distance_gain(spot, listener, reference_distance, max_distance)
                    * speaker_aim_gain(listener, spot, spec))
            if gain <= 0.0:
                continue
            tier = self._wall_tier(spot, listener, occlusion_provider, max_distance)
            terms.append((slot, spot, gain, float(spec.delay_ms), tier,
                          channels.get(slot), float(getattr(spec, "tone", 1.0)),
                          mark_of(spec)))
        # A staff pan leans the room towards a side of itself, at the same
        # overall energy: the direction law is pure arithmetic and lives in
        # ``pan.py``, and ``auto`` (the default) leaves these terms untouched.
        if direction != DEFAULT_DIRECTION:
            terms = list(apply_direction(terms, direction))
        return terms

    @staticmethod
    def _wall_tier(spot, listener, provider, max_distance):
        """The room's own wall measurement for one speaker, 0 when unknown."""
        if not callable(provider) or listener is None:
            return 0
        try:
            return int(provider(spot, listener, max_distance))
        except Exception:
            return 0

    def why_not(self, position):
        """One line naming what stops a note reaching a room: menus and logs."""
        if self.game is None:
            return "no map to play into"
        best = None
        for cabinet_id, anchor in cabinet_anchors(self.game):
            gap = _distance(anchor, position)
            if best is None or gap < best[2]:
                best = (cabinet_id, anchor, gap)
        if best is None:
            return "this map has no jukebox"
        cabinet_id, anchor, gap = best
        if cabinet_mode(self.game, cabinet_id) == CINEMA_OFF:
            return f"jukebox {cabinet_id} is set to play its own stereo (off)"
        reach = cabinet_reach(self.game, cabinet_id)
        if gap > reach:
            return (f"jukebox {cabinet_id} is {gap:.0f} m away "
                    f"(its room reaches {reach:.0f} m)")
        return room_diagnosis(self.game, anchor, room_id=cabinet_id)


def router_for(audio):
    """The router that lives with this audio manager, built on first use.

    One per client, next to the audio it plays into, so it disappears with the
    audio manager on a relogin instead of holding a dead map. The game is
    filled in by the first note that arrives (the manager itself has no back
    reference to it).
    """
    router = getattr(audio, "cinema_live", None)
    if router is None:
        router = LiveRoomRouter()
        try:
            audio.cinema_live = router
        except Exception:
            pass
    return router


def wall_filter(owner, tier, tone=None):
    """The room's wall -- and the speaker's own voicing -- as one filter.

    ``tier`` is the same measurement the room applies to the song -- one tile
    of wall is a light lowpass, three or more is the heavy one -- so a note
    played behind a wall sounds the way the song does behind that same wall.
    ``owner`` is whatever holds the filters (the piano or drum audio), so both
    instruments muffled by the same wall get the same filter object.

    ``tone`` is the map's own voicing for the speaker this copy is going to
    (the seventh field of every term). A speaker holds one direct filter, so a
    dulled speaker and a wall in the way are composed by
    ``listener.speaker_filter`` rather than stacked -- and a caller that never
    learned about voicing (an older instrument, a test's own ``play_one``)
    keeps exactly the wall it always got.
    """
    openness = tone_openness(tone)
    if openness is not None and openness < TONE_NEUTRAL:
        getter = getattr(owner, "room_tone_filter", None)
        if callable(getter):
            try:
                return getter(tier, openness)
            except Exception:
                return None
        return None
    try:
        if tier >= 2:
            return owner.get_occlusion_filter()
        if tier == 1:
            return owner.get_light_occlusion_filter()
    except Exception:
        return None
    return None


def _router_for(game):
    """The router this client plays through, with its game filled in.

    ``router_for`` keys one router per audio manager, and the manager holds no
    back reference to the game: the first note that arrives tells it which map
    it is standing on.
    """
    router = router_for(getattr(game, "audio_mngr", None))
    if router.game is None:
        router.game = game
    return router


def room_for(game, position, *, pan=None):
    """``(cabinet_id, plan)`` for the room a live note belongs to, or None.

    The answer every machine gives for one performer, because nothing here
    measures a listener: a staff pan names its cabinet (``room_by_id``) and
    with no pan the room is the one the performer is standing *in*
    (``route_for``, which applies the room's own reach -- see ``inside_room``).
    That equality is the point -- it is what makes "no pan" mean one thing on
    every client, and what lets a listener be told *where* a band belongs
    without the answer depending on where that listener is standing.

    A destination that does not resolve (a cabinet the map turned to ``off``,
    a speaker set with no stereo front pair left) answers None here too, so a
    caller falls back to the instrument rather than into an invented room.
    """
    if position is None or game is None:
        return None
    if getattr(game, "gameplay", None) is None:
        return None
    router = _router_for(game)
    if pan is None:
        return router.route_for(position)
    try:
        cabinet = pan[0]
    except (TypeError, IndexError):
        return None
    return router.room_by_id(cabinet)


def room_runtime(game, position, *, pan=None, cabinet=None):
    """The bank this note's room is *playing through* on this machine, or None.

    ``room_for`` is the room a note belongs to, and it is the same answer on
    every client because nothing there measures a listener. Whether a song is
    playing in that room -- and therefore whether there is a beat to be on --
    is this machine's own question: the bank is that song's own output, so its
    frame queue is the clock a note has to land on (``wait_advance``) and its
    speakers are where the note's copies come out. None means nothing is
    playing in that room *here*, and the caller keeps the arrival-time path
    rather than holding a note for a song nobody on this machine can hear.

    The song asked for is the map cabinet's own jukebox song, never a Music Bot
    feed: a bot's song is one listener's private one (a different song on every
    machine), so it can never be the beat two players share.
    """
    if cabinet is None:
        room = room_for(game, position, pan=pan)
        if room is None:
            return None
        cabinet = room[0]
    players = getattr(getattr(game, "gameplay", None), "jukebox_player", None)
    entry = (getattr(players, "players", None) or {}).get(cabinet)
    bank = entry.get("cinema") if isinstance(entry, dict) else None
    if bank is None or not getattr(bank, "sources", None):
        return None
    return bank


def room_schedule(game, position, *, pan=None):
    """The room's own clock as a ``schedule(ms, fire)`` callable, or None.

    What a speaker carrying a delay trim waits out is *the room's* time, not
    the wall clock: the same clock the song's own trim is measured on, so a
    trimmed speaker's note lands with that speaker's song instead of with a
    frame boundary of this machine's game loop. The room's own spawn cost
    rides along (``tail_ms``), for the same reason the note's own wait spends
    it: being audible *at* the instant is what is being asked for.

    A room that is not playing here has no clock to offer, and the caller keeps
    ``game.call_after`` -- which is also every map without a room.
    """
    bank = room_runtime(game, position, pan=pan)
    if bank is None:
        return None
    tail = bank.note_spawn_ms()

    def schedule(ms, fire):
        return bank.wait_advance(ms, fire, tail_ms=tail)

    return schedule


def room_terms_for(game, position, *, pan=None, listener=None,
                   occlusion_provider=None):
    """The speakers a live note plays at through a room, or ``()`` for none.

    The one answer both instruments and :func:`route_to_room` work from, and
    the *listener's* half of the question: ``room_for`` decides which room the
    note belongs to, and this says how loud each of that room's speakers is at
    these ears. ``terms_for_plan`` already drops a speaker that does not reach
    this listener at all (its gain is zero), so an empty tuple is the honest
    "that room is out of reach here" -- the note is then heard by nobody on
    this client rather than at the instrument (``note_goes_to_a_room``).
    """
    if position is None or game is None:
        return ()
    if getattr(game, "gameplay", None) is None:
        return ()
    position = tuple(float(value) for value in position)
    router = _router_for(game)
    if listener is None:
        listener = getattr(getattr(game, "audio_mngr", None), "position", None)
    if pan is None:
        return tuple(router.speaker_terms(position, listener,
                                          occlusion_provider))
    cabinet, direction = (pan[0], pan[1] if len(pan) > 1
                          else DEFAULT_DIRECTION)
    room = router.room_by_id(cabinet)
    if room is None:
        return ()
    return tuple(router.terms_for_plan(room[1], listener, occlusion_provider,
                                      direction=direction))


def note_goes_to_a_room(game, position, *, pan=None):
    """Whether a live note is heard from a room on this client at all.

    Asked *before* the note is played, because the answer is what decides
    whether the instrument itself makes a sound: a note coming out of a hall's
    speakers must not also be heard at the instrument standing in that hall,
    which is two copies of one note -- one of them in the wrong place.

    This client's own switches come first and are the whole answer
    (``note_reaches_a_room``): a pan names *which* room a band belongs to, never
    whether *you* hear one, so a listener who turned the rooms off hears the
    band where they asked for it -- at the instrument. What the answer does
    **not** depend on is where this listener is standing: the room carrying a
    band is the performer's room, decided identically on every client
    (``room_for``), so "no pan" and "pan cleared" are one answer everywhere
    instead of one per pair of ears. How much of that room is audible here is
    the room's own ramp (``room_terms_for``): a listener it does not reach
    hears *nothing* of the note -- the room replaces the instrument, it does
    not join it -- and that silence is reported by ``report_reach`` so it
    cannot be mistaken for a broken instrument.
    """
    if not note_reaches_a_room(game):
        return False
    return room_for(game, position, pan=pan) is not None


def _report_spawn_cost(game, position, pan, cost_ms):
    """Tell the room what one of its own notes cost to reach a speaker (ms)."""
    bank = room_runtime(game, position, pan=pan)
    if bank is None:
        return
    try:
        bank.note_spawn_report(cost_ms)
    except Exception:
        pass


def _report_reach(game, position, pan, terms):
    """Hand one note's outcome to the router, for the "out of reach" line.

    Only when a room actually owns the note: with no room to play into, the
    instrument was heard instead and there is nothing to report. The cabinet is
    the pan's own name, or the room ``route_for`` already cached for a
    performer nobody moved, so this costs no resolution of its own.
    """
    if game is None:
        return
    room = room_for(game, position, pan=pan)
    if room is None:
        return
    _router_for(game).report_reach(room[0], terms)


def zone_reverb(game, position):
    """The map's reverb at a point, or None when there is none to send.

    The note's own room, not the listener's: a venue copy without it was heard
    drier than the instrument standing in the same zone used to be.
    """
    gameplay = getattr(game, "gameplay", None)
    getter = getattr(getattr(gameplay, "map", None), "get_reverb_at", None)
    if position is None or not callable(getter):
        return None
    try:
        entry = getter(position[0], position[1], position[2])
    except Exception:
        return None
    return getattr(entry, "reverb", None) or None


def route_to_room(game, position, play_one, *, listener=None,
                  occlusion_provider=None, schedule=None, wanted=None,
                  pan=None):
    """Play one live note at every speaker of the room nearest ``position``.

    ``play_one(x, y, z, gain, tier, delay_ms, channel)`` is the caller's own
    "spawn this sample here": each instrument keeps its own buffers, volume
    rule and tracking, and this decides *where*, *how loud* and *which half of
    the sample*. ``channel`` is ``'l'``/``'r'`` at the speakers the room's
    profile gives a whole channel to (its screen-wall pair) and None
    everywhere else, so a tom the kit pans left comes out of the left speaker
    the way the song's left channel does -- an instrument is only ever handed
    a half the room itself plays. A speaker with a crossover is handed one
    field more, the mark itself (``crossed_samples`` makes that speaker's own
    copy of the note); the eighth field, the speaker's voicing, is sent
    whenever either of the two means something, since an instrument that has
    learned the ninth has learned the eighth. ``schedule(ms, fn)`` is the
    game's main-thread timer, used only for a speaker carrying a trim --
    without it a trimmed speaker would strike with the room's other speakers
    and then be late against its own song.

    ``wanted()`` is asked immediately before each speaker plays, and it exists
    because of exactly that timer: a copy that waits for its trim can outlive
    the note it belongs to. A key released (or a hat choked) inside those few
    milliseconds would then have nothing left to stop its room copy, and the
    speaker would ring on with no note under it -- the copy has to know it is
    no longer wanted rather than trust that the note is still there.

    ``pan`` is ``(cabinet, direction)`` when staff moved this performer (see
    ``pan.py``). It replaces *where the note comes out* -- the named cabinet
    rather than the room nearest the performer -- and leans that room towards
    the direction, so the performer's position stops deciding anything. A
    destination that no longer resolves (a deleted speaker set, a cabinet the
    map turned to ``off``) plays the note nowhere instead of inventing a room.

    Returns the number of speakers the note reached, so a caller can log or
    test "did anything come out" without guessing. Zero is a real answer in
    two different situations -- a room with no terms for these ears, and a room
    that does not resolve -- and only the first one is silence where a band was
    expected, so that one says so once (``report_reach``).
    """
    if position is None or game is None:
        return 0
    if getattr(game, "gameplay", None) is None:
        return 0
    terms = room_terms_for(game, position, pan=pan, listener=listener,
                           occlusion_provider=occlusion_provider)
    _report_reach(game, position, pan, terms)
    spoken = 0
    # What this room's notes cost to sound *here*, measured on the way out:
    # the note is audible as soon as its first speaker starts, so the first
    # inline copy is the one that answers "how late is a note on this
    # machine", and it is the number the scheduler spends before the beat
    # (``bank.note_spawn_ms``) instead of guessing one value for every machine.
    started = time.perf_counter()
    measured = False
    for slot, spot, gain, delay_ms, tier, *rest in terms:
        channel = rest[0] if rest else None
        tone = rest[1] if len(rest) > 1 else None
        crossed = rest[2] if len(rest) > 2 else FULL_RANGE
        def _spawn(spot=spot, gain=gain, tier=tier, slot=slot, delay_ms=delay_ms,
                   channel=channel, tone=tone, crossed=crossed):
            if wanted is not None:
                try:
                    if not wanted():
                        return
                except Exception:
                    return
            try:
                openness = tone_openness(tone)
                dulled = openness is not None and openness < TONE_NEUTRAL
                where = (spot[0], spot[1], spot[2], gain, tier, delay_ms, channel)
                if crossed != FULL_RANGE:
                    # The ninth field is this speaker's own crossover: a note
                    # played at a bass cabinet is that cabinet's copy of the
                    # note, and at a tweeter only its top. The speaker's
                    # voicing travels with it (None when the map set none),
                    # because the two are decided by the same element and an
                    # instrument reading one of them reads both.
                    play_one(*where, tone, crossed)
                elif dulled:
                    # The eighth field is the speaker's own voicing, and it is
                    # only ever sent when the map actually set one, so a caller
                    # that has not learned about tone is never handed it.
                    play_one(*where, tone)
                else:
                    # A speaker the map never dulled or crossed: the
                    # seven-field call an instrument has always received.
                    play_one(*where)
            except Exception:
                return
        if delay_ms > 0.5 and callable(schedule):
            schedule(delay_ms, _spawn)
        else:
            _spawn()
            if not measured:
                measured = True
                _report_spawn_cost(game, position, pan,
                                   (time.perf_counter() - started) * 1000.0)
        spoken += 1
    return spoken
