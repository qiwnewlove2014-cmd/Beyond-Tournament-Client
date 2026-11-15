"""Megaphone / PA cabinet alignment simulation.

The question this tool answers: put N cabinets on one map and how far apart do
they START? That is what "megaphone ตู้" sounds like when it is wrong -- an
echo/flam between cabinets, worse the more of them a map has, and independent
of mono/stereo because nothing here touches the stereo pair.

It drives the REAL production helpers from ``libs/voice_chat``
(``speaker_path_distances``, ``aligned_stagger_seconds``,
``speaker_frames_delay``, ``_megaphone_margin_frames``), so a table below is
the shipped maths and not a copy of it.

Three laws, each asserted (exit code 1 if one breaks):

  1. **The nearest cabinet is the reference** and starts on time -- the old
     rule paid every speaker its own distance to the listener, which put a
     whole hall's PA late by ``distance / 343`` before any stagger.
  2. **No two cabinets are more than ``MEGAPHONE_SPREAD_SECONDS`` apart**, so a
     map's PA fires as one system while the precedence effect still places the
     sound at the nearest cabinet.
  3. **The installer's per-speaker ``delay`` is untouched** -- 0-0.5 s, the
     builder's own trim, added on top and never capped.

Run:  python tools/megaphone_cabinet_sim.py
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from libs import voice_chat as vc

FRAME_MS = vc.MEGAPHONE_FRAME_SECONDS * 1000.0
CAP_MS = vc.MEGAPHONE_SPREAD_SECONDS * 1000.0
GATE_FRAMES = 2                     # MegaphoneJitterBuffer.PRE_BUFFER_FRAMES

failures = []


def check(name, condition, detail=""):
    print(("PASS  " if condition else "FAIL  ") + name + (("  " + detail) if detail else ""))
    if not condition:
        failures.append(name)


def speaker(x, y, z=0.0, delay=0.0):
    return {"position": (float(x), float(y), float(z)), "delay": float(delay)}


def old_rule_ms(distances, trims):
    """The rule before 2026-11-14: every speaker paid its own distance."""
    return [d / vc.SPEED_OF_SOUND * 1000.0 + t * 1000.0
            for d, t in zip(distances, trims)]


def new_rule_ms(speakers, listener, ground=0.0):
    paths = vc.speaker_path_distances(speakers, listener, ground)
    stagger = vc.aligned_stagger_seconds(paths)
    out = []
    for index, spk in enumerate(speakers):
        frames = vc.speaker_frames_delay(spk.get("delay", 0.0),
                                         stagger[index * 2])
        out.append(frames * FRAME_MS)
    return out


def header(title):
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


# ---------------------------------------------------------------------------
# 1. One talker, twenty cabinets: what the ear actually gets
# ---------------------------------------------------------------------------
header("1. ONE TALKER, TWENTY CABINETS -- onset per cabinet at the listener")

# A realistic spread: a line of cabinets across a hall, 4 m to 100 m away.
distances = [4.0 + 5.0 * i for i in range(20)]
speakers = [speaker(d, 0.0) for d in distances]
trims = [0.0] * len(speakers)

old = old_rule_ms(distances, trims)
new = new_rule_ms(speakers, (0.0, 0.0))
print(f"{'cabinet':>7} {'distance':>9} {'OLD onset':>10} {'NEW onset':>10} {'delta':>8}")
for index, (d, o, n) in enumerate(zip(distances, old, new)):
    print(f"{index:>7} {d:>8.0f}m {o:>9.0f}ms {n:>9.0f}ms {o - n:>7.0f}ms")

old_spread = max(old) - min(old)
new_spread = max(new) - min(new)
print(f"\nOLD spread {old_spread:6.0f} ms   OLD first sound {min(old):5.0f} ms")
print(f"NEW spread {new_spread:6.0f} ms   NEW first sound {min(new):5.0f} ms")
check("the nearest cabinet starts on time", min(new) == 0.0, f"{min(new):.0f} ms")
check("cabinets span at most the cap", new_spread <= CAP_MS + 1e-9,
      f"{new_spread:.0f} ms <= {CAP_MS:.0f} ms")
check("the old rule really did echo", old_spread > 200.0, f"{old_spread:.0f} ms")

# ---------------------------------------------------------------------------
# 2. The listener walks: the wavefront must not move under them
# ---------------------------------------------------------------------------
header("2. THE LISTENER WALKS -- does the PA image change while they move?")

# Four cabinets down one side of a hall; the listener walks past them.
hall = [speaker(0, 0), speaker(0, 20), speaker(0, 40), speaker(0, 60)]
rows = []
for step in range(0, 61, 10):
    listener = (0.0, float(step))
    # The production baseline is FROZEN at the stream's origin, so the stagger
    # is the one computed when the first frame arrived.
    frozen = vc.aligned_stagger_seconds(
        vc.speaker_path_distances(hall, listener))
    frames = [int(vc.speaker_frames_delay(0.0, frozen[i * 2])) for i in range(len(hall))]
    rows.append((step, frames, [round(f * FRAME_MS) for f in frames]))
print(f"{'listener y':>11}  frames (cabinet 0..3)      onset ms")
for step, frames, onset in rows:
    print(f"{step:>11}  {str(frames):<24}  {onset}")

check("the wavefront keeps its order as the listener walks",
      all(frames == rows[0][1] for _, frames, _ in rows),
      "frozen at the stream's origin")

# ---------------------------------------------------------------------------
# 3. The installer's trim: uncapped, and still the builder's
# ---------------------------------------------------------------------------
header("3. THE INSTALLER'S TRIM -- 0-0.5 s per speaker, deliberately staggered")

trimmed = [speaker(0, 0), speaker(0, 20), speaker(0, 40), speaker(0, 80)]
trims = [0.0, 0.1, 0.25, 0.5]
for spk, trim in zip(trimmed, trims):
    spk["delay"] = trim
onset = new_rule_ms(trimmed, (0.0, 0.0))
print(f"{'speaker':>8} {'trim':>7} {'onset':>8}")
for index, (trim, ms) in enumerate(zip(trims, onset)):
    print(f"{index:>8} {trim * 1000:>6.0f}ms {ms:>7.0f}ms")

check("the trim is added, never capped", onset[-1] >= 500.0, f"{onset[-1]:.0f} ms")
check("the trim keeps the builder's order", onset == sorted(onset))
check("half a second is twenty-five frames",
      vc.speaker_frames_delay(0.5, 0.0) == 25)

# ---------------------------------------------------------------------------
# 4. First word: gate + reserve + stagger, both transports
# ---------------------------------------------------------------------------
header("4. FIRST WORD -- gate + PA reserve + this cabinet's stagger")

print(f"start gate: {GATE_FRAMES} frames ({GATE_FRAMES * FRAME_MS:.0f} ms)")
for label, margin in (("sequenced (channel 31)", vc.MEGAPHONE_MARGIN_SEQUENCED),
                      ("legacy (channel 30)", vc.MEGAPHONE_MARGIN_LEGACY)):
    worst = max(new) + margin * FRAME_MS + GATE_FRAMES * FRAME_MS
    print(f"  {label:<24} reserve {margin} frames -> worst first word "
          f"{worst:6.0f} ms")
check("the sequenced transport stays under a quarter second",
      max(new) + vc.MEGAPHONE_MARGIN_SEQUENCED * FRAME_MS
      + GATE_FRAMES * FRAME_MS < 250.0)
check("the margin helper still answers per sender",
      vc._megaphone_margin_frames("nobody-sequenced") == vc.MEGAPHONE_MARGIN_LEGACY)

# ---------------------------------------------------------------------------
# 5. Two cabinets, a dozen ways: the pair a map is most likely to build
# ---------------------------------------------------------------------------
header("5. PAIRS AT EVERY SPACING -- how far apart do two cabinets start?")

print(f"{'spacing':>8} {'OLD apart':>11} {'NEW apart':>11}")
worst_new = 0.0
for spacing in (1, 2, 5, 10, 20, 30, 50, 80, 120):
    pair = [speaker(0, 0), speaker(0, spacing)]
    old_pair = old_rule_ms([0.0, float(spacing)], [0.0, 0.0])
    new_pair = new_rule_ms(pair, (0.0, 0.0))
    apart_old = abs(old_pair[1] - old_pair[0])
    apart_new = abs(new_pair[1] - new_pair[0])
    worst_new = max(worst_new, apart_new)
    print(f"{spacing:>7}m {apart_old:>9.0f}ms {apart_new:>9.0f}ms")

check("no pair is farther apart than the cap", worst_new <= CAP_MS + 1e-9,
      f"{worst_new:.0f} ms")

# ---------------------------------------------------------------------------
print("\n" + "=" * 78)
if failures:
    print(f"FAILED: {len(failures)} check(s): " + ", ".join(failures))
    sys.exit(1)
print("ALL CHECKS PASSED -- a map's cabinets fire as one system.")
