"""Party Sync's "Hear my own voice" monitor.

A session's voice leg is everybody *else's* voice: the Server never sends the
local player their own channel, and the PA monitor that lets a broadcaster hear
themselves plays through the map's speakers -- so a talker standing where there
is no PA heard nothing of themselves, which is the one thing a singer needs
while adjusting their own key or effect.

These checks pin the three halves of it: the rule that decides when the monitor
runs (`voice_chat.party_voice_monitor_wanted`, asked once per capture chunk),
the source it plays through (centred, direct-to-ear, bounded, main-thread
only), and the Party Sync line that turns it on.

No game, audio device or OpenAL is required: the source and the buffer are
fakes, and the capture loop's own decision is asked through the same function
it uses.
"""

import os
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

import cyal

from libs import party_sync_audio
from libs.music_bot import gate as music_bot_gate
from libs.music_bot.controller import MapMusicBot
from libs.music_bot.menus import MusicBotMenus
from libs.voice_chat import party_voice_monitor_wanted

FRAME = b"\x01\x00" * 960      # one 20 ms mono16 frame at 48 kHz
CHUNK = b"\x01\x00" * 480      # one 10 ms chunk, what the monitor is fed


class FakeBuffer:
    def __init__(self):
        self.data = b""
        self.sample_rate = 0
        self.format = None

    def set_data(self, data, sample_rate=48000, format=None):
        self.data = bytes(data)
        self.sample_rate = sample_rate
        self.format = format


class FakeSource:
    def __init__(self):
        self.queued = []
        self.played = 0
        self.state = cyal.SourceState.INITIAL
        self.spatialize = True
        self.relative = False
        self.position = None
        self.direct_channels = False
        self.rolloff_factor = 1.0
        self.gain = 0.0
        self.stops = 0

    def queue_buffers(self, buf):
        self.queued.append(buf)

    @property
    def buffers_processed(self):
        return self.played

    @property
    def buffers_queued(self):
        return len(self.queued)

    def unqueue_buffers(self):
        for _ in range(self.played):
            if self.queued:
                self.queued.pop(0)
        self.played = 0

    def play(self):
        self.state = cyal.SourceState.PLAYING

    def stop(self):
        self.stops += 1
        self.state = cyal.SourceState.STOPPED


class FakeContext:
    def __init__(self):
        self.sources = []

    def gen_source(self):
        source = FakeSource()
        self.sources.append(source)
        return source

    def gen_buffer(self):
        return FakeBuffer()


class FakeAudioManager:
    def __init__(self):
        self.context = FakeContext()
        self.deferred = []

    def defer_audio(self, fn):
        self.deferred.append(fn)


def make_monitor():
    audio = FakeAudioManager()
    monitor = party_sync_audio.PartyVoiceMonitor(
        SimpleNamespace(audio_mngr=audio), audio)
    return monitor, audio


class MonitorSourceTests(unittest.TestCase):
    def setUp(self):
        self.monitor, self.audio = make_monitor()

    def test_the_monitor_is_centred_and_never_positional(self):
        self.monitor.feed(CHUNK)
        source = self.audio.context.sources[0]
        self.assertFalse(source.spatialize)
        self.assertTrue(source.relative)
        self.assertEqual(source.position, (0.0, 0.0, 0.0))
        self.assertTrue(source.direct_channels)
        self.assertEqual(source.rolloff_factor, 0.0)
        self.assertEqual(source.gain, 1.0)
        self.assertEqual(len(source.queued), 1)
        self.assertIs(source.state, cyal.SourceState.PLAYING)

    def test_the_feed_is_mono16_at_48k(self):
        self.monitor.feed(FRAME)
        buffer = self.audio.context.sources[0].queued[0]
        self.assertEqual(buffer.sample_rate, 48000)
        self.assertEqual(buffer.format, cyal.BufferFormat.MONO16)
        self.assertEqual(len(buffer.data), len(FRAME))

    def test_chunks_the_ear_already_has_are_let_go(self):
        self.monitor.feed(CHUNK)
        source = self.audio.context.sources[0]
        source.played = 1
        self.monitor.feed(CHUNK)
        self.assertEqual(self.monitor._waiting_samples, 480)

    def test_a_stalled_main_thread_drops_the_backlog(self):
        limit = self.monitor.queue_limit_samples
        for _ in range((limit // 480) + 4):
            self.monitor.feed(CHUNK)
        self.assertGreaterEqual(self.monitor.skips, 1)
        self.assertLessEqual(self.monitor._waiting_samples, limit)
        self.assertGreaterEqual(self.audio.context.sources[0].stops, 1)

    def test_close_frees_the_source_and_its_queue(self):
        self.monitor.feed(CHUNK)
        self.monitor.close()
        self.assertIsNone(self.monitor.source)
        self.assertEqual(self.monitor._waiting_samples, 0)

    def test_a_client_with_no_audio_device_does_not_crash(self):
        monitor = party_sync_audio.PartyVoiceMonitor(SimpleNamespace())
        monitor.feed(CHUNK)
        self.assertIsNone(monitor.source)


class MonitorFeedTests(unittest.TestCase):
    def setUp(self):
        self.monitor, self.audio = make_monitor()

    def test_a_frame_from_the_capture_worker_is_deferred_to_the_main_thread(self):
        thread = threading.Thread(
            target=lambda: party_sync_audio.feed_voice_monitor(
                self.monitor, FRAME))
        thread.start()
        thread.join()
        self.assertEqual(len(self.audio.deferred), 1)
        self.assertEqual(self.audio.context.sources, [])
        self.audio.deferred[0]()
        self.assertEqual(len(self.audio.context.sources[0].queued), 1)

    def test_a_frame_on_the_main_thread_plays_at_once(self):
        party_sync_audio.feed_voice_monitor(self.monitor, FRAME)
        self.assertEqual(self.audio.deferred, [])
        self.assertEqual(len(self.audio.context.sources[0].queued), 1)

    def test_no_monitor_and_an_empty_frame_are_ignored(self):
        party_sync_audio.feed_voice_monitor(None, FRAME)
        party_sync_audio.feed_voice_monitor(self.monitor, b"")
        self.assertEqual(self.audio.context.sources, [])

    def test_the_monitor_belongs_to_the_map_view_and_dies_with_it(self):
        gp = SimpleNamespace(game=SimpleNamespace(audio_mngr=self.audio))
        first = party_sync_audio.voice_monitor_for(gp)
        self.assertIsNotNone(first)
        self.assertIs(party_sync_audio.voice_monitor_for(gp), first)
        first.feed(CHUNK)
        party_sync_audio.close_voice_monitor(gp)
        self.assertIsNone(first.source)


class TheRuleTests(unittest.TestCase):
    def test_nothing_runs_outside_a_session(self):
        bot = SimpleNamespace(party_sync_monitor_voice=True)
        self.assertFalse(party_voice_monitor_wanted(
            False, object(), False, False, bot))

    def test_the_button_is_what_turns_it_on(self):
        on = SimpleNamespace(party_sync_monitor_voice=True)
        off = SimpleNamespace(party_sync_monitor_voice=False)
        self.assertTrue(party_voice_monitor_wanted(
            True, object(), False, False, on))
        self.assertFalse(party_voice_monitor_wanted(
            True, object(), False, False, off))
        self.assertFalse(party_voice_monitor_wanted(
            True, object(), False, False, None))

    def test_a_pa_monitor_or_the_bots_mix_already_carries_the_voice(self):
        bot = SimpleNamespace(party_sync_monitor_voice=True)
        self.assertFalse(party_voice_monitor_wanted(
            True, object(), True, False, bot))
        self.assertFalse(party_voice_monitor_wanted(
            True, object(), False, True, bot))


class FakeMenu:
    def __init__(self, game, title, **kwargs):
        self.game = game
        self.title = title
        self.items = []
        self.pos = -1

    def add_items(self, items):
        self.items.extend(items)

    def speak_current_item(self):
        return None


class MonitorMenuItemTests(unittest.TestCase):
    def setUp(self):
        music_bot_gate.clear()
        self.addCleanup(music_bot_gate.clear)
        self.addCleanup(mock.patch.stopall)
        mock.patch("libs.menu.Menu", FakeMenu).start()
        mock.patch("libs.menus.set_default_sounds").start()
        self.speak = mock.patch("libs.music_bot.menus.speak").start()

        self.gp = SimpleNamespace(substates=[])
        self.gp.add_substate = lambda menu: (
            self.gp.substates.append(menu) or menu)
        self.gp.pop_last_substate = lambda: (
            self.gp.substates.pop() if self.gp.substates else None)

        self.bot = mock.MagicMock()
        self.bot._find_gameplay.return_value = self.gp
        self.bot.party_sync_monitor_voice = False
        self.bot.party_sync_monitor_voice_label.side_effect = (
            lambda: "Hear my own voice: %s" % (
                "ON" if self.bot.party_sync_monitor_voice else "OFF"))

        def set_monitor(value):
            self.bot.party_sync_monitor_voice = bool(value)
            return self.bot.party_sync_monitor_voice

        self.bot.set_party_sync_monitor_voice.side_effect = set_monitor
        # The line itself is the bot's one builder, shared with Ctrl+F8; the
        # mock runs the real one so both menus are checked as they ship.
        self.bot.party_sync_monitor_voice_item.side_effect = (
            lambda say, refresh=None:
            MapMusicBot.party_sync_monitor_voice_item(self.bot, say, refresh))
        self.bot.song_requests_switch_item.return_value = (
            "Song requests: OFF", lambda: None, None, "Whether it takes them.")
        self.handler = MusicBotMenus(self.bot)

    def _open(self, ps):
        self.bot._party_sync_pair.return_value = (self.gp, ps)
        self.handler._open_party_sync_menu()
        return self.gp.substates[-1]

    def _monitor_item(self, menu):
        for item in menu.items:
            if str(item[0]).startswith("Hear my own voice"):
                return item
        return None

    def host(self):
        return SimpleNamespace(role="host", guests=[], cinema_listeners=[],
                               cinema_cabinet="", host_name="Anh")

    def test_a_host_is_offered_the_monitor(self):
        menu = self._open(self.host())
        item = self._monitor_item(menu)
        self.assertIsNotNone(item)
        self.assertEqual(item[0], "Hear my own voice: OFF")
        self.assertIn("own ears", item[3])
        self.assertIn("speakers", item[3])

    def test_the_monitor_line_sits_above_the_capture_page(self):
        menu = self._open(self.host())
        labels = [str(item[0]) for item in menu.items]
        monitor = labels.index("Hear my own voice: OFF")
        external = labels.index("External audio from a music program")
        self.assertLess(monitor, external)
        # External audio is the last thing to choose; Back closes the page.
        self.assertEqual(
            labels[-2:],
            ["External audio from a music program", "Back"],
        )

    def test_a_guest_is_offered_the_monitor_too(self):
        ps = SimpleNamespace(role="guest", guests=[], host_name="Anh",
                             cinema_cabinet="")
        self.assertIsNotNone(self._monitor_item(self._open(ps)))

    def test_no_session_has_no_monitor_line(self):
        self.assertIsNone(self._monitor_item(self._open(None)))

    def test_pressing_it_turns_the_monitor_on_in_place(self):
        menu = self._open(self.host())
        self._monitor_item(menu)[1]()
        self.assertTrue(self.bot.party_sync_monitor_voice)
        self.bot.set_party_sync_monitor_voice.assert_called_once_with(True)
        self.speak.assert_called_with(
            "Your own voice: you will hear it in this session.")
        # The label is the state: the same page is redrawn, not a second menu.
        self.assertIs(self.gp.substates[-1], menu)
        self.assertEqual(len(self.gp.substates), 1)
        self.assertEqual(self._monitor_item(menu)[0], "Hear my own voice: ON")

    def test_pressing_it_again_turns_it_off(self):
        menu = self._open(self.host())
        self._monitor_item(menu)[1]()
        self._monitor_item(menu)[1]()
        self.assertFalse(self.bot.party_sync_monitor_voice)
        self.speak.assert_called_with("Your own voice: silent again.")
        self.assertIs(self.gp.substates[-1], menu)
        self.assertEqual(self._monitor_item(menu)[0], "Hear my own voice: OFF")


class _Source:
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


class QuickMenuWiringTests(_Source, unittest.TestCase):
    """Ctrl+F8 is the session's own door, so it carries the same line.

    Walking back through the Music Bot menu to find the monitor is the trip the
    quick menu exists to save, and it is a different menu object (rebuilt to
    change its own label), so the wiring is pinned rather than assumed.
    """

    def setUp(self):
        self.body = self._function(
            self._source("libs", "gameplay.py"),
            "_open_party_sync_quick_menu",
        )

    def test_the_quick_menu_offers_the_talkers_own_monitor(self):
        self.assertIn("party_sync_monitor_voice_item", self.body)
        self.assertIn("_open_party_sync_quick_menu(ps)", self.body)

    def test_the_monitor_sits_above_the_capture_page(self):
        self.assertLess(
            self.body.index("party_sync_monitor_voice_item"),
            self.body.index("External audio from a music program"),
        )

    def test_both_menus_build_the_line_in_one_place(self):
        page = self._function(
            self._source("libs", "music_bot", "menu_pages", "party.py"),
            "_open_party_sync_menu",
        )
        self.assertIn("party_sync_monitor_voice_item", page)
        self.assertNotIn("set_party_sync_monitor_voice", page)


if __name__ == "__main__":
    unittest.main()
