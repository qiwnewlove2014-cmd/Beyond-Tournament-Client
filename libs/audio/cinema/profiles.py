"""Cinema profiles: the per-slot Mid/Side weights that shape a room.

A profile weight ``(gain_l, gain_r)`` is applied to a speaker as
``left * gain_l + right * gain_r``, producing that one speaker's mono feed.
The vocabulary that matters:

    (1, 0)      exactly the left channel
    (0, 1)      exactly the right channel
    (0.5, 0.5)  the mid   -- mono-safe centre
    (0.5, -0.5) the side  -- the difference that carries the stereo image

``front_only`` exists to be boring on purpose: it is the pair of plain
two-source jukebox speakers expressed in this vocabulary, so it must render
bit-identical audio to today's playback and gives the whole system a
regression baseline to prove itself against.

No profile gives two slots the same weights. Two speakers playing the same
content at different distances comb filter at the listener, which is the
one failure mode that ruins a multi-speaker room faster than anything else.
That is exactly why the sides carry the difference and the rears carry
opposite-leaning ambience instead of a copy of the front.
"""

from math import sqrt

from .layout import LAYER_SLOTS, SLOT_ORDER

DEFAULT_PROFILE = "front_only"

# Slots that keep their authored weights regardless of equal-power scaling.
# The screen wall is where the audience faces and where the stereo image
# must survive untouched; only the added room speakers are rebalanced.
IMAGE_SLOTS = ("front_l", "front_r")

# The height pair's weights, in one home because the roof layer has to sound
# like the same layer whichever shape a cabinet plays.
#
# The mid, leaning a little to each side: never the same pair for both
# speakers (two speakers playing identical content at different distances comb
# filter at the listener -- the whole point of the weights), and never a copy
# of ``front_c``'s (0.5, 0.5) either, for the same reason. The lean is a little
# deeper than the first cut (0.1) because a roof that is *only* the mid is a
# point source above the audience: with a touch of the difference each side, the
# pair arrives as a layer of air over the room instead of a speaker you can
# point at. They are deliberately **not** image slots: the roof layer is part of
# the room, not part of the screen wall, so it takes the layer's share with the
# walls and the rears, and a bigger room does not get a louder front.
HEIGHT_WEIGHTS = {"height_l": (0.42, 0.58), "height_r": (0.58, 0.42)}

# The wide pair: the front channels broadened towards the walls, mostly their
# own side and a little of the other. The wider the split the further out the
# stage's own channel sits -- this is the layer that does the widening, and
# ``tools/cinema_layer_balance_sim.py`` is what says how far to take it: at
# 0.9/0.1 the left channel's energy sits 58.7% of the way to the side walls at a
# seat behind the cabinet, against 55.4% in a room without the pair. It is a
# layer over a shape, so it is never an image slot and never enters the
# authored shape's scale.
WIDE_WEIGHTS = {"wide_l": (0.9, 0.1), "wide_r": (0.1, 0.9)}

# The ceiling centre: the mid with a touch of the difference, which makes the
# top of the room wide instead of a point, and keeps it off every other slot's
# weights -- ``front_c`` is the exact mid, and two speakers playing identical
# content at different distances comb filter at the listener.
TOP_WEIGHTS = {"top_c": (0.55, 0.45)}

# One dict for the whole optional layer, so a profile adds it in one spread.
LAYER_WEIGHTS = {**WIDE_WEIGHTS, **HEIGHT_WEIGHTS, **TOP_WEIGHTS}

# How much power the optional layers may add, as a fraction of the room's own
# authored shape -- and the numbers behind it, measured with
# ``tools/cinema_layer_balance_sim.py --sweep`` on the twelve-speaker theatre at
# a seat 5 m behind the cabinet (the seven-speaker room is the 0.00 row):
#
#     layer share   power   stage width   height   side signal   front share
#     0.00 (walls)  2.218     55.39%       0.00%    1.058         84.06%
#     0.15          2.307     56.50%       1.86%    1.010         80.81%
#     0.35          2.426     57.83%       4.14%    0.954         76.85%
#     0.50          2.515     58.73%       5.70%    0.917         74.13%
#     0.70          2.634     59.81%       7.62%    0.874         70.79%
#     1.00          2.812     61.23%      10.20%    0.819         66.30%
#
# Half the shape is where the room is clearly wider and taller -- the left and
# right channels sit 3.3 points further out, the overhead layer is 5.7% of the
# room's energy, and the whole room is 0.55 dB louder than the seven-speaker
# one -- while the front pair still carries nearly three quarters of the energy.
# Past that, width keeps being bought out of the screen wall (a full share leaves
# it 66%), and a room whose front is no longer the loudest thing in it is not a
# cinema. The side signal *falls* as the share grows, on purpose: the layers are
# mid-heavy (a roof plays the middle of the programme), so this number is the
# room's mid growing, not its walls being quietened -- the walls' own gains are
# unchanged from the seven-speaker room (pinned by
# ``tests/test_cinema_layer_balance.py``).
LAYER_POWER_SHARE = 0.5



class CinemaProfile:
    """Named slot weights, optionally energy-normalised for the added speakers."""

    __slots__ = ("name", "label", "weights", "equal_power_surrounds",
                 "protect_image")

    def __init__(self, name, label, weights, equal_power_surrounds=False,
                 protect_image=True):
        self.name = name
        self.label = label
        self.weights = {slot: (float(gain_l), float(gain_r))
                        for slot, (gain_l, gain_r) in weights.items()}
        self.equal_power_surrounds = bool(equal_power_surrounds)
        # A stereo profile keeps the screen wall at its authored level no
        # matter how big the room grows: the front pair IS the stereo image.
        # A mono programme has no image to protect, so every speaker it
        # reaches shares one equal-power budget instead.
        self.protect_image = bool(protect_image)

    @property
    def slots(self):
        """Slots this profile wants, in room order."""
        return tuple(slot for slot in SLOT_ORDER if slot in self.weights)

    def weight(self, slot):
        return self.weights.get(slot)

    def surround_slots(self):
        """Slots sharing the equal-power budget rather than the author's level.

        A layer is **optional** and is not counted here: a profile's scale is
        the level its *authored shape* pays, so adding a wide pair or a roof to
        a shape must not turn down the centre and the walls of every room
        already built with that shape. The renderer pays exactly this -- it
        divides over the fed slots that are *not* layers (``router._plan_for``)
        and gives the layers `LAYER_POWER_SHARE` of that power between them --
        which is what ``tests/test_cinema_layer_balance.py`` measures.
        """
        slots = tuple(slot for slot in self.slots if slot not in LAYER_SLOTS)
        if not self.protect_image:
            return slots
        return tuple(slot for slot in slots if slot not in IMAGE_SLOTS)

    def surround_scale(self):
        """1/sqrt(n) for the added speakers, or 1.0 when there is nothing to share.

        Equal-power (rather than equal-amplitude) scaling keeps the summed
        energy roughly constant as the room grows, which is the same
        convention the megaphone talkover mixing already uses so a busy room
        never clips the master bus.
        """
        if not self.equal_power_surrounds:
            return 1.0
        count = len(self.surround_slots())
        if count <= 0:
            return 1.0
        return 1.0 / sqrt(count)

    def __repr__(self):
        return f"CinemaProfile({self.name!r}, slots={len(self.weights)})"


PROFILES = {
    # The parity profile: two speakers, the original channels, nothing added.
    "front_only": CinemaProfile(
        "front_only",
        "Front speakers only",
        {
            "front_l": (1.0, 0.0),
            "front_r": (0.0, 1.0),
        },
    ),
    # A real centre speaker. Cinema dialogue lives in the mid, and keeping it
    # in its own speaker stops the front pair from fighting over it.
    "front_stage": CinemaProfile(
        "front_stage",
        "Front left, centre and right",
        {
            "front_l": (1.0, 0.0),
            "front_c": (0.5, 0.5),
            "front_r": (0.0, 1.0),
            **LAYER_WEIGHTS,
        },
        equal_power_surrounds=True,
    ),
    # The walls carry the side signal: this is what widens the room without
    # touching the front image at all.
    "surround": CinemaProfile(
        "surround",
        "Front stage with side speakers",
        {
            "front_l": (1.0, 0.0),
            "front_c": (0.5, 0.5),
            "front_r": (0.0, 1.0),
            "side_l": (0.5, -0.5),
            "side_r": (-0.5, 0.5),
            **LAYER_WEIGHTS,
        },
        equal_power_surrounds=True,
    ),
    # The full room. The rears lean toward the OPPOSITE channel of the side
    # they stand on (rear_l carries more of the right channel): that is what
    # decorrelates them from the screen wall. A rear pair carrying its own
    # side would be the front pair again, quieter and a metre further away --
    # two correlated speakers at different distances comb filter at the
    # listener, which is the one thing that makes a room sound broken. The
    # level (a quarter of a front channel) is what keeps bass and vocals at
    # the screen and the room around the audience. Flip the two weights and
    # the rears become a mirrored copy of the front instead of ambience.
    "theatre": CinemaProfile(
        "theatre",
        "Front stage, sides and rear",
        {
            "front_l": (1.0, 0.0),
            "front_c": (0.5, 0.5),
            "front_r": (0.0, 1.0),
            "side_l": (0.5, -0.5),
            "side_r": (-0.5, 0.5),
            "rear_l": (0.15, 0.35),
            "rear_r": (0.35, 0.15),
            **LAYER_WEIGHTS,
        },
        equal_power_surrounds=True,
    ),
    # For a mono programme there is no image to protect, so every speaker
    # plays the same mid. The front pair keeps full weight because that is
    # where the audience faces; the room speakers are equal-power trimmed.
    "mono_spread": CinemaProfile(
        "mono_spread",
        "Mono spread around the room",
        {slot: (0.5, 0.5) for slot in SLOT_ORDER},
        equal_power_surrounds=True,
        protect_image=False,
    ),
    # The custom mode: exactly the speakers someone placed, each playing the
    # channel of its own position. It is a profile so the renderer, the
    # read-outs and the menus read it like every other shape, but nothing here
    # asks for a pair and nothing is filled from the ring: a room with a single
    # side speaker plays that single side speaker. A declared label is the
    # truth in this mode (the geometry never overrules it), which is what makes
    # it the one shape a lone speaker can be.
    "custom": CinemaProfile(
        "custom",
        "Exactly the speakers you place",
        {
            "front_l": (1.0, 0.0),
            "front_c": (0.5, 0.5),
            "front_r": (0.0, 1.0),
            "side_l": (0.5, -0.5),
            "side_r": (-0.5, 0.5),
            "rear_l": (0.15, 0.35),
            "rear_r": (0.35, 0.15),
            # Custom is where a builder's own layers must be heard too: the
            # mode plays what was placed, and a slot with no weight here would
            # be placed and then silently unfed.
            **LAYER_WEIGHTS,
        },
    ),
}


def profile_names():
    """Profile ids in a stable order for menus and the debug overlay."""
    return tuple(PROFILES)


def get_profile(profile):
    """Look up a profile by name, falling back to the parity profile.

    Server and builder data are untrusted here: an unknown name must play
    the plain jukebox rather than silence the room.
    """
    if isinstance(profile, CinemaProfile):
        return profile
    name = str(profile or DEFAULT_PROFILE).strip().lower()
    return PROFILES.get(name, PROFILES[DEFAULT_PROFILE])
