"""A voice through a cinema room over a lossy link: how many holes are heard?

The room has no jitter reserve: its speakers play exactly the 20 ms frames the
playout hands them (libs/audio/cinema/speech.py). A slot the playout skips is
therefore real silence, and once a speaker's queue empties the leg needs
``START_FRAMES`` fresh frames before it restarts at all -- the "full level but it
stutters" report. ``ROOM_CONCEAL_FRAMES`` (libs/voice_chat.py) is what decides
whether that slot is bridged by repeating the last frame or left empty.

This tool models only that decision and the room's queue, so the number it prints
is the number of holes a listener would hear for a given loss/jitter pattern.
It is deterministic: one seeded random stream feeds both runs.

Run:  python tools/cinema_voice_stutter_sim.py
      python tools/cinema_voice_stutter_sim.py --loss 0.03 --jitter 0.12
"""

import argparse
import random

FRAME_MS = 20.0
# libs/audio/cinema/speech.START_FRAMES: fresh frames a stopped speaker needs.
START_FRAMES = 3
# libs/voice_chat.ROOM_CONCEAL_FRAMES: how many frames a hole may be bridged.
ROOM_CONCEAL_FRAMES = 3


def run(loss=0.02, jitter=0.10, seconds=30.0, seed=7, repeat=True):
    """Return (holes, restarts, slots) for one talker over one room leg."""
    rng = random.Random(seed)
    slots = int(seconds * 1000 / FRAME_MS)
    # One speaker is enough: every speaker of a room is handed the same frames.
    queued = 0.0           # frames in the speaker's queue, in frame units
    playing = False
    fresh = 0              # frames queued since it stopped
    last_frame = None
    repeated = 0
    holes = 0
    restarts = 0
    for _ in range(slots):
        if playing:
            queued -= 1.0
            if queued <= 0.0:
                playing = False
                queued = 0.0
        arrived = rng.random() >= loss
        late = arrived and rng.random() < jitter
        packet = arrived and not late
        frame = None
        if packet:
            frame = True
            repeated = 0
            last_frame = frame
        elif repeat and last_frame is not None and repeated < ROOM_CONCEAL_FRAMES:
            frame = last_frame
            repeated += 1
        if frame is None:
            # Nothing was handed to the room for this slot: if a speaker was
            # playing it just lost 20 ms of audio. A stopped one simply stays
            # stopped (the hole is the gap before it restarts).
            if playing:
                holes += 1
            continue
        if not playing:
            fresh += 1
            queued += 1.0
            if fresh >= START_FRAMES:
                playing = True
                restarts += 1
                fresh = 0
            continue
        queued += 1.0
    return holes, restarts, slots


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--loss", type=float, default=0.02,
                        help="probability a frame is lost on the link")
    parser.add_argument("--jitter", type=float, default=0.10,
                        help="probability an arrived frame misses its slot")
    parser.add_argument("--seconds", type=float, default=30.0)
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args()

    before = run(args.loss, args.jitter, args.seconds, args.seed, repeat=False)
    after = run(args.loss, args.jitter, args.seconds, args.seed, repeat=True)
    print(f"loss={args.loss:.0%} jitter={args.jitter:.0%} "
          f"{args.seconds:.0f}s of speech ({before[2]} slots)")
    print(f"  without the room repeat : {before[0]:4d} silent frames, "
          f"{before[1]:3d} speaker restarts")
    print(f"  with the room repeat    : {after[0]:4d} silent frames, "
          f"{after[1]:3d} speaker restarts")
    if before[0]:
        saved = 100.0 * (before[0] - after[0]) / before[0]
        print(f"  holes removed           : {saved:.1f}%")


if __name__ == "__main__":
    main()
