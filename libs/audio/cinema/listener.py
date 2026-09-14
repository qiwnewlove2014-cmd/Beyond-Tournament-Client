"""Where the listener is looking, and where each speaker is aimed.

The room is anchored in the world, not to the listener: every cinema speaker
is a positioned OpenAL source, so the engine already pans the room correctly
when the player turns around -- facing the screen puts the screen wall in
front, spinning on the spot swings the rear pair behind them and the walls
past their ears. Nothing in this module replaces that; it exists because
"the engine handles it" is not something a room can be debugged or tested
against.

Two questions this answers directly:

    * ``ListenerPose.quadrant_to(point)`` / ``is_facing(point)`` -- is the
      listener facing that speaker, standing beside it, or standing with
      their back to it? Derived from the real OpenAL listener orientation the
      game already sets from the player's body every frame.
    * ``cone_gain(...)`` -- given a speaker that was built with an aim
      (``aim_yaw``) and a coverage cone, how much of it reaches a listener
      standing *there*? A rear wall speaker aimed at the seating area and a
      speaker aimed at the wall behind it are physically different objects,
      and a room full of unaimed speakers cannot tell them apart.

``aim_yaw`` uses the same convention as the rest of the map data and the
megaphone speakers: 0 is +Y, +90 is +X, so an aim of 270 points along -X.
"""

from math import asin, atan2, cos, degrees, radians, sin, sqrt

# Below this many degrees off-axis a speaker counts as dead ahead / dead
# behind; a panning calculation that flips sides while someone breathes is
# worse than one that reports the centre line.
CENTRE_DEADZONE_DEG = 15.0


def _unit(vector):
    length = sqrt(sum(float(value) ** 2 for value in vector))
    if length <= 1e-9:
        return None
    return tuple(float(value) / length for value in vector)


def _dot(first, second):
    return sum(float(a) * float(b) for a, b in zip(first, second))


def _cross(first, second):
    ax, ay, az = first
    bx, by, bz = second
    return (ay * bz - az * by, az * bx - ax * bz, ax * by - ay * bx)


def _clamp(value, low=-1.0, high=1.0):
    return low if value < low else (high if value > high else value)


def aim_vector(aim_yaw, aim_pitch=0.0):
    """Unit vector for a speaker aim (0 = +Y, +90 = +X), matching the map data."""
    yaw = radians(float(aim_yaw))
    pitch = radians(float(aim_pitch))
    return (sin(yaw) * cos(pitch), cos(yaw) * cos(pitch), sin(pitch))


def cone_gain(listener, speaker_position, aim_yaw, inner=None, outer=None,
              outer_gain=0.2, aim_pitch=0.0):
    """How loud a listener hears an aimed speaker, 0.0-1.0.

    An unaimed speaker returns 1.0: coverage is the room's job, not the
    speaker's, and a cinema that fades out when the player turns their head
    is a cinema with holes in it. This is only for speakers a builder chose
    to aim, which is the case worth being able to model.
    """
    if aim_yaw is None or listener is None or speaker_position is None:
        return 1.0
    to_listener = (float(listener[0]) - float(speaker_position[0]),
                   float(listener[1]) - float(speaker_position[1]),
                   float(listener[2]) - float(speaker_position[2]))
    direction = _unit(to_listener)
    if direction is None:
        return 1.0
    axis = _unit(aim_vector(aim_yaw, aim_pitch))
    if axis is None:
        return 1.0
    angle = degrees(_acos(_clamp(_dot(axis, direction))))
    # Cone angles are total opening angles (the OpenAL convention), so the
    # useful comparison is against their half-width.
    half_inner = float(inner) / 2.0 if inner is not None else None
    half_outer = float(outer) / 2.0 if outer is not None else None
    if half_inner is None and half_outer is None:
        return 1.0
    if half_inner is None:
        half_inner = half_outer
    if half_outer is None or half_outer <= half_inner:
        return 1.0 if angle <= half_inner else float(outer_gain)
    if angle <= half_inner:
        return 1.0
    if angle >= half_outer:
        return float(outer_gain)
    span = half_outer - half_inner
    return 1.0 - (1.0 - float(outer_gain)) * (angle - half_inner) / max(1e-6, span)


def _acos(value):
    from math import acos
    return acos(value)


def distance_gain(position, listener, reference_distance, max_distance):
    """The room's own linear fade, measured at one speaker.

    Full volume within ``reference_distance`` of the listener, silent at
    ``max_distance``. One function for the whole feature on purpose: the room
    applies this to every song frame, and a live instrument played at a
    speaker has to be shaped by exactly the same numbers, or the same speaker
    is louder for the band than it is for the song.
    """
    if listener is None or position is None:
        return 1.0
    distance = sqrt(sum((float(listener[i]) - float(position[i])) ** 2
                        for i in range(3)))
    reference_distance = float(reference_distance)
    max_distance = float(max_distance)
    if distance <= reference_distance:
        return 1.0
    if distance >= max_distance:
        return 0.0
    span = max(0.0001, max_distance - reference_distance)
    return max(0.0, 1.0 - (distance - reference_distance) / span)


def speaker_aim_gain(listener, position, spec):
    """How much of an aimed speaker reaches a listener standing at ``listener``.

    1.0 for a speaker with no aim -- omnidirectional is the room's default and
    the coverage is the room's job. Shared with the bank for the same reason
    as :func:`distance_gain`: a live note and the song must agree.
    """
    if listener is None or spec is None or not getattr(spec, "has_cone", False):
        return 1.0
    return cone_gain(listener, position, spec.aim_yaw, spec.cone_inner,
                     spec.cone_outer,
                     spec.cone_outer_gain if spec.cone_outer_gain is not None
                     else 0.0)


class ListenerPose:
    """The listener's head: forward/up vectors, position, and what it faces.

    Accepts either the raw OpenAL orientation the game sets (``(fx, fy, fz,
    ux, uy, uz)``) or the ``(horizontal, pitch, lean)`` degrees tuple the
    audio manager's setter takes, so it can be built from live state or from
    six numbers in a test.
    """

    __slots__ = ("_forward", "_up", "_right", "position")

    def __init__(self, orientation=None, position=None):
        forward, up = self._split(orientation)
        self._forward = _unit(forward) or (0.0, 1.0, 0.0)
        up = _unit(up) or (0.0, 0.0, 1.0)
        right = _unit(_cross(self._forward, up))
        if right is None:
            # Facing straight up or down: any perpendicular is as good as
            # another, and the listener's own body is the sane tie-breaker.
            right = _unit(_cross(self._forward, (0.0, 0.0, 1.0))) or (1.0, 0.0, 0.0)
        self._right = right
        self._up = _unit(_cross(right, self._forward)) or up
        self.position = None if position is None else tuple(float(v) for v in position)

    @staticmethod
    def _split(orientation):
        if orientation is None:
            return (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)
        values = [float(value) for value in orientation]
        if len(values) >= 6:
            return values[:3], values[3:6]
        if len(values) >= 3:
            # (horizontal, pitch, lean) degrees, as make_orientation consumes.
            horizontal, pitch, lean = values[:3]
            horizontal = radians(horizontal)
            pitch = radians(pitch)
            lean = radians(lean)
            forward = (sin(horizontal), cos(horizontal), sin(pitch) * cos(lean))
            up = (-sin(pitch) * sin(horizontal) + sin(lean),
                  -sin(pitch) * cos(horizontal) + sin(lean),
                  cos(pitch) * cos(lean))
            return forward, up
        return (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)

    @classmethod
    def from_audio_manager(cls, audio):
        """The live pose, or None when there is no audio manager to read."""
        if audio is None:
            return None
        listener = getattr(audio, "listener", None)
        try:
            orientation = tuple(listener.orientation)
            position = tuple(listener.position)
        except Exception:
            try:
                orientation = tuple(audio.orientation)
                position = tuple(audio.position)
            except Exception:
                return None
        return cls(orientation, position)

    @property
    def forward(self):
        return self._forward

    @property
    def right(self):
        return self._right

    @property
    def up(self):
        return self._up

    @property
    def yaw_deg(self):
        """Compass yaw of the head: 0 = +Y, +90 = +X."""
        return degrees(atan2(self._forward[0], self._forward[1])) % 360.0

    def relative(self, point):
        """``(bearing, elevation, distance)`` of a world point from the head.

        Bearing is 0 dead ahead, positive to the listener's right, +/-180
        behind -- the same convention the room's bearings use, so a facing
        test and a placement both read the same way.
        """
        if point is None or self.position is None:
            return None
        delta = (float(point[0]) - self.position[0],
                 float(point[1]) - self.position[1],
                 float(point[2]) - self.position[2])
        distance = sqrt(_dot(delta, delta))
        direction = _unit(delta)
        if direction is None:
            return (0.0, 0.0, 0.0)
        bearing = degrees(atan2(_dot(direction, self._right), _dot(direction, self._forward)))
        elevation = degrees(asin(_clamp(_dot(direction, self._up))))
        return (bearing, elevation, distance)

    def bearing_to(self, point):
        relative = self.relative(point)
        return None if relative is None else relative[0]

    def side_to(self, point, deadzone=CENTRE_DEADZONE_DEG):
        """``"left"``, ``"right"`` or ``"centre"`` (the speaker's world side)."""
        bearing = self.bearing_to(point)
        if bearing is None:
            return None
        if abs(bearing) <= deadzone:
            return "centre"
        return "right" if bearing > 0 else "left"

    def quadrant_to(self, point, deadzone=CENTRE_DEADZONE_DEG):
        """An eight-way reading: ``"front-left"``, ``"rear"``, ``"right"``..."""
        bearing = self.bearing_to(point)
        if bearing is None:
            return None
        magnitude = abs(bearing)
        if magnitude <= deadzone:
            return "front" if abs(bearing) < 90 else "rear"
        if magnitude >= 180.0 - deadzone:
            return "rear"
        if abs(magnitude - 90.0) <= deadzone:
            side = "right" if bearing > 0 else "left"
            return side
        if magnitude < 90.0:
            side = "right" if bearing > 0 else "left"
            return f"front-{side}"
        if magnitude > 90.0:
            side = "right" if bearing > 0 else "left"
            return f"rear-{side}"
        return "front" if abs(bearing) < 90 else "rear"

    def is_facing(self, point, arc=90.0):
        """True when the point sits inside the arc in front of the listener."""
        bearing = self.bearing_to(point)
        if bearing is None:
            return False
        return abs(bearing) <= float(arc) / 2.0 + CENTRE_DEADZONE_DEG

    def has_back_to(self, point, arc=90.0):
        bearing = self.bearing_to(point)
        if bearing is None:
            return False
        return abs(bearing) >= 180.0 - (float(arc) / 2.0 + CENTRE_DEADZONE_DEG)

    def describe(self, point):
        """A short human reading, for the debug overlay and for test failure
        messages that a person can actually act on."""
        relative = self.relative(point)
        if relative is None:
            return "unknown"
        bearing, elevation, distance = relative
        quadrant = self.quadrant_to(point)
        tilt = "level"
        if elevation > 20.0:
            tilt = "above"
        elif elevation < -20.0:
            tilt = "below"
        return f"{quadrant} {distance:.1f}m {tilt} ({bearing:+.0f}deg)"

    def __repr__(self):
        return f"ListenerPose(yaw={self.yaw_deg:.0f}, position={self.position})"


def facing_report(pose, placement, *, arc=90.0):
    """``[(slot, quadrant, in_front, distance), ...]`` for a resolved room.

    This is the sentence the system can say about a room: which speakers the
    listener is facing, which are behind them, and which sides are closest.
    It is what a ``/cinema`` debug readout is built from, and what makes the
    turn-around behaviour testable without an audio device.
    """
    if pose is None or placement is None:
        return ()
    report = []
    for slot in placement.slots:
        position = placement.speakers[slot].position
        relative = pose.relative(position)
        if relative is None:
            report.append((slot, "unknown", False, None))
            continue
        report.append((slot, pose.quadrant_to(position),
                       pose.is_facing(position, arc), relative[2]))
    return tuple(report)
