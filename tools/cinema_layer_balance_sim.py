"""What the optional layers do to a room's image, measured seat by seat.

A twelve-speaker theatre has three more layers than the seven-speaker room it
grew out of, and "wider and taller" has to be a number rather than a hope:
this tool builds both rooms from the real renderer (so the weights and the
equal-power share are the ones that play) and prints, for a handful of seats,
how far off centre the left and right channels are placed, how much of the
room's energy arrives from overhead, and how the front pair's own share of the
room compares.

The measurements are power sums, in the units the profile already uses:

    energy of a speaker   E = (distance gain)^2 * (gain_l^2 + gain_r^2)
    lateral position      sin(azimuth), 0 = dead ahead, +/-1 = the side walls
    stage width           sqrt(sum(E_l * sin^2) / sum(E_l)) for the left
                          channel (and the mirror for the right), averaged
    height                sum(E of the overhead layer) / sum(E)
    side ratio            sum((gain_l - gain_r)^2) / sum((gain_l + gain_r)^2)
    front share           sum(E of front_l/front_r) / sum(E)

Power sums, not phase sums, for the same reason the equal-power share is an
equal-power share: every speaker arrives with its own delay, so the room is
diffuse at the listener and the energies add while the waveforms do not.

Read the table, not just the difference: the old room is the seven wall
speakers of the same profile at the same positions, so the only differences are
the three layers and the share they are given.

``--sweep`` walks the layer power share one row at a time, which is how
``profiles.LAYER_POWER_SHARE`` was chosen -- a share is a level, and a level is
read off a measurement rather than argued about. ``--weights`` compares layer
voicings in the same room.

Run with: python tools/cinema_layer_balance_sim.py [--sweep|--weights]
"""

import os
import sys
from math import atan2, cos, radians, sin

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from libs.audio.cinema.layout import (IDEAL_BEARING, OVERHEAD_SLOTS,
                                      CinemaSpeakerSpec)
from libs.audio.cinema.listener import distance_gain
from libs.audio.cinema.profiles import PROFILES, CinemaProfile
from libs.audio.cinema.router import CinemaRenderer

ANCHOR = (0.0, 0.0, 0.0)

# The rings the server's room preset lays a speaker on, at the default 60 m
# reach: the screen wall a quarter of the way out, the walls three tenths, the
# rear four tenths, the wide pair with the walls, and the overhead layer the
# same distance out but four metres up.
RINGS = {
    "front": 5.0,
    "centre": 4.0,
    "side": 6.0,
    "rear": 8.0,
}
HEIGHT_LIFT = 4.0

SLOT_RING = {
    "front_l": "front", "front_c": "centre", "front_r": "front",
    "wide_l": "side", "wide_r": "side",
    "side_l": "side", "side_r": "side",
    "rear_l": "rear", "rear_r": "rear",
    "height_l": "side", "height_r": "side", "top_c": "centre",
}

WALLS = ("front_l", "front_c", "front_r", "side_l", "side_r", "rear_l", "rear_r")
LAYERS = ("wide_l", "wide_r", "height_l", "height_r", "top_c")


def spec_for(slot):
    """One speaker at its slot's own bearing and ring, as a preset lays it."""
    angle = radians(IDEAL_BEARING[slot])
    radius = RINGS[SLOT_RING[slot]]
    z = HEIGHT_LIFT if slot in OVERHEAD_SLOTS else 0.0
    return CinemaSpeakerSpec(slot, (sin(angle) * radius,
                                    ANCHOR[1] + cos(angle) * radius, z))


def room(slots, profile="theatre"):
    """The renderer this room would really build."""
    return CinemaRenderer(ANCHOR, profile, specs=[spec_for(slot) for slot in slots])


def measure(renderer, listener):
    """The room's image at one seat: (width, height, side, front share, power)."""
    layout = renderer.layout
    terms = renderer.plan()
    total = 0.0
    left = 0.0
    left_side = 0.0
    right = 0.0
    right_side = 0.0
    overhead = 0.0
    front = 0.0
    mid = 0.0
    side = 0.0
    for slot, gain_l, gain_r in terms:
        position = layout.position(slot)
        if position is None:
            continue
        heard = distance_gain(position, listener, 8.0, 60.0)
        energy = heard * heard * (gain_l * gain_l + gain_r * gain_r)
        total += energy
        if slot in OVERHEAD_SLOTS:
            overhead += energy
        if slot in ("front_l", "front_r"):
            front += energy
        # Where the speaker actually stands, not where its slot ideally is: a
        # room being measured is the room the map laid out.
        dx = float(position[0]) - ANCHOR[0]
        dy = float(position[1]) - ANCHOR[1]
        lateral = sin(atan2(dx, dy)) ** 2
        left_energy = (heard * gain_l) ** 2
        right_energy = (heard * gain_r) ** 2
        left += left_energy
        right += right_energy
        left_side += left_energy * lateral
        right_side += right_energy * lateral
        mid += (heard * (gain_l + gain_r)) ** 2
        side += (heard * (gain_l - gain_r)) ** 2
    if total <= 0.0:
        return (0.0, 0.0, 0.0, 0.0, 0.0)
    width = 0.5 * (((left_side / left) ** 0.5 if left else 0.0)
                   + ((right_side / right) ** 0.5 if right else 0.0))
    return (width, overhead / total, side / mid if mid else 0.0,
            front / total, total)


SEATS = ((0.0, -2.0), (0.0, -5.0), (0.0, -8.0), (0.0, -12.0), (3.0, -6.0))


def seat(dx=0.0, dy=-5.0, eye=1.6):
    """A listener ``dy`` metres behind the cabinet (the audience is at -Y)."""
    return (ANCHOR[0] + dx, ANCHOR[1] + dy, ANCHOR[2] + eye)


def summary(renderer, listener=None):
    """One seat's numbers, as the sweep prints them."""
    return measure(renderer, seat() if listener is None else listener)


def probe(layers, share):
    """The twelve-speaker theatre with these layer weights and this power share."""
    import libs.audio.cinema.router as router
    base = PROFILES["theatre"]
    weights = {slot: (gain_l, gain_r) for slot, (gain_l, gain_r) in base.weights.items()
               if slot not in LAYERS}
    weights.update(layers)
    profile = CinemaProfile("theatre", "probe", weights, equal_power_surrounds=True)
    original = router.LAYER_POWER_SHARE
    router.LAYER_POWER_SHARE = share
    try:
        renderer = CinemaRenderer(ANCHOR, profile,
                                  specs=[spec_for(slot) for slot in WALLS + LAYERS])
        return summary(renderer)
    finally:
        router.LAYER_POWER_SHARE = original


def weight_sweep():
    """Candidate layer voicings, measured at one seat.

    ``wide`` is the pair that broadens the stage (its difference coefficient is
    ``gain_l - gain_r``), ``height`` the roof pair and ``top`` the ceiling
    centre. Each row is the same room with one set of numbers swapped in, so the
    column that moves is the layer's own share of the image.
    """
    candidates = {
        "none (old room)": None,
        "current": {"wide_l": (0.85, 0.15), "wide_r": (0.15, 0.85),
                    "height_l": (0.45, 0.55), "height_r": (0.55, 0.45),
                    "top_c": (0.6, 0.4)},
        "wide 0.90": {"wide_l": (0.9, 0.1), "wide_r": (0.1, 0.9),
                      "height_l": (0.45, 0.55), "height_r": (0.55, 0.45),
                      "top_c": (0.6, 0.4)},
        "wide 0.90, roof 0.42, top 0.55": {"wide_l": (0.9, 0.1), "wide_r": (0.1, 0.9),
                                           "height_l": (0.42, 0.58),
                                           "height_r": (0.58, 0.42),
                                           "top_c": (0.55, 0.45)},
        "wide 0.95, roof 0.40, top 0.55": {"wide_l": (0.95, 0.05), "wide_r": (0.05, 0.95),
                                           "height_l": (0.4, 0.6),
                                           "height_r": (0.6, 0.4),
                                           "top_c": (0.55, 0.45)},
    }
    print(f"{'layers':<33} | {'power':>7} | {'width':>7} | {'height':>7} | "
          f"{'side':>6} | {'front':>7}")
    print("-" * 88)
    for name, layers in candidates.items():
        if layers is None:
            width, height, side, front, power = summary(room(WALLS))
        else:
            width, height, side, front, power = probe(layers, 0.5)
        print(f"{name:<33} | {power:7.3f} | {width * 100:6.2f}% | {height * 100:6.2f}% | "
              f"{side:6.3f} | {front * 100:6.2f}%")


def sweep(shares):
    """The measured row for every layer power share, at one seat."""
    import libs.audio.cinema.router as router
    print(f"{'share':>6} | {'power':>7} | {'width':>7} | {'height':>7} | "
          f"{'side':>6} | {'front':>7}")
    print("-" * 60)
    original = router.LAYER_POWER_SHARE
    try:
        for share in shares:
            router.LAYER_POWER_SHARE = share
            width, height, side, front, power = summary(room(WALLS + LAYERS))
            print(f"{share:6.2f} | {power:7.3f} | {width * 100:6.2f}% | "
                  f"{height * 100:6.2f}% | {side:6.3f} | {front * 100:6.2f}%")
    finally:
        router.LAYER_POWER_SHARE = original


def table():
    old = room(WALLS)
    new = room(WALLS + LAYERS)
    print(f"theatre profile: {len(old.slots)} speakers -> {len(new.slots)} speakers")
    print(f"weights: wide={new.profile.weight('wide_l')} "
          f"height={new.profile.weight('height_l')} "
          f"top={new.profile.weight('top_c')}")
    print()
    print(f"{'seat':>10} | {'width':>12} | {'height':>12} | {'side':>12} | "
          f"{'front':>12} | {'power':>12}")
    print("-" * 88)
    for dx, dy in SEATS:
        listener = seat(dx, dy)
        before = measure(old, listener)
        after = measure(new, listener)
        def cell(pair, scale=1.0):
            return f"{pair[0] * scale:5.3f}->{pair[1] * scale:5.3f}"
        print(f"{dx:4.1f},{dy:5.1f} | {cell((before[0], after[0]), 100):>12}"
              f" | {cell((before[1], after[1]), 100):>12}"
              f" | {cell((before[2], after[2])):>12}"
              f" | {cell((before[3], after[3]), 100):>12}"
              f" | {cell((before[4], after[4])):>12}")
    print()
    print("width/height/front in percent (of the image / of the room's energy),")
    print("side as the difference signal over the mid, power in front-pair units.")


if __name__ == "__main__":
    if "--weights" in sys.argv[1:]:
        weight_sweep()
    elif "--sweep" in sys.argv[1:]:
        sweep([0.0, 0.15, 0.35, 0.5, 0.7, 1.0])
    else:
        table()
