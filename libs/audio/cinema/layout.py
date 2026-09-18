"""Speaker slots and room placement for the Cinema Speaker System.

Slots are named by their job in the room rather than by a channel index:

    front_l  front_c  front_r   -- the screen wall, carries the real image
    side_l   side_r             -- the walls, carries the difference
    rear_l   rear_r             -- behind the listener, carries ambience

Engine axes match ``AudioManager.make_orientation``: X is left to right,
Y is backward to forward, Z is down to up. So the screen wall is +Y, the
listener's left is -X, and the rear pair sits at -Y.

Placement comes from the map's ``cinemaSpeaker`` elements when they exist.
When they do not, :class:`CinemaLayout` falls back to a geometric ring
around the anchor so the layout can be developed and tested before any map
has speakers in it. A speaker declared with ``slot="auto"`` is assigned the
slot nearest its bearing from the anchor, so builders can drop speakers
wherever they like and let the room sort itself out.
"""

from math import atan2, cos, degrees, radians, sin, sqrt

SLOT_ORDER = ("front_l", "front_c", "front_r", "side_l", "side_r", "rear_l", "rear_r")

AUTO_SLOT = "auto"

# How big a room is, in metres -- the room's own scale, deliberately wider
# than the plain jukebox pair's falloff (full within 8 m, silent at 40). A
# cabinet with no speakers keeps that 8/40 exactly, so nothing about the
# shipped jukebox moves when a room is placed; a room is meant to be heard
# from the back row, which the old radius could not reach.
#
# ROOM_RADIUS is how far from the CABINET a speaker may stand and still
# belong to the room; ROOM_MAX_DISTANCE is how far from the LISTENER a
# speaker may be before it is silent. They are equal on purpose, so "inside
# the room" and "audible" are the same statement rather than two numbers that
# can drift apart.
ROOM_RADIUS = 60.0
ROOM_REFERENCE_DISTANCE = 8.0     # full volume within this many metres
ROOM_MAX_DISTANCE = 60.0          # silent at (and beyond) this many metres

# A cabinet may reach further or less far than every other cabinet: a hall
# whose back wall stands 90 m from the cabinet needs a 90 m room, and a
# cabinet squeezed into a booth wants a short one so it stops claiming the
# stage's speakers. The reach is the cabinet's own ``cinema_radius`` (written
# in the map, sent by the server) and it is ONE number under two names -- how
# far a speaker may stand from the cabinet to belong to the room, and how far
# from a listener it is still heard (see ``placement.RoomPlan.reach``).
#
# The bounds are the same on the server (``CINEMA_RADIUS_MIN`` / ``_MAX``),
# and a value outside them is ignored rather than obeyed: a hand-edited map
# must not be able to stretch a room across the whole map -- or collapse it.
MIN_ROOM_REACH = 10.0
MAX_ROOM_REACH = 240.0

# Bearing bands used to snap an `auto` speaker to a slot, in degrees where
# 0 is straight ahead, positive is to the listener's right and +/-180 is
# directly behind. The front gets the widest band because the screen wall is
# where the stereo image has to survive intact, and a centre speaker only a
# little off-axis is still doing the centre's job.
_BEARING_BANDS = (
    (20, "front_c"),
    (75, "front_r"),
    (120, "side_r"),
    (180.1, "rear_r"),
)

# Mirror map for negative bearings; the centre is its own mirror image.
_MIRROR = {"front_r": "front_l", "side_r": "side_l", "rear_r": "rear_l"}

# Where each slot belongs in a room, in degrees relative to the screen wall
# (0 = dead ahead, negative = the audience's left). This is the reference the
# placement resolver measures a builder's speakers against, and it is what
# lets a rotated or lopsided room still resolve to the right slots.
IDEAL_BEARING = {
    "front_c": 0.0,
    "front_l": -30.0,
    "front_r": 30.0,
    "side_l": -90.0,
    "side_r": 90.0,
    "rear_l": -150.0,
    "rear_r": 150.0,
}

# Slots that only work as a mirrored pair. A left wall speaker with no right
# wall speaker is not half a room, it is a room with a steering bias, so an
# unpaired member is dropped and the profile falls back to what the rest of
# the room can honestly reproduce.
SLOT_PAIRS = (("side_l", "side_r"), ("rear_l", "rear_r"))

# The deepest decorrelation trim a speaker may carry, in milliseconds. Past
# this the offset stops reading as a wider wall and starts reading as an echo,
# and the room has to keep enough queued audio to cut that far back (see
# ``bank.CinemaSpeakerBank._recent``).
MAX_TRIM_MS = 100.0


def slot_for_bearing(bearing):
    """Nearest slot for a bearing in degrees (0 ahead, +right, +/-180 behind)."""
    angle = float(bearing)
    while angle > 180.0:
        angle -= 360.0
    while angle < -180.0:
        angle += 360.0
    if angle < 0:
        mirrored = slot_for_bearing(-angle)
        return _MIRROR.get(mirrored, mirrored)
    for limit, slot in _BEARING_BANDS:
        if angle <= limit:
            return slot
    return "rear_r"


def bearing_from(anchor, position):
    """Bearing of ``position`` seen from ``anchor``: 0 ahead, +right, +/-180 behind."""
    dx = float(position[0]) - float(anchor[0])
    dy = float(position[1]) - float(anchor[1])
    if dx == 0.0 and dy == 0.0:
        return 0.0
    return degrees(atan2(dx, dy))


class CinemaSpeakerSpec:
    """One placed speaker: its slot, where it stands and how it is trimmed.

    ``level`` trims a speaker that is hotter or quieter than its neighbours
    without touching the profile weights. ``delay_ms`` is a deliberate
    Haas-style offset; it never accelerates anything, so the renderer
    reports it as extra latency for the jam-note sync.

    ``aim_yaw`` (absolute degrees, 0 = +Y, +90 = +X, the same convention as
    ``bearing_from`` and the megaphone speaker's own aim) is the direction
    the cabinet faces. It is optional and only used to work out whether a
    listener stands in front of or behind that particular speaker; a speaker
    without an aim is omnidirectional, which is the right default for a
    cinema room where every seat has to hear every wall.

    ``tone`` is the speaker's own voicing, an openness in 0..1 where 1.0 is
    the map's voicing untouched: the builder's way of saying "this one is
    duller than the others" -- a rear pair darker than the screen wall, a
    speaker inside a booth. It only ever cuts highs (it is not a level, and it
    is not a wall: ``level`` is the map's tool for loudness, and a wall is
    measured from geometry), and it is applied by ``listener.speaker_filter``
    together with whatever wall stands between that speaker and these ears.

    ``crossover`` is the map's *crossover* mark, and its sign is the side the
    speaker keeps: a positive number is a bass cabinet (nothing above that
    frequency is fed to it) and a negative one is a tweeter (nothing below it).
    It is carried here exactly as the map wrote it (``None`` = full range,
    which is every speaker that existed before this attribute), and what the
    number *means* is decided in one place where it is used -- the room's own
    ``crossover`` -- rather than clamped into the right band by whoever reads
    it next.

    ``crossover_high`` is the second edge, and it is what makes the speaker a
    *band* (a mid cabinet: it keeps what lies between the two edges) rather than
    a two-way split. It is carried raw for the same reason -- and it is a
    separate field rather than being folded in here because this module is part
    of the portable core, which may not import the reader that composes a mark
    (``crossover.mark_of``).
    """

    __slots__ = ("slot", "position", "level", "delay_ms", "tone", "crossover",
                 "crossover_high", "name", "room", "aim_yaw", "cone_inner",
                 "cone_outer", "cone_outer_gain", "declared")

    def __init__(self, slot, position, level=1.0, delay_ms=0.0, tone=1.0,
                 name=None, room=None, aim_yaw=None, cone_inner=None,
                 cone_outer=None, cone_outer_gain=None, declared=None,
                 crossover=None, crossover_high=None):
        self.slot = str(slot or AUTO_SLOT).strip().lower()
        self.position = (float(position[0]), float(position[1]), float(position[2]))
        self.level = max(0.0, min(4.0, float(level)))
        self.delay_ms = max(0.0, min(MAX_TRIM_MS, float(delay_ms)))
        self.tone = max(0.0, min(1.0, float(tone)))
        # As the map wrote it, *including its sign*: the sign is the side the
        # speaker keeps (positive = a bass cabinet, negative = a tweeter), 0
        # and None both mean "full range", and an unusable value is the map
        # saying nothing rather than a corner frequency nobody can explain.
        # See ``cinema.crossover``.
        try:
            self.crossover = None if crossover is None else float(crossover)
        except (TypeError, ValueError):
            self.crossover = None
        # The band's other edge, as written: what it means is decided with the
        # first one (``crossover.crossover_hz(value, crossover_high)``).
        try:
            self.crossover_high = None if crossover_high is None else float(crossover_high)
        except (TypeError, ValueError):
            self.crossover_high = None
        self.name = name
        self.room = str(room or "").strip()
        self.aim_yaw = None if aim_yaw is None else float(aim_yaw) % 360.0
        self.cone_inner = None if cone_inner is None else max(0.0, float(cone_inner))
        self.cone_outer = None if cone_outer is None else max(0.0, float(cone_outer))
        self.cone_outer_gain = (None if cone_outer_gain is None
                                else max(0.0, min(1.0, float(cone_outer_gain))))
        # The slot the builder typed in (None when they left it on auto), kept
        # for diagnostics: a room that had to overrule its own labels should be
        # able to say so out loud.
        self.declared = None if declared is None else str(declared).strip().lower()

    @property
    def has_cone(self):
        """True when this speaker's aim should shape its output at all."""
        return self.aim_yaw is not None and (self.cone_inner is not None
                                             or self.cone_outer is not None)

    def __repr__(self):
        return (f"CinemaSpeakerSpec({self.slot!r}, level={self.level}, "
                f"delay_ms={self.delay_ms}, tone={self.tone}, "
                f"aim={self.aim_yaw})")


# Percent-scaled attributes a builder types into a map element. ``level`` and
# ``volume`` both mean the same thing to a speaker (how hot it runs), and
# ``delay`` is accepted in milliseconds for cinema speakers because a wall
# speaker's offset is far smaller than a megaphone tower's.
_PERCENT_KEYS = ("level", "volume", "gain")


def _first(raw, keys, default=None):
    for key in keys:
        if key in raw and raw[key] is not None:
            return raw[key]
    return default


def _number(value, default=None):
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _position_of(raw):
    """A speaker's world position, from x/y/z or from the centre of its box."""
    x = _number(_first(raw, ("x", "position_x")))
    y = _number(_first(raw, ("y", "position_y")))
    z = _number(_first(raw, ("z", "position_z")))
    if x is not None and y is not None and z is not None:
        return (x, y, z)
    try:
        minx, maxx, miny, maxy, minz, maxz = (float(raw["minx"]), float(raw["maxx"]),
                                              float(raw["miny"]), float(raw["maxy"]),
                                              float(raw["minz"]), float(raw["maxz"]))
    except (KeyError, TypeError, ValueError):
        return None
    return ((minx + maxx) / 2.0, (miny + maxy) / 2.0, (minz + maxz) / 2.0)


def coerce_spec(raw):
    """Turn a map element (or a spec object) into a usable speaker, or None.

    Map data is builder-authored and travels through a network round trip, so
    every field is treated as advisory: an unusable one is dropped here rather
    than allowed to break playback for the whole room.
    """
    if isinstance(raw, CinemaSpeakerSpec):
        # A spec built by hand (a test, a future caller) carries its own slot
        # as its label; only a spec that was explicitly left on ``auto`` has
        # nothing to declare.
        if raw.declared is None and raw.slot in IDEAL_BEARING:
            raw.declared = raw.slot
        return raw
    if not raw:
        return None
    if isinstance(raw, dict):
        source = raw
    else:
        source = {key: getattr(raw, key) for key in (
            "x", "y", "z", "minx", "maxx", "miny", "maxy", "minz", "maxz",
            "channel", "slot", "room", "level", "volume", "delay", "delay_ms",
            "tone", "crossover", "crossover_high",
            "aim_yaw", "aim_pitch", "inner_cone_angle", "outer_cone_angle",
            "outer_cone_gain", "id") if hasattr(raw, key)}
        position = getattr(raw, "position", None)
        if position is not None:
            source["x"], source["y"], source["z"] = position
    position = _position_of(source)
    if position is None:
        return None
    declared = _first(source, ("channel", "slot"))
    declared = None if declared is None else str(declared).strip().lower()
    level = _number(_first(source, _PERCENT_KEYS), None)
    if level is None:
        level = 1.0
    elif level > 4.0:
        level = level / 100.0
    delay_ms = _number(_first(source, ("delay_ms",)), None)
    if delay_ms is None:
        delay_ms = _number(_first(source, ("delay",)), 0.0)
    # A speaker's voicing is written as a **percentage, always** (100 = as
    # placed, 0 = the darkest a speaker can be made) and converted to the
    # spec's 0..1 openness here -- in one place, so the menu, the map file and
    # every reader of a term agree about what a number means. It deliberately
    # does not borrow ``level``'s "above 4.0 is a percentage" idiom: that rule
    # makes a builder typing 1 mean unity, which is a change the room would
    # silently swallow, whereas percent-always has no silent band.
    tone = _number(_first(source, ("tone",)), None)
    if tone is None:
        tone = 100.0
    tone = tone / 100.0
    return CinemaSpeakerSpec(
        declared if declared else AUTO_SLOT,
        position,
        level=level,
        delay_ms=delay_ms,
        tone=tone,
        crossover=_number(_first(source, ("crossover",)), None),
        crossover_high=_number(_first(source, ("crossover_high",)), None),
        name=_first(source, ("id", "name")),
        room=_first(source, ("room",), ""),
        aim_yaw=_number(_first(source, ("aim_yaw",))),
        cone_inner=_number(_first(source, ("inner_cone_angle", "cone_inner"))),
        cone_outer=_number(_first(source, ("outer_cone_angle", "cone_outer"))),
        cone_outer_gain=_number(_first(source, ("outer_cone_gain", "cone_outer_gain"))),
        declared=declared,
    )


def _ring_position(anchor, bearing, radius):
    angle = radians(float(bearing))
    return (
        float(anchor[0]) + sin(angle) * radius,
        float(anchor[1]) + cos(angle) * radius,
        float(anchor[2]),
    )


class CinemaLayout:
    """Resolve each slot to a world position, from the map or from a ring.

    The geometric ring is not a stand-in for missing map data so much as a
    sane default: any room with a jukebox in it can host a convincing front
    stage without a builder placing a single speaker.
    """

    DEFAULT_FRONT_RADIUS = 5.0
    DEFAULT_SIDE_RADIUS = 6.0
    DEFAULT_REAR_RADIUS = 8.0

    # Slot -> (bearing, radius attribute) baked into the ring fallback.
    _RING = (
        ("front_l", -30.0, "front_radius"),
        ("front_c", 0.0, "front_radius"),
        ("front_r", 30.0, "front_radius"),
        ("side_l", -90.0, "side_radius"),
        ("side_r", 90.0, "side_radius"),
        ("rear_l", -135.0, "rear_radius"),
        ("rear_r", 135.0, "rear_radius"),
    )

    def __init__(self, anchor, specs=None, *, front_radius=None, side_radius=None,
                 rear_radius=None, use_ring=True):
        self.anchor = (float(anchor[0]), float(anchor[1]), float(anchor[2]))
        self.front_radius = float(self.DEFAULT_FRONT_RADIUS if front_radius is None else front_radius)
        self.side_radius = float(self.DEFAULT_SIDE_RADIUS if side_radius is None else side_radius)
        self.rear_radius = float(self.DEFAULT_REAR_RADIUS if rear_radius is None else rear_radius)
        self.use_ring = bool(use_ring)
        self._specs = {}
        self._ring = {}
        for spec in self._accepted(specs):
            self._specs[spec.slot] = spec
        if self.use_ring:
            self._build_ring()

    def _accepted(self, specs):
        """Yield usable specs, dropping unknown slots and unusable positions.

        Map data is builder-authored, so a typo must degrade to 'this
        speaker does not exist' rather than break playback for the room.
        """
        for spec in specs or ():
            speaker = coerce_spec(spec)
            if speaker is None:
                continue
            slot = speaker.slot
            if slot == AUTO_SLOT:
                slot = slot_for_bearing(bearing_from(self.anchor, speaker.position))
            if slot not in SLOT_ORDER:
                continue
            speaker.slot = slot
            yield speaker

    def _build_ring(self):
        radii = {
            "front_radius": self.front_radius,
            "side_radius": self.side_radius,
            "rear_radius": self.rear_radius,
        }
        for slot, bearing, radius_key in self._RING:
            # A real speaker always wins; the ring only fills the gaps.
            if slot in self._specs:
                continue
            self._ring[slot] = _ring_position(self.anchor, bearing, radii[radius_key])

    @property
    def slots(self):
        """Slots that actually have a speaker, in room order."""
        return tuple(slot for slot in SLOT_ORDER
                     if slot in self._specs or slot in self._ring)

    def position(self, slot):
        spec = self._specs.get(slot)
        if spec is not None:
            return spec.position
        return self._ring.get(slot)

    def positions(self):
        return {slot: self.position(slot) for slot in self.slots}

    def spec(self, slot):
        return self._specs.get(slot)

    def level(self, slot):
        spec = self._specs.get(slot)
        return spec.level if spec is not None else 1.0

    def delay_ms(self, slot):
        spec = self._specs.get(slot)
        return spec.delay_ms if spec is not None else 0.0

    def tone(self, slot):
        """The map's own voicing for a slot, 1.0 when it placed no preference.

        A ring speaker (no map element behind it) has no tone to read, so it
        is as open as the profile gives it -- the attribute belongs to a
        speaker a builder placed, not to a slot a profile invented.
        """
        spec = self._specs.get(slot)
        return spec.tone if spec is not None else 1.0

    def crossover(self, slot):
        """This slot's *first* crossover edge, signed, 0.0 when it has none.

        The sign is the side the speaker keeps -- a positive number is a bass
        cabinet, a negative one a tweeter -- and ``crossover_hz`` is the one
        place that says what a mark means. This is the map's own number, not
        the mark: a speaker that also holds ``crossover_high`` is a band (see
        :meth:`crossover_high`), and the two are composed into one mark by
        ``crossover.mark_of`` -- which this module may not import, because the
        portable core imports nothing but the standard library and its five
        siblings. Same rule as ``tone``: a ring speaker (a slot a profile
        invented, with no map element behind it) never carries one, because a
        mark is something a builder puts on a speaker they placed.
        """
        return self._edge_of(slot, "crossover")

    def crossover_high(self, slot):
        """This slot's *second* crossover edge, 0.0 when it has none.

        A second edge next to a positive first one is a band: the speaker keeps
        the middle (a mid cabinet, the third speaker of a three-way room) and
        the room's own reader builds it from the pair. Read here only as the
        number the map wrote, exactly like the first edge, so the renderer's
        signature can carry both without this module knowing what a band means.
        """
        return self._edge_of(slot, "crossover_high")

    def _edge_of(self, slot, name):
        """One raw crossover attribute of one slot, 0.0 when it is unusable."""
        spec = self._specs.get(slot)
        value = getattr(spec, name, None) if spec is not None else None
        if not value:
            return 0.0
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    @property
    def max_delay_s(self):
        if not self._specs:
            return 0.0
        return max(spec.delay_ms for spec in self._specs.values()) / 1000.0

    def distance_from(self, listener, slot):
        """Listener distance to a slot, or None when the slot is unplaced."""
        position = self.position(slot)
        if position is None or listener is None:
            return None
        return sqrt(sum((float(listener[i]) - position[i]) ** 2 for i in range(3)))
