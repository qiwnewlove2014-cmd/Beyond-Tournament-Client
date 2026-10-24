"""What a cabinet's cinema room costs per frame, on this machine.

The room mixes one MONO feed per fed speaker per frame -- that is the work a
listener pays for as long as a cabinet plays, on the transport's thread and, in
the relay transport, on the game's own. It is also the only part of the sound
whose cost scales with how many speakers a builder placed, so it is the first
thing to measure when a player reports the game stuttering in a room with a lot
of speakers while the Server is healthy.

The room renders the same way the bank feeds it: one call per frame, one mix per
fed speaker. Each iteration hands the renderer a *fresh* frame object, because a
frame's verdict and decoded window are cached by identity and replaying one
object would measure the cache.

Measured on the owner's machine (2026-10-23, 12 speakers, every one dialled with
its own crossover, delay and level, 1920-sample (40 ms) relay frames):

    reference loop  6.38 ms per frame
    numpy mix       0.49 ms per frame

and the same room at 20 ms frames went 3.35 ms -> 0.49 ms. The frame is 12.8%
of the game's budget before the fix, 1% after, which is what a slower machine
feels as the game hitching all the way through a song.

Run:  python tools/cinema_frame_cost.py
      python tools/cinema_frame_cost.py --speakers 24 --frames 400
"""

import argparse
import array
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from libs.audio.cinema.layout import CinemaSpeakerSpec, IDEAL_BEARING  # noqa: E402
from libs.audio.cinema.router import CinemaRenderer  # noqa: E402

ANCHOR = (0.0, 0.0, 0.0)
FULL_ROOM = ("front_l", "front_c", "front_r", "side_l", "side_r", "rear_l", "rear_r",
             "wide_l", "wide_r", "height_l", "height_r", "top_c")


def specs(count):
    """``count`` speakers on the room's ring, each dialled differently.

    Distinct delays, crossovers and levels are the case that costs the most: a
    room whose speakers are all identical is fed in one group and reads cheaper
    than the room an installer actually set up.
    """
    from math import cos, radians, sin
    placed = []
    for index in range(count):
        slot = FULL_ROOM[index % len(FULL_ROOM)]
        angle = radians(IDEAL_BEARING[slot])
        placed.append(CinemaSpeakerSpec(
            slot,
            (ANCHOR[0] + sin(angle) * 8.0 + index, ANCHOR[1] + cos(angle) * 8.0,
             ANCHOR[2] + (5.0 if slot.startswith("height") or slot == "top_c" else 0.0)),
            delay_ms=float(index * 3),
            crossover=(40.0 + index * 5, None) if index % 3 == 0 else None,
            level=float(100 - index),
        ))
    return placed


def frame_bytes(seconds, sample_rate=48000):
    count = int(seconds * sample_rate)
    left = array.array("h", [((index * 37) % 20000) - 10000 for index in range(count)])
    right = array.array("h", [((index * 53) % 20000) - 10000 for index in range(count)])
    return left.tobytes(), right.tobytes()


def measure(speakers, frame_ms, frames):
    renderer = CinemaRenderer(ANCHOR, "custom", specs=specs(speakers))
    left, right = frame_bytes(frame_ms / 1000.0)
    for _ in range(8):                       # warm up caches and numpy's import
        renderer.render(bytes(left), bytes(right))
    samples = []
    for _ in range(frames):
        # A new object each frame: the verdict and the decoded window are cached
        # by identity, so a replayed frame would measure the cache.
        current_l, current_r = bytes(left), bytes(right)
        started = time.perf_counter()
        renderer.render(current_l, current_r)
        samples.append((time.perf_counter() - started) * 1000.0)
    return len(renderer.slots), statistics.median(samples), max(samples)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--speakers", type=int, action="append",
                        help="speakers in the room (may repeat; default 7, 12, 24)")
    parser.add_argument("--frame-ms", type=int, action="append",
                        help="frame length (default 20 and 40: direct and relay)")
    parser.add_argument("--frames", type=int, default=200)
    parser.add_argument("--reference", action="store_true",
                        help="force the pure-Python mix (what a machine with no numpy pays)")
    args = parser.parse_args(argv)

    from libs.audio.cinema import channel
    if args.reference:
        channel.set_vector_mix(None)
        mix = "reference loop (what a machine with no numpy pays)"
    elif channel._VECTOR_MIX is not None:
        mix = "vectorised (numpy handed in by fastmix.py)"
    else:
        mix = "reference loop (no numpy on this machine)"

    speakers = args.speakers or [7, 12]
    frame_ms = args.frame_ms or [20, 40]
    print(f"mix: {mix}")
    for count in speakers:
        for length in frame_ms:
            fed, median, worst = measure(count, length, max(10, args.frames))
            share = median / length * 100.0
            note = "" if fed >= count else f"  (only {fed} fed: the room is capped at 12)"
            print(f"{fed:2d} speaker(s), {length:2d} ms frame: "
                  f"{median:5.2f} ms median ({share:4.1f}% of the frame), "
                  f"worst {worst:5.2f} ms{note}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
