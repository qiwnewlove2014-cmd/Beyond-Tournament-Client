"""What a trimmed speaker comes back as after it runs dry.

``realign`` puts a stopped speaker back on the room's content instant by
filling it from the frames the room still holds, ``base + _frames_behind`` of
them -- and each frame handed to it is cut back by its own trim remainder. Both
the depth and the cut are part of the same delay, so an aim that counts the
trim's remainder twice pays it twice.

Measured per trim, this prints the delay a listener hears before the stumble and
after the speaker rejoins, plus the trim's own decomposition: the trims the room
was ever *tested* with (20, 40, 60 ms) divide the frame exactly, and the ones
that came back a whole frame late are the ones with a remainder.
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "tests"))

from room_sim import FRAME_20MS, FRAME_40MS, SAMPLERATE, RoomRig  # noqa: E402

TRIMS_MS = (5, 10, 20, 30, 40, 60, 100, 25)


def main():
    for frame, name in ((FRAME_20MS, "20ms"), (FRAME_40MS, "40ms")):
        print("== %s frames" % name)
        print("  trim   wanted   hold  cut | before      after")
        for trim in TRIMS_MS:
            rig = RoomRig({"front_l": 0.0, "front_r": float(trim)},
                          samples=frame)
            rig.run(80)
            before = sorted(set(rig.lag("front_r", "front_l")[40:]))
            holds = rig.bank.hold_frames("front_r")
            cut = rig.bank._delay_samples("front_r")
            rig.starve("front_r")
            rig.run(160)
            after = sorted(set(rig.lag("front_r", "front_l")[80:]))
            wanted = int(round(trim * SAMPLERATE / 1000.0))
            print("  %4d  %6d   %4d  %4d | %-11s %s%s"
                  % (trim, wanted, holds, cut, before, after,
                     "" if after == [wanted] else "   <-- moved"))
        print()


if __name__ == "__main__":
    main()
