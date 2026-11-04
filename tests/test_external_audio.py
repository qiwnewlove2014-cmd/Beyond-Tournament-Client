"""Pins for the external-program audio source (libs/external_audio.py).

The feature is "another program's sound, on the legs this bot already has",
so the tests have two halves:

* the source's own contract -- framing, resampling, the mono/stereo choice,
  the level, the bounded queue and a capture that dies without taking the
  game with it;
* a 36-scenario simulation matrix over the combinations a real machine
  produces (48 k/44.1 k, mono/stereo program, mono/stereo mode, quiet/loud
  level, centred/one-sided signal), plus the four shapes that are not a
  matrix cell: silence, a half frame, an empty packet and a clipping sum.

The mixer half is pinned on `AudioStreamer`'s static helpers, because that is
where "stereo survives the mix" is actually decided.
"""

import time
import unittest
from collections import deque
from types import SimpleNamespace
from unittest import mock

import numpy as np

from libs.external_audio import (
    display_program_name,
    is_virtual_device,
    DEFAULT_GAIN,
    FRAME_BYTES,
    LIVE_FRAME_BYTES,
    MAX_GAIN,
    MODE_MONO,
    MODE_STEREO,
    MONO_FRAME_BYTES,
    ExternalAudioSource,
    SourceUnavailable,
    clamped_gain,
    downmix_to_mono,
    mode_label,
    running_apps,
    shape_frame,
    to_stereo48k,
)


def pcm(frames=960, value=1000, channels=2, left_only=False):
    """One block of interleaved s16 with a constant level."""
    block = np.zeros((frames, channels), dtype=np.int16)
    block[:, 0] = value
    if channels > 1 and not left_only:
        block[:, 1] = value
    return block.tobytes()


def sides(stereo_bytes):
    """(left, right) int16 arrays of an interleaved stereo block."""
    block = np.frombuffer(stereo_bytes, dtype=np.int16).reshape(-1, 2)
    return block[:, 0], block[:, 1]


class FakeBackend:
    """A capture backend that answers from a script instead of a sound card."""

    def __init__(self, packets=(), open_error=None, read_error=None):
        self.packets = list(packets)
        self.open_error = open_error
        self.read_error = read_error
        self.opened = False
        self.closed = False
        self.reads = 0

    def open(self):
        if self.open_error:
            raise SourceUnavailable(self.open_error)
        self.opened = True

    def read(self, timeout=0.1):
        self.reads += 1
        if self.packets:
            return self.packets.pop(0)
        if self.read_error:
            raise SourceUnavailable(self.read_error)
        return None

    def close(self):
        self.closed = True


class TestFraming(unittest.TestCase):
    def test_mono_is_duplicated_into_both_sides(self):
        stereo = to_stereo48k(pcm(channels=1, value=700), channels=1, rate=48000)
        left, right = sides(stereo)
        self.assertEqual(len(stereo), FRAME_BYTES)
        self.assertTrue((left == right).all())
        self.assertTrue((left == 700).all())

    def test_stereo_passes_through_unchanged(self):
        source = pcm(left_only=True, value=400)
        self.assertEqual(to_stereo48k(source, 2, 48000), source)

    def test_44100_is_resampled_to_twenty_ms(self):
        block = pcm(frames=882, value=1200, channels=2)
        stereo = to_stereo48k(block, channels=2, rate=44100)
        self.assertEqual(len(stereo), FRAME_BYTES)
        left, right = sides(stereo)
        # A constant signal survives linear interpolation at a constant level.
        self.assertTrue((left == 1200).all())
        self.assertTrue((right == 1200).all())

    def test_44100_mono_is_resampled_and_duplicated(self):
        stereo = to_stereo48k(pcm(frames=882, channels=1, value=500), 1, 44100)
        left, right = sides(stereo)
        self.assertEqual(len(stereo), FRAME_BYTES)
        self.assertTrue((left == right).all())

    def test_a_partial_sample_is_dropped_not_padded(self):
        odd = pcm(frames=4) + b"\x01"
        self.assertEqual(len(to_stereo48k(odd, 2, 48000)) % 4, 0)

    def test_empty_packet_is_empty(self):
        self.assertEqual(to_stereo48k(b"", 2, 48000), b"")
        self.assertEqual(to_stereo48k(b"\x01", 2, 48000), b"")

    def test_more_than_two_channels_keeps_the_first_pair(self):
        block = np.zeros((960, 6), dtype=np.int16)
        block[:, 0] = 10
        block[:, 1] = 20
        block[:, 2:] = 999
        left, right = sides(to_stereo48k(block.tobytes(), 6, 48000))
        self.assertTrue((left == 10).all())
        self.assertTrue((right == 20).all())


class TestShapeFrame(unittest.TestCase):
    def test_stereo_keeps_the_two_sides_apart(self):
        source = pcm(frames=960, left_only=True, value=900)
        shaped = shape_frame(source, MODE_STEREO, 1.0)
        left, right = sides(shaped)
        self.assertTrue((left == 900).all())
        self.assertTrue((right == 0).all())

    def test_mono_centres_one_average_on_both_sides(self):
        block = np.zeros((960, 2), dtype=np.int16)
        block[:, 0] = 1000
        block[:, 1] = 0
        left, right = sides(shape_frame(block.tobytes(), MODE_MONO, 1.0))
        self.assertTrue((left == 500).all())
        self.assertTrue((right == 500).all())

    def test_gain_scales_every_sample(self):
        left, right = sides(shape_frame(pcm(value=1000), MODE_STEREO, 0.5))
        self.assertTrue((left == 500).all())
        self.assertTrue((right == 500).all())

    def test_gain_clips_at_the_format_edge_instead_of_wrapping(self):
        left, _right = sides(shape_frame(pcm(value=20000), MODE_STEREO, 2.0))
        self.assertTrue((left == 32767).all())

    def test_zero_gain_is_silence(self):
        left, right = sides(shape_frame(pcm(value=5000), MODE_STEREO, 0.0))
        self.assertTrue((left == 0).all() and (right == 0).all())

    def test_gain_is_clamped_to_the_supported_range(self):
        self.assertEqual(clamped_gain(99), MAX_GAIN)
        self.assertEqual(clamped_gain(-3), 0.0)
        self.assertEqual(clamped_gain("nonsense"), DEFAULT_GAIN)
        self.assertEqual(clamped_gain(float("nan")), DEFAULT_GAIN)

    def test_mode_label_only_understands_the_two_names(self):
        self.assertEqual(mode_label("MONO"), MODE_MONO)
        self.assertEqual(mode_label("stereo"), MODE_STEREO)
        self.assertEqual(mode_label("whatever"), MODE_STEREO)

    def test_short_payload_is_trimmed_to_whole_frames(self):
        shaped = shape_frame(pcm(frames=3) + b"\x00\x00", MODE_STEREO, 1.0)
        self.assertEqual(len(shaped), 3 * 4)


class TestDownmix(unittest.TestCase):
    def test_average_of_the_two_sides(self):
        block = np.zeros((960, 2), dtype=np.int16)
        block[:, 0] = 900
        block[:, 1] = 300
        mono = downmix_to_mono(block.tobytes())
        self.assertEqual(len(mono), MONO_FRAME_BYTES)
        self.assertTrue((np.frombuffer(mono, dtype=np.int16) == 600).all())

    def test_empty_stays_empty(self):
        self.assertEqual(downmix_to_mono(b""), b"")


class TestSource(unittest.TestCase):
    def test_frames_are_exactly_twenty_ms(self):
        source = ExternalAudioSource(None)
        source._stage(pcm(frames=960))
        self.assertEqual(len(source.frames), 1)
        self.assertEqual(len(source.frames[0]), FRAME_BYTES)

    def test_the_live_cut_is_ten_ms_beside_the_song_cut(self):
        seen = []
        source = ExternalAudioSource(None, live_sink=seen.append)
        source._stage(pcm(frames=480))
        self.assertEqual(len(seen), 1)                 # 10 ms live
        self.assertEqual(len(seen[0]), LIVE_FRAME_BYTES)
        self.assertEqual(len(source.frames), 0)        # 20 ms song still filling
        source._stage(pcm(frames=480))
        self.assertEqual(len(seen), 2)
        self.assertEqual(len(source.frames), 1)
        self.assertEqual(len(source.frames[0]), FRAME_BYTES)

    def test_a_half_frame_waits_for_the_rest(self):
        source = ExternalAudioSource(None)
        source._stage(pcm(frames=480))
        self.assertEqual(len(source.frames), 0)
        source._stage(pcm(frames=480))
        self.assertEqual(len(source.frames), 1)

    def test_sink_receives_every_shaped_frame(self):
        seen = []
        source = ExternalAudioSource(None, sink=seen.append, mode=MODE_MONO)
        source._stage(pcm(frames=1920, left_only=True, value=800))
        self.assertEqual(len(seen), 2)
        for frame in seen:
            left, right = sides(frame)
            self.assertTrue((left == right).all())

    def test_drain_respects_the_per_frame_budget(self):
        source = ExternalAudioSource(None)
        source._stage(pcm(frames=960 * 5))
        self.assertEqual(len(source.drain(limit=2)), 2)
        self.assertEqual(len(source.drain(limit=2)), 2)
        self.assertEqual(len(source.frames), 1)

    def test_the_buffer_drops_the_oldest_instead_of_growing(self):
        source = ExternalAudioSource(None)
        source.frames = type(source.frames)(maxlen=2)
        source._stage(pcm(frames=960 * 3, value=100))
        self.assertEqual(len(source.frames), 2)
        self.assertEqual(source.dropped, 1)

    def test_a_refused_device_is_reported_not_raised(self):
        source = ExternalAudioSource(FakeBackend(open_error="no device"))
        source.run()
        self.assertEqual(source.error, "no device")
        self.assertFalse(source.running)

    def test_a_capture_that_dies_mid_stream_closes_its_backend(self):
        backend = FakeBackend(packets=[(pcm(), 2, 48000)],
                              read_error="device removed")
        source = ExternalAudioSource(backend)
        source.run()
        self.assertEqual(source.error, "device removed")
        self.assertTrue(backend.closed)

    def test_stop_clears_the_pending_tail(self):
        source = ExternalAudioSource(None)
        source._stage(pcm(frames=480))
        source.stop()
        self.assertEqual(len(source._pending), 0)

    def test_status_names_the_mode_and_level(self):
        source = ExternalAudioSource(None, mode=MODE_MONO, gain=0.6,
                                     label="Ableton")
        self.assertIn("Ableton", source.status_line())
        self.assertIn("mono", source.status_line())
        self.assertIn("60%", source.status_line())

    def test_a_silent_capture_says_so_after_the_idle_window(self):
        source = ExternalAudioSource(None, label="Ableton")
        source.started_at -= 10.0
        self.assertIn("no sound yet", source.status_line())

    def test_an_error_outranks_the_idle_notice(self):
        source = ExternalAudioSource(None, label="Ableton")
        source.started_at -= 10.0
        source.error = "blocked"
        self.assertIn("stopped", source.status_line())


class TestStreamerMixing(unittest.TestCase):
    """`AudioStreamer._mix_external_into_stereo` -- the stereo-survives rule."""

    @classmethod
    def setUpClass(cls):
        from libs.music_bot.streaming import AudioStreamer
        cls.mix = staticmethod(AudioStreamer._mix_external_into_stereo)

    def _silent_song(self):
        return b"\x00" * FRAME_BYTES

    def test_a_one_sided_capture_stays_on_its_side(self):
        captured = shape_frame(pcm(frames=960, left_only=True, value=8000),
                               MODE_STEREO, 1.0)
        out = self.mix(self._silent_song(), [captured], ("external-test", 1))
        left, right = sides(out)
        self.assertTrue((left > 0).all())
        self.assertTrue((right == 0).all())

    def test_a_centred_capture_reaches_both_sides(self):
        captured = shape_frame(pcm(frames=960, value=6000), MODE_MONO, 1.0)
        out = self.mix(self._silent_song(), [captured], ("external-test", 2))
        left, right = sides(out)
        self.assertTrue((left > 0).all())
        self.assertTrue((np.array_equal(left, right)))

    def test_the_song_keeps_its_own_sides(self):
        song = np.zeros((960, 2), dtype=np.int16)
        song[:, 0] = 4000
        out = self.mix(song.tobytes(),
                       [shape_frame(pcm(value=0), MODE_STEREO, 1.0)],
                       ("external-test", 3))
        left, right = sides(out)
        self.assertTrue((left > 0).all())
        self.assertTrue((right == 0).all())

    def test_a_short_capture_frame_does_not_truncate_the_song(self):
        out = self.mix(self._silent_song(),
                       [pcm(frames=100, value=500)],
                       ("external-test", 4))
        self.assertEqual(len(out), FRAME_BYTES)

    def test_nothing_to_mix_returns_the_song_unchanged(self):
        self.assertEqual(self.mix(self._silent_song(), [], ("x", 5)),
                         self._silent_song())
        self.assertEqual(self.mix(b"", [pcm()], ("x", 6)), b"")

    def test_a_loud_capture_does_not_wrap_the_image(self):
        captured = shape_frame(pcm(value=32767), MODE_STEREO, 2.0)
        song = pcm(frames=960, value=32767)
        left, right = sides(self.mix(song, [captured], ("external-test", 7)))
        self.assertTrue((left >= 0).all())
        self.assertTrue((right >= 0).all())


def _matrix():
    """The 32 cells of the real-machine matrix plus 4 named shapes."""
    cases = []
    for rate in (48000, 44100):
        for channels in (1, 2):
            for mode in (MODE_STEREO, MODE_MONO):
                for gain in (0.85, 2.0):
                    for signal in ("tone", "left_only"):
                        cases.append({
                            "name": f"{rate}/{channels}ch/{mode}/{gain}/{signal}",
                            "rate": rate,
                            "channels": channels,
                            "mode": mode,
                            "gain": gain,
                            "value": 20000 if signal == "left_only" else 1000,
                            "left_only": signal == "left_only",
                        })
    cases.extend([
        {"name": "digital silence", "rate": 48000, "channels": 2,
         "mode": MODE_STEREO, "gain": 1.0, "value": 0, "left_only": False},
        {"name": "a half frame", "rate": 48000, "channels": 2,
         "mode": MODE_STEREO, "gain": 1.0, "value": 900, "left_only": False,
         "frames": 480},
        {"name": "an empty packet", "rate": 48000, "channels": 2,
         "mode": MODE_STEREO, "gain": 1.0, "value": 0, "left_only": False,
         "frames": 0},
        {"name": "a clipping sum", "rate": 48000, "channels": 2,
         "mode": MODE_STEREO, "gain": 2.0, "value": 32767, "left_only": False},
    ])
    return cases


class TestScenarioMatrix(unittest.TestCase):
    """36 simulated captures, each asserting the contract of its own shape."""

    def test_every_scenario_holds(self):
        cases = _matrix()
        self.assertEqual(len(cases), 36)
        for case in cases:
            with self.subTest(case["name"]):
                frames = case.get("frames", 960 if case["rate"] == 48000 else 882)
                block = pcm(frames=frames, value=case["value"],
                            channels=case["channels"],
                            left_only=case["left_only"] and case["channels"] == 2)
                stereo = to_stereo48k(block, case["channels"], case["rate"])
                if not stereo:
                    self.assertEqual(frames, 0)
                    continue
                shaped = shape_frame(stereo, case["mode"], case["gain"])
                self.assertEqual(len(shaped) % 4, 0)
                left, right = sides(shaped)
                self.assertEqual(len(left), len(right))
                # The mode decides whether the two sides may differ.
                if case["mode"] == MODE_MONO:
                    self.assertTrue(np.array_equal(left, right))
                # A one-sided program stays one-sided in stereo mode.
                if (case["mode"] == MODE_STEREO and case["channels"] == 2
                        and case["left_only"]):
                    self.assertTrue((right == 0).all())
                # A positive signal never wraps negative, whatever the level.
                self.assertTrue((left >= 0).all())
                self.assertTrue((right >= 0).all())
                # And the level is the one that was asked for, up to clipping.
                if case["gain"] == 1.0 and case["value"] <= 16000:
                    self.assertTrue((left <= case["value"]).all())


class TestOneMixPerLeg(unittest.TestCase):
    """An external capture is mixed once, on the leg that is being sent.

    This is a source guard on purpose: the two mixes are chosen inside the
    streamer's send path (stereo for a Party Sync guest or a room, mono for the
    PA and a public broadcast), and the bug it pins is invisible until a
    listener hears the program twice and without its sides -- `live_frames`
    feeds the centre of the stereo mix, so an external frame placed there as
    well as in `_mix_external_into_stereo` is summed twice.
    """

    def test_the_mono_mix_keeps_its_own_list(self):
        from pathlib import Path
        source = (Path(__file__).resolve().parents[1] / "libs" / "music_bot"
                  / "streaming.py").read_text(encoding="utf-8")
        source = source.replace("\r\n", "\n")
        self.assertIn("[(mono_data, 0.75)] + mono_live_frames", source)
        self.assertNotIn("[(mono_data, 0.75)] + live_frames", source)
        self.assertIn(
            "self._mix_live_into_stereo(\n"
            "                        payload, live_frames, mix_limiter_key)",
            source)
        self.assertIn(
            "self._mix_external_into_stereo(\n"
            "                        payload, external_frames, mix_limiter_key)",
            source)


class TestWhereTheEntryLives(unittest.TestCase):
    """The entry is in Party Sync, and the page it opens is short.

    The first version was one page that listed every program the moment it
    opened, reached from a Music Bot menu that is already long: a screen
    reader had to walk the wall before it found the search -- which did not
    exist. These are source guards because that shape is a menu layout, not a
    number; the pages themselves are built only with a live Gameplay.
    """

    def _source(self, *parts):
        from pathlib import Path
        path = Path(__file__).resolve().parents[1].joinpath(*parts)
        return path.read_text(encoding="utf-8").replace("\r\n", "\n")

    def test_the_music_bot_menu_no_longer_carries_it(self):
        source = self._source("libs", "music_bot", "menus.py")
        self.assertNotIn("External audio from a music program", source)
        self.assertNotIn("go_external_audio", source)

    def test_the_party_sync_page_carries_it(self):
        source = self._source("libs", "music_bot", "menu_pages", "party.py")
        self.assertIn(
            '"External audio from a music program", do_external_audio, None,',
            source)
        # A guest may capture only with the host's grant: the line follows
        # the same rule the server enforces on every frame.
        self.assertIn('may_send = (ps is None or ps.role != "guest"', source)
        self.assertIn('may_stream = getattr(ps, "may_stream", None)', source)
        self.assertIn('_options.get("username", "")', source)

    def test_the_root_page_does_not_list_programs(self):
        source = self._source("libs", "music_bot", "menu_pages", "external.py")
        body = source.split("def _open_external_audio_menu", 1)[1]
        body = body.split("def _open_external_program_menu", 1)[0]
        self.assertNotIn("playing_apps", body)
        self.assertNotIn("for app in", body)
        # ...and the picker is where the search and the cap live.
        picker = source.split("def _open_external_program_menu", 1)[1]
        self.assertIn("game.input.run", picker)
        self.assertIn('kind="system"', picker)
        self.assertIn("self.PAGE_MATCHES", picker)
        self.assertIn("PAGE_MATCHES = 24", source)
        self.assertIn("DEVICE_MATCHES = 8", source)


class TestSignalVerdict(unittest.TestCase):
    """Whether sound is arriving, said in words a player can act on.

    The question this answers is the one the first version could not: "I
    picked the program -- is it working?" A name, a mode and a level cannot
    tell a muted program from a capture that never opened.
    """

    def test_a_silent_program_and_a_quiet_one_read_differently(self):
        silent = ExternalAudioSource(None)
        silent._note_signal(b"\x00" * FRAME_BYTES)
        self.assertIn("silent", silent.signal_line())
        quiet = ExternalAudioSource(None)
        quiet._note_signal(pcm(value=300))
        self.assertIn("quiet", quiet.signal_line())

    def test_a_good_level_says_good_with_its_peak(self):
        source = ExternalAudioSource(None)
        source._note_signal(pcm(value=12000))
        self.assertIn("good", source.signal_line())
        self.assertIn("peak", source.signal_line())

    def test_full_scale_is_reported_as_clipping_not_good(self):
        source = ExternalAudioSource(None)
        source._note_signal(pcm(value=32767))
        self.assertIn("clipping", source.signal_line())

    def test_a_capture_that_stops_arriving_says_how_long(self):
        source = ExternalAudioSource(None, label="Ableton")
        source._note_signal(pcm(value=1000))
        source.signal_log.clear()
        source.last_frame_at = time.monotonic() - 5
        self.assertIn("nothing arriving", source.signal_line())

    def test_the_window_forgets_an_old_peak(self):
        source = ExternalAudioSource(None)
        source.signal_log.append((time.monotonic() - 10.0, 30000))
        self.assertIsNone(source.recent_peak())

    def test_the_report_says_what_to_check_when_nothing_comes(self):
        source = ExternalAudioSource(None)
        source.started_at -= 3
        report = source.signal_report()
        self.assertIn("muted", report)
        self.assertIn("device", report)

    def test_the_report_names_the_level_it_sees(self):
        source = ExternalAudioSource(None)
        source._note_signal(pcm(value=12000))
        self.assertIn("peak", source.signal_report())

    def test_a_stopped_capture_reports_its_reason(self):
        source = ExternalAudioSource(None)
        source.error = "the device is in use"
        self.assertIn("the device is in use", source.signal_report())
        self.assertIn("stopped", source.signal_line())

    def test_status_carries_the_signal_once_frames_arrive(self):
        source = ExternalAudioSource(None, label="Ableton")
        source._note_signal(pcm(value=12000))
        self.assertIn("signal", source.status_line())
        self.assertIn("Ableton", source.status_line())


class TestThePickerFindsAnyProgram(unittest.TestCase):
    """A program with no Windows audio session must still be selectable.

    In the field the picker showed one program -- a screen reader -- because
    only that one held a WASAPI session; the music program the player wanted
    was on ASIO, which Windows never sees. The session list is a starting
    point now, not the whole answer.
    """

    NAMES = {20: "nvda.exe", 30: "FL64.exe", 40: "explorer.exe", 1: "game.exe"}

    def _apps(self, windows=(20, 30, 1)):
        return running_apps(
            own_pid=1,
            playing=[{"pid": 20, "name": "nvda.exe"}],
            windows=list(windows),
            namer=lambda pid: self.NAMES.get(pid, ""),
        )

    def test_a_program_with_no_audio_session_is_still_listed(self):
        names = [app["name"] for app in self._apps()]
        self.assertIn("FL64.exe", names)

    def test_the_game_itself_is_never_a_source(self):
        self.assertNotIn("game.exe", [app["name"] for app in self._apps()])

    def test_programs_already_playing_are_marked_and_sorted_first(self):
        apps = self._apps()
        self.assertTrue(apps[0]["playing"])
        self.assertFalse([a for a in apps if a["name"] == "FL64.exe"][0]["playing"])

    def test_a_window_with_no_process_name_is_skipped_not_named(self):
        apps = running_apps(own_pid=1, playing=[], windows=[999],
                            namer=lambda pid: "")
        self.assertEqual(apps, [])

    def test_a_session_that_cannot_be_named_is_still_offered_by_pid(self):
        apps = running_apps(own_pid=1, playing=[{"pid": 77, "name": ""}],
                            windows=[], namer=lambda pid: "")
        self.assertEqual(apps, [{"pid": 77, "name": "Process 77",
                                 "playing": True}])

    def test_the_report_names_asio_when_it_stays_silent(self):
        source = ExternalAudioSource(None)
        source.started_at -= 3
        self.assertIn("ASIO", source.signal_report())

    def test_the_picker_asks_for_running_apps_and_marks_the_playing_ones(self):
        from pathlib import Path
        source = (Path(__file__).resolve().parents[1] / "libs" / "music_bot"
                  / "menu_pages" / "external.py").read_text(encoding="utf-8")
        picker = source.split("def _open_external_program_menu", 1)[1]
        self.assertIn("running_apps()", picker)
        self.assertIn("(playing now)", picker)
        self.assertIn("display_program_name", picker)

    def test_a_process_name_drops_its_exe_suffix(self):
        self.assertEqual(display_program_name("explorer.exe"), "explorer")
        self.assertEqual(display_program_name("FL64.EXE"), "FL64")
        self.assertEqual(display_program_name("  Spotify.exe  "), "Spotify")
        self.assertEqual(display_program_name("Process 77"), "Process 77")
        self.assertEqual(display_program_name(""), "Unknown program")


class TestAnyProgramByDevice(unittest.TestCase):
    """The device route: a program Windows cannot name is still reachable.

    ASIO never enters the Windows audio engine, so it has no session and no
    process loopback; the device it renders to is the only handle left, and a
    virtual cable is that handle made explicit.
    """

    def test_a_cable_is_recognised_and_speakers_are_not(self):
        for name in ("CABLE Input (VB-Audio Virtual Cable)",
                     "VoiceMeeter Input (VB-Audio VoiceMeeter VAIO)",
                     "Virtual Audio Cable", "ASIO4ALL loopback"):
            self.assertTrue(is_virtual_device(name), name)
        for name in ("Speakers (Realtek(R) Audio)",
                     "Headphones (Sony WH-1000XM4)"):
            self.assertFalse(is_virtual_device(name), name)

    def test_the_backend_can_be_pointed_at_one_device(self):
        from libs.external_audio import LoopbackBackend
        backend = LoopbackBackend(device_id="{0.0.0.00000000}.{abc}")
        self.assertEqual(backend.device_id, "{0.0.0.00000000}.{abc}")
        self.assertFalse(backend.whole_computer)

    def test_the_bot_hands_the_device_id_to_the_backend(self):
        from pathlib import Path
        source = (Path(__file__).resolve().parents[1] / "libs" / "music_bot"
                  / "controller.py").read_text(encoding="utf-8")
        self.assertIn("device_id=device_id", source)
        self.assertIn('if kind in ("device", "input"):', source)

    def test_the_root_offers_the_device_or_input_page(self):
        from pathlib import Path
        source = (Path(__file__).resolve().parents[1] / "libs" / "music_bot"
                  / "menu_pages" / "external.py").read_text(encoding="utf-8")
        self.assertIn('"Choose a device or input", go_sources, None,', source)
        self.assertIn('"Capture a playback device", go_playback, None,', source)
        self.assertIn("def _open_external_source_menu(self):", source)
        self.assertIn("def _open_external_device_menu(self):", source)
        self.assertIn("render_devices()", source)

    def test_the_playback_page_starts_the_playback_capture(self):
        from libs import menu as menu_mod, menus
        from libs.music_bot.menu_pages.external import ExternalAudioMenus

        class Page:
            def __init__(self, game, title, **kwargs):
                self.title = title
                self.items = []

            def add_items(self, items):
                self.items.extend(items)

        gp = SimpleNamespace(
            substates=[],
            pop_last_substate=lambda: None,
            add_substate=lambda page: gp.substates.append(page),
        )
        bot = SimpleNamespace(
            game=object(), external_audio_mode=MODE_STEREO,
            external_audio_gain=0.85, _find_gameplay=lambda: gp,
            start_external_audio=mock.MagicMock(),
        )
        handler = ExternalAudioMenus()
        handler._bot = bot
        handler._say = lambda _text: None
        handler._open_external_audio_menu = mock.MagicMock()
        devices = [
            {"id": "{speakers}", "name": "Speakers (USB DAC)"},
            {"id": "{cable}", "name": "CABLE Input", "virtual": True},
        ]
        with mock.patch.object(menu_mod, "Menu", Page), \
                mock.patch.object(menus, "set_default_sounds"), \
                mock.patch("libs.external_audio.render_devices",
                           return_value=devices):
            handler._open_external_device_menu()

        page = gp.substates[-1]
        self.assertEqual(page.title, "Capture a playback device")
        labels = [item[0] for item in page.items]
        self.assertIn("Capture Speakers (USB DAC)", labels)
        self.assertIn("Capture CABLE Input (virtual cable)", labels)

        page.items[0][1]()
        bot.start_external_audio.assert_called_once_with(
            kind="device", target="{speakers}", label="Speakers (USB DAC)",
            mode=MODE_STEREO, gain=0.85,
        )
        handler._open_external_audio_menu.assert_called_once_with()


class TestAnAudioInputCanBeCaptured(unittest.TestCase):
    """A line-in is a source of its own, not a program and not a loopback.

    The playback-device route reaches a DAW Windows cannot name; the input
    route reaches the thing plugged into the interface, which has no program
    at all. Both feed the same queues, so the only new decisions are which
    endpoint is opened and which WASAPI flags it is opened with.
    """

    def _source(self, *parts):
        from pathlib import Path
        path = Path(__file__).resolve().parents[1].joinpath(*parts)
        return path.read_text(encoding="utf-8").replace("\r\n", "\n")

    def test_the_input_backend_does_not_ask_for_the_loopback_flag(self):
        from libs.external_audio import InputDeviceBackend

        class Recorder:
            class ProcessLoopbackRecorder:
                AUDCLNT_STREAMFLAGS_LOOPBACK = 0x2
                AUDCLNT_STREAMFLAGS_EVENTCALLBACK = 0x4
                AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM = 0x80000000

        flags = InputDeviceBackend(device_id="{input}")._stream_flags(Recorder)
        self.assertFalse(flags & Recorder.ProcessLoopbackRecorder.AUDCLNT_STREAMFLAGS_LOOPBACK)
        self.assertTrue(flags & Recorder.ProcessLoopbackRecorder.AUDCLNT_STREAMFLAGS_EVENTCALLBACK)
        self.assertTrue(flags & Recorder.ProcessLoopbackRecorder.AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM)

    def test_an_input_without_a_device_is_refused(self):
        from libs.external_audio import InputDeviceBackend
        with self.assertRaises(SourceUnavailable):
            InputDeviceBackend(device_id="").open()

    def test_capture_devices_reads_the_recording_flow(self):
        source = self._source("libs", "external_audio.py")
        self.assertIn("E_CAPTURE", source)
        self.assertIn("def capture_devices():", source)
        self.assertIn("_endpoint_devices(E_CAPTURE)", source)

    def test_the_bot_builds_an_input_backend(self):
        source = self._source("libs", "music_bot", "controller.py")
        self.assertIn("InputDeviceBackend", source)
        self.assertIn('if kind == "input":', source)
        self.assertIn("kind=kind", source)

    def test_the_program_picker_no_longer_mixes_in_hardware(self):
        source = self._source("libs", "music_bot", "menu_pages", "external.py")
        picker = source.split("def _open_external_program_menu", 1)[1]
        self.assertNotIn("Capture an audio input instead", picker)
        self.assertNotIn("Capture an audio device instead", picker)
        self.assertIn('"Capture an audio input", go_input, None,', source)
        self.assertIn("def _open_external_source_menu(self):", source)
        self.assertIn("def _open_external_input_menu(self):", source)
        self.assertIn("capture_devices()", source)
        self.assertIn('kind="input"', source)

    def test_the_source_page_groups_the_two_hardware_routes(self):
        from libs import menu as menu_mod, menus
        from libs.music_bot.menu_pages.external import ExternalAudioMenus

        class Page:
            def __init__(self, game, title, **kwargs):
                self.title = title
                self.items = []

            def add_items(self, items):
                self.items.extend(items)

        gp = SimpleNamespace(
            substates=[],
            pop_last_substate=lambda: None,
            add_substate=lambda page: gp.substates.append(page),
        )
        bot = SimpleNamespace(game=object(), _find_gameplay=lambda: gp)
        handler = ExternalAudioMenus()
        handler._bot = bot
        with mock.patch.object(menu_mod, "Menu", Page), \
                mock.patch.object(menus, "set_default_sounds"):
            handler._open_external_source_menu()

        page = gp.substates[-1]
        self.assertEqual(page.title, "Choose a device or input")
        labels = [item[0] for item in page.items]
        self.assertEqual(labels[:2], ["Capture a playback device",
                                      "Capture an audio input"])
        self.assertEqual(labels[-1], "Back")

        handler._open_external_device_menu = mock.MagicMock()
        handler._open_external_input_menu = mock.MagicMock()
        page.items[0][1]()
        handler._open_external_device_menu.assert_called_once_with()
        page.items[1][1]()
        handler._open_external_input_menu.assert_called_once_with()

    def test_the_input_page_builds_and_lists_the_recording_endpoints(self):
        from libs import menu as menu_mod, menus
        from libs.music_bot.menu_pages.external import ExternalAudioMenus

        class Page:
            def __init__(self, game, title, **kwargs):
                self.title = title
                self.items = []

            def add_items(self, items):
                self.items.extend(items)

        gp = SimpleNamespace(
            substates=[],
            pop_last_substate=lambda: None,
            add_substate=lambda page: gp.substates.append(page),
        )
        bot = SimpleNamespace(
            game=object(), external_audio_mode=MODE_STEREO,
            external_audio_gain=0.85, _find_gameplay=lambda: gp,
            start_external_audio=mock.MagicMock(),
        )
        handler = ExternalAudioMenus()
        handler._bot = bot
        handler._say = lambda _text: None
        handler._open_external_audio_menu = mock.MagicMock()
        with mock.patch.object(menu_mod, "Menu", Page), \
                mock.patch.object(menus, "set_default_sounds"), \
                mock.patch("libs.external_audio.capture_devices",
                           return_value=[{"id": "{input}", "name": "USB Interface"}]):
            handler._open_external_input_menu()

        page = gp.substates[-1]
        self.assertEqual(page.title, "Capture an audio input")
        self.assertIn("Capture input USB Interface", [item[0] for item in page.items])
        self.assertIn("Back", [item[0] for item in page.items])
        self.assertIn("microphone", page.items[0][3].lower())

        page.items[0][1]()
        bot.start_external_audio.assert_called_once_with(
            kind="input", target="{input}", label="USB Interface",
            mode=MODE_STEREO, gain=0.85,
        )
        handler._open_external_audio_menu.assert_called_once_with()

    def test_the_input_report_talks_about_an_input_not_a_program(self):
        source = ExternalAudioSource(None, kind="input")
        source.started_at -= 3
        report = source.signal_report()
        self.assertIn("input", report)
        self.assertNotIn("ASIO", report)


class TestTheChooseADeviceOrInputSimulation(unittest.TestCase):
    """What the two hardware routes do with several kinds of endpoint.

    The menu tests prove the choice reaches the controller; this matrix proves
    the capture itself handles the shapes a real machine reports. A fake
    backend makes the test deterministic: each profile delivers exactly one
    packet and then raises, so the source's worker exits at the end of the
    packet without a sound card or a sleeping thread.
    """

    def _capture(self, packet, kind):
        song, live = [], []
        backend = FakeBackend(packets=[packet], read_error="finished")
        source = ExternalAudioSource(
            backend, sink=song.append, live_sink=live.append, kind=kind,
            gain=1.0,
        )
        source.run()
        return source, song, live

    def test_a_playback_device_keeps_its_stereo_image(self):
        source, song, live = self._capture(
            (pcm(frames=960, left_only=True, value=9000), 2, 48000),
            "device",
        )
        self.assertEqual(len(song), 1)
        self.assertEqual(len(live), 2)
        left, right = sides(song[0])
        self.assertTrue((left == 9000).all())
        self.assertTrue((right == 0).all())

    def test_a_usb_interface_is_an_input_not_a_program(self):
        source, song, live = self._capture(
            (pcm(frames=960, value=6000), 2, 48000), "input",
        )
        self.assertEqual(source.kind, "input")
        self.assertEqual(len(song), 1)
        self.assertEqual(len(live), 2)
        left, right = sides(song[0])
        self.assertTrue((left == 6000).all())
        self.assertTrue((right == 6000).all())

    def test_a_mono_microphone_is_duplicated_to_both_sides(self):
        source, song, _live = self._capture(
            (pcm(frames=960, channels=1, value=4500), 1, 48000), "input",
        )
        self.assertEqual(source.peak, 4500)
        left, right = sides(song[0])
        self.assertTrue((left == 4500).all())
        self.assertTrue((right == 4500).all())

    def test_a_stereo_line_in_at_44_1_k_is_resampled_to_one_frame(self):
        _source, song, live = self._capture(
            (pcm(frames=882, value=1200), 2, 44100), "input",
        )
        self.assertEqual(len(song), 1)
        self.assertEqual(len(song[0]), FRAME_BYTES)
        self.assertEqual(len(live), 2)
        left, right = sides(song[0])
        self.assertTrue((left == 1200).all())
        self.assertTrue((right == 1200).all())

    def test_a_six_channel_device_keeps_the_first_pair(self):
        block = np.zeros((960, 6), dtype=np.int16)
        block[:, 0] = 10
        block[:, 1] = 20
        block[:, 2:] = 999
        _source, song, _live = self._capture((block.tobytes(), 6, 48000), "device")
        left, right = sides(song[0])
        self.assertTrue((left == 10).all())
        self.assertTrue((right == 20).all())

    def test_a_silent_input_still_delivers_frames_and_says_silent(self):
        source, song, _live = self._capture(
            (b"\x00" * FRAME_BYTES, 2, 48000), "input",
        )
        self.assertEqual(len(song), 1)
        self.assertTrue((np.frombuffer(song[0], dtype=np.int16) == 0).all())
        self.assertEqual(source.signal_word(source.recent_peak()), "silent")


class TestTheRelayCarriesItToTheParty(unittest.TestCase):
    """A captured program needs a sender even when no song is playing.

    The party leg was wired, but the relay that sends it only started on the
    guitar and the microphone: with no song and a program captured, frames
    went into the queue and no thread took them out, so a second client heard
    silence while the Signal row said good.
    """

    def _has_input(self, **queues):
        from libs.music_bot.streaming import live_relay_has_input
        return live_relay_has_input(SimpleNamespace(**queues))

    def test_a_captured_program_counts_as_live_input(self):
        self.assertTrue(self._has_input(external_pcm_queue=deque([b"x"])))

    def test_an_empty_queue_is_not_input(self):
        self.assertFalse(self._has_input(external_pcm_queue=deque()))

    def test_the_guitar_still_counts(self):
        self.assertTrue(self._has_input(guitar_pcm_queue=deque([b"x"])))

    def test_a_queue_that_cannot_be_read_is_not_input(self):
        class Broken:
            def __len__(self):
                raise RuntimeError("gone")

        self.assertFalse(self._has_input(external_pcm_queue=Broken()))

    def test_the_controller_starts_the_relay_for_a_program(self):
        from pathlib import Path
        source = (Path(__file__).resolve().parents[1] / "libs" / "music_bot"
                  / "controller.py").read_text(encoding="utf-8")
        body = source.split("def start_external_audio", 1)[1]
        body = body.split("def stop_external_audio", 1)[0]
        self.assertIn("self._ensure_live_relay_streamer()", body)
        self.assertIn("if live_relay_has_input(self):", source)

    def test_both_senders_ask_the_same_question(self):
        from pathlib import Path
        source = (Path(__file__).resolve().parents[1] / "libs" / "music_bot"
                  / "streaming.py").read_text(encoding="utf-8")
        self.assertIn("def live_relay_has_input(bot):", source)
        self.assertIn("if live_relay_has_input(self.bot):", source)


class TestTheRelaySendsStereoToTheParty(unittest.TestCase):
    """With no song, the relay is the sender: it must not centre the program.

    The party leg carries two sides. Folding a captured stereo program into
    one channel was the complaint this fixes: a stereo instrument heard
    centred is not the instrument the player sent.
    """

    def _external(self, left, right):
        block = np.zeros((960, 2), dtype=np.int16)
        block[:, 0] = left
        block[:, 1] = right
        return block.tobytes()

    def test_the_programs_two_sides_survive(self):
        from libs.music_bot.streaming import mix_live_stereo
        frame = mix_live_stereo([self._external(9000, 3000)], [], "k")
        self.assertEqual(len(frame), 3840)
        left, right = sides(frame)
        self.assertGreater(int(left[100]), int(right[100]))

    def test_a_centre_source_lands_on_both_sides_equally(self):
        from libs.music_bot.streaming import mix_live_stereo
        frame = mix_live_stereo([], [pcm(frames=960, channels=1, value=8000)], "k")
        left, right = sides(frame)
        self.assertEqual(int(left[100]), int(right[100]))

    def test_nothing_in_means_nothing_out(self):
        from libs.music_bot.streaming import mix_live_stereo
        self.assertEqual(mix_live_stereo([], [], "k"), b"")

    def test_a_short_program_frame_is_padded_not_dropped(self):
        from libs.music_bot.streaming import mix_live_stereo
        self.assertEqual(len(mix_live_stereo([bytes([16, 0]) * 100], [], "k")), 3840)

    def test_the_relay_owns_a_stereo_encoder_and_asks_the_leg(self):
        from pathlib import Path
        source = (Path(__file__).resolve().parents[1] / "libs" / "music_bot"
                  / "streaming.py").read_text(encoding="utf-8")
        relay = source.split("class LiveRelayStreamer", 1)[1]
        self.assertIn("self.encoder_stereo.set_channels(2)", relay)
        self.assertIn("if stereo_upload_eligible(self.bot, 2):", relay)
        self.assertIn("mix_live_stereo(", relay)


class TestTheJamLeg(unittest.TestCase):
    """A party's live channel: 10 ms frames, not the song path's 240 ms.

    A song has a shared clock every listener is lined up against; a person
    playing over a captured program does not. The live leg is selected only
    for a session, only without the megaphone path, and only when the Server
    advertised it -- an older Server keeps the proven channel-22 copy.
    """

    def _source(self, *parts):
        from pathlib import Path
        path = Path(__file__).resolve().parents[1].joinpath(*parts)
        return path.read_text(encoding="utf-8").replace("\r\n", "\n")

    def _server_source(self, *parts):
        from pathlib import Path
        path = Path(__file__).resolve().parents[2].joinpath("server", *parts)
        return path.read_text(encoding="utf-8").replace("\r\n", "\n")

    def test_the_relay_selects_the_live_channel_for_a_party(self):
        relay = self._source("libs", "music_bot", "streaming.py").split(
            "class LiveRelayStreamer", 1)[1]
        self.assertIn("consts.CHANNEL_MUSICBOT_LIVE", relay)
        self.assertIn("music_live_supported", relay)
        self.assertIn("party_private", relay)
        self.assertIn("broadcast_to_megaphone", relay)
        self.assertIn("external_live_queue", relay)
        self.assertIn("live_10", relay)
        self.assertIn("pace = 0.010 if live_10 else 0.020", relay)
        self.assertIn("self._pace(pace)", relay)
        # One frame per tick: a second frame summed into the same slot is not
        # a queue, it is two slices of the program played on top of each other.
        self.assertIn("external_frames.append(live_queue.popleft())", relay)
        self.assertNotIn("while live_queue and len(external_frames) < 2", relay)
        self.assertNotIn("while external_queue and len(external_frames) < 2", relay)

    def test_the_receiver_routes_the_live_channel_to_a_live_frame(self):
        dispatch = self._source("libs", "networking.py")
        self.assertIn("CHANNEL_MUSICBOT_LIVE", dispatch)
        self.assertIn("process_music_live_data", dispatch)
        handler = self._source("libs", "event_handeler.py")
        live = handler.split("def process_music_live_data", 1)[1].split(
            "def process_music_timeline_data", 1)[0]
        self.assertIn("live=True", live)

    def test_a_live_jam_buffers_sixty_ms_and_grows_only_on_a_cut(self):
        source = self._source("libs", "voice_chat.py")
        self.assertIn("PRE_BUFFER_MS_LIVE = 60.0", source)
        self.assertIn("MAX_PRE_BUFFER_MS_LIVE = 100.0", source)
        self.assertIn("if live:", source)
        self.assertIn("threshold = max(2, int(round(target / frame_ms)))", source)
        self.assertIn("MAX_PRE_BUFFER_MS_LIVE", source)
        self.assertIn("+ 10.0", source)

    def test_a_late_wakeup_keeps_the_ideal_deadline(self):
        """A 1 ms oversleep must not move the next deadline by 1 ms forever.

        The relay ran at ~87 frames/s on a 10 ms stream because it measured
        the next wait from the wall-clock instant after the previous sleep.
        Windows waking 1-2 ms late then accumulated into a queue overflow and
        an audible drop every 5-7 seconds. The deadline is ideal time now.
        """
        from libs.music_bot.streaming import LiveRelayStreamer
        relay = LiveRelayStreamer.__new__(LiveRelayStreamer)
        relay.last_send_time = 1.000
        with mock.patch("libs.music_bot.streaming.time.perf_counter",
                        return_value=1.011):
            relay._pace(0.010)
        self.assertAlmostEqual(relay.last_send_time, 1.010, places=6)

    def test_a_long_stall_starts_a_fresh_cadence(self):
        from libs.music_bot.streaming import LiveRelayStreamer
        relay = LiveRelayStreamer.__new__(LiveRelayStreamer)
        relay.last_send_time = 1.000
        with mock.patch("libs.music_bot.streaming.time.perf_counter",
                        return_value=1.250):
            relay._pace(0.010)
        # 250 ms behind: catch up from now instead of bursting 25 frames.
        self.assertAlmostEqual(relay.last_send_time, 1.250, places=6)

    def test_the_mix_can_cut_a_ten_ms_frame(self):
        from libs.music_bot.streaming import mix_live_stereo
        block = np.zeros((480, 2), dtype=np.int16)
        block[:, 0] = 9000
        block[:, 1] = 3000
        frame = mix_live_stereo([block.tobytes()], [], "k", frame_bytes=1920)
        self.assertEqual(len(frame), 1920)
        left, right = sides(frame)
        self.assertGreater(int(left[10]), int(right[10]))

    def test_the_server_relays_a_jam_on_its_own_channel(self):
        source = self._server_source("libs", "instrument_handler.ts")
        self.assertIn("CHANNEL_MUSICBOT_LIVE", source)
        self.assertIn("if (live && target.music_live_capable)", source)
        self.assertIn("packet.writeUInt8(player.voice_channel, 0)", source)
        self.assertIn("CHANNEL_MUSICBOT_LIVE", self._server_source(
            "libs", "networking.ts"))
        self.assertIn("music_live_capable", self._server_source(
            "libs", "objects", "player.ts"))


if __name__ == "__main__":
    unittest.main()
