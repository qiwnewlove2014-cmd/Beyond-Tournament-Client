"""Resolve a builder's speakers into a room, however badly they were placed.

A cinema room is authored by hand, in the map editor, by a human who is
standing in the room. That means every assumption we could make about the
data is wrong somewhere: someone labels the left speaker ``right``, someone
drops seven speakers in a heap near the cabinet, someone forgets the right
wall and only builds the left one, someone rotates the whole layout ninety
degrees because the map's screen wall faces a different way than the engine's
+Y axis, someone leaves every speaker on ``auto`` because they do not want to
think about channels at all.

None of those may produce a silent jukebox, a broken stereo image, or two
speakers playing identical content. So this module never trusts a label
without checking it against the geometry, and it never trusts the geometry
without asking whether the room could be rotated:

    measure   every speaker's bearing from the anchor
    orient    if the labelled speakers agree on one rotation, apply it
              (that is what makes a rotated room snap back into place)
    assign    each speaker to a slot -- its label when the geometry is
              compatible with it, the geometry when it is not, the nearest
              remaining slot when several speakers want the same one
    pair      keep only mirrored pairs for the walls and the rear
    profile   pick the richest profile the surviving slots can honestly
              reproduce (and ``front_only``, the bit-exact parity profile,
              when the room is just a left and a right speaker)

The result is either a usable room or ``None``. ``None`` is a real answer:
the caller then plays the plain two-source jukebox, which is exactly the
shipped behaviour, so a misconfigured room degrades into the audio the
project already had rather than into silence.
"""

from math import sqrt

from .layout import (AUTO_SLOT, IDEAL_BEARING, SLOT_ORDER, SLOT_PAIRS,
                     CinemaSpeakerSpec, bearing_from, coerce_spec,
                     slot_for_bearing)

# Speakers built for one cabinet are expected to live inside the cabinet's
# own audible radius. Matches the jukebox pair's max distance so a room never
# reaches further than the plain playback it replaces.
ROOM_RADIUS = 40.0

# How far a speaker's slot may sit from its measured bearing before the
# geometry wins the argument. Wide on purpose: a builder placing a wall
# speaker by eye is not going to hit 90 degrees exactly, and the profile only
# cares which wall it is on.
TOLERANCE_DEG = 60.0

# Below this bearing the measurement is too close to the centre line to
# overrule a label: a speaker typed as ``front_c`` and standing 12 degrees off
# axis is a centre speaker, not a mislabelled side speaker.
DECISIVE_DEG = 20.0

# Two speakers closer together than this are not two speakers, they are one
# speaker with a comb filter. A room that has them stacked is a room nobody
# can place, so the extras are dropped rather than fed different channels.
MIN_SEPARATION = 2.0

MAX_ROOM_SPEAKERS = 12

# Requested-profile sentinel: let the room's own slots choose.
AUTO_PROFILE = "auto"


def angular_delta(first, second):
    """Signed shortest angle from ``first`` to ``second``, in degrees."""
    return (float(second) - float(first) + 180.0) % 360.0 - 180.0


def circular_mean(angles):
    """Mean of angles in degrees, safe across the +/-180 seam."""
    from math import atan2, cos, degrees, radians, sin
    if not angles:
        return 0.0
    total_x = sum(cos(radians(float(a))) for a in angles)
    total_y = sum(sin(radians(float(a))) for a in angles)
    if total_x == 0.0 and total_y == 0.0:
        # Exactly opposed measurements: the rotation is unknowable, so the
        # only safe reading is "the room faces the way the engine says".
        return 0.0
    return degrees(atan2(total_y, total_x))


def rebase(bearing, yaw):
    """A bearing measured against the screen wall rather than the engine axis."""
    return angular_delta(yaw, bearing)


class PlacedSpeaker:
    """One speaker after it earned a slot, with the story of how it got it."""

    __slots__ = ("slot", "spec", "declared", "source", "note")

    def __init__(self, slot, spec, declared=None, source="label", note=""):
        self.slot = slot
        self.spec = spec
        self.declared = declared
        self.source = source          # label | geometry | floor | mirrored
        self.note = note

    @property
    def position(self):
        return self.spec.position

    @property
    def name(self):
        return self.spec.name

    def __repr__(self):
        return f"PlacedSpeaker({self.slot!r} via {self.source}, {self.spec!r})"


def auto_profile(slots):
    """The richest profile a set of slots can reproduce, or None.

    ``None`` means the room cannot carry a stereo programme at all (no left
    and right front speaker), so cinema is not the right answer for it.
    """
    slots = set(slots)
    if "front_l" not in slots or "front_r" not in slots:
        return None
    if "rear_l" in slots and "rear_r" in slots:
        return "theatre"
    if "side_l" in slots and "side_r" in slots:
        return "surround"
    if "front_c" in slots:
        return "front_stage"
    return "front_only"


class RoomPlacement:
    """A resolved room: which slot each speaker took, and what it cost.

    ``warnings`` is not decoration. A room that had to overrule a label or
    drop an unpaired wall speaker is a room whose map data is wrong, and the
    only way anyone ever fixes that is by being told; the debug overlay and
    the tests both read this list.
    """

    __slots__ = ("anchor", "yaw_deg", "speakers", "profile_name", "warnings",
                 "dropped", "candidates")

    def __init__(self, anchor, yaw_deg, speakers, profile_name, warnings,
                 dropped, candidates):
        self.anchor = anchor
        self.yaw_deg = yaw_deg
        self.speakers = speakers
        self.profile_name = profile_name
        self.warnings = tuple(warnings)
        self.dropped = tuple(dropped)
        self.candidates = candidates

    @property
    def slots(self):
        return tuple(slot for slot in SLOT_ORDER if slot in self.speakers)

    @property
    def specs(self):
        """Specs for the renderer, in room order."""
        return tuple(self.speakers[slot].spec for slot in self.slots)

    def slot_of(self, name):
        """Reverse lookup for tests and the overlay: which slot a speaker got."""
        for slot, speaker in self.speakers.items():
            if speaker.name == name:
                return slot
        return None

    def summary(self):
        parts = [f"{slot}:{self.speakers[slot].source}" for slot in self.slots]
        text = f"{self.profile_name} ({', '.join(parts)})"
        if self.warnings:
            text += f" warnings={len(self.warnings)}"
        return text

    def __repr__(self):
        return (f"RoomPlacement({self.profile_name!r}, yaw={self.yaw_deg:.1f}, "
                f"slots={list(self.slots)}, warnings={len(self.warnings)})")


def _room_yaw(speakers, anchor, explicit=None, tolerance=TOLERANCE_DEG):
    """The room's screen direction, if the labels agree on one.

    A single labelled speaker says nothing about rotation (any yaw explains
    one measurement), and two labels that disagree by more than the tolerance
    were probably misplaced rather than rotated, so both cases fall back to
    the engine axis and let the per-speaker geometry check sort it out.
    """
    if explicit is not None:
        return float(explicit) % 360.0
    labelled = [s for s in speakers if s.declared in IDEAL_BEARING]
    if len(labelled) < 2:
        return 0.0
    deltas = [angular_delta(IDEAL_BEARING[s.declared], bearing_from(anchor, s.position))
              for s in labelled]
    mean = circular_mean(deltas)
    if any(abs(angular_delta(mean, d)) > tolerance for d in deltas):
        return 0.0
    return mean % 360.0


def _name_of(speaker):
    return str(speaker.name or speaker.declared or "speaker")


def _free_slot_for(bearing, taken):
    """The unclaimed slot whose ideal bearing is closest to ``bearing``."""
    best = None
    best_delta = None
    for slot in SLOT_ORDER:
        if slot in taken:
            continue
        delta = abs(angular_delta(bearing, IDEAL_BEARING[slot]))
        if best_delta is None or delta < best_delta:
            best, best_delta = slot, delta
    return best


def _pair_up(placed, warnings, dropped):
    """Keep mirrored walls; drop a lone wall or rear rather than steer the room."""
    for left, right in SLOT_PAIRS:
        if (left in placed) == (right in placed):
            continue
        lonely = left if left in placed else right
        speaker = placed.pop(lonely)
        dropped.append(speaker.spec)
        warnings.append(
            f"{_name_of(speaker.spec)}: {lonely} has no partner, so the pair "
            f"is ignored (the room falls back to the speakers it has)"
        )
    # The front pair is the stereo image. One front speaker cannot carry it,
    # and pretending otherwise would fold the programme into one side of the
    # room, which is worse than the plain jukebox the caller falls back to.
    present = [slot for slot in ("front_l", "front_r") if slot in placed]
    if len(present) == 1:
        lonely = present[0]
        speaker = placed.pop(lonely)
        dropped.append(speaker.spec)
        warnings.append(
            f"{_name_of(speaker.spec)}: only one front speaker ({lonely}); "
            f"a stereo image needs both, so this room is not used"
        )
    return placed


def _report(report, message):
    """Record why a room was refused, for callers that have to explain it.

    A refusal with no reason is the single most frustrating thing about map
    data: something the builder cannot see silently turns their room into a
    plain jukebox. ``report`` is an optional list the resolver appends to, so
    menus and logs can say which wall is missing instead of "no room".
    """
    if report is not None:
        report.append(str(message))


def resolve_room(speakers, anchor, *, radius=ROOM_RADIUS, room=None,
                 tolerance_deg=TOLERANCE_DEG, yaw=None, profile=AUTO_PROFILE,
                 max_speakers=MAX_ROOM_SPEAKERS, report=None):
    """Resolve speakers into a room, or return None when there is no room.

    ``room`` filters by the speaker's own room id (a cabinet id or a zone
    name). Speakers without a room id belong to whichever cabinet they are
    near, so a builder who never touches the field still gets a working room.

    ``report`` is an optional list that receives the reason for a refusal (see
    :func:`_report`); passing it changes nothing about the decision.
    """
    anchor = (float(anchor[0]), float(anchor[1]), float(anchor[2]))
    candidates = []
    for raw in speakers or ():
        speaker = coerce_spec(raw)
        if speaker is None:
            continue
        if not _in_room(speaker, anchor, radius, room):
            continue
        candidates.append(speaker)
    if not candidates:
        _report(report, f"no cinema speaker within {float(radius):.0f}m of it")
        return None

    yaw = _room_yaw(candidates, anchor, yaw, tolerance_deg)
    warnings = []
    dropped = []
    placed = {}
    for speaker in _ordered(candidates):
        crowding = _crowding(speaker, placed)
        if crowding is not None:
            dropped.append(speaker)
            warnings.append(
                f"{_name_of(speaker)}: only {_distance(speaker.position, crowding.spec.position):.1f}m "
                f"from {crowding.slot}, too close to be its own speaker; ignored"
            )
            continue
        measured = rebase(bearing_from(anchor, speaker.position), yaw)
        geometry = slot_for_bearing(measured)
        declared = speaker.declared if speaker.declared in IDEAL_BEARING else None
        slot, source, note = _choose_slot(speaker, measured, geometry, declared,
                                          placed, tolerance_deg)
        if slot is None:
            dropped.append(speaker)
            continue
        if note:
            warnings.append(note)
        # The spec carries the resolved slot from here on: the renderer reads
        # ``plan.specs`` directly, and a spec still holding its old label
        # would put the repaired room back the way the map had it wrong.
        speaker.slot = slot
        placed[slot] = PlacedSpeaker(slot, speaker, declared, source, note)

    placed = _pair_up(placed, warnings, dropped)
    # A stereo front pair is the one thing a room cannot do without, whatever
    # profile was requested: without it there is no image to protect and the
    # plain jukebox pair does the job better.
    if "front_l" not in placed or "front_r" not in placed:
        _report(report, f"{len(candidates)} speaker(s) here, but no stereo front pair "
                        f"(needs a front_l and a front_r)")
        for note in warnings[:2]:
            _report(report, note)
        return None
    profile_name = (profile if profile and str(profile).strip().lower() != AUTO_PROFILE
                    else None) or auto_profile(placed.keys())
    if profile_name is None:
        _report(report, f"no profile reproduces the slots that survived ({', '.join(placed)})")
        return None
    overflow = [slot for slot in SLOT_ORDER if slot in placed][max_speakers:]
    for slot in overflow:
        warnings.append(f"{slot}: beyond the {max_speakers}-speaker limit, ignored")
        dropped.append(placed.pop(slot).spec)
    return RoomPlacement(anchor, yaw, placed, profile_name, warnings, dropped,
                         candidates)


def nearest_anchor(position, anchors, limit=None):
    """``(index, distance)`` of the closest anchor, or None when out of range.

    Distances are returned rather than just the winner so a caller can compare
    a speaker against every cabinet without measuring it twice.
    """
    best_index = None
    best_distance = None
    for index, anchor in enumerate(anchors or ()):
        distance = _distance(position, anchor)
        if limit is not None and distance > limit:
            continue
        if best_distance is None or distance < best_distance:
            best_index, best_distance = index, distance
    if best_index is None:
        return None
    return (best_index, best_distance)


def exclusive_speakers(speakers, anchor, rivals=(), radius=ROOM_RADIUS):
    """Keep only the speakers that belong to this cabinet and no other.

    Rooms on one map are close together -- two cabinets in neighbouring rooms
    are easily inside each other's radius -- and a speaker claimed by both
    would play two songs at once. A speaker therefore belongs to the cabinet
    it stands closest to, and a speaker that already names its room is left
    alone: a builder who spelled the room out wins over the geometry.

    Ties go to the cabinet asking, so a symmetric layout stays symmetric.
    """
    rivals = [tuple(float(value) for value in rival) for rival in rivals or ()]
    kept = []
    for raw in speakers or ():
        speaker = coerce_spec(raw)
        if speaker is None:
            continue
        if speaker.room:
            kept.append(raw)
            continue
        mine = _distance(anchor, speaker.position)
        if any(_distance(rival, speaker.position) < mine for rival in rivals):
            continue
        if mine > radius:
            continue
        kept.append(raw)
    return kept


def _in_room(speaker, anchor, radius, room):
    if speaker.room and room is not None:
        return speaker.room == str(room)
    if speaker.room and room is None:
        # A speaker assigned to some other room may still be this cabinet's
        # when it is standing right next to it; distance decides.
        return _distance(anchor, speaker.position) <= radius
    return _distance(anchor, speaker.position) <= radius


def _distance(first, second):
    return sqrt(sum((float(first[i]) - float(second[i])) ** 2 for i in range(3)))


def _crowding(speaker, placed, min_separation=MIN_SEPARATION):
    """An already-placed speaker too close to this one, or None."""
    for other in placed.values():
        if _distance(speaker.position, other.spec.position) < min_separation:
            return other
    return None


def _slot_side(slot):
    """Which side of the audience a slot belongs to."""
    if slot == "front_c" or slot is None:
        return "centre"
    return "left" if slot.endswith("_l") else "right"


def _ordered(speakers):
    """Labelled speakers first, then ``auto`` ones, each in a stable order.

    Order decides who wins a contested slot, and it must not depend on the
    iteration order of a network payload: a room that resolves differently on
    two clients is a room that sounds different on two clients.
    """
    return sorted(
        speakers,
        key=lambda s: (0 if s.declared in IDEAL_BEARING else 1,
                       str(s.name or ""), s.position),
    )


def _mislabelled(declared, measured, geometry, tolerance_deg):
    """Why the room should overrule a label, or "" when the label stands.

    Two things a label can get wrong, in order of how much they hurt:

    * the side -- a left speaker labelled right mirrors the whole stereo
      image, which is the one placement error an audience notices instantly;
    * the rack -- a speaker labelled for the screen wall but standing behind
      the audience, which only matters once the difference is bigger than the
      tolerance (so a centre speaker typed a few degrees off axis, or a wall
      speaker dropped in by eye, keeps the role the builder gave it).
    """
    if geometry == declared:
        return ""
    if abs(measured) > DECISIVE_DEG and _slot_side(geometry) != _slot_side(declared):
        return "side"
    if abs(angular_delta(measured, IDEAL_BEARING[declared])) > tolerance_deg:
        return "rack"
    return ""


def _choose_slot(speaker, measured, geometry, declared, placed, tolerance_deg):
    """Pick this speaker's slot, preferring its label when the room agrees."""
    name = _name_of(speaker)
    if declared is not None:
        if declared not in placed:
            why = _mislabelled(declared, measured, geometry, tolerance_deg)
            if why:
                if geometry not in placed:
                    detail = ("it is on the other side of the room" if why == "side"
                              else "it stands in a different part of the room")
                    return (geometry, "geometry",
                            f"{name}: labelled {declared} but stands at {measured:.0f}deg "
                            f"({geometry}) -- {detail}; trusting the position, not the label")
                return (None, "dropped",
                        f"{name}: labelled {declared} and {geometry} is already taken")
            return (declared, "label", None)
        # The label is already claimed: the second speaker goes where it
        # actually stands, and only if that slot is free.
        if geometry not in placed:
            return (geometry, "geometry",
                    f"{name}: {declared} was already labelled, so it takes {geometry} by position")
        slot = _free_slot_for(measured, placed)
        if slot is None:
            return (None, "dropped", f"{name}: no free slot near {measured:.0f}deg")
        return (slot, "floor", f"{name}: duplicate {declared}, moved to the nearest free slot {slot}")

    if geometry not in placed:
        return (geometry, "geometry", None)
    slot = _free_slot_for(measured, placed)
    if slot is None:
        return (None, "dropped", f"{name}: no free slot near {measured:.0f}deg")
    return (slot, "floor", f"{name}: {geometry} was taken, moved to the nearest free slot {slot}")


class RoomPlan:
    """What a cabinet should play through: a profile and where the speakers are.

    ``specs`` is None when the room exists only as a geometric ring behind
    the cabinet (a server-marked cabinet in a map nobody has placed speakers
    in yet); the renderer then falls back to its default ring layout.

    ``fill`` says whether a slot the profile wants but the map has no speaker
    for may be invented at its geometric ring position. For a room built from
    the map's own speakers that is ``False``: a builder who placed four
    speakers has a four-speaker room, and hearing a fifth from a wall nobody
    put it in is exactly the sort of thing that makes placing speakers look
    broken. It stays ``True`` when the profile was *asked for* (a server
    ``cinema_profile``, or a map with no speakers placed at all), because
    then the ring is the only thing there is to play.
    """

    __slots__ = ("profile", "specs", "placement", "fill")

    def __init__(self, profile, specs=None, placement=None, fill=False):
        self.profile = profile
        self.specs = specs
        self.placement = placement
        self.fill = bool(fill)

    @property
    def profile_name(self):
        return self.profile

    @property
    def warnings(self):
        return self.placement.warnings if self.placement is not None else ()

    def summary(self):
        """Human-readable room description for menus and logs."""
        if self.placement is None:
            return f"{self.profile} (built-in ring layout, no placed speakers)"
        return f"{self.profile}: {self.placement.summary()}"

    def __repr__(self):
        count = len(self.specs) if self.specs is not None else "ring"
        return f"RoomPlan({self.profile!r}, speakers={count}, fill={self.fill})"


def room_profile(placement, requested=None):
    """The profile a room should play, honouring an explicit request.

    An unknown request (a map attribute from an older or newer build) falls
    back to the room's own reading rather than to silence.
    """
    if placement is None:
        return None
    if requested and str(requested).strip().lower() not in ("", AUTO_PROFILE):
        return str(requested).strip().lower()
    return placement.profile_name
