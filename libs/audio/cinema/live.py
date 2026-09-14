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

from .listener import distance_gain, speaker_aim_gain
from .plugin import (CINEMA_AUTO, CINEMA_OFF, cabinet_anchors, preview_room,
                     room_diagnosis)
from .layout import ROOM_MAX_DISTANCE, ROOM_REFERENCE_DISTANCE

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

    def route_for(self, position):
        """``(cabinet_id, plan)`` for the room nearest ``position``, or None.

        A cabinet the map set to ``off`` is not a target: the map's decision
        about that cabinet outranks anything a performer does, exactly as it
        does for playback, and a cabinet with no speakers around it resolves to
        no room at all instead of a synthetic ring nobody placed.
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
        self._cache = (now, point, result)
        return result

    def _resolve(self, point):
        best = None
        for cabinet_id, anchor in cabinet_anchors(self.game):
            gap = _distance(anchor, point)
            if best is None or gap < best[2]:
                best = (cabinet_id, anchor, gap)
        if best is None:
            return None
        cabinet_id, anchor, _gap = best
        if cabinet_mode(self.game, cabinet_id) == CINEMA_OFF:
            return None
        plan = preview_room(self.game, anchor, room_id=cabinet_id)
        if plan is None:
            return None
        return (cabinet_id, plan)

    def speaker_terms(self, position, listener=None, occlusion_provider=None,
                      reference_distance=ROOM_REFERENCE_DISTANCE,
                      max_distance=ROOM_MAX_DISTANCE):
        """One term per speaker of the room near ``position``.

        Each term is ``(slot, world position, gain, delay_ms, wall tier)``.
        ``gain`` folds in the map's own level for that speaker, the room's
        distance ramp at the listener and the speaker's aim; the tier is the
        same wall measurement the room applies to the song (0 clear, 1 light,
        2 heavy) so a note behind a wall is muffled rather than silenced.
        An empty list means "nothing to play into", never an error.
        """
        target = self.route_for(position)
        if target is None:
            return []
        _cabinet_id, plan = target
        placement = getattr(plan, "placement", None)
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
            terms.append((slot, spot, gain, float(spec.delay_ms), tier))
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
        cabinet_id, anchor, _gap = best
        if cabinet_mode(self.game, cabinet_id) == CINEMA_OFF:
            return f"jukebox {cabinet_id} is set to play its own stereo (off)"
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


def wall_filter(owner, tier):
    """The room's wall, as a filter: muffled, never silenced.

    ``tier`` is the same measurement the room applies to the song -- one tile
    of wall is a light lowpass, three or more is the heavy one -- so a note
    played behind a wall sounds the way the song does behind that same wall.
    ``owner`` is whatever holds the filters (the piano or drum audio), so both
    instruments muffled by the same wall get the same filter object.
    """
    try:
        if tier >= 2:
            return owner.get_occlusion_filter()
        if tier == 1:
            return owner.get_light_occlusion_filter()
    except Exception:
        return None
    return None


def route_to_room(game, position, play_one, *, listener=None,
                  occlusion_provider=None, schedule=None, wanted=None):
    """Play one live note at every speaker of the room nearest ``position``.

    ``play_one(x, y, z, gain, tier, delay_ms)`` is the caller's own "spawn this
    sample here": each instrument keeps its own buffers, volume rule and
    tracking, and this decides *where* and *how loud*. ``schedule(ms, fn)`` is
    the game's main-thread timer, used only for a speaker carrying a trim --
    without it a trimmed speaker would strike with the room's other speakers
    and then be late against its own song.

    ``wanted()`` is asked immediately before each speaker plays, and it exists
    because of exactly that timer: a copy that waits for its trim can outlive
    the note it belongs to. A key released (or a hat choked) inside those few
    milliseconds would then have nothing left to stop its room copy, and the
    speaker would ring on with no note under it -- the copy has to know it is
    no longer wanted rather than trust that the note is still there.

    Returns the number of speakers the note reached, so a caller can log or
    test "did anything come out" without guessing.
    """
    if position is None or game is None:
        return 0
    position = tuple(float(value) for value in position)
    gameplay = getattr(game, "gameplay", None)
    if gameplay is None:
        return 0
    router = router_for(getattr(game, "audio_mngr", None))
    if router.game is None:
        router.game = game
    if listener is None:
        listener = getattr(getattr(game, "audio_mngr", None), "position", None)
    terms = router.speaker_terms(position, listener, occlusion_provider)
    spoken = 0
    for slot, spot, gain, delay_ms, tier in terms:
        def _spawn(spot=spot, gain=gain, tier=tier, slot=slot, delay_ms=delay_ms):
            if wanted is not None:
                try:
                    if not wanted():
                        return
                except Exception:
                    return
            try:
                play_one(spot[0], spot[1], spot[2], gain, tier, delay_ms)
            except Exception:
                return
        if delay_ms > 0.5 and callable(schedule):
            schedule(delay_ms, _spawn)
        else:
            _spawn()
        spoken += 1
    return spoken
