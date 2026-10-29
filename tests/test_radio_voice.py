"""The radio in your hand is the pedal on the shelf -- pinned.

`libs/radio_voice.py` colours the copy of a speaker's voice that a listener
holding a radio hears (`voice_chat._play_voice_frame`). What this file holds it
to:

1. **It is the Voice Effect list's own `radio` pedal.** The chain is a
   `voice_effects._EffectProcessor("radio")` -- 430 Hz high-pass, 2.8 kHz
   cabinet, 6x tanh, 0.05 bias -- so the bytes are the shelf's bytes, not a
   second telephone that drifts from it.
2. **A telephone band.** Noise through it keeps 300 Hz-3 kHz and loses more
   than 20 dB above 5 kHz; a 150 Hz tone is more than half gone.
3. **A frame in is the same frame out.** Same length, same rate, no clipping,
   and an average level within a few decibels of the voice that arrived.
4. **Silence is cheap.** A pause hands back zeros without running the chain.
5. **The key-up clicks -- once, after silence, and only then.**
6. **State is per speaker**, and a 33rd voice forgets the first rather than
   growing without bound.
7. **The Voice Effect feature cannot move.** The radio is the receive path;
   the boards are the send path. Processing a radio frame writes no option and
   moves no board, and `test_the_listening_condition_is_one_condition` pins the
   one gate both threads read.
"""

import time
import unittest
from types import SimpleNamespace
from unittest import mock

import numpy as np

from libs import consts, radio_voice, voice_chat
from libs.music_bot import voice_effects

SAMPLE_RATE = 48000
FRAME = 960          # 20 ms at 48 kHz
FRAME_BYTES = FRAME * 2


def tone(hz, amp=0.25, frames=1):
    count = FRAME * frames
    t = np.arange(count) / SAMPLE_RATE
    return np.round(np.sin(2 * np.pi * hz * t) * amp * 32767.0).astype(
        "<i2").tobytes()


def noise(frames=8, amp=0.08, seed=5):
    rng = np.random.default_rng(seed)
    return np.round(rng.standard_normal(FRAME * frames) * amp * 32767.0).clip(
        -32768, 32767).astype("<i2").tobytes()


def samples(pcm):
    return np.frombuffer(pcm, dtype="<i2").astype(np.float64) / 32768.0


def rms(pcm):
    block = samples(pcm)
    return float(np.sqrt((block ** 2).mean())) if block.size else 0.0


def band_energy(pcm, low, high):
    """Energy between two frequencies, in the window's own units."""
    block = samples(pcm)
    spectrum = np.abs(np.fft.rfft(block * np.hanning(block.size))) ** 2
    freqs = np.fft.rfftfreq(block.size, 1.0 / SAMPLE_RATE)
    return float(spectrum[(freqs >= low) & (freqs < high)].sum())


def play(pcm):
    """Push a whole signal through the radio as one speaker's frames."""
    return b"".join(radio_voice.frame(("chan", "speaker"),
                                     pcm[at:at + FRAME_BYTES])
                    for at in range(0, len(pcm), FRAME_BYTES))


class TheRadioIsThePedalTests(unittest.TestCase):
    def setUp(self):
        radio_voice.reset()
        self.addCleanup(radio_voice.reset)

    def test_the_radio_is_the_shelf_s_pedal_plus_one_block(self):
        """The shelf's `radio` row, the radio's wah, and nothing else.

        Two things are checked, because either one alone would let the radio
        drift: the *spec* is `_SPECS["radio"]` with exactly `envelope_wah`
        different -- so the tone underneath the wah is still the pedal a player
        can hear on the shelf -- and `_SPECS` itself is untouched, so no pedal
        in the Music Bot grew a wah because the radio did.
        """
        shelf = dict(voice_effects._SPECS[radio_voice.PRESET])
        ours = radio_voice.radio_spec()
        self.assertNotIn("envelope_wah", shelf,
                         "the shelf's Radio row grew a wah")
        self.assertEqual(ours["envelope_wah"], radio_voice.WAH)
        self.assertEqual(
            {key: value for key, value in ours.items()
             if key not in ("envelope_wah", "out")},
            {key: value for key, value in shelf.items() if key != "out"})
        self.assertEqual(ours["out"], radio_voice.OUT)

        # ... and the bytes are that chain, made by the same class: after the
        # key-up (a squelch click, by design) they match sample for sample.
        pcm = tone(900, frames=3)
        played = [radio_voice.frame(("chan", "s"), pcm[i * FRAME_BYTES:
                                                      (i + 1) * FRAME_BYTES])
                  for i in range(3)]
        chain = voice_effects._EffectProcessor(radio_voice.PRESET,
                                               spec=radio_voice.radio_spec())
        expected = [chain.process(pcm[i * FRAME_BYTES:(i + 1) * FRAME_BYTES])
                    for i in range(3)]
        self.assertNotEqual(played[0], expected[0], "the key-up did not click")
        for index in (1, 2):
            self.assertEqual(played[index], expected[index],
                             f"frame {index} is not the spec's chain")

    def test_the_wah_follows_the_voice_and_not_a_clock(self):
        """A loud syllable opens the band; a quiet one leaves it closed.

        The block is an envelope follower (`_envelope_wah_block`), and the
        reason it sounds like a speaker rather than a tremolo is that it has no
        rate at all. One frame each, measured on the chain's own state.
        """
        quiet = np.round(np.random.default_rng(2).standard_normal(FRAME)
                         * 0.02 * 32767.0).astype("<i2").tobytes()
        loud = np.round(np.random.default_rng(3).standard_normal(FRAME)
                        * 0.30 * 32767.0).clip(-32768, 32767).astype(
                            "<i2").tobytes()
        radio_voice.frame(("quiet", "s"), quiet)
        radio_voice.frame(("loud", "s"), loud)
        self.assertLess(radio_voice._table[("quiet", "s")].processor._env, 0.10)
        self.assertGreater(radio_voice._table[("loud", "s")].processor._env, 0.30)

    def test_a_frame_in_is_the_same_frame_out(self):
        for count in (FRAME, FRAME * 2, 480, 1000):
            with self.subTest(samples=count):
                pcm = tone(900, frames=1)[:count * 2]
                out = radio_voice.frame(("chan", "s"), pcm)
                self.assertEqual(len(out), len(pcm))

    def test_the_band_is_a_telephone_band(self):
        """300 Hz-3 kHz stays; the top of a voice is more than 20 dB down."""
        voice = noise()
        out = play(voice)
        for low, high, least in ((3000.0, 5000.0, 20.0),
                                 (5000.0, 24000.0, 30.0)):
            with self.subTest(band=f"{int(low)}-{int(high)}"):
                lost = 10.0 * np.log10(
                    band_energy(out, low, high) / band_energy(voice, low, high))
                self.assertLess(lost, -least, f"only {lost:.1f} dB")
        for low, high, least in ((300.0, 1000.0, 3.0),
                                 (1000.0, 3000.0, 3.0)):
            with self.subTest(band=f"{int(low)}-{int(high)}"):
                kept = 10.0 * np.log10(
                    band_energy(out, low, high) / band_energy(voice, low, high))
                self.assertGreater(kept, least, f"only {kept:.1f} dB")

    def test_the_bottom_of_a_voice_is_cut(self):
        pcm = tone(150, frames=2)
        out = play(pcm)
        # The high-pass is one biquad, so it takes the fundamental with it;
        # the shaper's harmonics keep some energy in the band, which is why
        # this is "more than half gone" rather than "gone".
        self.assertLess(band_energy(out, 100.0, 200.0)
                        / band_energy(pcm, 100.0, 200.0), 0.5)

    def test_the_level_is_the_voice_it_arrived_as(self):
        voice = noise()
        out = play(voice)
        gain = 20.0 * np.log10(rms(out) / rms(voice))
        self.assertLess(abs(gain), 4.0, f"{gain:+.1f} dB")
        self.assertLessEqual(float(np.max(np.abs(samples(out)))),
                             voice_effects.PEAK_CEILING + 1e-3)

    def test_silence_hands_back_zeros_and_shuts_the_radio(self):
        silence = bytes(FRAME_BYTES)
        out = silence
        for _ in range(radio_voice.QUIET_FRAMES + 2):
            out = radio_voice.frame(("chan", "s"), silence)
            self.assertEqual(len(out), FRAME_BYTES)
        self.assertEqual(out, silence)

    def test_the_key_up_clicks_once_and_only_after_a_pause(self):
        """Two transmissions with a shut radio between them, and one click each.

        The reference is the same speaker with the click off, fed the same
        frames: the only differences must be at the two key-ups, and each of
        those must last `SQUELCH_MS` rather than the whole frame.
        """
        pcm = tone(900, frames=1)
        silence = bytes(FRAME_BYTES)
        sequence = ([pcm, pcm] + [silence] * (radio_voice.QUIET_FRAMES + 1)
                    + [pcm, pcm])
        with mock.patch.object(radio_voice, "SQUELCH_LEVEL", 0.0):
            radio_voice.reset()
            plain = [radio_voice.frame(("ref", "r"), item) for item in sequence]
        radio_voice.reset()
        keyed = [radio_voice.frame(("chan", "k"), item) for item in sequence]

        self.assertNotEqual(keyed[0], plain[0], "the first key-up did not click")
        self.assertEqual(keyed[1], plain[1], "the click lasted more than a frame")
        self.assertNotEqual(keyed[-2], plain[-2], "a shut radio re-opened flat")
        self.assertEqual(keyed[-1], plain[-1])

        difference = np.abs(samples(keyed[-2]) - samples(plain[-2]))
        click = int(round(radio_voice.SQUELCH_MS * SAMPLE_RATE / 1000.0))
        self.assertGreater(float(difference[:click].max()), 0.01)
        self.assertEqual(float(difference[click:].max()), 0.0,
                         "the click outlived its length")

    def test_the_click_can_be_turned_off(self):
        pcm = tone(900, frames=1)
        with mock.patch.object(radio_voice, "SQUELCH_LEVEL", 0.0):
            opened = radio_voice.frame(("chan", "k"), pcm)
        chain = voice_effects._EffectProcessor(radio_voice.PRESET,
                                               spec=radio_voice.radio_spec())
        self.assertEqual(opened, chain.process(pcm))

    def test_every_voice_has_its_own_filters(self):
        radio_voice.frame(("chan", "a"), tone(900))
        radio_voice.frame(("chan", "b"), tone(900))
        table = radio_voice._table
        self.assertIsNot(table[("chan", "a")].processor,
                         table[("chan", "b")].processor)
        # A channel change is a new stream: the same speaker on another
        # channel must not inherit this one's filters.
        radio_voice.frame(("other", "a"), tone(900))
        self.assertIsNot(table[("chan", "a")].processor,
                         table[("other", "a")].processor)

    def test_a_thirty_third_voice_forgets_the_first(self):
        for index in range(radio_voice.MAX_SPEAKERS):
            radio_voice.frame(("chan", f"spk{index}"), tone(900))
        self.assertEqual(len(radio_voice._table), radio_voice.MAX_SPEAKERS)
        radio_voice.frame(("chan", "extra"), tone(900))
        self.assertEqual(len(radio_voice._table), radio_voice.MAX_SPEAKERS)
        self.assertNotIn(("chan", "spk0"), radio_voice._table)
        self.assertIn(("chan", "extra"), radio_voice._table)

    def test_the_cost_fits_a_frame(self):
        """One speaker's colour, paid on the decode worker per 20 ms frame."""
        pcm = tone(600)
        radio_voice.frame(("chan", "cost"), pcm)      # warm the chain
        frames = 200
        started = time.perf_counter()
        for _ in range(frames):
            radio_voice.frame(("chan", "cost"), pcm)
        cost = (time.perf_counter() - started) / frames * 1000.0
        self.assertLess(cost, 4.0, f"{cost:.2f} ms a frame")


class TheVoiceEffectFeatureIsNotTouchedTests(unittest.TestCase):
    def setUp(self):
        radio_voice.reset()
        self.addCleanup(radio_voice.reset)

    def test_a_radio_frame_writes_no_option_and_moves_no_board(self):
        from libs.music_bot.voice_effects import builder
        before = (builder.values(), builder.revision(), voice_effects.enabled())
        with mock.patch.object(builder.options, "set") as setter:
            play(tone(900, frames=3))
            setter.assert_not_called()
        after = (builder.values(), builder.revision(), voice_effects.enabled())
        self.assertEqual(before, after)

    def test_the_listening_condition_is_one_condition(self):
        """The worker colours a frame only when the main thread would play it."""
        channel = SimpleNamespace(has_radio=True)
        gameplay = SimpleNamespace(voice_channels={"chan": channel},
                                   player=SimpleNamespace(has_radio=True))
        self.assertTrue(voice_chat._radio_audible(gameplay, "chan"))
        self.assertFalse(voice_chat._radio_audible(
            gameplay, consts.CHANNEL_MEGAPHONE))
        gameplay.player.has_radio = False
        self.assertFalse(voice_chat._radio_audible(gameplay, "chan"))
        gameplay.player.has_radio = True
        channel.has_radio = False
        self.assertFalse(voice_chat._radio_audible(gameplay, "chan"))
        channel.has_radio = True
        self.assertFalse(voice_chat._radio_audible(gameplay, "unknown"))

    def test_a_proximity_leg_with_no_sender_id_still_names_the_speaker(self):
        """The ordinary voice leg carries no id; the receiver's source is it.

        `EventHandeler.process_voice_data` calls `recieve` with no `sender_id`,
        so a radio frame would have gone uncoloured if the tone depended on
        one. The key falls back to the per-speaker receiver object, and two
        speakers on one channel must still be two streams.
        """
        first, second = object(), object()
        self.assertEqual(voice_chat._radio_key("chan", None, first, None),
                         voice_chat._radio_key("chan", None, first, None))
        self.assertNotEqual(voice_chat._radio_key("chan", None, first, None),
                            voice_chat._radio_key("chan", None, second, None))
        self.assertEqual(voice_chat._radio_key("chan", None, None, second),
                         voice_chat._radio_key("chan", None, second, None))
        # A party leg's own id wins, and the channel is part of the key.
        self.assertEqual(voice_chat._radio_key("chan", "alice", first, None),
                         ("chan", "alice"))
        self.assertNotEqual(
            voice_chat._radio_key("chan", "alice", None, None),
            voice_chat._radio_key("other", "alice", None, None))


class _FakeBuffer:
    def __init__(self):
        self.data = None

    def set_data(self, data, sample_rate=None, format=None):
        self.data = data


class _FakeSource:
    def __init__(self):
        self.buffers_processed = 0
        self.buffers_queued = 0
        self.state = "stopped"
        self.queued = []

    def unqueue_buffers(self):
        return None

    def queue_buffers(self, buf):
        self.queued.append(buf)
        self.buffers_queued += 1

    def play(self):
        pass


class TheColouredCopyIsTheOneQueuedTests(unittest.TestCase):
    def setUp(self):
        self.sources = [_FakeSource()]
        self.radio = _FakeSource()
        self.gameplay = SimpleNamespace(
            voice_channels={"chan": SimpleNamespace(has_radio=True)},
            player=SimpleNamespace(has_radio=True),
        )
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(voice_chat, "_get_buffer_from_pool",
                          lambda mngr: _FakeBuffer()).start()

    def test_the_radio_source_gets_the_coloured_copy(self):
        raw = tone(900)
        coloured = radio_voice.frame(("chan", "sender"), raw)
        voice_chat._play_voice_frame(None, self.sources, raw, 1, self.radio,
                                     "chan", self.gameplay, radio_data=coloured)
        self.assertEqual(self.radio.queued[0].data, coloured)
        # ... and the voice in the room is the frame that arrived, untouched.
        # (The first buffers a cold source queues are its jitter pad.)
        self.assertEqual(self.sources[0].queued[-1].data, raw)

    def test_a_radio_copy_with_no_colour_is_the_frame_itself(self):
        """A tool or a test can queue the copy without the worker's pass."""
        raw = tone(900)
        voice_chat._play_voice_frame(None, self.sources, raw, 1, self.radio,
                                     "chan", self.gameplay)
        self.assertEqual(self.radio.queued[0].data, raw)


if __name__ == "__main__":
    unittest.main()
