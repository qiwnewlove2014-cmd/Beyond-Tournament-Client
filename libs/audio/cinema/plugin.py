"""The plugin seam between a playing source and the room's speakers.

This module is the only part of the Cinema Speaker System the Jukebox ever
touches, and it is deliberately tiny: ask for a renderer, hand over PCM
frames, give the renderer back when the song ends. Everything else -- the
queue, the relay, the direct ffmpeg stream, the timeline alignment, the
recovery watches -- keeps working exactly as it does today.

What a cabinet plays through is **the map's decision**, not a player's: the
jukebox element carries a ``cinema_mode`` (``auto``, ``off``, or a profile
id) and the server sends it with every play. ``auto`` uses the speakers a
builder placed around that cabinet if there are any, so a cabinet with no
room around it -- every cabinet on every map that shipped before this feature
-- is still the plain two-source jukebox, byte for byte. ``off`` refuses the
room even when one stands around the cabinet, and a profile id names the
shape outright.

The player option (:data:`OPTION_ENABLED`) is only an override on top of
that: it is how somebody who prefers the plain jukebox turns rooms off for
themselves. With it off, :func:`acquire_renderer` returns None and the caller
runs its original two-source code path untouched.
"""

from .bank import DEFAULT_CATEGORY, CinemaSpeakerBank
from .channel import AUTO
from .listener import ListenerPose, facing_report
from .layout import ROOM_MAX_DISTANCE, ROOM_REFERENCE_DISTANCE
from .placement import (AUTO_PROFILE, ROOM_RADIUS, RoomPlan, exclusive_speakers,
                        resolve_room, room_profile)
from .profiles import DEFAULT_PROFILE, get_profile
from .router import CinemaRenderer

# Player preference. The per-cabinet decision is made by the map (the
# jukebox element's cinema mode), so this is only a local override for the
# players who want the plain jukebox back everywhere.
OPTION_ENABLED = "cinema_speakers"

# Attribute the host attaches itself to on the game object.
HOST_ATTRIBUTE = "cinema_speakers"

# On by default: a cabinet that nobody gave a room to is unaffected either
# way, so the default only decides whether a room a builder *did* place is
# heard. Somebody who wants the plain jukebox everywhere can still say so.
DEFAULT_ENABLED = True

# The two reserved values of a cabinet's cinema mode; anything else is a
# profile id (see ``profiles.py``).
CINEMA_OFF = "off"
CINEMA_AUTO = AUTO_PROFILE


def _option_enabled():
    try:
        from ... import options
        return bool(options.get(OPTION_ENABLED, DEFAULT_ENABLED))
    except Exception:
        # A missing settings back end must never stop the game from starting;
        # staying in the pre-cinema behaviour is always the safe answer.
        return DEFAULT_ENABLED


def rooms_enabled():
    """Whether this listener hears a jukebox through the room around it.

    The value :func:`acquire_renderer` itself acts on, asked rather than
    copied, so the menu line that switches it and the playback path can never
    disagree about it. Off is the plain two-source jukebox, which is why the
    switch sits next to the other listening choices instead of being buried.
    """
    return _option_enabled()


def set_rooms_enabled(game, enabled):
    """Record the listener's choice and apply it to a room already playing.

    A room is released when this goes off, because the switch exists to be
    heard: leaving a song playing into a room until the next track would make
    the line look broken for as long as anyone is listening. A game that never
    used cinema is left with no host at all, exactly as before.
    """
    enabled = bool(enabled)
    try:
        from ... import options
        options.set(OPTION_ENABLED, enabled)
    except Exception:
        pass
    host = host_for(game, create=False)
    if host is not None:
        host.set_enabled(enabled)
    return enabled


class CinemaSpeakerHost:
    """One renderer per playing jukebox, keyed by jukebox id."""

    def __init__(self, game=None, *, enabled=None, profile=None, max_speakers=None,
                 room_radius=None):
        self.game = game
        self._enabled = bool(_option_enabled() if enabled is None else enabled)
        self.profile = get_profile(profile or DEFAULT_PROFILE).name
        self.max_speakers = max_speakers
        self.room_radius = float(ROOM_RADIUS if room_radius is None else room_radius)
        self._renderers = {}
        self._banks = {}
        # The resolved room per cabinet, kept for the debug overlay and for
        # tests: "which speaker ended up where, and what did the map get wrong".
        self._placements = {}

    @property
    def enabled(self):
        return self._enabled

    def set_enabled(self, enabled):
        """Turn the whole feature on or off; off also releases every room."""
        enabled = bool(enabled)
        if not enabled:
            self.release_all()
        self._enabled = enabled
        return self._enabled

    @staticmethod
    def _key(jukebox_id):
        return str(jukebox_id)

    def acquire(self, jukebox_id, anchor, **options):
        """Return the renderer for this cabinet, or None when cinema is off.

        A re-offered play event for the same song (a map reload) keeps the
        existing renderer, and a reload that changed the room *re-shapes it in
        place*: the bank's slots are rebuilt against the new renderer so
        whatever is streaming into it keeps playing, a speaker the builder
        just placed joins on the current beat, and a speaker they removed goes
        away -- all without the song being stopped and restarted. A room that
        only moved the cabinet keeps the channel verdict it already reached
        (re-analysing from scratch would flip a mono stream back to stereo for
        a second or two and feed two speakers the same content meanwhile).
        """
        if not self._enabled:
            return None
        key = self._key(jukebox_id)
        placement = options.pop("placement", None)
        requested = options.pop("profile", None)
        if placement is not None:
            requested = room_profile(placement, requested)
        profile_name = get_profile(requested or self.profile).name
        anchor = tuple(float(value) for value in anchor)
        layout = options.pop("layout", None)
        specs = options.pop("specs", None)
        if layout is None and options.pop("fill", None) is False and specs:
            # A room read off the map is only the speakers the map has: fill
            # nothing in from the geometric ring.
            from .layout import CinemaLayout
            layout = CinemaLayout(anchor, specs, use_ring=False)
            specs = None
        if layout is not None:
            specs = None
        renderer = CinemaRenderer(
            anchor,
            profile_name,
            layout,
            specs=specs,
            max_speakers=self.max_speakers,
            detect_channels=options.pop("detect_channels", True),
            declared_layout=options.pop("declared_layout", AUTO),
        )
        current = self._renderers.get(key)
        if current is not None and current.signature == renderer.signature:
            return current
        bank = self._banks.get(key)
        # A room that is retiring is not re-shaped: its sources are about to
        # be deleted by whoever retires it, and reshaping it first would move
        # speakers between a room nobody feeds and the room replacing it.
        if (bank is not None and current is not None and bank.sources
                and not bank._stopped and not bank.spent):
            if bank.reconfigure(renderer):
                renderer.channel = current.channel
                if placement is not None:
                    self._placements[key] = placement
                self._renderers[key] = renderer
                return renderer
            # The room could not be re-shaped (no device, no buffers): keep
            # describing and playing the room that is actually audible.
            self._renderers[key] = bank.renderer
            return bank.renderer
        if current is not None:
            renderer.channel = current.channel
        if placement is not None:
            self._placements[key] = placement
        self._renderers[key] = renderer
        return renderer

    def plan_for(self, jukebox_id, anchor, requested=None, room_id=None):
        """Resolve this cabinet's room from the map, or None to stay plain.

        The server's own profile (an explicit ``cinema_profile``) is only a
        hint: whatever the map's speakers can actually reproduce wins, and a
        cabinet with neither a profile nor any speakers near it is not a
        cinema at all.

        ``room_id`` is the cabinet whose speakers make up the room, for
        callers that feed a cabinet under their own registry key (the music
        bot's test output) rather than claiming the cabinet's own room.
        """
        requested = "" if requested is None else str(requested).strip().lower()
        if requested == CINEMA_OFF:
            # The map said this cabinet plays its own stereo, so a room
            # standing around it is not used. This is a decision about the
            # cabinet, taken once, not a per-listener preference.
            return None
        room_id = self._key(room_id if room_id is not None else jukebox_id)
        placement = resolve_room(
            exclusive_speakers(map_speakers(self.game), anchor,
                               rivals=self._rival_anchors(room_id),
                               radius=self.room_radius),
            anchor,
            radius=self.room_radius, room=room_id,
            profile=requested or AUTO_PROFILE,
        )
        if placement is not None:
            # Only an explicitly asked-for profile may pad itself out with
            # speakers the map does not have (see RoomPlan.fill); a room read
            # off the map is exactly the speakers someone placed.
            return RoomPlan(room_profile(placement, requested), placement.specs,
                            placement,
                            fill=str(requested or "").strip().lower() not in
                                 ("", AUTO_PROFILE))
        if requested and str(requested).strip().lower() not in ("", AUTO_PROFILE):
            # A cabinet the server marked but a map with no speakers in it: a
            # geometric ring behind the cabinet is still a real room, and it
            # is what makes the feature usable before anyone places a speaker.
            return RoomPlan(get_profile(requested).name, None, None, fill=True)
        return None

    def _rival_anchors(self, jukebox_id):
        """Anchors of the other cabinets on the map, who may want the speakers."""
        return map_cabinet_anchors(self.game, exclude=jukebox_id)

    def placement(self, jukebox_id):
        """The resolved room for a cabinet, for diagnostics."""
        return self._placements.get(self._key(jukebox_id))

    def warnings(self, jukebox_id):
        placement = self.placement(jukebox_id)
        return placement.warnings if placement is not None else ()

    def describe(self, jukebox_id, audio=None):
        """A one-line reading of this room and the listener's place in it.

        "Which speakers are in front of me, which are behind me, and where is
        the image" is the question a person debugging a room actually asks;
        this is the answer, and it is also how the turn-around behaviour is
        checked without an audio device (see ListenerPose).
        """
        renderer = self._renderers.get(self._key(jukebox_id))
        if renderer is None:
            return "no cinema room"
        placement = self.placement(jukebox_id)
        if placement is None:
            return f"{renderer.profile.name} (ring layout, not from the map)"
        audio = audio if audio is not None else getattr(self.game, "audio_mngr", None)
        pose = ListenerPose.from_audio_manager(audio)
        parts = [placement.summary()]
        for slot, quadrant, in_front, distance in facing_report(pose, placement):
            if distance is None:
                parts.append(f"{slot} {quadrant}")
                continue
            side = "ahead" if in_front else "behind"
            parts.append(f"{slot} {side}/{quadrant} {distance:.1f}m")
        return " | ".join(parts)

    def acquire_bank(self, jukebox_id, anchor, **options):
        """Acquire the OpenAL speaker bank for this cabinet, or None when off.

        Re-acquiring the same room (a map reload re-offering the same song)
        keeps the existing bank and just refreshes its settings: rebuilding it
        would create new sources and interrupt audio that is already flowing.
        """
        volume = options.pop("volume", 100)
        cabinet_volume = options.pop("cabinet_volume", 100)
        # The room's own scale, not the plain pair's 8/40: a caller that wants
        # something else passes it (see ``layout.ROOM_MAX_DISTANCE``).
        reference_distance = options.pop("reference_distance", ROOM_REFERENCE_DISTANCE)
        max_distance = options.pop("max_distance", ROOM_MAX_DISTANCE)
        occlusion_provider = options.pop("occlusion_provider", None)
        reverb_slot = options.pop("reverb_slot", None)
        eq_slot = options.pop("eq_slot", None)
        category = options.pop("category", None)
        renderer = self.acquire(jukebox_id, anchor, **options)
        if renderer is None:
            return None
        key = self._key(jukebox_id)
        bank = self._banks.get(key)
        if (bank is not None and bank.renderer is renderer and bank.sources
                and not bank.spent):
            bank.set_volume(volume)
            bank.set_cabinet_volume(cabinet_volume)
            bank.set_reverb(reverb_slot)
            bank.set_eq_slot(eq_slot)
            if category:
                bank.category = str(category)
            return bank
        bank = CinemaSpeakerBank(
            self.game, renderer, volume=volume, cabinet_volume=cabinet_volume,
            reference_distance=reference_distance, max_distance=max_distance,
            occlusion_provider=occlusion_provider, reverb_slot=reverb_slot,
            eq_slot=eq_slot, category=category or DEFAULT_CATEGORY,
        )
        self._banks[key] = bank
        return bank

    def renderer(self, jukebox_id):
        return self._renderers.get(self._key(jukebox_id))

    def bank(self, jukebox_id):
        return self._banks.get(self._key(jukebox_id))

    def release(self, jukebox_id, bank=None):
        """Give a room back. ``bank`` names the room being released.

        A retired room is released half a second after its replacement has
        already claimed the key, so releasing "whatever is registered now"
        would tear down the new song's room. Naming the room being retired
        releases that one and leaves the live room alone.
        """
        key = self._key(jukebox_id)
        current = self._banks.get(key)
        if bank is not None and current is not bank:
            try:
                bank.stop()
            except Exception:
                pass
            return bank
        popped = self._banks.pop(key, None)
        if popped is not None:
            popped.stop()
        renderer = self._renderers.pop(key, None)
        if renderer is not None:
            renderer.stop()
        self._placements.pop(key, None)
        return popped if popped is not None else renderer

    def release_all(self):
        banks = list(self._banks.values())
        self._banks.clear()
        for bank in banks:
            bank.stop()
        renderers = list(self._renderers.values())
        self._renderers.clear()
        for renderer in renderers:
            renderer.stop()
        self._placements.clear()
        return len(renderers)

    @property
    def renderers(self):
        return dict(self._renderers)

    @property
    def banks(self):
        return dict(self._banks)

    def __len__(self):
        return len(self._renderers)

    def __repr__(self):
        state = "on" if self._enabled else "off"
        return f"CinemaSpeakerHost({state}, rooms={len(self._renderers)})"


def host_for(game, *, create=True):
    """The host attached to a game object, created on first use."""
    if game is None:
        return None
    host = getattr(game, HOST_ATTRIBUTE, None)
    if host is None and create:
        host = CinemaSpeakerHost(game)
        try:
            setattr(game, HOST_ATTRIBUTE, host)
        except Exception:
            return host
    return host


def set_enabled(game, enabled):
    """Toggle cinema for a game, returning the resulting state."""
    host = host_for(game)
    return None if host is None else host.set_enabled(enabled)


def _active_host(game):
    """The host when cinema is actually in use, else None.

    A host is only attached to the game object once something has turned the
    feature on, so a player who never uses cinema keeps an untouched game
    object and the caller keeps its original code path.
    """
    host = host_for(game, create=False)
    if host is not None:
        return host if host.enabled else None
    if not _option_enabled():
        return None
    return host_for(game)


def _map_of(game):
    """The map this game is playing, if there is one."""
    if game is None:
        return None
    map_ = getattr(getattr(game, "gameplay", None), "map", None)
    if map_ is None:
        map_ = getattr(game, "map", None)
    return map_


def map_speakers(game):
    """Every cinema speaker element of the map this game is playing.

    Deliberately tolerant: a dedicated server, a headless test harness or a
    game object without a map all simply have no speakers, which resolves to
    "no room" and leaves the jukebox on its plain two-source path.
    """
    map_ = _map_of(game)
    getter = getattr(map_, "get_cinema_speakers", None)
    if not callable(getter):
        return ()
    try:
        return tuple(getter())
    except Exception:
        return ()


def cabinet_anchor(game, jukebox_id):
    """Where a cabinet stands *now*, from the map, or None when it is gone.

    A builder can move a cabinet as well as its speakers, and the room has to
    follow the map the player is actually playing rather than the position the
    song started at.
    """
    map_ = _map_of(game)
    for zone in getattr(map_, "jukebox_list", ()) or ():
        if str(getattr(zone, "id", "")) != str(jukebox_id):
            continue
        center = getattr(zone, "center", None)
        if not center:
            return None
        try:
            return tuple(float(value) for value in center)
        except (TypeError, ValueError):
            return None
    return None


def cabinet_anchors(game):
    """``(cabinet_id, anchor)`` for every jukebox this map has.

    The single place that reads the map's cabinets, so a caller that needs to
    know which cabinet is *which* (rather than only where the others stand)
    takes the same view of the map as the jukebox and the menus do.
    """
    map_ = _map_of(game)
    found = []
    for zone in getattr(map_, "jukebox_list", ()) or ():
        center = getattr(zone, "center", None)
        if not center:
            continue
        try:
            anchor = tuple(float(value) for value in center)
        except (TypeError, ValueError):
            continue
        found.append((str(getattr(zone, "id", "")), anchor))
    return found


def map_cabinet_anchors(game, exclude=None):
    """Position of every jukebox on the map except ``exclude``.

    Two cabinets can easily sit inside each other's room radius, so whoever
    resolves a room needs the rivals' positions to hand each speaker to the
    cabinet it actually stands next to (see ``exclusive_speakers``).
    """
    return [anchor for cabinet_id, anchor in cabinet_anchors(game)
            if exclude is None or cabinet_id != str(exclude)]


def preview_room(game, anchor, *, room_id=None, radius=None):
    """Resolve the room the map describes, without turning the feature on.

    Used by menus: a player must be able to see which cabinets have a room
    before deciding to route audio into one, and previewing must not change
    how the jukeboxes themselves play. :func:`cinema_room` is the playing
    path and still requires the feature to be enabled.
    """
    speakers = map_speakers(game)
    if not speakers:
        return None
    radius = float(ROOM_RADIUS if radius is None else radius)
    placement = resolve_room(
        exclusive_speakers(speakers, anchor,
                           rivals=map_cabinet_anchors(game, exclude=room_id),
                           radius=radius),
        anchor, radius=radius, room=room_id, profile=AUTO_PROFILE,
    )
    if placement is None:
        return None
    return RoomPlan(placement.profile_name, placement.specs, placement)


def room_diagnosis(game, anchor, *, room_id=None, radius=None):
    """Why a cabinet has no room, in one line, for menus and logs.

    This runs the same resolver that plays the room and hands back its own
    reason, so a tester standing in front of four speakers hears which wall is
    missing instead of "no cinema speakers around it".
    """
    anchor = tuple(float(value) for value in anchor)
    radius = float(ROOM_RADIUS if radius is None else radius)
    reasons = []
    placement = resolve_room(
        exclusive_speakers(map_speakers(game), anchor,
                           rivals=map_cabinet_anchors(game, exclude=room_id),
                           radius=radius),
        anchor, radius=radius, room=room_id, profile=AUTO_PROFILE, report=reasons,
    )
    if placement is not None:
        return placement.summary()
    return "; ".join(reasons) or "the speakers here do not make a usable room"


def cinema_room(game, jukebox_id, anchor, requested=None, *, host=None, room_id=None):
    """What a cabinet should play through, or None to stay a plain jukebox.

    This is the single call the jukebox makes. It is a module function so the
    jukebox never has to know about rooms, placements, profiles or the map's
    speaker elements -- and so a caller with no host at all (feature off)
    still gets a cheap, correct "no".
    """
    host = host or _active_host(game)
    if host is None:
        return None
    return host.plan_for(jukebox_id, anchor, requested, room_id=room_id)


def acquire_renderer(game, jukebox_id, anchor, **options):
    """Single entry point for a source that wants to play through speakers."""
    host = _active_host(game)
    if host is None:
        return None
    return host.acquire(jukebox_id, anchor, **options)


def acquire_bank(game, jukebox_id, anchor, **options):
    """Single entry point for a source that wants an OpenAL room."""
    host = _active_host(game)
    if host is None:
        return None
    return host.acquire_bank(jukebox_id, anchor, **options)


def release_renderer(game, jukebox_id, bank=None):
    """Release a cabinet's room; its OpenAL sources are the caller's to delete.

    ``bank`` names the specific room being released, for a retired room that
    must go back without taking the song which replaced it down with it.
    """
    host = host_for(game, create=False)
    return None if host is None else host.release(jukebox_id, bank)


def release_all(game):
    host = host_for(game, create=False)
    return 0 if host is None else host.release_all()
