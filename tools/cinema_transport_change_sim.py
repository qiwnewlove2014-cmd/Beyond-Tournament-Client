"""What a frame-size change does to a room that is already playing.

A trimmed speaker's delay is a count of queued frames plus a sample cut, the
count is a duration only while the frame size is known, and the count is spent
when the room starts -- so a transport that changes size under a song (the
recovery watchdog handing a cabinet to the direct streamer, a room the relay
takes back over, a scrub) leaves every trimmed speaker where the *old* size put
it. This prints the settled programme lag a listener would hear, per trim, for
each swap direction: the two transports the game really uses are 20 ms (the
direct streamer's own decode) and 40 ms (the server relay's PCM frames).

This is the table ``_reform_on_new_frame_size`` is written against, and
``tests/test_cinema_frame_size.py`` holds the room to it.
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "tests"))

from room_sim import FRAME_20MS, FRAME_40MS, SAMPLERATE, RoomRig  # noqa: E402

SLOTS = ("front_l", "front_r")
TRIMS_MS = (5, 10, 20, 30, 40, 60, 100)
PAIR = (FRAME_20MS, FRAME_40MS)
NAMES = {FRAME_20MS: "20ms", FRAME_40MS: "40ms"}


def settle(rig, ticks=400):
    rig.run(ticks)
    return sorted(set(rig.lag("front_r", "front_l")[60:]))


def swap(first, second, trim):
    """Play at ``first``, change the transport to ``second``, read the lag."""
    rig = RoomRig(delays={"front_r": float(trim)}, slots=SLOTS,
                  profile="surround", samples=first)
    settle(rig, 300)
    rig.samples = second
    values = settle(rig, 400)
    return values


def table():
    print("       trim   wanted | 20->40            | 40->20")
    for trim in TRIMS_MS:
        wanted = int(round(trim * SAMPLERATE / 1000.0))
        cells = []
        for first, second in (PAIR, PAIR[::-1]):
            values = swap(first, second, trim)
            cells.append(" ".join(
                "%d%s" % (value, "" if value == wanted else "*")
                for value in values))
        print("  %4d ms  %6d | %-17s | %s"
              % (trim, wanted, cells[0], cells[1]))
    print("  (* = not the trim the map asked for)")


if __name__ == "__main__":
    table()
