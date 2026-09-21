"""A room through a mid-song frame-size change, tick by tick.

When a room is re-formed, what happens to its queues and its clock decides
whether the trims land right or come back *topped up*: a re-form that leaves the
room's history in place is refilled by ``realign`` from frames the room still
holds, and the trimmed speaker comes back a frame or two deep -- the delay this
work is trying to undo. This walks one change frame by frame, printing the
clock, the holds, the depths and what each speaker played, which is how that was
seen rather than guessed.
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "tests"))

from room_sim import FRAME_20MS, FRAME_40MS, TAPE, RoomRig  # noqa: E402

SLOTS = ("front_l", "front_r")


def shown(value):
    if value is None:
        return "  -  "
    value %= TAPE
    return "%6d" % (value if value <= TAPE // 2 else value - TAPE)


def main():
    trim = float(sys.argv[1]) if len(sys.argv) > 1 else 30.0
    first = int(sys.argv[2]) if len(sys.argv) > 2 else FRAME_20MS
    second = int(sys.argv[3]) if len(sys.argv) > 3 else FRAME_40MS
    rig = RoomRig(delays={"front_r": trim}, slots=SLOTS, profile="surround",
                  samples=first)
    rig.run(300)
    print("settled at %d: %s" % (first, sorted(set(
        rig.lag("front_r", "front_l")[60:]))))
    print("trim %s -> hold=%d cut=%d frame=%.0fms"
          % (trim, rig.bank.hold_frames("front_r"),
             rig.bank._delay_samples("front_r"), rig.bank.frame_ms()))
    rig.samples = second
    for _ in range(24):
        rig.tick()
        bank = rig.bank
        print("t%4d frame=%4.0f q=%d start=%s plays=%s hold=%s | "
              "front_l %s (%d)  front_r %s (%d)  lag=%s%s"
              % (rig.ticks, bank.frame_ms(), bank.frames_queued,
                 bank._start_frame, bank._plays_started,
                 "->".join("%s:%d" % (slot, bank.hold_frames(slot))
                           for slot in SLOTS),
                 shown(rig.played["front_l"][-1]),
                 rig.depths["front_l"][-1],
                 shown(rig.played["front_r"][-1]),
                 rig.depths["front_r"][-1],
                 shown(rig.played["front_l"][-1] - rig.played["front_r"][-1])
                 if None not in (rig.played["front_l"][-1],
                                 rig.played["front_r"][-1]) else "-",
                 "  HOLD" if bank.awaiting_refill else ""))


if __name__ == "__main__":
    main()
