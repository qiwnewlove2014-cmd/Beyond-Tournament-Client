"""The Developer Music Bot feature gate, as the Client enforces it.

`server/libs/music_bot_gate.ts` owns the catalogue and the lock file; the Server
pushes the locked ids in the `staff_permissions` snapshot. This suite pins the
Client half: the gate module itself, the key bindings that must stop, and the
two menus whose lines must disappear. Nothing here needs a game, OpenAL, ffmpeg,
yt-dlp or a network.

Pins the rule in `.agents/skills/music_bot_integration/`: a lock is global (it
applies to the Developer who set it too) and it is applied on top of rank access,
never instead of it.
"""

import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

import pygame

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from libs.event_handeler import EventHandeler
from libs.gameplay import Gameplay
from libs.music_bot import gate


class FakeMenu:
    """Drop-in for libs.menu.Menu capturing the items it was built with."""

    def __init__(self, *args, **kwargs):
        self.items = []

    def add_items(self, items):
        self.items = list(items)

    def speak_current_item(self):
        pass

    def add_option(self, *args, **kwargs):
        pass


class FakeMusicBot:
    def __init__(self):
        self.volume = 50
        self.calls = []
        self.playing = False
        self.paused = False
        self.last_stop_clear_queue = True

    def open_search(self):
        self.calls.append("open_search")

    def set_volume(self, vol):
        self.calls.append(("set_volume", vol))

    def seek_by(self, seconds):
        self.calls.append(("seek_by", seconds))

    def previous_feed_track(self):
        self.calls.append("previous_feed_track")

    def next_feed_track(self):
        self.calls.append("next_feed_track")

    def stop(self, clear_queue=True):
        self.calls.append("stop")
        self.last_stop_clear_queue = clear_queue

    def toggle_pause(self):
        self.calls.append("toggle_pause")

    def toggle_broadcast(self):
        self.calls.append("toggle_broadcast")

    def speak_status(self):
        self.calls.append("speak_status")

    def has_last_track(self):
        return False


class GateStateTestCase(unittest.TestCase):
    """Every case starts and ends with an empty gate.

    The gate is process-global on purpose (a lock is global), so a case that
    left one behind would change what the next suite sees.
    """

    def setUp(self):
        gate.clear()

    def tearDown(self):
        gate.clear()


def make_gp(**role_flags):
    gp = Gameplay.__new__(Gameplay)
    gp.music_bot = FakeMusicBot()
    for key, val in role_flags.items():
        setattr(gp, key, val)
    return gp


class GateModuleTests(GateStateTestCase):
    def test_an_empty_gate_allows_everything_silently(self):
        self.assertEqual(gate.locked_ids(), frozenset())
        self.assertTrue(gate.allow("seek"))
        with mock.patch("libs.music_bot.gate.speak") as speak:
            self.assertFalse(gate.blocked("seek"))
        speak.assert_not_called()

    def test_the_snapshot_replaces_the_whole_locked_set(self):
        gate.apply_locked({"seek": "Seek", "eq": "Equalizer"})
        self.assertEqual(gate.locked_ids(), frozenset({"seek", "eq"}))
        applied = gate.apply_locked({"reverb": "Room reverb"})
        self.assertEqual(gate.locked_ids(), frozenset({"reverb"}))
        self.assertEqual(applied, frozenset({"reverb"}))
        self.assertFalse(gate.is_locked("seek"))

    def test_a_malformed_snapshot_locks_nothing_rather_than_crashing(self):
        for value in (None, [], "seek", 3, {"": "nameless"}, {3: "Seek"}):
            gate.apply_locked({"seek": "Seek"})
            gate.apply_locked(value)
            self.assertEqual(gate.locked_ids(), frozenset(), value)

    def test_an_id_without_a_label_still_locks(self):
        """A lock is the Server's decision first and its wording second, so a
        malformed label must not quietly unlock anything."""
        gate.apply_locked({"seek": None})
        self.assertTrue(gate.is_locked("seek"))
        self.assertEqual(gate.label("seek"), "seek")

    def test_a_locked_feature_says_the_servers_words_when_it_refuses(self):
        gate.apply_locked({"seek": "Seek"})
        with mock.patch("libs.music_bot.gate.speak") as speak:
            self.assertTrue(gate.blocked("seek"))
        speak.assert_called_once_with("Seek is locked by staff right now.")
        self.assertFalse(gate.allow("seek"))

    def test_a_lock_with_no_label_still_reads_as_a_sentence(self):
        gate.apply_locked({"seek": ""})
        with mock.patch("libs.music_bot.gate.speak") as speak:
            self.assertTrue(gate.blocked("seek"))
        self.assertIn("locked by staff", speak.call_args[0][0])

    def test_menu_lines_name_the_feature_they_belong_to(self):
        gate.apply_locked({"search": "Search and play"})
        lines = gate.filter_items([
            ("search", "Search YouTube", 1),
            (None, "Help", 2),
            ("seek", "Seek", 3),
        ])
        self.assertEqual(lines, [("Help", 2), ("Seek", 3)])


class KeyBindingTests(GateStateTestCase):
    """A locked feature's key stops, and the key says why."""

    def test_the_search_key_is_silent_about_rank_but_not_about_a_lock(self):
        gp = make_gp(is_staff=True)
        gate.apply_locked({"search": "Search and play"})
        with mock.patch("libs.gameplay.speak"):
            gp.music_bot_control(0)
        self.assertEqual(gp.music_bot.calls, [])
        with mock.patch("libs.music_bot.gate.speak") as speak:
            gp.music_bot_control(0)
        speak.assert_called_once()
        self.assertIn("locked by staff", speak.call_args[0][0])

    def test_playback_keys_stop_when_playback_is_locked(self):
        gp = make_gp(is_staff=True)
        gp.music_bot.playing = True
        gate.apply_locked({"playback": "Play, pause and stop"})
        with mock.patch("libs.gameplay.speak"):
            gp.music_bot_control(pygame.KMOD_CTRL)
            gp.music_bot_control(pygame.KMOD_SHIFT)
        self.assertEqual(gp.music_bot.calls, [])

    def test_the_broadcast_key_stops_when_broadcast_is_locked(self):
        gp = make_gp(is_staff=True)
        gate.apply_locked({"broadcast": "Broadcast to others"})
        with mock.patch("libs.gameplay.speak"):
            gp.music_bot_control(pygame.KMOD_ALT)
        self.assertEqual(gp.music_bot.calls, [])

    def test_the_volume_keys_stop_when_volume_is_locked(self):
        gp = make_gp(is_staff=True)
        gate.apply_locked({"volume": "Volume"})
        with mock.patch("libs.gameplay.speak"):
            gp.music_bot_volume(10)
            gp.music_bot_volume_key(1)
        self.assertEqual(gp.music_bot.calls, [])

    def test_seeking_stops_when_seek_is_locked(self):
        gp = make_gp(is_staff=True)
        gate.apply_locked({"seek": "Seek"})
        with mock.patch("libs.gameplay.speak"):
            gp.music_bot_volume_key(1, pygame.KMOD_CTRL)
        self.assertEqual(gp.music_bot.calls, [])

    def test_the_feed_keys_stop_when_the_feed_is_locked(self):
        gp = make_gp(is_staff=True)
        gate.apply_locked({"feed": "Personal feed"})
        with mock.patch("libs.gameplay.speak"):
            gp.buffer_cycle_l(pygame.KMOD_CTRL)
            gp.buffer_cycle_r(pygame.KMOD_CTRL)
        self.assertEqual(gp.music_bot.calls, [])

    def test_party_sync_does_not_open_when_party_is_locked(self):
        gp = make_gp(is_staff=True)
        gp.party_sync = SimpleNamespace(role="host")
        gp._open_party_sync_quick_menu = lambda ps: gp.music_bot.calls.append("menu")
        gate.apply_locked({"party": "Party Sync"})
        with mock.patch("libs.gameplay.speak"):
            gp.party_sync_leave_key()
        self.assertEqual(gp.music_bot.calls, [])

    def test_a_locked_bot_beats_the_rank_answer_for_everyone(self):
        """Developer included: a lock is a kill switch, not a permission."""
        gp = make_gp(is_staff=True, is_builder=True, is_technician=True,
                     can_use_music_bot=True)
        self.assertTrue(gp._can_use_music_bot())
        gate.apply_locked({"bot": "Music Bot itself"})
        self.assertFalse(gp._can_use_music_bot())


class MenuTests(GateStateTestCase):
    """A locked feature's line is not built, so it cannot be chosen."""

    def _bot(self):
        from libs.music_bot import MapMusicBot

        bot = MapMusicBot.__new__(MapMusicBot)
        bot.game = SimpleNamespace()
        game = SimpleNamespace()
        gp = SimpleNamespace(
            add_substate=lambda item: None,
            pop_last_substate=lambda: None,
            can_broadcast_megaphone=True,
        )
        bot._find_gameplay = lambda: gp
        bot.queue_mode = False
        bot.next_up_queue = []
        bot.subtitle_label = lambda: "Subtitles: OFF"
        bot.cinema_routing_allowed = lambda: False
        bot.cinema_listening_allowed = lambda: False
        bot.broadcast_to_megaphone = False
        bot.water_muffle_enabled = False
        bot.reverb_enabled = True
        bot.crossfade_enabled = False
        bot._eq_profile_label = lambda: "Flat"
        bot.transpose_label = lambda: "Original"
        bot.speech_transpose_label = lambda: "Original"
        bot.output_device_label = lambda: "System Default"
        bot.game = game
        return bot

    def _labels(self, bot, method):
        captured = []
        bot._find_gameplay().add_substate = captured.append
        with mock.patch("libs.menu.Menu", FakeMenu), \
                mock.patch("libs.menus.set_default_sounds"):
            getattr(bot, method)()
        return [label() if callable(label) else label
                for label, _action in captured[-1].items]

    def test_the_mode_menu_drops_every_locked_line(self):
        bot = self._bot()
        gate.apply_locked({
            "search": "Search and play",
            "broadcast": "Broadcast to others",
            "queue": "Queue",
            "subtitles": "Spoken subtitles",
        })
        labels = self._labels(bot, "_show_mode_menu")
        for gone in ("Search YouTube", "Play a YouTube link",
                     "Choose Local File", "Queue Mode: OFF",
                     "Subtitles: OFF", "Broadcast to Megaphone: OFF"):
            self.assertNotIn(gone, labels, labels)
        # The hub lines stay: Settings is where a Developer sees what is left.
        self.assertIn("Music Bot Settings", labels)
        self.assertIn("Help", labels)
        self.assertIn("Cancel", labels)

    def test_the_mode_menu_keeps_every_line_when_nothing_is_locked(self):
        bot = self._bot()
        labels = self._labels(bot, "_show_mode_menu")
        for present in ("Search YouTube", "Play a YouTube link",
                        "Choose Local File", "Queue Mode: OFF",
                        "Subtitles: OFF", "Broadcast to Megaphone: OFF"):
            self.assertIn(present, labels, labels)

    def test_the_party_switch_disappears_when_requests_are_locked(self):
        """`requests` is a catalogue id with a guard: the host's /p switch."""
        bot = self._bot()
        ps = SimpleNamespace(role="host", guests=[])
        bot._party_sync_pair = lambda: (bot._find_gameplay(), ps)
        bot.song_requests_switch_item = lambda _speak: (
            "Song requests: OFF", lambda: None)
        self.assertIn("Song requests: OFF",
                      self._labels(bot, "_open_party_sync_menu"))
        gate.apply_locked({"requests": "Song requests"})
        self.assertNotIn("Song requests: OFF",
                         self._labels(bot, "_open_party_sync_menu"))

    def test_the_settings_menu_drops_only_the_locked_switches(self):
        bot = self._bot()
        gate.apply_locked({"output_device": "Music output device",
                           "eq": "Equalizer"})
        labels = self._labels(bot, "_open_settings_menu")
        self.assertFalse(any(label.startswith("Music Output Device")
                             for label in labels), labels)
        self.assertFalse(any(label.startswith("Equalizer")
                             for label in labels), labels)
        self.assertTrue(any(label.startswith("Room Reverb")
                            for label in labels), labels)
        self.assertIn("Back", labels)


class SnapshotTests(GateStateTestCase):
    """The packet is what turns a Developer's lock into a dead menu."""

    def _handler(self, gp):
        scheduled = []
        handler = SimpleNamespace(
            gameplay=gp,
            game=SimpleNamespace(put=lambda callback: scheduled.append(callback)),
        )
        return handler, scheduled

    def test_a_refresh_applies_the_locked_map_and_stops_a_running_bot(self):
        gp = make_gp(can_use_music_bot=True)
        gp.music_bot.playing = True
        handler, scheduled = self._handler(gp)
        EventHandeler.staff_permissions(handler, {
            "is_staff": True,
            "can_use_music_bot": True,
            "music_bot_locked": {"bot": "Music Bot itself"},
        })
        self.assertTrue(gate.is_locked("bot"))
        self.assertEqual(len(scheduled), 1,
                         "the bot must be stopped on the main thread")
        scheduled[0]()
        self.assertEqual(gp.music_bot.calls, ["stop"])

    def test_a_refresh_without_the_key_keeps_the_last_answer(self):
        gp = make_gp(is_staff=True)
        handler, _scheduled = self._handler(gp)
        EventHandeler.staff_permissions(handler, {
            "is_staff": True,
            "music_bot_locked": {"seek": "Seek"},
        })
        # A Server built before the gate sends no key at all: an unrelated rank
        # change must not read as "everything is unlocked again".
        EventHandeler.staff_permissions(handler, {
            "is_staff": True,
            "can_use_music_bot": True,
        })
        self.assertTrue(gate.is_locked("seek"))

    def test_a_fresh_login_applies_the_servers_locked_map(self):
        """Leaving and rejoining is a new session, and the login snapshot is its
        only answer.

        The reported bug (2026-10-21): a lock worked until the player left the
        game or switched character, then every locked feature was usable again
        while the F8 form still named the Developer who locked it. The Client
        was right to apply what the login said -- the Server simply sent no
        `music_bot_locked` in it, so the empty gate read as "nothing is locked".
        """
        gp = make_gp(is_staff=True)
        gp.player = SimpleNamespace(name="")
        handler = SimpleNamespace(
            gameplay=gp,
            game=SimpleNamespace(
                replace=lambda state: None,
                put=lambda callback: callback(),
                presence_sounds=SimpleNamespace(configure=lambda data: None),
            ),
            client=SimpleNamespace(put=lambda value: None),
        )
        with mock.patch("libs.event_handeler.speak"):
            EventHandeler.connected(handler, {
                "username": "tester",
                "is_staff": True,
                "can_use_music_bot": True,
                "music_bot_locked": {"seek": "Seek"},
            })
        self.assertTrue(gate.is_locked("seek"))

        with mock.patch("libs.event_handeler.speak"):
            EventHandeler.connected(handler, {
                "username": "tester",
                "is_staff": True,
                "can_use_music_bot": True,
            })
        self.assertEqual(gate.locked_ids(), frozenset(),
                         "a fresh session with no key is a Server that locks nothing")

    def test_a_snapshot_with_an_empty_map_unlocks_everything(self):
        gp = make_gp(is_staff=True)
        handler, _scheduled = self._handler(gp)
        gate.apply_locked({"seek": "Seek"})
        EventHandeler.staff_permissions(handler, {
            "is_staff": True,
            "music_bot_locked": {},
        })
        self.assertEqual(gate.locked_ids(), frozenset())


if __name__ == "__main__":
    unittest.main()
