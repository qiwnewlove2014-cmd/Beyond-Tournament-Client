"""The Music Bot's speech key: the song's shifter, on the microphone.

The song's key (`test_music_bot_transpose.py`) moves the decode. A voice heard
through the map's PA speakers, a cinema room's speakers or the talker's own
monitor is not a decode at all: it is the microphone, cut into 20 ms frames and
sent as Opus. So the same DSP (`libs/music_bot/transpose.py`) is applied to
those frames on their way out (``libs/voice_chat.SpeechKeyLine``), which gives
the broadcast one voice for everybody -- a listener's client retunes nothing --
and keeps the song's own dial completely separate.

Pinned here, in the order that matters:

1. **The frames keep their count and the interval is the interval**: a voice at
   +7 is a fifth up and still 20 ms a frame, which is what the PA's sequence,
   its timeline and a room's playout measure.
2. **The line delays, and the release hands the delay back**: a press ends with
   its last frame, then the look-ahead, as whole frames only.
3. **Only the broadcast leg is tuned.** Proximity voice is never shaped, and a
   dial at the original key builds no line at all, so microphone frames are
   byte-for-byte what they always were.
4. **The performer's own monitor hears the tuned frame**, not their untuned
   microphone, so the PA check sounds like the PA.
5. **The dial itself**: clamped, persisted, its own option, its own menu line,
   and it never moves the song's key.

No game, OpenAL, ffmpeg or network is required.
"""

import os
import sys
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from libs import consts, voice_chat  # noqa: E402
from libs.music_bot import MapMusicBot  # noqa: E402
from libs.music_bot import menus as menus_module  # noqa: E402
from libs.voice_chat import (SPEECH_KEY_OPTION, SpeechKeyLine,  # noqa: E402
                             VOICE_FRAME_BYTES, VoiceChatRecord,
                             speech_key_semitones)

SR = 48000
FRAME = VOICE_FRAME_BYTES             # one 20 ms mono16 frame at 48 kHz


# --------------------------------------------------------------- the signals


def tone(seconds=3.0, freq=220.0, amplitude=0.4):
    t = np.arange(int(SR * seconds)) / SR
    return (amplitude * np.sin(2.0 * np.pi * freq * t)).astype(np.float32)


def to_pcm(signal):
    return np.clip(signal * 32768.0, -32768, 32767).astype("<i2").tobytes()


def from_pcm(data):
    return np.frombuffer(bytes(data), dtype="<i2").astype(np.float64) / 32768.0


def play_mono(line, pcm):
    out = bytearray()
    for start in range(0, len(pcm) - FRAME + 1, FRAME):
        shaped = line.shape(pcm[start:start + FRAME])
        assert len(shaped) == FRAME
        out += shaped
    return bytes(out)


def peak_hz(samples):
    window = np.hanning(len(samples))
    spectrum = np.abs(np.fft.rfft(samples * window))
    return float(np.argmax(spectrum)) * SR / len(samples)


# ---------------------------------------------------------------- the line


class SpeechKeyLineTests(unittest.TestCase):
    def test_a_frame_in_is_a_frame_out(self):
        """Byte count is what the PA's sequence and a room's playout measure."""
        pcm = to_pcm(tone(1.0))
        for semitones in (-12, -5, -1, 1, 4, 7, 12):
            with self.subTest(semitones=semitones):
                line = SpeechKeyLine(semitones=semitones)
                for start in range(0, len(pcm) - FRAME + 1, FRAME):
                    self.assertEqual(
                        len(line.shape(pcm[start:start + FRAME])), FRAME)

    def test_the_interval_is_the_interval(self):
        pcm = to_pcm(tone(3.0, freq=220.0))
        up = from_pcm(play_mono(SpeechKeyLine(semitones=7), pcm))[SR:]
        down = from_pcm(play_mono(SpeechKeyLine(semitones=-12), pcm))[SR:]
        self.assertAlmostEqual(peak_hz(up), 220.0 * 2 ** (7 / 12.0), delta=6.0)
        self.assertAlmostEqual(peak_hz(down), 110.0, delta=4.0)

    def test_the_voice_keeps_its_level(self):
        pcm = from_pcm(to_pcm(tone(2.0)))
        line = SpeechKeyLine(semitones=5)
        out = from_pcm(play_mono(line, to_pcm(tone(2.0))))
        quiet = float(np.abs(pcm).max())
        self.assertGreater(float(np.abs(out[SR:]).max()), quiet * 0.5)

    def test_the_line_never_invents_silence_once_it_runs(self):
        """The look-ahead is real delay, not a gap that repeats (the song's bug)."""
        for semitones in (-12, 0, 7):
            with self.subTest(semitones=semitones):
                line = SpeechKeyLine(semitones=semitones)
                play_mono(line, to_pcm(tone(1.0)))
                self.assertEqual(line.transposer.padded_samples, 0)

    def test_the_tail_is_whole_frames(self):
        for semitones in (-12, 0, 4, 12):
            with self.subTest(semitones=semitones):
                line = SpeechKeyLine(semitones=semitones)
                play_mono(line, to_pcm(tone(1.0)))
                expected = (line.transposer.delay_samples() * 2) // FRAME
                tail = line.tail_frames(FRAME)
                self.assertEqual(len(tail), expected)
                for frame in tail:
                    self.assertEqual(len(frame), FRAME)

    def test_the_release_hands_the_sentence_back(self):
        """The tail is the END of the press, so a released word is not cut off."""
        pcm = to_pcm(tone(2.0, freq=220.0))
        line = SpeechKeyLine(semitones=4)
        out = from_pcm(play_mono(line, pcm) + b"".join(line.tail_frames(FRAME)))
        self.assertAlmostEqual(peak_hz(out[-SR:]), 220.0 * 2 ** (4 / 12.0),
                               delta=6.0)


# ------------------------------------------------- the two threads on it


class SpeechKeyLineThreadTests(unittest.TestCase):
    """One press has two owners: the capture thread feeds the line, the release flushes it.

    ``VoiceChatRecord.run`` shapes frames on the capture thread while
    ``voice_chat_finish2`` flushes the same line on the game loop. Shaping
    trims what the read head has already passed, and a trim landing between
    the flush's ensure and its read walked the read off the end of the
    stretched stream -- the crash report of 2026-10-22 (index 3941 of 3941,
    ``transpose._read`` from ``flush``). The line's own lock is what keeps the
    two callers from being inside it together.
    """

    def test_the_capture_thread_and_the_release_never_enter_the_line_together(self):
        line = SpeechKeyLine(semitones=3)
        pcm = to_pcm(tone(1.0))
        chunks = [pcm[i:i + FRAME] for i in range(0, len(pcm) - FRAME + 1, FRAME)]
        state = threading.Lock()
        overlaps, shaped = [], [0]

        def watched(method):
            """Records a call that found the line already in use."""

            def wrapper(*args, **kwargs):
                if not state.acquire(blocking=False):
                    overlaps.append(method.__name__)
                    return method(*args, **kwargs)
                try:
                    return method(*args, **kwargs)
                finally:
                    state.release()

            return wrapper

        transposer = line.transposer
        transposer.process = watched(transposer.process)
        transposer.flush = watched(transposer.flush)

        stop = threading.Event()

        def capture():
            for chunk in chunks:
                if stop.is_set():
                    return
                line.shape(chunk)
                shaped[0] += 1

        worker = threading.Thread(target=capture, daemon=True)
        worker.start()
        try:
            for _ in range(120):
                line.shape(chunks[0])
                line.tail_frames(FRAME)
        finally:
            stop.set()
            worker.join(5)

        self.assertEqual(overlaps, [], "two threads were inside the line at once")
        self.assertGreater(shaped[0], 0, "the capture thread never ran")


# -------------------------------------------------------- which press is tuned


class RecorderGateTests(unittest.TestCase):
    def recorder(self):
        rec = VoiceChatRecord.__new__(VoiceChatRecord)
        rec._speech_line = None
        return rec

    def test_proximity_voice_is_never_tuned(self):
        """The key is the broadcast's, not the player's own chat."""
        rec = self.recorder()
        with mock.patch.object(voice_chat, "speech_key_semitones", lambda: 5):
            self.assertIsNone(rec._speech_line_for(False))
        self.assertIsNone(rec._speech_line)

    def test_a_moved_dial_builds_the_line_for_a_broadcast(self):
        rec = self.recorder()
        with mock.patch.object(voice_chat, "speech_key_semitones", lambda: 5):
            line = rec._speech_line_for(True)
            self.assertIsInstance(line, SpeechKeyLine)
            self.assertEqual(line.semitones, 5)
            # The same press keeps its one line.
            self.assertIs(rec._speech_line_for(True), line)

    def test_the_original_key_needs_no_line_at_all(self):
        rec = self.recorder()
        with mock.patch.object(voice_chat, "speech_key_semitones", lambda: 0):
            self.assertIsNone(rec._speech_line_for(True))
        self.assertIsNone(rec._speech_line)

    def test_the_dial_moves_the_line_that_is_running(self):
        rec = self.recorder()
        with mock.patch.object(voice_chat, "speech_key_semitones", lambda: 3):
            line = rec._speech_line_for(True)
        with mock.patch.object(voice_chat, "speech_key_semitones", lambda: -4):
            self.assertIs(rec._speech_line_for(True), line)
        self.assertEqual(line.semitones, -4)

    def test_a_line_already_open_keeps_its_delay_at_the_original_key(self):
        """As a song does: gliding back to zero must not jump the line forward."""
        rec = self.recorder()
        with mock.patch.object(voice_chat, "speech_key_semitones", lambda: 3):
            line = rec._speech_line_for(True)
        with mock.patch.object(voice_chat, "speech_key_semitones", lambda: 0):
            self.assertIs(rec._speech_line_for(True), line)
        self.assertEqual(line.semitones, 0)


# ------------------------------------------------------------- the capture


class FakeInput:
    """A capture device that hands over one delivery period of a steady tone."""

    def __init__(self, samples, sample=6000):
        self.samples = samples
        self.sample = sample
        self.captured = []

    @property
    def available_samples(self):
        return self.samples

    def capture_samples(self, buf):
        count = len(buf) // 2
        data = np.full(count, self.sample, dtype="<i2").tobytes()
        buf[:] = data
        self.captured.append(count)
        self.samples = 0


class FakeCompression:
    def __init__(self, channel=consts.CHANNEL_VOICECHAT):
        self.channel = channel
        self.frames = []

    def put(self, value):
        self.frames.append(bytes(value))

    def set_channel(self, channel):
        self.channel = channel


def options_returning(**values):
    """A stand-in for libs.options.get that only answers the speech key."""

    def get(key, default=None):
        return values.get(key, default)

    return get


class CaptureTests(unittest.TestCase):
    def recorder(self, key):
        rec = VoiceChatRecord.__new__(VoiceChatRecord)
        rec.running = True
        rec.recording = True
        rec.stereo = False
        rec.player = SimpleNamespace(
            gameplay=SimpleNamespace(voice_chat_using_megaphone=True))
        rec.game = SimpleNamespace()
        rec.audio_input = FakeInput(VOICE_FRAME_BYTES // 2)
        rec.vc_compression = FakeCompression()
        rec._speech_line = None
        self.key = key
        return rec

    def run_once(self, rec):
        worker = threading.Thread(target=rec.run, daemon=True)
        with mock.patch.object(
                voice_chat.options, "get",
                options_returning(**{SPEECH_KEY_OPTION: self.key})):
            worker.start()
            deadline = time.monotonic() + 2.0
            while not rec.vc_compression.frames and time.monotonic() < deadline:
                time.sleep(0.002)
            rec.running = False
            worker.join(2)
        self.assertFalse(worker.is_alive())
        return rec.vc_compression.frames

    def test_the_original_key_sends_the_microphone_unchanged(self):
        rec = self.recorder(0)
        frames = self.run_once(rec)
        self.assertEqual(len(frames), 1)
        raw = np.full(VOICE_FRAME_BYTES // 2, 6000, dtype="<i2").tobytes()
        self.assertEqual(frames[0], raw)
        self.assertIsNone(rec._speech_line)

    def test_a_tuned_broadcast_is_still_one_frame_of_the_same_size(self):
        rec = self.recorder(5)
        frames = self.run_once(rec)
        self.assertEqual(len(frames), 1)
        self.assertEqual(len(frames[0]), VOICE_FRAME_BYTES)
        self.assertIsNotNone(rec._speech_line)

    def test_the_pa_channel_is_unchanged_by_the_key(self):
        rec = self.recorder(5)
        self.run_once(rec)
        self.assertEqual(rec.vc_compression.channel,
                         consts.CHANNEL_MEGAPHONE)

    def test_the_monitor_is_fed_the_tuned_frame_not_the_raw_chunk(self):
        """The performer auditions the PA, so they must hear what the PA plays."""
        rec = self.recorder(5)
        with mock.patch.object(voice_chat, "_feed_local_megaphone_direct") as fed:
            self.run_once(rec)
        self.assertTrue(fed.called)
        sizes = {len(call.args[1]) for call in fed.call_args_list}
        self.assertEqual(sizes, {VOICE_FRAME_BYTES})


# -------------------------------------------------------------- the release


class ReleaseTests(unittest.TestCase):
    def recorder(self, line=None):
        rec = VoiceChatRecord.__new__(VoiceChatRecord)
        rec.running = True
        rec.recording = False
        rec.stereo = False
        rec.player = None
        rec.game = SimpleNamespace()
        rec.audio_input = FakeInput(VOICE_FRAME_BYTES // 2)
        rec.vc_compression = FakeCompression(consts.CHANNEL_MEGAPHONE)
        rec._speech_line = line
        return rec

    def test_the_tail_is_sent_after_the_last_frame(self):
        line = SpeechKeyLine(semitones=4)
        play_mono(line, to_pcm(tone(1.0)))
        expected = (line.transposer.delay_samples() * 2) // FRAME
        rec = self.recorder(line)
        rec.voice_chat_finish2()
        frames = rec.vc_compression.frames
        self.assertEqual(len(frames), 1 + expected)
        for frame in frames:
            self.assertEqual(len(frame), VOICE_FRAME_BYTES)
        self.assertIsNone(rec._speech_line)

    def test_a_press_at_the_original_key_sends_only_its_last_frame(self):
        rec = self.recorder(None)
        rec.voice_chat_finish2()
        self.assertEqual(len(rec.vc_compression.frames), 1)

    def test_a_dead_microphone_drops_the_line_instead_of_raising(self):
        rec = self.recorder(SpeechKeyLine(semitones=3))

        class Dead:
            @property
            def available_samples(self):
                raise voice_chat.cyal.exceptions.InvalidDeviceError("gone")

        rec.audio_input = Dead()
        rec.voice_chat_finish2()   # must not raise
        self.assertIsNone(rec._speech_line)


# ------------------------------------------------------------------ the dial


class DialTests(unittest.TestCase):
    def bot(self):
        bot = MapMusicBot.__new__(MapMusicBot)
        bot.transpose_semitones = 0
        bot.speech_transpose_semitones = 0
        bot.streamer = None
        return bot

    def test_the_dial_stops_at_an_octave_each_way(self):
        bot = self.bot()
        with mock.patch("libs.music_bot.controller.options.set"):
            self.assertEqual(bot.set_speech_transpose(99), 12)
            self.assertEqual(bot.set_speech_transpose(-99), -12)
            self.assertEqual(bot.set_speech_transpose(2.6), 3)

    def test_the_dial_is_persisted_under_its_own_option(self):
        bot = self.bot()
        with mock.patch("libs.music_bot.controller.options.set") as save:
            self.assertEqual(bot.set_speech_transpose(4), 4)
        save.assert_called_once_with("music_bot_speech_transpose", 4)
        self.assertEqual(bot.speech_transpose_semitones, 4)

    def test_the_speech_dial_never_moves_the_song(self):
        bot = self.bot()
        bot.transpose_semitones = 4
        with mock.patch("libs.music_bot.controller.options.set"):
            bot.set_speech_transpose(-3)
        self.assertEqual(bot.transpose_semitones, 4)

    def test_the_song_dial_never_moves_the_speech(self):
        bot = self.bot()
        bot.speech_transpose_semitones = -3
        with mock.patch("libs.music_bot.controller.options.set"):
            bot.set_transpose(7)
        self.assertEqual(bot.speech_transpose_semitones, -3)

    def test_the_label_says_the_key(self):
        bot = self.bot()
        self.assertEqual(bot.speech_transpose_label(), "Original key")
        bot.speech_transpose_semitones = 1
        self.assertEqual(bot.speech_transpose_label(), "1 semitone up")
        bot.speech_transpose_semitones = -1
        self.assertEqual(bot.speech_transpose_label(), "1 semitone down")
        bot.speech_transpose_semitones = 5
        self.assertEqual(bot.speech_transpose_label(), "5 semitones up")

    def test_a_bot_built_without_the_option_reads_its_own_voice(self):
        bot = self.bot()
        del bot.speech_transpose_semitones
        self.assertEqual(bot.speech_transpose_label(), "Original key")

    def test_the_capture_thread_reads_the_option_the_dial_writes(self):
        with mock.patch.object(
                voice_chat.options, "get",
                options_returning(**{SPEECH_KEY_OPTION: 99})):
            self.assertEqual(speech_key_semitones(), 12)
        with mock.patch.object(
                voice_chat.options, "get",
                options_returning(**{SPEECH_KEY_OPTION: "not a key"})):
            self.assertEqual(speech_key_semitones(), 0)


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


class MenuTests(unittest.TestCase):
    def setUp(self):
        FakeMenu.built = []
        self.addCleanup(mock.patch.stopall)
        self.bot = mock.MagicMock()
        self.bot.transpose_semitones = 2
        self.bot.speech_transpose_semitones = 2
        self.bot.transpose_label.return_value = "2 semitones up"
        self.bot.speech_transpose_label.return_value = "Original key"
        # The settings menu now also carries Auto-Tune and its Retune Speed; a
        # bot with it off is the default, and what the speech key's own line
        # says depends on that (see test_music_bot_autotune for the on case).
        self.bot.autotune_enabled = False
        self.bot.autotune_label.return_value = "OFF"
        self.bot.autotune_speed_label.return_value = "Normal"
        self.bot.output_device_label.return_value = "Same as the game"
        self.bot._eq_profile_label.return_value = "Normal (Flat)"
        self.bot.water_muffle_enabled = True
        self.bot.reverb_enabled = True
        self.bot.crossfade_enabled = False
        self.bot._find_gameplay.return_value = SimpleNamespace(
            add_substate=lambda item: None, pop_last_substate=lambda: None)
        self.handler = menus_module.MusicBotMenus(self.bot)
        mock.patch("libs.menu.Menu", FakeMenu).start()
        mock.patch("libs.menus.set_default_sounds").start()
        self.speak = mock.patch("libs.music_bot.menus.speak").start()

    def settings_labels(self):
        self.handler._open_settings_menu()
        return [label() if callable(label) else label
                for label, _action, *_extra in FakeMenu.built[-1].items]

    def test_the_settings_menu_carries_the_speech_line(self):
        self.assertIn("Speech Key: Original key", self.settings_labels())

    def test_the_two_keys_sit_together_and_the_muffle_still_sits_last(self):
        labels = self.settings_labels()
        self.assertEqual(labels.index("Speech Key: Original key"),
                         labels.index("Song Key: 2 semitones up") + 1)
        self.assertEqual(labels[-2], "Underwater Muffle: ON")
        self.assertEqual(labels[-1], "Back")

    def test_the_speech_menu_steps_and_resets(self):
        self.handler._open_speech_transpose_menu()
        menu = FakeMenu.built[-1]
        self.assertEqual(menu.title, "Music Bot Speech Key: Original key")
        self.assertEqual([label for label, _action, *_extra in menu.items],
                         ["Up One Semitone", "Down One Semitone",
                          "Original Voice", "Back"])
        menu.items[0][1]()
        self.bot.set_speech_transpose.assert_called_once_with(3)
        menu.items[1][1]()
        self.bot.set_speech_transpose.assert_called_with(1)
        menu.items[2][1]()
        self.bot.set_speech_transpose.assert_called_with(0)
        self.assertEqual(self.speak.call_count, 3)

    def test_the_speech_menu_is_not_the_song_key_menu(self):
        self.handler._open_speech_transpose_menu()
        speech = FakeMenu.built[-1]
        self.handler._open_transpose_menu()
        song = FakeMenu.built[-1]
        self.assertIsNot(speech, song)
        self.assertEqual(song.title, "Music Bot Key: 2 semitones up")


if __name__ == "__main__":
    unittest.main()
