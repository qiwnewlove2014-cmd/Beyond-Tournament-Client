"""The cinema room's centre-layer pan.

This is the cheap DSP half of Vocal focus: it is not stem separation, and
these checks say exactly that.  The default is a byte-for-byte bypass; an
active mode leaves the target channel alone and removes only the filtered
centre from the other side; a mono master is left untouched because it has no
centre to move.
"""

import math
import struct
import unittest

from libs.audio.cinema.vocal import (IN_SONG, LEFT, RIGHT, VocalFocus,
                                     normalize)


class VocalFocusTests(unittest.TestCase):
    RATE = 48000

    def frame(self, seconds=0.2):
        """A centre 1 kHz voice with a 300 Hz instrument only on the left."""
        count = int(self.RATE * seconds)
        left = []
        right = []
        for index in range(count):
            t = index / self.RATE
            centre = int(8000 * math.sin(2 * math.pi * 1000 * t))
            side = int(5000 * math.sin(2 * math.pi * 300 * t))
            left.append(max(-32768, min(32767, centre + side)))
            right.append(centre)
        return (struct.pack(f"<{count}h", *left),
                struct.pack(f"<{count}h", *right))

    @staticmethod
    def rms(pcm, start=0.05):
        samples = struct.unpack(f"<{len(pcm) // 2}h", pcm)
        first = int(len(samples) * start)
        chosen = samples[first:]
        return math.sqrt(sum(value * value for value in chosen) / len(chosen))

    def test_the_default_is_a_true_bypass(self):
        left, right = self.frame()
        focus = VocalFocus(IN_SONG)
        self.assertIs(focus.apply(left, right)[0], left)
        self.assertIs(focus.apply(left, right)[1], right)

    def test_left_keeps_the_left_and_takes_the_centre_out_of_the_right(self):
        left, right = self.frame()
        focus = VocalFocus(LEFT)
        out_left, out_right = focus.apply(left, right)
        self.assertEqual(out_left, left)
        self.assertLess(self.rms(out_right), self.rms(right) * 0.6)
        self.assertGreater(self.rms(out_right), 0.0)

    def test_right_is_the_mirror_image(self):
        left, right = self.frame()
        focus = VocalFocus(RIGHT)
        out_left, out_right = focus.apply(left, right)
        self.assertEqual(out_right, right)
        self.assertLess(self.rms(out_left), self.rms(left) * 0.6)

    def test_a_mono_master_has_no_centre_to_move(self):
        left, _right = self.frame()
        focus = VocalFocus(LEFT)
        self.assertEqual(focus.apply(left, left), (left, left))

    def test_an_unknown_value_is_the_bypass(self):
        self.assertEqual(normalize("sideways"), IN_SONG)
        self.assertEqual(normalize("LEFT"), LEFT)
        self.assertEqual(VocalFocus("typo").mode, IN_SONG)


if __name__ == "__main__":
    unittest.main()