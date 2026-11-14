"""What the game does to a microphone's signal, and the two places body was lost.

The capture is 48 kHz/16-bit, folded to mono when the device refuses a mono
open, cut into 10 ms chunks and encoded as mono Opus 20 ms frames. Two of those
steps were quietly costing an external microphone its low end:

* the stereo fold averaged the pair, which cancels the signal when an
  interface hands the two sides over in opposite polarity;
* the encoder was left on libopus's own default bitrate, because pyogg exposes
  no setter for it.

These checks pin the phase-safe fold, the bitrate ctl, and the one send loop
every press (plain and shaped) goes through, so the microphone gain keeps
applying to both.
"""

import array
import os
import unittest
from types import SimpleNamespace

import pyogg

from libs import voice_chat


class _Source:
    """Read a client file, and one method of it, the way the sibling tests do."""

    def _source(self, *parts):
        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            *parts
        )
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()

    def _function(self, source, name):
        start = source.index(f"    def {name}(")
        rest = source[start:]
        return rest[:rest.index("\n    def ", 1)]


def tone(samples=480, amplitude=8000, period=48):
    """A cheap square-ish wave: real energy, no numpy in the test's behalf."""
    out = array.array("h")
    for index in range(samples):
        out.append(amplitude if (index % period) < (period // 2) else -amplitude)
    return list(out)


def stereo_bytes(left, right):
    samples = array.array("h")
    for one, other in zip(left, right):
        samples.append(int(one))
        samples.append(int(other))
    return samples.tobytes()


def mono_of(payload):
    out = array.array("h")
    out.frombytes(payload)
    return list(out)


class DownmixTests(unittest.TestCase):
    def test_a_capsule_copied_into_both_sides_keeps_its_level(self):
        ring = tone()
        mixed = mono_of(voice_chat.stereo_to_mono(
            stereo_bytes(ring, ring), None))
        self.assertEqual(len(mixed), len(ring))
        self.assertEqual(max(abs(v) for v in mixed),
                         max(abs(v) for v in ring))

    def test_an_out_of_phase_pair_does_not_cancel_the_signal(self):
        ring = tone()
        flipped = [-value for value in ring]
        capture = SimpleNamespace(_mono_inverted=None)

        mixed = mono_of(voice_chat.stereo_to_mono(
            stereo_bytes(ring, flipped), capture))

        # A plain average would be silence here: this is the whole bug.
        self.assertTrue(capture._mono_inverted)
        self.assertGreaterEqual(max(abs(v) for v in mixed),
                                max(abs(v) for v in ring) * 0.9)

    def test_an_in_phase_pair_is_an_average_not_a_flip(self):
        ring = tone()
        capture = SimpleNamespace(_mono_inverted=None)

        mixed = mono_of(voice_chat.stereo_to_mono(
            stereo_bytes(ring, ring), capture))

        self.assertFalse(capture._mono_inverted)
        self.assertEqual(max(abs(v) for v in mixed),
                         max(abs(v) for v in ring))

    def test_the_polarity_is_a_wiring_fact_and_is_not_re_decided(self):
        capture = SimpleNamespace(_mono_inverted=True)
        voice_chat.stereo_to_mono(stereo_bytes(tone(), tone()), capture)
        self.assertTrue(capture._mono_inverted)

    def test_a_chunk_too_short_to_be_a_stereo_frame_is_passed_through(self):
        self.assertEqual(voice_chat.stereo_to_mono(b"\x01\x02\x03"), b"\x01\x02\x03")
        self.assertEqual(voice_chat.stereo_to_mono(b""), b"")

    def test_silence_decides_nothing_and_stays_silent(self):
        capture = SimpleNamespace(_mono_inverted=None)
        mixed = voice_chat.stereo_to_mono(
            stereo_bytes([0] * 480, [0] * 480), capture)
        self.assertIsNone(capture._mono_inverted)
        self.assertEqual(set(mono_of(mixed)), {0})


class BitrateTests(_Source, unittest.TestCase):
    def _encoder(self, application="voip"):
        encoder = pyogg.OpusEncoder()
        encoder.set_application(application)
        encoder.set_channels(1)
        encoder.set_sampling_frequency(48000)
        return encoder

    def test_libopus_accepts_the_hint_through_the_wrapper(self):
        encoder = self._encoder()
        # pyogg builds its encoder on the first frame; the build in
        # `voice_chat_compression.__init__` primes it exactly like this.
        encoder.encode(array.array("h", bytes(1920)))
        self.assertTrue(voice_chat.set_opus_bitrate(encoder, 64000))
        # It is still an encoder afterwards, not a broken one.
        self.assertTrue(encoder.encode(array.array("h", tone(960))))

    def test_an_unbuilt_encoder_says_no_instead_of_crashing(self):
        # No frame yet, so pyogg holds no pointer: the ctl must answer false,
        # not reach into a null one.
        self.assertFalse(
            voice_chat.set_opus_bitrate(self._encoder(), 64000))
        self.assertFalse(voice_chat.set_opus_bitrate(object(), 64000))
        self.assertFalse(voice_chat.set_opus_bitrate(None, 64000))

    def test_the_two_voice_workers_ask_for_their_own_bitrate(self):
        # A plain press is mono voip; a shaped one is audio mode and carries
        # the harmony and tuning, so it must not be the thinner of the two.
        self.assertGreaterEqual(voice_chat.VOICE_OPUS_BITRATE, 32000)
        self.assertGreaterEqual(voice_chat.SHAPED_OPUS_BITRATE,
                                voice_chat.VOICE_OPUS_BITRATE)

    def test_the_encoder_is_told_its_bitrate_when_it_is_built(self):
        source = self._source("libs", "voice_chat.py")
        start = source.index("class voice_chat_compression")
        body = source[start:source.index("\nclass ", start + 1)]
        self.assertIn("set_opus_bitrate(self.encoder, self.bitrate)", body)
        self.assertIn('SHAPED_OPUS_BITRATE if application == "audio"', body)
        # pyogg's pointer only exists after a frame, so the build primes it.
        self.assertIn("self.encoder.encode(array.array", body)


class TheMicrophoneGainIsOneLoopTests(_Source, unittest.TestCase):
    """The gain lives in the send loop, so every press and every worker gets it.

    The shaped press builds a second worker (`_speech_sender`,
    `application="audio"`) of the SAME class, which is what makes one gain block
    enough -- and what must not drift.
    """

    def test_the_gain_is_applied_in_the_one_send_loop(self):
        source = self._source("libs", "voice_chat.py")
        self.assertEqual(source.count('options.get("megaphone_mic_volume"'), 1)
        self.assertGreater(
            source.index('options.get("megaphone_mic_volume"'),
            source.index("class voice_chat_compression"),
        )

    def test_a_shaped_press_is_sent_by_that_same_class(self):
        body = self._function(self._source("libs", "voice_chat.py"),
                              "_speech_sender")
        self.assertIn("voice_chat_compression(", body)
        self.assertIn('application="audio"', body)

    def test_both_presses_hand_the_worker_a_bytearray(self):
        # `run()` applies the gain to a bytearray and skips anything else (a
        # callable is a flush), so the producer side is part of the contract.
        source = self._source("libs", "voice_chat.py")
        self.assertEqual(source.count("sender.put(bytearray(chunk_bytes))"), 1)


if __name__ == "__main__":
    unittest.main()
