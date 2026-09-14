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

from .layout import SLOT_ORDER

DEFAULT_PROFILE = "front_only"

# Slots that keep their authored weights regardless of equal-power scaling.
# The screen wall is where the audience faces and where the stereo image
# must survive untouched; only the added room speakers are rebalanced.
IMAGE_SLOTS = ("front_l", "front_r")


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
        """Slots sharing the equal-power budget rather than the author's level."""
        if not self.protect_image:
            return self.slots
        return tuple(slot for slot in self.slots if slot not in IMAGE_SLOTS)

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
        },
        equal_power_surrounds=True,
    ),
    # For a mono programme there is no image to protect, so every speaker
    # plays the same mid. The front pair keeps full weight because that is
    # where the audience faces; the room speakers are equal-power trimmed.
    "mono_spread": CinemaProfile(
        "mono_spread",
        "Mono spread around the room",
        {slot: (0.5, 0.5) for slot in
         ("front_l", "front_c", "front_r", "side_l", "side_r", "rear_l", "rear_r")},
        equal_power_surrounds=True,
        protect_image=False,
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
