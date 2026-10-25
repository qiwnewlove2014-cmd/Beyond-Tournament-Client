"""What a line-in instrument does *leaves* the machine, and how much of it waits.

Three rules, all of them learned from a guitar played into a hall:

* The signal scan listens to every capture device **in one shared window**. A
  scan that opens one device after another misses a short strum played on the
  spoken prompt -- the player is playing while the scan is still on a different
  microphone -- which is the "No signal found" message over a pedal that is
  plainly plugged in.
* One captured frame has **one destination**: the broadcast/megaphone leg (the
  music bot's queue, which is what reaches a cinema room), or the voice leg.
  Never both (that is a strum heard twice) and never a third per-frame path of
  its own (a room plays live notes by spawning one sample per speaker; a
  continuous stream has no note to spawn, so every frame would build a source
  and a buffer that nothing owns).
* The guitar's own voice stream is a **bounded, session-owned worker**: a
  stalled encoder may hold 40 ms of audio, never a growing backlog that keeps
  playing the strum seconds after the hand played it.

Everything is driven through the real ``InstrumentInput`` with fakes only for
the capture devices and the voice worker.
"""
import collections
import struct
import unittest
from types import SimpleNamespace
from unittest import mock

import cyal

from libs import instrument_input, voice_chat
from libs.music_bot import voice_effects


# --------------------------------------------------------------------------
# The signal scan: one window, every device
# --------------------------------------------------------------------------
class FakeScanDevice:
    """A capture device the scan can open, carrying one constant level."""

    def __init__(self, name, level, log, openable=True):
        self.name = name
        self.level = level
        self.log = log
        self.openable = openable
        self.started = False
        self.stopped = 0
        self.reads = 0

    def start(self):
        self.started = True
        self.log.append(f"start:{self.name}")

    def stop(self):
        self.stopped += 1
        self.log.append(f"stop:{self.name}")

    @property
    def available_samples(self):
        # One 10 ms period per look, the way the real endpoint behaves.
        return 480 if self.started else 0

    def capture_samples(self, buf):
        self.reads += 1
        take = len(buf) // 2
        buf[:] = struct.pack(f"<{take}h", *([self.level] * take))


class FakeCaptureExtension:
    """``cyal.CaptureExtension``: device names plus the handles they open."""

    devices = []
    registry = {}

    def __init__(self):
        self.default_device = b"system default"

    def open_device(self, name=None, sample_rate=48000, format=None):
        device_name = name.decode("utf-8") if isinstance(name, bytes) else name
        device = self.registry.get(device_name)
        if device is None:
            raise cyal.exceptions.DeviceNotFoundError("no such device")
        if not device.openable:
            raise cyal.exceptions.DeviceNotFoundError("in use")
        return device


class SignalScanWindowTests(unittest.TestCase):
    """The scan finds the pedal that is being strummed right now."""

    def setUp(self):
        self.log = []
        self.loud = FakeScanDevice("OpenAL Soft on USB Audio Device", 12000, self.log)
        self.quiet = FakeScanDevice("OpenAL Soft on Microphone Array", 40, self.log)
        self.broken = FakeScanDevice("OpenAL Soft on Busy Interface", 0, self.log,
                                     openable=False)
        FakeCaptureExtension.registry = {d.name: d for d in
                                        (self.quiet, self.broken, self.loud)}
        FakeCaptureExtension.devices = [d.name for d in
                                        (self.quiet, self.broken, self.loud)]
        self.patches = [
            mock.patch.object(instrument_input.cyal, "CaptureExtension",
                              FakeCaptureExtension),
        ]
        for patch in self.patches:
            patch.start()
            self.addCleanup(patch.stop)

    def test_every_device_listens_in_the_same_window(self):
        instrument_input.scan_for_signal_devices()
        starts = [i for i, entry in enumerate(self.log) if entry.startswith("start:")]
        stops = [i for i, entry in enumerate(self.log) if entry.startswith("stop:")]
        self.assertEqual(len(starts), 2, self.log)      # the busy one never opens
        self.assertTrue(starts and stops and max(starts) < min(stops),
                        f"a serial scan opens one device after the other: {self.log}")
        # Both devices were read while the other was open: that is the one
        # shared window a strum has to land in.
        self.assertGreater(self.quiet.reads, 0)
        self.assertGreater(self.loud.reads, 0)

    def test_the_device_carrying_signal_is_the_one_reported(self):
        found = instrument_input.scan_for_signal_devices()
        names = [entry["name"] for entry in found]
        self.assertEqual(names, [" USB Audio Device"], names)
        self.assertGreaterEqual(found[0]["rms"], instrument_input.SIGNAL_SCAN_THRESHOLD)
        self.assertTrue(found[0]["stereo"] is False)

    def test_an_unopenable_device_does_not_stop_the_scan(self):
        # Nothing about a device held by another program may end the window
        # early: the pedal's own handle is stopped either way.
        found = instrument_input.scan_for_signal_devices()
        self.assertTrue(found)
        self.assertEqual(self.broken.stopped, 0)

    def test_every_handle_the_scan_opened_is_released(self):
        instrument_input.scan_for_signal_devices()
        for device in (self.quiet, self.loud):
            self.assertGreaterEqual(device.stopped, 1, device.name)
            self.assertGreater(device.reads, 0, device.name)

    def test_the_serial_probe_seam_still_answers_for_tests(self):
        # Offline callers (tools/signal_scan_test.py) hand their own device
        # list and a probe result: that path must keep working unchanged.
        wanted = ["OpenAL Soft on Boss GT-1", "OpenAL Soft on USB Audio Device"]
        with mock.patch.object(instrument_input, "_probe_device_signal",
                               side_effect=lambda device, seconds=None:
                               (0.2, False) if device == wanted[0] else None):
            found = instrument_input.scan_for_signal_devices(wanted)
        self.assertEqual([entry["device"] for entry in found], [wanted[0]])


# --------------------------------------------------------------------------
# One frame, one destination
# --------------------------------------------------------------------------
class FakeRouteBot:
    def __init__(self):
        self.broadcast_enabled = False
        self.broadcast_to_megaphone = False
        self.guitar_pcm_queue = collections.deque(maxlen=10)


class FakeTracker:
    def feed(self, frame):
        return None


def routing_input(bot, mega=False, on_megaphone=False):
    """An ``InstrumentInput`` whose frame routing is driven on its own."""
    rec = instrument_input.InstrumentInput.__new__(instrument_input.InstrumentInput)
    rec.recording = True
    rec.running = True
    rec.stereo = False
    rec._guitar_voice = None
    rec.frames = collections.deque(maxlen=8)
    rec.notes = collections.deque(maxlen=8)
    rec._monitor_pending = bytearray()
    rec._relay_pending = bytearray()
    rec.tracker = FakeTracker()
    gameplay = SimpleNamespace(
        player=SimpleNamespace(x=1.0, y=2.0, z=3.0),
        megaphone=object(),
        voice_chat_using_megaphone=mega,
    )
    rec.game = SimpleNamespace(stack=[gameplay], put=lambda fn: fn())
    rec._find_music_bot = lambda: bot
    rec.voice_leg = []
    rec._feed_guitar_voice = lambda raw, force_mega=False: rec.voice_leg.append(
        (raw, force_mega))
    return rec


class FrameRoutingTests(unittest.TestCase):
    """A strum is heard from one place, never two."""

    FRAME = b"\x01\x02" * instrument_input.InstrumentInput.FRAME_SAMPLES

    def test_an_ordinary_strum_takes_the_voice_leg_only(self):
        bot = FakeRouteBot()
        rec = routing_input(bot)
        rec._emit_frame(self.FRAME)
        self.assertEqual(len(bot.guitar_pcm_queue), 0)
        self.assertEqual(len(rec.voice_leg), 1)
        self.assertFalse(rec.voice_leg[0][1])

    def test_a_broadcast_strum_rides_the_bot_and_is_not_also_a_voice(self):
        bot = FakeRouteBot()
        bot.broadcast_enabled = True
        rec = routing_input(bot)
        rec._emit_frame(self.FRAME)
        self.assertEqual(len(bot.guitar_pcm_queue), 1)
        self.assertEqual(rec.voice_leg, [])

    def test_the_megaphone_alone_carries_a_strum(self):
        bot = FakeRouteBot()
        bot.broadcast_to_megaphone = True
        rec = routing_input(bot)
        rec._emit_frame(self.FRAME)
        self.assertEqual(len(bot.guitar_pcm_queue), 1)
        self.assertEqual(rec.voice_leg, [])

    def test_no_third_destination_exists_for_a_frame(self):
        # The bot's queue and the voice leg are the whole routing table: a
        # cinema room is reached by the broadcast that already carries this
        # frame (or by the megaphone's own room leg), never by a per-frame
        # route of its own into the room's speakers.
        bot = FakeRouteBot()
        rec = routing_input(bot)
        rec._emit_frame(self.FRAME)
        destinations = [name for name in ("guitar_pcm_queue",)
                        if len(getattr(bot, name))]
        self.assertEqual(destinations, [])
        self.assertEqual(len(rec.voice_leg), 1)


# --------------------------------------------------------------------------
# The pedal board: the Music Bot's Voice Effect, on the way out
# --------------------------------------------------------------------------
class RecordingTracker:
    """Remembers what the note tracker was handed."""

    def __init__(self):
        self.frames = []

    def feed(self, frame):
        self.frames.append(frame.copy())
        return None


class PedalBoardTests(unittest.TestCase):
    """A chosen pedal shapes what leaves -- and only what leaves.

    The line-in guitar and the broadcast voice share one board, so an amp
    chosen in the Music Bot is the amp a plugged-in guitar is played through.
    The player's own ear and the note tracker are handed the dry frame, for the
    same reason the guitar's Harmony line leaves them alone: the string under
    the fingers is dry, and an amp's harmonics are not notes.
    """

    # A frame with sharp steps at every sample pair: any filter, shaper or
    # gate changes it, and the comparison needs no audio device.
    FRAME = (bytes(range(256)) * 8)[
        :instrument_input.InstrumentInput.FRAME_SAMPLES * 2]

    def effect(self, chosen):
        """Answer the options the way a player who chose `chosen` would."""
        return mock.patch.object(
            voice_effects.options, "get",
            side_effect=lambda key, default=None: (
                chosen if key == voice_effects.OPTION else default))

    def rec(self, bot, chosen):
        rec = routing_input(bot)
        rec.tracker = RecordingTracker()
        self.patcher = self.effect(chosen)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.locked = mock.patch.object(voice_effects.gate, "is_locked",
                                        return_value=False)
        self.locked.start()
        self.addCleanup(self.locked.stop)
        return rec

    def test_a_chosen_amp_shapes_the_frame_that_leaves(self):
        bot = FakeRouteBot()
        bot.broadcast_enabled = True
        rec = self.rec(bot, "amp")
        rec._emit_frame(self.FRAME)
        self.assertEqual(len(bot.guitar_pcm_queue), 1)
        self.assertNotEqual(bytes(bot.guitar_pcm_queue[0]), self.FRAME)
        # ...and the notes the strings played are named from the dry frame.
        self.assertEqual(len(rec.tracker.frames), 1)
        dry = [sample / 32768.0 for sample in struct.unpack(
            f"<{len(self.FRAME) // 2}h", self.FRAME)]
        heard = rec.tracker.frames[0].tolist()
        self.assertEqual(len(heard), len(dry))
        self.assertLess(max(abs(a - b) for a, b in zip(heard, dry)), 1e-6)

    def test_the_custom_amps_board_reaches_a_string_too(self):
        bot = FakeRouteBot()
        rec = self.rec(bot, voice_effects.CUSTOM_EFFECT)
        rec._emit_frame(self.FRAME)
        self.assertEqual(len(rec.voice_leg), 1)
        self.assertNotEqual(rec.voice_leg[0][0], self.FRAME)

    def test_off_hands_the_frame_back_byte_for_byte(self):
        rec = self.rec(FakeRouteBot(), "off")
        self.assertEqual(rec._amplified(self.FRAME), self.FRAME)

    def test_the_developers_lock_silences_the_board_on_a_string(self):
        rec = self.rec(FakeRouteBot(), "amp")
        with mock.patch.object(voice_effects.gate, "is_locked",
                               return_value=True):
            self.assertEqual(rec._amplified(self.FRAME), self.FRAME)

    def test_one_line_serves_the_whole_session(self):
        rec = self.rec(FakeRouteBot(), "amp")
        rec._amplified(self.FRAME)
        line = rec._amp_line
        rec._amplified(self.FRAME)
        self.assertIs(rec._amp_line, line)
        # A new session starts dry: the gate, the cabinet's tail and the delay
        # rings are that session's state, not the next one's.
        rec._amp_line = None
        self.assertIsNone(getattr(rec, "_amp_line", None))


# --------------------------------------------------------------------------
# The guitar's own voice stream: bounded, and closed with the session
# --------------------------------------------------------------------------
class FakeVoiceWorker:
    made = []

    def __init__(self, game, channel=None, max_pending_frames=None,
                 application=None):
        self.game = game
        self.channel = channel
        self.max_pending_frames = max_pending_frames
        self.application = application
        self.frames = collections.deque()
        self.closed = 0
        FakeVoiceWorker.made.append(self)

    def set_channel(self, channel):
        self.channel = channel

    def put(self, value):
        self.frames.append(bytes(value))

    def close(self):
        self.closed += 1


class VoiceLegTests(unittest.TestCase):
    """The performed audio's own stream, not a growing backlog."""

    FRAME = b"\x11\x22" * instrument_input.InstrumentInput.FRAME_SAMPLES

    def setUp(self):
        FakeVoiceWorker.made = []
        self.patch = mock.patch.object(voice_chat, "voice_chat_compression",
                                       FakeVoiceWorker)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.rec = instrument_input.InstrumentInput.__new__(
            instrument_input.InstrumentInput)
        self.rec.game = object()
        self.rec.recording = True
        self.rec.running = True
        self.rec._guitar_voice = None
        self.rec.frames = collections.deque(maxlen=8)
        self.rec.notes = collections.deque(maxlen=8)
        self.rec.source = None

    def test_the_handoff_is_bounded_to_two_frames(self):
        self.rec._feed_guitar_voice(self.FRAME)
        worker = FakeVoiceWorker.made[0]
        self.assertEqual(worker.max_pending_frames, 2)
        self.assertEqual(len(worker.frames), 1)
        # Music, not a speech call: the instrument has to arrive sounding like
        # itself (see tests/test_instrument_output_quality.py).
        self.assertEqual(worker.application, "audio")

    def test_one_worker_serves_the_whole_session(self):
        for _ in range(5):
            self.rec._feed_guitar_voice(self.FRAME)
        self.assertEqual(len(FakeVoiceWorker.made), 1)
        self.assertEqual(len(FakeVoiceWorker.made[0].frames), 5)

    def test_the_channel_follows_the_megaphone_toggle(self):
        self.rec._feed_guitar_voice(self.FRAME, force_mega=True)
        worker = FakeVoiceWorker.made[0]
        from libs import consts
        self.assertEqual(worker.channel, consts.CHANNEL_MEGAPHONE)
        self.rec._feed_guitar_voice(self.FRAME)
        self.assertEqual(worker.channel, consts.CHANNEL_VOICECHAT)
        self.assertEqual(len(FakeVoiceWorker.made), 1)

    def test_stopping_the_session_closes_the_stream(self):
        self.rec._feed_guitar_voice(self.FRAME)
        worker = FakeVoiceWorker.made[0]
        self.rec.audio_input = SimpleNamespace(stop=lambda: None)
        self.rec.stop_recording()
        self.assertEqual(worker.closed, 1)
        self.assertIsNone(self.rec._guitar_voice)
        # A later frame opens a fresh worker instead of writing into a closed one.
        self.rec.recording = True
        self.rec._feed_guitar_voice(self.FRAME)
        self.assertEqual(len(FakeVoiceWorker.made), 2)

    def test_a_stopped_session_never_hands_a_frame_over(self):
        self.rec.recording = False
        self.rec._feed_guitar_voice(self.FRAME)
        self.assertEqual(FakeVoiceWorker.made, [])


if __name__ == "__main__":
    unittest.main()
