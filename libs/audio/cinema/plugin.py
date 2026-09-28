"""The plugin seam between a playing source and the room's speakers.

The only part of the Cinema Speaker System the Jukebox touches: ask for a
renderer, hand over PCM frames, give it back when the song ends. What a cabinet
plays through is the *map's* decision, never a player's -- ``cinema_mode``
(``auto``/``off``/a profile id) rides every play event, and ``auto`` uses the
speakers around that cabinet if there are any, so every cabinet that shipped
before this feature is the plain two-source jukebox byte for byte.
``OPTION_ENABLED`` is only a listener's own override of that: with it off
:func:`acquire_renderer` returns None and the caller keeps its own code path.

Rules, measured numbers and every trap: .agents/skills/cinema_speaker_system/.
"""

from .bank import DEFAULT_CATEGORY, CinemaSpeakerBank
from .channel import AUTO
from .listener import ListenerPose, facing_report
from .layout import (MAX_ROOM_REACH, MIN_ROOM_REACH, ROOM_MAX_DISTANCE,
                     ROOM_REFERENCE_DISTANCE, coerce_spec)
from .placement import (AUTO_PROFILE, ROOM_RADIUS, RoomPlan, claims_point,
                        claims_speaker, exclusive_speakers, inside_area,
                        resolve_room, room_profile)
from .profiles import DEFAULT_PROFILE, get_profile
from .router import CinemaRenderer, renderer_for

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

    The value :func:`acquire_renderer` asks, one home so a menu line and the playback
    path can never disagree. Off is the plain two-source jukebox.
    """
    return _option_enabled()


def listening_summary():
    """One phrase for what *this client* hears out of a cabinet right now.

    The three listening switches, each said the way its own menu line says it:
    routing happens on the *listener's* machine, so a switch here and a room that
    would not resolve look alike. Read live and asked rather than copied; silent and
    ``libs``-only, so it is safe from a menu callback.
    """
    from .live import live_instruments_enabled
    from .speech import speech_enabled

    return ", ".join((
        "jukebox songs from the room" if _option_enabled()
        else "jukebox songs at your ears",
        "the band from the room" if live_instruments_enabled()
        else "the band where they stand",
        "voices from the room" if speech_enabled()
        else "voices on the map's PA",
    ))


def set_rooms_enabled(game, enabled):
    """Record the listener's choice and apply it to a room already playing.

    A room is released when this goes off, because the switch exists to be heard.
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

        A re-offered play event for the same song (a map reload) keeps the existing
        renderer, and a reload that changed the room re-shapes it in place -- slots
        rebuilt against the new renderer so the stream keeps playing, a speaker placed
        on the current beat, one removed gone, and the channel verdict kept (re-analysing
        would flip a mono stream back to stereo for a second).
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
        # The room is built by the one factory the read-outs use too, so a
        # menu can never describe a room this would not play: ``fill`` is the
        # difference between a room read off the map (only the speakers the
        # map has) and a requested shape (padded from the ring).
        layout = options.pop("layout", None)
        specs = options.pop("specs", None)
        renderer = renderer_for(
            anchor,
            profile_name,
            layout=layout,
            specs=specs,
            fill=options.pop("fill", None) is not False,
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

        The server's own profile is only a hint: what the map's speakers can actually
        reproduce wins. ``room_id`` is the cabinet whose speakers make up the room, for a
        caller feeding under its own registry key (the music bot's test output).
        """
        requested = "" if requested is None else str(requested).strip().lower()
        if requested == CINEMA_OFF:
            # The map said this cabinet plays its own stereo, so a room
            # standing around it is not used. This is a decision about the
            # cabinet, taken once, not a per-listener preference.
            return None
        room_id = self._key(room_id if room_id is not None else jukebox_id)
        scope_reasons = []
        candidates, reach = cabinet_candidates(self.game, room_id, anchor,
                                              default=self.room_radius,
                                              report=scope_reasons)
        room_reasons = []
        placement = resolve_room(
            candidates,
            anchor,
            radius=reach, room=room_id,
            profile=requested or AUTO_PROFILE,
            report=room_reasons,
        )
        if placement is not None:
            return _plan_from(placement, requested, reach)
        if requested and str(requested).strip().lower() not in ("", AUTO_PROFILE):
            # A cabinet the server marked on a map with no speakers: a geometric ring
            # behind the cabinet is still a real room, and it is what makes the feature
            # usable before anyone places a speaker. A map that *did* place speakers this
            # cabinet took is not that case -- see ``_ring_notes``.
            return _ring_room(requested, reach,
                              _ring_notes(scope_reasons, room_reasons, candidates))
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

        Which speakers are in front of me, which behind, where the image is -- the
        question a person debugging a room actually asks, and how the turn-around
        behaviour is checked without an audio device.
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

        Re-acquiring the same room keeps the existing bank and refreshes its settings:
        rebuilding it would create new sources and interrupt audio already flowing.
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

        A retired room is released half a second after its replacement claimed the key,
        so releasing "whatever is registered now" would tear down the new song's room.
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

    Attached to the game object only once something turned the feature on, so a
    player who never uses cinema keeps an untouched game object.
    """
    host = host_for(game, create=False)
    if host is not None:
        return host if host.enabled else None
    if not _option_enabled():
        return None
    return host_for(game)


def _gameplay_or(game):
    """The gameplay a game hangs its live state off, or the thing itself.

    What a cabinet says about itself hangs off the *gameplay*, so a read-out holding
    only the gameplay gets the answer the live path would: one reader, two callers.
    """
    return getattr(game, "gameplay", None) or game


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

    Deliberately tolerant: a dedicated server, a headless test harness or a game
    object with no map all simply have no speakers, which resolves to "no room".
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

    A builder can move a cabinet as well as its speakers, so the room follows the map
    the player is actually playing rather than the position the song started at.
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

    The single place that reads the map's cabinets, so whoever needs to know which
    cabinet is *which* takes the jukebox's own view of the map.
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

    Two cabinets can easily sit inside each other's room radius, so whoever resolves
    a room needs the rivals' positions to hand each speaker to the cabinet it
    actually stands next to.
    """
    return [anchor for cabinet_id, anchor in cabinet_anchors(game)
            if exclude is None or cabinet_id != str(exclude)]


def cabinet_element(game, jukebox_id):
    """The map's own jukebox element for this cabinet, or None."""
    key = str(jukebox_id)
    for element in getattr(_map_of(game), "jukebox_list", ()) or ():
        if str(getattr(element, "id", "")) == key:
            return element
    return None


def cabinet_reach(game, jukebox_id, default=None):
    """How far this cabinet's room reaches, in metres.

    ``cinema_radius``, the number the server validates and sends with the cabinet
    state and with every play event -- one number, two names: how far a speaker may
    stand from the cabinet to belong to its room *and* how far from a listener that
    speaker is still heard, so the two can never drift apart.

    Missing or unusable (an older server, a hand-edited map) is ``default``. A value
    outside ``MIN_ROOM_REACH``/``MAX_ROOM_REACH`` -- the very numbers the Server
    validates with (``CINEMA_RADIUS_MIN``/``_MAX``) -- is *ignored*, so a map that
    spells out 5000 behaves like one that spelled out nothing instead of swallowing
    every speaker on it. ``game`` may be the game or the gameplay hanging off it.
    """
    fallback = float(ROOM_MAX_DISTANCE if default is None else default)
    reader = getattr(getattr(_gameplay_or(game), "jukebox_player", None),
                     "cinema_reach", None)
    value = None
    if callable(reader):
        try:
            value = reader(jukebox_id)
        except Exception:
            value = None
    if value is None:
        # No player yet (nothing has played and no cabinet menu has been
        # opened), but the state the server sent on joining already names every
        # cabinet: the live path needs the reach before any song plays.
        value = _state_reach(game, jukebox_id)
    if value is None:
        return fallback
    try:
        value = float(value)
    except (TypeError, ValueError):
        return fallback
    if value < MIN_ROOM_REACH or value > MAX_ROOM_REACH:
        return fallback
    return value


def _state_reach(game, jukebox_id):
    """The reach the server last sent for a cabinet, or None.

    The cabinet state names every cabinet and arrives on join, so it answers before
    any song plays -- when a band in a hall with no song needs it. The player's own
    cache is asked first, because that is the copy a play event updates.
    """
    state = getattr(_gameplay_or(game), "jukebox_state", None)
    boxes = state.get("jukeboxes") if isinstance(state, dict) else None
    box = (boxes or {}).get(str(jukebox_id))
    if not isinstance(box, dict):
        return None
    raw = box.get("cinema_reach", box.get("cinema_radius"))
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def cabinet_area(game, jukebox_id):
    """The named map zone this cabinet stands in, as ``(name, bounds)``.

    The map's venues are already drawn as zones, so the zone a cabinet stands in is
    that setup's own boundary: a speaker outside it belongs to another hall, however
    close it stands. Two things are deliberately *not* an area, because either would
    take a room away from speakers it should feed: the cabinet's own **marker zone**
    (the builder writes one named ``jukebox`` at the element's own bounds -- it would
    always win and every other speaker would be "outside the area", so it is skipped
    by *size*, not by name or bounds, which a hand-written map can round) and a zone
    with **no cinema speaker in it** (a venue drawn before anybody places one
    describes nothing yet; rooms re-resolve once a second, so it arrives on the
    current beat). Where several qualify the *smallest* wins, the rule
    ``Map.get_zone_at`` itself uses.
    """
    element = cabinet_element(game, jukebox_id)
    map_ = _map_of(game)
    if element is None or map_ is None:
        return (None, None)
    center = getattr(element, "center", None)
    if not center:
        return (None, None)
    feet = (float(center[0]), float(center[1]), float(center[2]))
    own = _zone_volume(element)
    held = [(spec.position, spec) for spec in
            (coerce_spec(raw) for raw in map_speakers(game)) if spec is not None]
    best = None
    best_volume = None
    for zone in getattr(map_, "zone_list", ()) or ():
        if not _zone_holds(zone, feet):
            continue
        volume = _zone_volume(zone)
        if volume <= own:
            continue          # the cabinet's own marker, not a room
        if not any(_zone_holds(zone, spot) for spot, _spec in held):
            continue          # nothing is placed in it yet
        if best_volume is None or volume < best_volume:
            best, best_volume = zone, volume
    if best is None:
        return (None, None)
    return (str(getattr(best, "zonename", "") or ""), _bounds_of(best))


def _zone_holds(zone, point):
    checker = getattr(zone, "in_bound", None)
    if not callable(checker):
        return False
    try:
        return bool(checker(point[0], point[1], point[2]))
    except Exception:
        return False


def _bounds_of(element):
    """``(minx, maxx, miny, maxy, minz, maxz)`` of a map element, or None."""
    try:
        return tuple(float(getattr(element, name))
                     for name in ("minx", "maxx", "miny", "maxy", "minz", "maxz"))
    except (AttributeError, TypeError, ValueError):
        return None


def _zone_volume(zone):
    try:
        return ((float(zone.maxx) - float(zone.minx) + 1.0)
                * (float(zone.maxy) - float(zone.miny) + 1.0)
                * (float(zone.maxz) - float(zone.minz) + 1.0))
    except (AttributeError, TypeError, ValueError):
        return float("inf")


def cabinet_candidates(game, jukebox_id, anchor, *, radius=None, default=None,
                      report=None):
    """The speakers one cabinet may take, and the reach it takes them at.

    The single home of the scope rule, so the playing path, the menus and the
    diagnostics ask one question: a speaker belongs to this cabinet when it stands
    inside the cabinet's own area (when the map has one) and within its reach, and no
    cabinet that could claim it stands closer. Returns ``(speakers, reach)`` so the
    room and the bank are built from the same number.
    """
    key = str(jukebox_id)
    reach = cabinet_reach(game, key, default) if radius is None else float(radius)
    area_name, area = cabinet_area(game, key)
    rivals = []
    rival_areas = []
    for other_id, other_anchor in cabinet_anchors(game):
        if other_id == key:
            continue
        rivals.append(other_anchor)
        rival_areas.append(cabinet_area(game, other_id)[1])
    kept = exclusive_speakers(map_speakers(game), anchor, rivals=rivals,
                              radius=reach, area=area, rival_areas=rival_areas,
                              report=report, area_name=area_name)
    return (kept, reach)


class SpeakerOwner:
    """Which cabinet would own the speaker standing at a point, and why.

    A read-out, not a rule: who owns it, how far it stands from that cabinet, and
    when nobody owns it, which of the two boundaries said no.
    """

    __slots__ = ("cabinet", "distance", "reach", "area", "reason")

    def __init__(self, cabinet="", distance=None, reach=None, area=None,
                 reason="no_cabinet"):
        self.cabinet = str(cabinet or "")
        self.distance = None if distance is None else float(distance)
        self.reach = None if reach is None else float(reach)
        self.area = area or None
        self.reason = reason

    @property
    def claimed(self):
        return self.reason == "claimed"

    def describe(self):
        """One line, said the same way wherever a menu or a log needs it."""
        if self.reason == "no_cabinet":
            return "no jukebox on this map, so no cabinet owns it"
        where = f" (its area is {self.area})" if self.area else ""
        if self.reason == "claimed":
            return (f"jukebox {self.cabinet} owns it, {self.distance:.0f} m from it"
                    f"{where}")
        if self.reason == "out_of_area":
            return (f"no cabinet owns it: it stands outside jukebox {self.cabinet}'s"
                    f" area{where}, {self.distance:.0f} m away, and a speaker is only"
                    f" that cabinet's inside its own room")
        return (f"no cabinet owns it: the nearest is jukebox {self.cabinet},"
                f" {self.distance:.0f} m away, and its reach is {self.reach:.0f} m")

    def __repr__(self):
        return f"SpeakerOwner({self.cabinet!r}, {self.reason})"


def speaker_owner(game, position, *, radius=None, default=None):
    """Which cabinet owns a speaker standing at ``position`` (a read-out).

    Asks the very decision the resolver makes about one point and every cabinet, so a
    menu or a log can say where a speaker belongs without a second scope rule.
    """
    try:
        feet = (float(position[0]), float(position[1]), float(position[2]))
    except (TypeError, ValueError, IndexError):
        return SpeakerOwner()
    nearest = None
    for cabinet_id, anchor in cabinet_anchors(game):
        reach = cabinet_reach(game, cabinet_id, default) if radius is None else float(radius)
        area_name, area = cabinet_area(game, cabinet_id)
        distance = _distance_between(anchor, feet)
        if claims_point(feet, anchor, reach, area):
            if nearest is None or distance < nearest.distance:
                nearest = SpeakerOwner(cabinet_id, distance, reach, area_name, "claimed")
            continue
        if nearest is not None:
            continue
        outside = bool(area) and not inside_area(feet, area)
        reason = "out_of_area" if outside else "out_of_reach"
        if (nearest is None or distance < nearest.distance
                or (nearest.reason == "out_of_area" and reason == "out_of_reach")):
            nearest = SpeakerOwner(cabinet_id, distance, reach, area_name, reason)
    return nearest or SpeakerOwner()


class CabinetNeighbour:
    """Another cabinet on the map, and what stands between the two.

    A read-out, never a rule: how far the next cabinet is, how far *its* room
    reaches, and whether the map draws one hall around both (``same_area``).
    """

    __slots__ = ("area", "cabinet", "distance", "reach", "same_area")

    def __init__(self, cabinet, distance, area=None, reach=None,
                 same_area=False):
        self.cabinet = str(cabinet or "")
        self.distance = float(distance)
        self.area = area or None
        self.reach = None if reach is None else float(reach)
        self.same_area = bool(same_area)

    def __repr__(self):
        return f"CabinetNeighbour({self.cabinet!r}, {self.distance:.0f} m)"


def cabinet_neighbours(game, jukebox_id):
    """Every other cabinet on the map, nearest first.

    Two cabinets standing close enough to share speakers are the one thing a map does
    not show by itself, so this answers "what else is around here" with distances and
    areas only -- and nothing here can change how a room plays.
    """
    key = str(jukebox_id)
    anchor = cabinet_anchor(game, key)
    if anchor is None:
        return ()
    mine_area = cabinet_area(game, key)[0]
    found = []
    for other_id, other_anchor in cabinet_anchors(game):
        if other_id == key:
            continue
        area = cabinet_area(game, other_id)[0]
        found.append(CabinetNeighbour(
            other_id, _distance_between(anchor, other_anchor), area,
            cabinet_reach(game, other_id, ROOM_MAX_DISTANCE),
            bool(mine_area) and area == mine_area))
    found.sort(key=lambda neighbour: neighbour.distance)
    return tuple(found)


def neighbour_line(game, jukebox_id):
    """The one-line read-out a cabinet's menu carries, or "" when it is alone.

    Short on purpose: a line read aloud while walking a menu, so it says the
    distance, the count and the id, and leaves the explanation to
    ``neighbour_note``.
    """
    others = cabinet_neighbours(game, jukebox_id)
    if not others:
        return ""
    nearest = others[0]
    where = ""
    if nearest.same_area and nearest.area:
        where = ", this same area"
    elif nearest.area:
        where = f", in '{nearest.area}'"
    who = f"{nearest.cabinet}{where}"
    if len(others) == 1:
        return f"Another cabinet at {nearest.distance:.0f} m ({who})"
    return (f"{len(others)} other cabinets, nearest at {nearest.distance:.0f} m "
            f"({who})")


def neighbour_note(game, jukebox_id):
    """What the cabinets around this one mean for the speakers here, or "".

    Asks the very rule the room is built with (``cabinet_area`` decides whether an
    area can separate the two cabinets at all), so the sentence cannot promise a
    speaker the resolver would hand to somebody else. Four answers for four
    situations: one hall around both (distance decides), a hall each, no halls drawn
    at all, and one of the two drawn.
    """
    others = cabinet_neighbours(game, jukebox_id)
    if not others:
        return ""
    key = str(jukebox_id)
    nearest = others[0]
    their_reach = 0.0 if nearest.reach is None else nearest.reach
    metre = f"{nearest.distance:.0f} m away"
    reaching = f"its room reaches {their_reach:.0f} m"
    if nearest.same_area:
        return (f"jukebox {nearest.cabinet} stands {metre} in this same area "
                f"('{nearest.area}') and {reaching}: speakers closer to "
                f"{nearest.cabinet} than to this cabinet go to its room, and "
                f"the rest are this cabinet's.")
    mine_area = cabinet_area(game, key)[0]
    if mine_area and nearest.area:
        return (f"jukebox {nearest.cabinet} stands {metre} in '{nearest.area}', "
                f"another area, and {reaching}: a speaker standing in that area "
                f"belongs to it, and one in '{mine_area}' belongs to this "
                f"cabinet.")
    if nearest.area:
        return (f"jukebox {nearest.cabinet} stands {metre} in '{nearest.area}' "
                f"and {reaching}: a speaker inside that area goes to whichever of "
                f"the two is nearer, and one outside it stays with this "
                f"cabinet.")
    if mine_area:
        return (f"jukebox {nearest.cabinet} stands {metre} with no hall drawn "
                f"around it, and {reaching}: a speaker outside this cabinet's "
                f"area ('{mine_area}') can be claimed by both, so the nearer "
                f"cabinet takes it, and one inside '{mine_area}' stays with "
                f"this cabinet.")
    return (f"jukebox {nearest.cabinet} stands {metre} and {reaching}: nothing "
            f"is drawn around either cabinet, so speakers are shared out by "
            f"distance and one closer to {nearest.cabinet} goes to its room.")


class RoomExtent:
    """What a reach is measured against: this cabinet's speakers and its room.

    Two numbers a person choosing a reach needs and no other read-out gives: the
    distance to the speaker standing furthest from the cabinet (the one a smaller
    reach would *cut*, so its crossover, tone, level and delay stop being heard) and
    the distance to the far corner of the zone drawn around the cabinet (the part a
    smaller reach leaves silent). Both come from the readers the room itself is built
    with, so a menu cannot promise geometry the resolver would refuse.
    """

    __slots__ = ("area", "corner", "speakers")

    def __init__(self, area=None, corner=None, speakers=()):
        self.area = area or None
        self.corner = None if corner is None else float(corner)
        # (name, distance), nearest first: the far end is the one a small
        # reach drops, and it is the end a person is asking about.
        self.speakers = tuple(speakers)

    @property
    def farthest(self):
        """How far the speaker standing furthest from the cabinet is."""
        return self.speakers[-1][1] if self.speakers else None

    def cut_by(self, reach):
        """The speakers a reach this small leaves out, furthest first."""
        try:
            value = float(reach)
        except (TypeError, ValueError):
            return []
        return [(name, distance) for name, distance in self.speakers
                if distance > value]

    def note(self, reach, prefix=" - "):
        """One menu-sized sentence: what this reach would leave unheard here.

        Nothing at all when the reach covers the room, so only the choices that bite say
        why. The first half is about the *speakers* (a cut one goes silent entirely), the
        second about the room's own drawn edge (the far end is simply not heard) -- the
        two different ways a small reach bites.
        """
        try:
            value = float(reach)
        except (TypeError, ValueError):
            return ""
        parts = []
        cut = self.cut_by(value)
        if cut:
            named = ", ".join(f"{name} ({distance:.0f} m)"
                              for name, distance in cut)
            parts.append(f"Leaves out {named}: a speaker past the reach is not"
                         f" this room's at all, so its crossover, tone and level"
                         f" are not heard")
        if self.corner is not None and self.corner > value:
            where = (f"The '{self.area}' area" if self.area
                     else "The zone drawn around it")
            parts.append(f"{where} reaches {self.corner:.0f} m from the cabinet,"
                         f" so the far end of this room is silent")
        if not parts:
            return ""
        return prefix + "; ".join(parts)

    def summary(self):
        """The same two numbers, said where a cabinet describes its room."""
        bits = []
        if self.speakers:
            one = len(self.speakers) == 1
            bits.append(f"the {'speaker' if one else 'speakers'} here "
                        f"{'stands' if one else 'stand'} up to"
                        f" {self.farthest:.0f} m from it")
        if self.corner is not None:
            where = (f"the '{self.area}' area it stands in" if self.area
                     else "the zone drawn around it")
            bits.append(f"{where} reaches {self.corner:.0f} m from it")
        if not bits:
            return ""
        return (", and ".join(bits)
                + ": a reach below that leaves the far end of this room"
                  " unheard.")


def room_extent(game, jukebox_id, anchor):
    """This cabinet's own geometry: its speakers' distances and its area's edge.

    A read-out, never a rule: asking it twice changes nothing, because
    ``cabinet_area`` and ``cabinet_candidates`` are the readers the room is built
    with.
    """
    here = tuple(float(value) for value in anchor)
    candidates, _reach = cabinet_candidates(game, jukebox_id, here,
                                            radius=MAX_ROOM_REACH)
    speakers = []
    for raw in candidates:
        spec = coerce_spec(raw)
        if spec is None:
            continue
        name = str(getattr(spec, "name", "") or "a speaker")
        speakers.append((name, _distance_between(spec.position, here)))
    speakers.sort(key=lambda item: item[1])
    area_name, bounds = cabinet_area(game, jukebox_id)
    return RoomExtent(area_name, _corner_distance(here, bounds), speakers)


def _corner_distance(anchor, bounds):
    """How far the far corner of a zone's bounds stands from ``anchor``.

    The room's own edge as the map's geometry writes it -- the furthest of the box's
    eight corners. A room is heard from the back row, so this is what a reach has to
    cover to be heard everywhere the map drew it.
    """
    if not bounds:
        return None
    try:
        values = [float(value) for value in tuple(bounds)[:6]]
    except (TypeError, ValueError):
        return None
    if len(values) < 6:
        return None
    minx, maxx, miny, maxy, minz, maxz = values
    farthest = 0.0
    for x in (minx, maxx):
        for y in (miny, maxy):
            for z in (minz, maxz):
                farthest = max(farthest,
                               _distance_between(anchor, (x, y, z)))
    return farthest


def _distance_between(first, second):
    return sum((float(first[index]) - float(second[index])) ** 2
               for index in range(3)) ** 0.5


def _ring_room(requested, reach, notes=()):
    """The ring behind the cabinet, as the plan a caller asked for."""
    return RoomPlan(get_profile(requested).name, None, None, fill=True,
                    reach=reach, notes=notes)


def _ring_notes(scope_reasons, room_reasons, candidates):
    """What to say when a requested shape plays the ring instead of the map.

    Silent when the map placed nothing for this cabinet: a ring is the shape a map
    with no cinema speakers gets, and that is not a mistake. It speaks when the map
    *did* place something this cabinet took -- or came within its reach of and then
    refused -- and no room came of it: heard as "the room stopped working", with
    every speaker's crossover, tone, level and delay unread and nothing on any screen
    to say so, which is why the reason travels with the room to whoever logs or asks.
    """
    if not candidates and not scope_reasons:
        return ()
    reasons = [str(reason) for reason in (*scope_reasons, *room_reasons)]
    if not reasons:
        return ()
    return ("no room from the map's speakers (" + "; ".join(reasons)
            + "), so this room is the ring behind the cabinet: none of the"
              " map's speakers is fed, and their crossover, tone, level and"
              " delay are not heard",)


def _plan_from(placement, requested, reach=None):
    """A resolved placement as the :class:`RoomPlan` a caller asked for.

    The ``fill`` rule lives here because the playing path and the menus both need it:
    only an explicitly asked-for profile may pad itself out with speakers the map
    does not have, while a room read off the map is exactly the speakers someone
    placed. ``reach`` is the same number the speakers were claimed with.
    """
    requested = "" if requested is None else str(requested).strip().lower()
    return RoomPlan(room_profile(placement, requested), placement.specs,
                    placement,
                    fill=requested not in ("", AUTO_PROFILE),
                    reach=reach)


def preview_room(game, anchor, *, room_id=None, radius=None):
    """Resolve the room the map describes, without turning the feature on.

    Used by menus: a player must see which cabinets have a room before deciding to
    route audio into one, and previewing must not change how the jukeboxes play.
    :func:`cinema_room` is the playing path and still requires the feature enabled.
    """
    plan, _silent = room_plan(game, anchor, room_id=room_id, radius=radius)
    return plan


def room_plan(game, anchor, *, requested=None, room_id=None, radius=None):
    """The room a cabinet's mode would play, and the speakers it leaves out.

    Returns ``(plan, silent)``: the room ``requested`` resolves to here (None when it
    has none), and ``[(name, slot), ...]`` -- the speakers standing around the cabinet
    that this shape does not feed, which is silent with nothing on any screen saying
    so. Derived from the very renderer playback would build
    (:func:`router.renderer_for`), so a read-out can never name a speaker the room
    would not feed, or stay quiet about one it will not. Previewing acquires nothing.
    """
    requested = "" if requested is None else str(requested).strip().lower()
    if requested == CINEMA_OFF:
        # The map said this cabinet plays its own stereo, so there is no shape
        # to read out and no speaker it leaves out: it is silent about
        # everything on purpose (see ``plan_for``).
        return None, []
    anchor = tuple(float(value) for value in anchor)
    scope_reasons = []
    candidates, reach = cabinet_candidates(game, room_id or "", anchor,
                                          radius=radius, report=scope_reasons)
    room_reasons = []
    placement = resolve_room(candidates, anchor, radius=reach, room=room_id,
                             profile=requested or AUTO_PROFILE,
                             report=room_reasons)
    if placement is not None:
        plan = _plan_from(placement, requested, reach)
        room = renderer_for(anchor, plan.profile, specs=plan.specs, fill=plan.fill)
        placed = [(placed.spec.name, slot)
                  for slot, placed in placement.speakers.items()]
        fed = set(room.slots)
    elif requested not in ("", AUTO_PROFILE):
        # A shape that was asked for outlives the map: the ring behind the cabinet is
        # the room, so NOT one of the speakers standing here is fed -- their slot names
        # may match the ring's, but a room that reads them as fed would be promising a
        # crossover, a tone and a delay that nothing on this map makes.
        plan = _ring_room(requested, reach,
                          _ring_notes(scope_reasons, room_reasons, candidates))
        room = renderer_for(anchor, plan.profile, specs=None, fill=True)
        placed = [(spec.name, spec.slot) for spec in
                  (coerce_spec(raw) for raw in candidates) if spec is not None]
        fed = set()
    else:
        return None, []
    silent = sorted(((name, slot) for name, slot in placed if slot not in fed),
                    key=lambda item: (item[1], str(item[0])))
    return plan, silent


def room_diagnosis(game, anchor, *, room_id=None, radius=None):
    """Why a cabinet has no room, in one line, for menus and logs.

    The same resolver that plays the room hands back its own reason, so a tester
    standing in front of four speakers hears which wall is missing instead of "no
    cinema speakers around it".
    """
    anchor = tuple(float(value) for value in anchor)
    reasons = []
    candidates, reach = cabinet_candidates(game, room_id or "", anchor,
                                          radius=radius, report=reasons)
    placement = resolve_room(
        candidates,
        anchor, radius=reach, room=room_id, profile=AUTO_PROFILE, report=reasons,
    )
    if placement is not None:
        return placement.summary()
    return "; ".join(reasons) or "the speakers here do not make a usable room"


def cinema_room(game, jukebox_id, anchor, requested=None, *, host=None, room_id=None):
    """What a cabinet should play through, or None to stay a plain jukebox.

    The single call the jukebox makes, so it never has to know about rooms,
    placements, profiles or the map's speaker elements -- and a caller with no host
    at all (feature off) still gets a cheap, correct "no".
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

    ``bank`` names the specific room, for a retired one that must go back without
    taking the song which replaced it down with it.
    """
    host = host_for(game, create=False)
    return None if host is None else host.release(jukebox_id, bank)


def release_all(game):
    host = host_for(game, create=False)
    return 0 if host is None else host.release_all()
