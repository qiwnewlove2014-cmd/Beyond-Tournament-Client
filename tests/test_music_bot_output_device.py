"""The Music Bot's own stream, played on a device of its own.

OpenAL binds a device to a context, so "the song on the speakers, the game in
the headset" cannot be asked of a source: it needs a second context, and a
second context must be current on the thread that touches its objects. The
Music Bot's sources are touched from three threads and its equalizer, reverb
and underwater muffle are EFX on the game's context. So the second device is a
child process of its own (`libs/music_bot/output_device.py` justifies it,
`device_worker.py` is the child), and this suite pins the three things that
make it true rather than merely possible:

1. **The wire.** One header for every frame kind, PCM only, and a queue that
   answers "not now" instead of dropping a frame -- the streamer retries that
   frame, exactly as it retries a spent OpenAL buffer pool.
2. **The child.** Feed it the bytes and it queues them onto an OpenAL-shaped
   device: a format decides mono/stereo, gain and pause land on the right
   slot, a flush empties it, and a truncated stream exits instead of guessing.
3. **The bot.** A device is only used when one is chosen; a room's speakers
   (map objects on the game's device) and the game's own playback are never
   taken over; and a device that will not open is said once and forgotten.

No sound card is required: the child is driven through its own `context_factory`
seam, and the parent through a fake process.
"""

import io
import os
import struct
import sys
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from libs.music_bot import MapMusicBot, device_worker, output_device


# --------------------------------------------------------------- the doubles


class FakeStdin():
    """A pipe end that records what the parent writes."""

    def __init__(self, block=None):
        self.data = bytearray()
        self.closed = False
        self._block = block

    def write(self, frame):
        if self._block is not None:
            self._block.wait(2.0)
        self.data.extend(frame)
        return len(frame)

    def flush(self):
        pass

    def close(self):
        self.closed = True


class FakeProcess():
    """The child's process handle, as `MusicOutputProcess` uses it."""

    def __init__(self, status=b'{"device": "speaker", "played_frames": 3}\n',
                 block=None, exit_code=None):
        self.stdin = FakeStdin(block)
        self.stdout = io.BytesIO(status)
        self._exit = exit_code
        self.killed = False

    def poll(self):
        return self._exit

    def wait(self, timeout=None):
        return self._exit or 0

    def kill(self):
        self.killed = True


class FakeSource():
    def __init__(self):
        import cyal
        self.gain = 1.0
        self.state = cyal.SourceState.STOPPED
        self.buffers_queued = 0
        self.buffers_processed = 0
        self.queued = []
        self.played = 0
        self.paused = False
        self.flushed = 0
        self.deleted = False

    def queue_buffers(self, buffer):
        self.queued.append(buffer)
        self.buffers_queued = len(self.queued)
        self.total_queued = getattr(self, "total_queued", 0) + 1

    def unqueue_buffers(self):
        if not self.queued:
            return None
        self.buffers_queued = len(self.queued) - 1
        return self.queued.pop(0)

    def play(self):
        import cyal
        self.state = cyal.SourceState.PLAYING
        self.played += 1

    def pause(self):
        import cyal
        self.paused = True
        self.state = cyal.SourceState.PAUSED

    def stop(self):
        import cyal
        self.state = cyal.SourceState.STOPPED
        self.flushed += 1
        self.queued = []
        self.buffers_queued = 0

    def delete(self):
        self.deleted = True


class FakeBuffer():
    def __init__(self):
        self.data = b""
        self.format = None
        self.rate = None

    def set_data(self, data, sample_rate=None, format=None):
        self.data = bytes(data)
        self.rate = sample_rate
        self.format = format


class FakeContext():
    """An OpenAL context with sockets, so the child's loop can be driven."""

    def __init__(self):
        self.sources = []
        self.buffers = []

    def gen_source(self):
        source = FakeSource()
        self.sources.append(source)
        return source

    def gen_buffer(self):
        buffer = FakeBuffer()
        self.buffers.append(buffer)
        return buffer


def fake_process(process):
    """A Popen factory that hands back `process` and remembers the argv."""
    started = []

    def popen(command, **kwargs):
        started.append((command, kwargs))
        return process
    popen.started = started
    return popen


def make_bot(**attrs):
    """The slice of MapMusicBot the output-device rules read."""
    bot = MapMusicBot.__new__(MapMusicBot)
    bot.game = SimpleNamespace(put=lambda fn: fn(), audio_mngr=SimpleNamespace(
        volume_categories={"music": [80]}))
    bot.volume = 50
    bot.playing = False
    bot.paused = False
    bot.streamer = None
    bot.cinema_bank = None
    bot.output_device = ""
    bot._output_process = None
    bot._find_gameplay = lambda: None
    # What Music Bot Settings reads its other lines from.
    bot.water_muffle_enabled = True
    bot.reverb_enabled = True
    bot.crossfade_enabled = False
    bot.eq_profile = "normal"
    bot.eq_values = {"bass": 50, "mid": 50, "treble": 50}
    bot.said = []
    for key, value in attrs.items():
        setattr(bot, key, value)
    return bot


class SaidMixin():
    def say(self):
        """Patch the spoken lines for the length of one test."""
        return mock.patch(
            "libs.music_bot.controller.speak",
            lambda text, *rest: self.bot.said.append(text),
        )


# ----------------------------------------------------------------- the wire


class FrameTests(unittest.TestCase):
    def setUp(self):
        self.process = FakeProcess()
        self.popen = fake_process(self.process)
        self.output = output_device.MusicOutputProcess(
            "OpenAL Soft on Speaker", popen=self.popen)

    def tearDown(self):
        self.output.stop()

    def frames(self, expect=None, timeout=1.0):
        """The frames the child has been sent so far (the writer is a thread)."""
        deadline = time.monotonic() + timeout
        while True:
            raw = bytes(self.process.stdin.data)
            parsed, index = [], 0
            while index + output_device.HEADER.size <= len(raw):
                kind, slot, length = output_device.HEADER.unpack(
                    raw[index:index + output_device.HEADER.size])
                index += output_device.HEADER.size
                parsed.append((kind, slot, raw[index:index + length]))
                index += length
            if expect is None or len(parsed) >= expect or time.monotonic() > deadline:
                break
            time.sleep(0.002)
        self.assertEqual(index, len(raw), "every byte belongs to a frame")
        return parsed

    def test_a_frame_is_audio_for_one_slot(self):
        self.output.start()
        self.assertTrue(self.output.push(b"\x01\x02" * 960, 0))
        frame = self.frames(expect=1)[0]
        self.assertEqual(frame[:2], (output_device.AUDIO, 0))
        self.assertEqual(frame[2], b"\x01\x02" * 960)

    def test_gain_pause_flush_and_format_are_their_own_frames(self):
        self.output.start()
        self.output.set_format(2, 2, 48000)
        self.output.set_gain(1, 0.25)
        self.output.set_pause(True, 1)
        self.output.flush(1)
        frames = self.frames(expect=4)
        self.assertEqual([kind for kind, _slot, _payload in frames],
                         [output_device.FORMAT, output_device.GAIN,
                          output_device.PAUSE, output_device.FLUSH])
        self.assertEqual(struct.unpack(">f", frames[1][2])[0], 0.25)
        self.assertEqual(frames[2][2], b"\x01")

    def test_a_frame_that_does_not_fit_is_retried_not_dropped(self):
        """The streamer's contract: False means "come back for this frame"."""
        self.output.start()
        self.assertFalse(self.output.push(b"\x00" * (output_device.MAX_PENDING_BYTES + 1)))
        self.assertTrue(self.output.push(b"\x00" * 3840))

    def test_a_dead_child_takes_no_more_frames(self):
        self.output.start()
        self.process._exit = 3
        self.assertFalse(self.output.alive())
        self.assertFalse(self.output.push(b"\x00" * 3840))

    def test_stopping_asks_it_to_finish_and_reads_what_it_played(self):
        self.output.start()
        self.output.push(b"\x00" * 3840, 0)
        self.assertEqual(len(self.frames(expect=1)), 1)
        self.output.stop()
        self.assertIn((output_device.QUIT, output_device.ALL_SLOTS, b""),
                      self.frames())
        self.assertEqual(self.output.status["played_frames"], 3)
        self.assertTrue(self.process.stdin.closed)

    def test_the_pipe_is_bounded_so_the_audio_thread_never_waits(self):
        """A child that stops reading must not stall the game's audio thread."""
        gate = threading.Event()
        self.process.stdin._block = gate
        self.output.start()
        pushes = []
        for _ in range(40):
            pushes.append(self.output.push(b"\x00" * 3840))
            if not pushes[-1]:
                break
        gate.set()
        self.assertIn(False, pushes, "the budget answers instead of blocking")

    def test_slots_are_leased_and_handed_back(self):
        self.output.start()
        slots = [self.output.lease_slot() for _ in range(output_device.MAX_SLOTS)]
        self.assertEqual(sorted(slots), list(range(output_device.MAX_SLOTS)))
        self.assertIsNone(self.output.lease_slot(), "a crossfade has two, never more")
        self.output.release_slot(slots[1])
        self.assertEqual(self.output.lease_slot(), slots[1])

    def test_handing_a_slot_back_does_not_clip_the_song_it_was_playing(self):
        """A streamer's teardown must not cut the last frames of a song."""
        self.output.start()
        slot = self.output.lease_slot()
        self.output.release_slot(slot)
        self.assertEqual(self.frames(), [], "no frame is sent by releasing")
        self.assertEqual(self.output.lease_slot(), slot, "and the slot is free again")

    def test_a_specifier_reads_as_a_menu_line(self):
        self.assertEqual(
            output_device.short_name("OpenAL Soft on Speakers (Realtek)"),
            "Speakers (Realtek)")
        self.assertEqual(output_device.short_name("Speakers"), "Speakers")

    def test_the_device_list_is_each_device_once(self):
        fake = SimpleNamespace(util=SimpleNamespace(
            get_all_device_specifiers=lambda: ["OpenAL Soft on A", "OpenAL Soft on A",
                                               "OpenAL Soft on B", ""]))
        with mock.patch.dict(sys.modules, {"cyal": fake}):
            self.assertEqual(output_device.available_devices(),
                             ("OpenAL Soft on A", "OpenAL Soft on B"))

    def test_the_child_is_started_with_an_argv_list(self):
        self.output.start()
        command, kwargs = self.popen.started[0]
        self.assertIsInstance(command, list)
        self.assertIn(output_device.DEVICE_FLAG, command)
        self.assertEqual(command[-1], "OpenAL Soft on Speaker")
        self.assertFalse(kwargs["shell"], "never a shell string")


# ---------------------------------------------------------------- the child


class WorkerTests(unittest.TestCase):
    def run_worker(self, frames, context=None):
        context = context or FakeContext()
        stream = io.BytesIO(frames)
        out = io.StringIO()
        status = device_worker.play("OpenAL Soft on Speaker", stream, out,
                                    context_factory=lambda device: context)
        return status, context, out.getvalue()

    @staticmethod
    def frame(kind, slot, payload=b""):
        return output_device.HEADER.pack(kind, slot, len(payload)) + payload

    def test_frames_are_queued_onto_the_device_it_was_given(self):
        frames = (self.frame(output_device.FORMAT, 0, struct.pack(">HI", 2, 48000))
                  + self.frame(output_device.AUDIO, 0, b"\x00\x01" * 960)
                  + self.frame(output_device.AUDIO, 0, b"\x00\x01" * 960)
                  + self.frame(output_device.QUIT, 0))
        status, context, report = self.run_worker(frames)
        self.assertEqual(status, 0)
        source = context.sources[0]
        self.assertEqual(source.total_queued, 2)
        self.assertEqual(context.buffers[0].rate, 48000)
        self.assertEqual(context.buffers[0].format.name, "STEREO16")
        self.assertEqual(source.played, 1, "it starts once it has frames")
        self.assertTrue(source.deleted, "and it is released on exit")

    def test_a_mono_format_is_what_the_buffers_are_declared_as(self):
        frames = (self.frame(output_device.FORMAT, 0, struct.pack(">HI", 1, 48000))
                  + self.frame(output_device.AUDIO, 0, b"\x00" * 960)
                  + self.frame(output_device.QUIT, 0))
        _status, context, _report = self.run_worker(frames)
        self.assertEqual(context.buffers[0].format.name, "MONO16")

    def test_gain_pause_and_flush_land_on_their_slot(self):
        frames = (self.frame(output_device.FORMAT, 0, struct.pack(">HI", 2, 48000))
                  + self.frame(output_device.FORMAT, 1, struct.pack(">HI", 2, 48000))
                  + self.frame(output_device.GAIN, 1, struct.pack(">f", 0.5))
                  + self.frame(output_device.PAUSE, 1, b"\x01")
                  + self.frame(output_device.FLUSH, 1)
                  + self.frame(output_device.QUIT, 0))
        _status, context, _report = self.run_worker(frames)
        first, second = context.sources
        self.assertEqual(first.gain, 1.0, "slot 0 was not the one asked")
        self.assertEqual(second.gain, 0.5)
        self.assertTrue(second.paused)
        self.assertGreaterEqual(second.flushed, 1)

    def test_a_crossfade_is_two_sources_on_one_device(self):
        frames = (self.frame(output_device.FORMAT, 0, struct.pack(">HI", 2, 48000))
                  + self.frame(output_device.FORMAT, 1, struct.pack(">HI", 2, 48000))
                  + self.frame(output_device.AUDIO, 0, b"\x00" * 3840)
                  + self.frame(output_device.AUDIO, 1, b"\x00" * 3840)
                  + self.frame(output_device.QUIT, 0))
        _status, context, _report = self.run_worker(frames)
        self.assertEqual(len(context.sources), 2)
        self.assertTrue(all(source.played for source in context.sources))

    def test_a_truncated_stream_stops_instead_of_guessing(self):
        frames = self.frame(output_device.AUDIO, 0, b"\x00" * 3840)[:-100]
        status, context, report = self.run_worker(frames)
        self.assertEqual(status, 0, "an end of stream is not a failure")
        self.assertEqual(
            context.sources, [],
            "a half frame is not played as if it were a whole one")
        self.assertIn('"played_frames"', report)

    def test_a_device_that_will_not_open_says_so_and_exits(self):
        def refuse(device):
            raise OSError("no such device")
        status = device_worker.play("OpenAL Soft on Nothing", io.BytesIO(b""),
                                    io.StringIO(), context_factory=refuse)
        self.assertEqual(status, device_worker.EXIT_NO_DEVICE)

    def test_the_report_is_one_json_line(self):
        status, _context, report = self.run_worker(self.frame(output_device.QUIT, 0))
        self.assertEqual(status, 0)
        self.assertEqual(len(report.strip().splitlines()), 1)
        self.assertIn('"device"', report)


# ------------------------------------------------------------------ the bot


class StreamerRoutingTests(unittest.TestCase):
    """`_queue_local`: one frame, one destination, in one order of precedence."""

    def streamer(self, **attrs):
        from libs.music_bot.streaming import AudioStreamer
        streamer = AudioStreamer.__new__(AudioStreamer)
        streamer._pending_room = None
        streamer.cinema = None
        streamer.spatial_pair = None
        streamer.remote = None
        streamer.remote_slot = 0
        streamer.channels = 2
        streamer._direct_seek_to = 0.0
        streamer._fed_content_seconds = 0.0
        for key, value in attrs.items():
            setattr(streamer, key, value)
        return streamer

    def test_a_chosen_device_takes_the_frame_instead_of_openal(self):
        remote = mock.MagicMock()
        remote.alive.return_value = True
        remote.push.return_value = True
        streamer = self.streamer(remote=remote, remote_slot=1)
        streamer._get_buffer = mock.Mock()
        self.assertTrue(streamer._queue_local(b"\x00" * 3840))
        remote.push.assert_called_once_with(b"\x00" * 3840, 1)
        streamer._get_buffer.assert_not_called()
        self.assertAlmostEqual(streamer.content_position(), 0.02, places=6)

    def test_a_full_queue_holds_the_frame_for_another_try(self):
        remote = mock.MagicMock()
        remote.alive.return_value = True
        remote.push.return_value = False
        streamer = self.streamer(remote=remote)
        self.assertFalse(streamer._queue_local(b"\x00" * 3840))
        self.assertEqual(streamer.content_position(), 0.0,
                         "a frame that was not taken is not content played")

    def test_a_room_keeps_a_song_its_speakers_have_a_position_for(self):
        remote = mock.MagicMock()
        remote.alive.return_value = True
        streamer = self.streamer(remote=remote, cinema=object())
        streamer._queue_local_cinema = mock.Mock(return_value=True)
        self.assertTrue(streamer._queue_local(b"\x00" * 3840))
        remote.push.assert_not_called()

    def test_a_dead_child_hands_the_song_back_to_the_game(self):
        remote = mock.MagicMock()
        remote.alive.return_value = False
        streamer = self.streamer(remote=remote)
        streamer._get_buffer = mock.Mock(return_value=None)
        self.assertFalse(streamer._queue_local(b"\x00" * 3840))
        streamer._get_buffer.assert_called_once()
        self.assertIsNone(streamer.remote, "and it stops being asked again")


class BotRoutingTests(SaidMixin, unittest.TestCase):
    def setUp(self):
        self.bot = make_bot()

    def test_nothing_is_opened_until_a_device_is_chosen(self):
        self.assertIsNone(self.bot._music_output_process())
        self.assertEqual(self.bot._stream_remote(), (None, 0))
        self.assertEqual(self.bot.output_device_label(), "Same as the game")

    def test_a_chosen_device_is_one_child_with_a_slot_per_stream(self):
        self.bot.output_device = "OpenAL Soft on Speaker"
        process = mock.MagicMock()
        process.alive.return_value = True
        process.start.return_value = process
        process.lease_slot.side_effect = [0, 1]
        with mock.patch.object(output_device, "MusicOutputProcess",
                               return_value=process) as factory:
            remote, slot = self.bot._stream_remote()
        factory.assert_called_once_with("OpenAL Soft on Speaker")
        self.assertIs(remote, process)
        self.assertEqual(slot, 0)
        remote2, slot2 = self.bot._stream_remote()
        self.assertIs(remote2, remote)
        self.assertEqual(slot2, 1, "a crossfade's second song gets its own slot")

    def test_a_room_speaker_is_never_taken_over(self):
        """A cabinet's room is map objects on the game's device, not a DEVICE."""
        self.bot.output_device = "OpenAL Soft on Speaker"
        self.bot.cinema_bank = object()
        self.assertEqual(self.bot._stream_remote(), (None, 0))

    def test_a_device_that_will_not_open_is_said_once_and_forgotten(self):
        self.bot.output_device = "OpenAL Soft on Gone"
        with self.say(), mock.patch.object(
                output_device, "MusicOutputProcess",
                side_effect=OSError("device vanished")):
            self.assertIsNone(self.bot._music_output_process())
        self.assertEqual(self.bot.output_device, "")
        self.assertEqual(len(self.bot.said), 1)
        self.assertIn("game's own device", self.bot.said[0])

    def test_a_child_that_died_falls_back_and_says_so(self):
        self.bot.output_device = "OpenAL Soft on Speaker"
        dead = mock.MagicMock()
        dead.alive.return_value = False
        self.bot._output_process = dead
        with self.say():
            self.assertIsNone(self.bot._music_output_process())
        self.assertEqual(self.bot.output_device, "")
        self.assertIn("stopped", self.bot.said[0])


class BotSelectionTests(SaidMixin, unittest.TestCase):
    def setUp(self):
        self.bot = make_bot()

    def test_choosing_a_device_is_persisted_and_said(self):
        old = mock.MagicMock()
        self.bot._output_process = old
        with self.say(), mock.patch("libs.music_bot.controller.speak",
                                    lambda text, *rest: self.bot.said.append(text)), \
                mock.patch("libs.music_bot.controller.options") as options_module:
            self.bot.select_output_device("OpenAL Soft on Speakers")
        self.assertEqual(self.bot.output_device, "OpenAL Soft on Speakers")
        options_module.set.assert_called_with("music_bot_output_device",
                                              "OpenAL Soft on Speakers")
        old.stop.assert_called_once()
        self.assertEqual(self.bot.output_device_label(), "Speakers")
        self.assertIn("Music output: Speakers", self.bot.said[0])
        self.assertIn("equalizer", self.bot.said[0].lower())

    def test_going_back_to_the_game_s_device_says_that(self):
        self.bot.output_device = "OpenAL Soft on Speakers"
        with self.say(), mock.patch("libs.music_bot.controller.speak",
                                    lambda text, *rest: self.bot.said.append(text)), \
                mock.patch("libs.music_bot.controller.options"):
            self.bot.select_output_device("")
        self.assertEqual(self.bot.output_device, "")
        self.assertIn("game's own device", self.bot.said[0])

    def test_choosing_the_same_device_changes_nothing(self):
        self.bot.output_device = "OpenAL Soft on Speakers"
        with mock.patch("libs.music_bot.controller.options") as options_module:
            self.bot.select_output_device("OpenAL Soft on Speakers")
        options_module.set.assert_not_called()

    def test_stopping_the_bot_is_what_silences_the_other_device(self):
        process = mock.MagicMock()
        self.bot._output_process = process
        for name in ("_cancel_crossfade", "_begin_playback_generation",
                     "_release_cinema_bank", "_stop_local", "_stop_subtitles",
                     "_clear_track_queue", "_clear_next_up_queue",
                     "_clear_personal_feed"):
            setattr(self.bot, name, mock.Mock())
        self.bot.searching = False
        self.bot.is_loading_stream = False
        self.bot.mode = "idle"
        self.bot._stream_announced = False
        self.bot._current_reverb_slot = None
        self.bot.stream_source = None
        with mock.patch("libs.music_bot.controller.speak"):
            self.bot.stop()
        process.flush.assert_called_once()

    def test_a_fade_out_is_not_cut_short(self):
        process = mock.MagicMock()
        self.bot._output_process = process
        for name in ("_cancel_crossfade", "_begin_playback_generation",
                     "_release_cinema_bank", "_stop_local", "_stop_subtitles",
                     "_clear_track_queue", "_clear_next_up_queue",
                     "_clear_personal_feed"):
            setattr(self.bot, name, mock.Mock())
        self.bot.searching = False
        self.bot.is_loading_stream = False
        self.bot.mode = "idle"
        self.bot._stream_announced = False
        self.bot._current_reverb_slot = None
        self.bot.stream_source = None
        with mock.patch("libs.music_bot.controller.speak"):
            self.bot.stop(fade=True)
        process.flush.assert_not_called()

    def test_leaving_the_bot_hands_the_device_back(self):
        """A silent child must not keep a speaker open after Music Bot is gone."""
        self.bot.download_mgr = mock.Mock()
        self.bot.audio_recorder = mock.Mock()
        self.bot.soundgroup = mock.Mock()
        self.bot.live_relay_streamer = None
        for name in ("_cancel_crossfade", "_begin_playback_generation",
                     "_release_cinema_bank", "_stop_local", "_stop_subtitles",
                     "_clear_track_queue", "_clear_next_up_queue",
                     "_clear_personal_feed"):
            setattr(self.bot, name, mock.Mock())
        self.bot.searching = False
        self.bot.is_loading_stream = False
        self.bot.mode = "idle"
        self.bot._stream_announced = False
        self.bot._current_reverb_slot = None
        self.bot.stream_source = None
        process = mock.MagicMock()
        self.bot._output_process = process
        with mock.patch("libs.music_bot.controller.speak"):
            self.bot.destroy()
        process.stop.assert_called_once()
        self.assertIsNone(self.bot._output_process)

    def test_a_playing_song_is_restarted_where_it_stood(self):
        """A stream's frames belong to the device they were opened on."""
        streamer = mock.MagicMock()
        streamer.is_alive.return_value = True
        self.bot.playing = True
        self.bot.streamer = streamer
        self.bot.track_position = lambda: 42.5
        self.bot._seek_restart = mock.Mock()
        with self.say(), mock.patch("libs.music_bot.controller.speak"), \
                mock.patch("libs.music_bot.controller.options"):
            self.bot.select_output_device("OpenAL Soft on Speakers")
        self.bot._seek_restart.assert_called_once_with(42.5, announce=False)


class FakeMenu():
    """What `libs.menu.Menu` is replaced with: it keeps the items it was given."""

    built = []

    def __init__(self, *args, **kwargs):
        self.items = []
        self.spoken = 0
        FakeMenu.built.append(self)

    def add_items(self, items):
        self.items = list(items)

    def speak_current_item(self):
        self.spoken += 1


class SettingsMenuTests(unittest.TestCase):
    def setUp(self):
        FakeMenu.built = []
        self.addCleanup(mock.patch.stopall)
        self.bot = make_bot()
        self.bot._find_gameplay = lambda: SimpleNamespace(
            add_substate=lambda item: None, pop_last_substate=lambda: None)
        from libs.music_bot import menus as menus_module
        self.handler = menus_module.MusicBotMenus(self.bot)
        mock.patch("libs.menu.Menu", FakeMenu).start()
        mock.patch("libs.menus.set_default_sounds").start()

    def settings_labels(self):
        self.handler._open_settings_menu()
        return [label() if callable(label) else label
                for label, _action in FakeMenu.built[-1].items]

    def test_the_settings_menu_carries_the_output_line(self):
        self.assertIn("Music Output Device: Same as the game",
                      self.settings_labels())

    def test_the_output_line_leads_and_the_muffle_sits_last(self):
        """The device is what a player comes here for; the muffle is not its neighbour."""
        labels = self.settings_labels()
        self.assertEqual(labels[0], "Music Output Device: Same as the game")
        self.assertEqual(labels[-2], "Underwater Muffle: ON")
        self.assertEqual(labels[-1], "Back")
        self.assertGreater(labels.index("Underwater Muffle: ON") -
                           labels.index("Music Output Device: Same as the game"), 1)

    def test_the_line_names_the_device_that_is_chosen(self):
        self.bot.output_device = "OpenAL Soft on Speakers"
        self.assertIn("Music Output Device: Speakers", self.settings_labels())

    def test_the_device_menu_lists_the_game_then_every_device(self):
        from libs.music_bot import menus as menus_module
        self.bot.select_output_device = mock.Mock()
        with mock.patch.object(menus_module.output_device, "available_devices",
                               lambda: ("OpenAL Soft on Speaker",
                                        "OpenAL Soft on Headphones")):
            self.handler._open_output_device_menu()
        items = FakeMenu.built[-1].items
        labels = [label for label, _action in items]
        self.assertEqual(labels, ["Same as the game", "Speaker", "Headphones",
                                  "Back"])
        # Picking one asks the bot for that exact specifier.
        items[1][1]()
        self.bot.select_output_device.assert_called_once_with(
            "OpenAL Soft on Speaker")
        items[0][1]()
        self.bot.select_output_device.assert_called_with("")

    def test_the_chosen_device_is_marked_in_the_list(self):
        from libs.music_bot import menus as menus_module
        self.bot.output_device = "OpenAL Soft on Speaker"
        self.bot.select_output_device = mock.Mock()
        with mock.patch.object(menus_module.output_device, "available_devices",
                               lambda: ("OpenAL Soft on Speaker",)):
            self.handler._open_output_device_menu()
        labels = [label for label, _action in FakeMenu.built[-1].items]
        self.assertEqual(labels[1], "Speaker (current)")


if __name__ == "__main__":
    unittest.main()
