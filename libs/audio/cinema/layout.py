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
    """

    __slots__ = ("slot", "position", "level", "delay_ms", "name")

    def __init__(self, slot, position, level=1.0, delay_ms=0.0, name=None):
        self.slot = str(slot or AUTO_SLOT).strip().lower()
        self.position = (float(position[0]), float(position[1]), float(position[2]))
        self.level = max(0.0, min(4.0, float(level)))
        self.delay_ms = max(0.0, min(100.0, float(delay_ms)))
        self.name = name

    def __repr__(self):
        return (f"CinemaSpeakerSpec({self.slot!r}, level={self.level}, "
                f"delay_ms={self.delay_ms})")


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
            slot = getattr(spec, "slot", None)
            position = getattr(spec, "position", None)
            if slot is None or position is None:
                continue
            try:
                resolved = (float(position[0]), float(position[1]), float(position[2]))
            except (TypeError, ValueError, IndexError):
                continue
            if slot == AUTO_SLOT:
                slot = slot_for_bearing(bearing_from(self.anchor, resolved))
            if slot not in SLOT_ORDER:
                continue
            yield CinemaSpeakerSpec(slot, resolved, getattr(spec, "level", 1.0),
                                    getattr(spec, "delay_ms", 0.0),
                                    getattr(spec, "name", None))

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
