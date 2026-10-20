"""The Music Bot's key shifter, and the dial that turns it.

The feature is "change the key of the song that is playing". A plain resample
cannot do that without also changing the tempo, and an ffmpeg filter cannot be
changed mid-song, so the song is transposed in the stream (`transpose.py`):
time-stretch it by the interval (WSOLA), then read it back at that interval,
which returns the tempo and the duration exactly where they were.

Pinned here, in the order that matters:

1. **One frame in, one frame out** -- for every interval, and across a change
   while the song plays. Byte count is what the cabinet's end-of-song
   hand-over, the pair clock and the network timeline measure, so this is the
   promise that a key change cannot move the beat.
2. **The interval is the interval**, measured on a tone: +12 is an octave,
   -12 is an octave down, and the level is not left behind by the stretch.
3. **The line never invents silence once it runs**: its look-ahead is real
   delay, not a gap that repeats (that was the first bug: padded_samples rose
   every frame until the search's drift was fixed).
4. **The song's own key is the song**, byte for byte after the delay, and a
   channel pair keeps its image because both sides use the same segment
   offsets.
5. **A key change bends instead of clicking** (the second bug: a filter whose
   carry-over was reset mid-bend put a full-amplitude step in the song).
6. **The streamer and the menu** actually route it: the frames the decode
   queues are shaped, the tail is handed back at the end, and the setting is
   clamped, persisted and applied to the stream that is playing.

No game, OpenAL, ffmpeg or network is required.
"""

import io
import os
import sys
import threading
import unittest
from collections import deque
from types import SimpleNamespace
from unittest import mock

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from libs.music_bot import MapMusicBot  # noqa: F401  (the dial's home)
from libs.music_bot import menus as menus_module
from libs.music_bot.streaming import AudioStreamer
from libs.music_bot.transpose import ChromaticTransposer

SR = 48000
FRAME = 960


# --------------------------------------------------------------- the signals


def tone(seconds=2.0, freq=440.0, channels=2, amplitude=0.4):
    t = np.arange(int(SR * seconds)) / SR
    mono = (amplitude * np.sin(2.0 * np.pi * freq * t)).astype(np.float32)
    if channels == 1:
        return mono.reshape(-1, 1)
    return np.stack([mono, mono * 0.8], axis=1)


def to_pcm(signal):
    return np.clip(signal * 32768.0, -32768, 32767).astype("<i2").tobytes()


def from_pcm(data, channels=2):
    return np.frombuffer(bytes(data), dtype="<i2").reshape(-1, channels).astype(np.float64) / 32768.0


def play(transposer, pcm, channels=2, per_frame=None):
    """Push a song through the line a frame at a time; returns the output bytes."""
    frame_bytes = FRAME * channels * 2
    frames = len(pcm) // frame_bytes
    out = bytearray()
    for index in range(frames):
        if per_frame is not None:
            per_frame(index, transposer)
        out += transposer.process(pcm[index * frame_bytes:(index + 1) * frame_bytes])
    return bytes(out)


def peak_hz(samples):
    window = np.hanning(len(samples))
    spectrum = np.abs(np.fft.rfft(samples * window))
    return float(np.argmax(spectrum)) * SR / len(samples)


def rms(samples):
    return float(np.sqrt(np.mean(np.asarray(samples, dtype=np.float64) ** 2)))


# ---------------------------------------------------------------- the shifter


class TransposerTests(unittest.TestCase):
    def test_a_frame_in_is_a_frame_out(self):
        """The promise every clock in the client depends on."""
        for semitones in range(-ChromaticTransposer.MAX_SEMITONES,
                               ChromaticTransposer.MAX_SEMITONES + 1):
            with self.subTest(semitones=semitones):
                pcm = to_pcm(tone(1.0))
                shifter = ChromaticTransposer(channels=2)
                shifter.set_semitones(semitones)
                out = play(shifter, pcm)
                self.assertEqual(len(out), len(pcm))

    def test_a_frame_in_is_a_frame_out_across_a_key_change(self):
        pcm = to_pcm(tone(2.0))
        shifter = ChromaticTransposer(channels=2)
        out = play(shifter, pcm,
                   per_frame=lambda index, sh: sh.set_semitones(7 if index == 30 else -5 if index == 60 else 0))
        self.assertEqual(len(out), len(pcm))

    def test_the_interval_is_the_interval(self):
        original = tone(2.0, freq=440.0)
        base = rms(from_pcm(to_pcm(original))[SR:2 * SR, 0])
        for semitones in (1, 5, 7, 12, -5, -12):
            with self.subTest(semitones=semitones):
                shifter = ChromaticTransposer(channels=2)
                shifter.set_semitones(semitones)
                out = from_pcm(play(shifter, to_pcm(original)))
                measured = peak_hz(out[SR:2 * SR, 0])
                expected = 440.0 * 2.0 ** (semitones / 12.0)
                self.assertAlmostEqual(measured, expected, delta=2.0)
                # The vocoder is additive, so the level must survive it. A whole
                # octave down is the worst case measured (0.964): at that much
                # compression a few partials no longer add in phase.
                self.assertAlmostEqual(rms(out[SR:2 * SR, 0]) / base, 1.0, delta=0.06)

    def test_it_never_invents_silence_once_it_runs(self):
        """The look-ahead is delay: every frame must come from the song."""
        for semitones in (-12, -5, 0, 1, 5, 7, 12):
            with self.subTest(semitones=semitones):
                shifter = ChromaticTransposer(channels=2)
                shifter.set_semitones(semitones)
                seen = {}

                def remember(index, sh):
                    if index == 40:
                        seen["padded"] = sh.padded_samples

                play(shifter, to_pcm(tone(2.0)), per_frame=remember)
                self.assertEqual(shifter.padded_samples - seen["padded"], 0)

    def test_the_original_key_is_the_song_after_its_delay(self):
        """Engaged at 0 semitones the line is a delay, not an effect."""
        signal = tone(2.0)
        shifter = ChromaticTransposer(channels=2)
        out = from_pcm(play(shifter, to_pcm(signal)))
        lag = ChromaticTransposer.lag_samples()
        # The first window ramps in; past that the samples are the song's.
        difference = np.abs(out[lag + SR // 2:, 0] - signal[SR // 2:len(signal) - lag, 0])
        self.assertLess(float(difference.max()), 1e-3)

    def test_both_sides_are_stretched_by_the_same_offsets(self):
        """Same input on both sides stays the same on both sides."""
        signal = tone(2.0)
        signal[:, 1] = signal[:, 0]
        shifter = ChromaticTransposer(channels=2)
        shifter.set_semitones(7)
        out = from_pcm(play(shifter, to_pcm(signal)))
        self.assertLessEqual(float(np.abs(out[:, 0] - out[:, 1]).max()), 1.0 / 32768)

    def test_a_key_change_bends_instead_of_clicking(self):
        pcm = to_pcm(tone(2.0, freq=220.0))
        steady = ChromaticTransposer(channels=2)
        still = from_pcm(play(steady, pcm))
        moving = ChromaticTransposer(channels=2)
        out = from_pcm(play(moving, pcm,
                            per_frame=lambda index, sh: sh.set_semitones(7 if index == 40 else 0)))
        quiet = float(np.abs(np.diff(still[SR:, 0])).max())
        self.assertLess(float(np.abs(np.diff(out[SR:, 0])).max()), quiet * 2.0)

    def test_the_dial_stops_at_an_octave_each_way(self):
        shifter = ChromaticTransposer(channels=2)
        self.assertEqual(shifter.set_semitones(99), 12)
        self.assertEqual(shifter.set_semitones(-99), -12)
        self.assertEqual(shifter.set_semitones(2.6), 3)

    def test_the_end_of_the_song_is_handed_back(self):
        shifter = ChromaticTransposer(channels=2)
        shifter.set_semitones(4)
        play(shifter, to_pcm(tone(1.0)))
        tail = shifter.flush()
        # The line holds LOOKAHEAD of the STRETCHED song, which is that much
        # divided by the interval once it is read at the interval.
        delay = int(round(ChromaticTransposer.lag_samples() / 2.0 ** (4 / 12.0)))
        self.assertEqual(len(tail), delay * 2 * 2)


# ------------------------------------------------------------ the streamer


class StreamerHookTests(unittest.TestCase):
    def streamer(self, channels=2):
        stream = AudioStreamer.__new__(AudioStreamer)
        stream.channels = channels
        stream._transpose_lock = threading.Lock()
        stream.transposer = None
        stream.transpose_semitones = 0
        return stream

    def test_a_stream_that_never_engaged_hands_the_frame_straight_back(self):
        stream = self.streamer()
        frame = b"\x01\x02" * FRAME * 2
        self.assertIs(stream._shape_frame(frame), frame)

    def test_set_transpose_shapes_frames_and_keeps_their_count(self):
        stream = self.streamer()
        pcm = to_pcm(tone(1.0))
        self.assertEqual(stream.set_transpose(5), 5)
        self.assertIsNotNone(stream.transposer)
        shaped = b"".join(
            stream._shape_frame(pcm[i * FRAME * 4:(i + 1) * FRAME * 4])
            for i in range(len(pcm) // (FRAME * 4)))
        self.assertEqual(len(shaped), len(pcm))
        # ... and it is not the song it was given.
        self.assertNotEqual(shaped, pcm)

    def test_the_prebuffer_is_shaped_before_it_is_queued(self):
        stream = self.streamer()
        stream.bot = None
        stream.running = True
        stream.PRE_BUFFER_COUNT = 4
        pcm = to_pcm(tone(1.0))
        stream.process = SimpleNamespace(
            poll=lambda: None,
            stdout=io.BytesIO(pcm[:stream.BUFFER_SIZE * 6]),
        )
        stream._diagnostic_startup_call = lambda label, fn, *a, **k: fn(*a, **k)
        stream._lock = threading.Lock()
        queued = []
        stream._queue_local = lambda data: queued.append(data) or True
        stream.set_transpose(7)
        count, leftover = stream._read_prebuffer()
        self.assertEqual(count, 4)
        self.assertEqual(len(queued), 4)
        self.assertTrue(all(len(frame) == stream.BUFFER_SIZE for frame in queued))
        # The same input through a bare transposer gives the same frames.
        reference = ChromaticTransposer(channels=2)
        reference.set_semitones(7)
        expected = b"".join(reference.process(pcm[i * FRAME * 4:(i + 1) * FRAME * 4])
                            for i in range(4))
        self.assertEqual(b"".join(queued), expected)
        # It stops reading the moment the buffer is full; the rest stays in ffmpeg.
        self.assertEqual(leftover, b"")

    def test_the_tail_is_the_look_ahead_handed_back(self):
        stream = self.streamer()
        stream.set_transpose(-3)
        stream._shape_frame(b"\x00\x00" * FRAME * 2)
        tail = stream._flush_transpose()
        self.assertEqual(len(tail), stream.transposer.delay_samples() * 4)
        # A stream that never engaged the shifter has nothing to give back.
        self.assertIsNone(self.streamer()._flush_transpose())

    def test_the_tail_is_queued_after_the_song_s_last_partial_frame(self):
        """A song is not a whole number of 20 ms frames; its last sliver comes first.

        The look-ahead is the END of the song, so it must be queued after the
        last partial frame the decode handed on. Appended before it (the first
        cut), the tail played first and the last sliver of the song landed
        after it.
        """
        stream = self.streamer()
        stream.bot = None
        stream.running = True
        stream.paused = False
        stream.spatial_pair = None
        stream.cinema = None
        stream._timeline_delay = 0
        stream._pause_buffer = deque()
        stream._tail_returned = False
        stream._lock = threading.Lock()
        stream._reclaim_processed = lambda: None
        stream._buffers_queued = lambda: 0
        stream._all_playing = lambda: True
        stream._route_aligned_network_frame = lambda *a, **k: None
        pcm = (b"\x10\x20\x30\x40"
               * (stream.BUFFER_SIZE // 4 * 2 + 25))  # two frames, then a sliver
        stream.process = SimpleNamespace(poll=lambda: 0, stdout=io.BytesIO(pcm))
        stream.set_transpose(2)
        queued = []
        stream._queue_local = lambda data: queued.append(data) or True
        stream._feed_and_drain(b"")
        self.assertEqual([len(chunk) for chunk in queued[:3]],
                         [stream.BUFFER_SIZE, stream.BUFFER_SIZE, 100])
        # ... and what comes last is the look-ahead, not more song.
        self.assertEqual(len(queued), 4)
        self.assertEqual(len(queued[-1]), stream.transposer.delay_samples() * 4)
        self.assertTrue(stream._tail_returned)


# ---------------------------------------------------------------- the dial


class BotDialTests(unittest.TestCase):
    def bot(self):
        bot = MapMusicBot.__new__(MapMusicBot)
        bot.transpose_semitones = 0
        bot.streamer = None
        return bot

    def test_set_transpose_clamps_persists_and_applies(self):
        bot = self.bot()
        live = mock.Mock()
        bot.streamer = live
        with mock.patch("libs.music_bot.controller.options.set") as save:
            self.assertEqual(bot.set_transpose(99), 12)
            self.assertEqual(bot.transpose_semitones, 12)
            save.assert_called_with("music_bot_transpose", 12)
            live.set_transpose.assert_called_once_with(12)
            self.assertEqual(bot.set_transpose(-2.4), -2)
            self.assertEqual(bot.transpose_semitones, -2)

    def test_set_transpose_without_a_stream_still_persists(self):
        bot = self.bot()
        with mock.patch("libs.music_bot.controller.options.set") as save:
            self.assertEqual(bot.set_transpose(3), 3)
        save.assert_called_once_with("music_bot_transpose", 3)

    def test_the_label_says_the_key(self):
        bot = self.bot()
        self.assertEqual(bot.transpose_label(), "Original key")
        bot.transpose_semitones = 1
        self.assertEqual(bot.transpose_label(), "1 semitone up")
        bot.transpose_semitones = -1
        self.assertEqual(bot.transpose_label(), "1 semitone down")
        bot.transpose_semitones = 5
        self.assertEqual(bot.transpose_label(), "5 semitones up")
        bot.transpose_semitones = -12
        self.assertEqual(bot.transpose_label(), "12 semitones down")

    def test_a_bot_built_without_the_option_reads_its_own_key(self):
        """Hand-built bots (tests, tools) must not need the attribute."""
        bot = self.bot()
        del bot.transpose_semitones
        self.assertEqual(bot.transpose_label(), "Original key")


class FakeMenu:
    built = []

    def __init__(self, game, title, parrent=None):
        self.game = game
        self.title = title
        self.items = []
        self.spoken = 0
        FakeMenu.built.append(self)

    def add_items(self, items):
        self.items = list(items)

    def speak_current_item(self):
        self.spoken += 1


class KeyMenuTests(unittest.TestCase):
    def setUp(self):
        FakeMenu.built = []
        self.addCleanup(mock.patch.stopall)
        self.bot = mock.MagicMock()
        self.bot.transpose_semitones = 2
        self.bot.transpose_label.return_value = "2 semitones up"
        self.bot._find_gameplay.return_value = SimpleNamespace(
            add_substate=lambda item: None, pop_last_substate=lambda: None)
        self.handler = menus_module.MusicBotMenus(self.bot)
        mock.patch("libs.menu.Menu", FakeMenu).start()
        mock.patch("libs.menus.set_default_sounds").start()
        self.speak = mock.patch("libs.music_bot.menus.speak").start()

    def labels(self):
        return [label() if callable(label) else label
                for label, _action in FakeMenu.built[-1].items]

    def test_the_settings_menu_carries_the_key_line(self):
        self.handler._open_settings_menu()
        self.assertIn("Song Key: 2 semitones up", self.labels())
        # ... and it is not the muffle's neighbour: the muffle stays last.
        self.assertEqual(self.labels()[-2:], ["Underwater Muffle: ON", "Back"])

    def test_the_key_menu_moves_by_a_semitone(self):
        self.handler._open_transpose_menu()
        self.assertEqual(FakeMenu.built[-1].title, "Music Bot Key: 2 semitones up")
        self.assertEqual(self.labels(),
                         ["Up One Semitone", "Down One Semitone", "Original Key", "Back"])
        for label, action in FakeMenu.built[-1].items:
            if label == "Up One Semitone":
                action()
        self.bot.set_transpose.assert_called_once_with(3)
        self.assertEqual(self.speak.call_count, 1)

    def test_the_key_menu_can_go_back_to_the_song_s_own_key(self):
        self.handler._open_transpose_menu()
        for label, action in FakeMenu.built[-1].items:
            if label == "Original Key":
                action()
        self.bot.set_transpose.assert_called_once_with(0)
        self.assertIn("own", self.speak.call_args[0][0])


if __name__ == "__main__":
    unittest.main()
