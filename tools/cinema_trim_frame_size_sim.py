"""A room started at each frame size: does the trim it hears still match?

A trim is played as a *count of queued frames* plus a sample cut, so the count
is a duration only while the frame size is known. That is what a *change* under
a song costs (see ``cinema_transport_change_sim.py``); this is the question
underneath it, and the one a re-form is written to reach: if the room simply
*starts* on a size, with nothing of any other size in its queues, does each trim
come out where the map asked for it?

Measured per trim, at each frame size: the settled programme lag a listener
would hear, ``hold_frames`` and the sample cut the room is using, and the depth
a trimmed speaker sits deeper than an untrimmed one.
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "tests"))

from room_sim import FRAME_20MS, FRAME_40MS, SAMPLERATE, RoomRig  # noqa: E402

SLOTS = ("front_l", "front_r")
# What Thebattlefield.map really dials in, plus the values the earlier probes
# walked: the interesting ones are those the frame size cannot divide.
TRIMS_MS = (5, 10, 20, 30, 40, 60, 100)


def heard(rig, ticks=400):
    """The settled programme lag of front_r against front_l, in samples."""
    rig.run(ticks)
    values = sorted(set(rig.lag("front_r", "front_l")[60:]))
    return values


def run(frame, trim_ms, ticks=400):
    rig = RoomRig(delays={"front_r": float(trim_ms)}, slots=SLOTS,
                  profile="surround", samples=frame)
    return rig, heard(rig, ticks)


def main():
    print("frame  trim   wanted | heard        holds  cut   depth+  verdict")
    for frame, name in ((FRAME_20MS, "20ms"), (FRAME_40MS, "40ms")):
        for trim in TRIMS_MS:
            rig, values = run(frame, trim)
            wanted = int(round(trim * SAMPLERATE / 1000.0))
            ok = values == [wanted]
            holds = rig.bank.hold_frames("front_r")
            cut = rig.bank._delay_samples("front_r")
            depth = sorted(set(
                deep - shallow for deep, shallow in zip(
                    rig.depths["front_r"][60:], rig.depths["front_l"][60:])))[-1:]
            print("%-5s  %4d  %6d | %-11s  %5d  %5d  %-6s  %s"
                  % (name, trim, wanted, values, holds, cut, depth,
                     "ok" if ok else "OFF by %s"
                     % [value - wanted for value in values]))


if __name__ == "__main__":
    main()
