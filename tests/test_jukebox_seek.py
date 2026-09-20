"""Scrubbing a cabinet: Left and Right on the cabinet's own Scrub line.

The whole reason this feature is worth having is that a needle move costs
nothing: the cabinet is already silent for the scrub, so the number can follow
the keys instead of restarting a stream per arrow. These tests pin that -- the
moves are silent, the steps are the three the line promises, a held arrow skims
(this client has no key repeat at all) -- and the two things that make it a
keystroke rather than a menu: the line answers Left and Right itself (nothing to
open first), and it plays the song on by itself once the hand stops, so no
cabinet can be left frozen by walking away from it.

Whether the room hears the song again is deliberately *not* decided here: the
Server's own session decides (see tools/jukebox_seek_test.js), which is why the
closing packet carries a needle and no opinion.
"""

import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

import pygame

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from libs import jukebox
from libs import menu as menu_module


class FakeClock:
    """A monotonic clock a test can move by hand."""

    def __init__(self, start=1000.0):
        self.now = start

    def __call__(self):
        return self.now

    def tick(self, seconds):
        self.now += seconds


class HeldKeys(dict):
    """What ``pygame.key.get_pressed()`` answers inside a test: nothing by
    default, which is how the real thing answers for a key nobody holds."""

    def __missing__(self, key):
        return False


class FakeNetwork:
    def __init__(self):
        self.sent = []

    def send(self, *args):
        self.sent.append(args)

    def events(self, event):
        return [call[2] for call in self.sent if call[1] == event]


class FakeGame:
    """A game whose scheduled work can be stepped by hand.

    The scrub's hold runs on ``call_after`` ticks (one small clock for the
    held-arrow skim and for closing the scrub), so a test needs to be able to
    fire the next tick instead of waiting for a real frame loop.
    """

    def __init__(self):
        self.network = FakeNetwork()
        self.scheduled = []
        self.cancelled = []
        self._next_id = 1

    def call_after(self, ms, function):
        entry = {"id": self._next_id, "ms": ms, "fn": function}
        self._next_id += 1
        self.scheduled.append(entry)
        return entry["id"]

    def cancel_before(self, timer_id):
        self.cancelled.append(timer_id)
        self.scheduled[:] = [e for e in self.scheduled if e["id"] != timer_id]

    def run_next_tick(self):
        """Fire the oldest pending tick, as the game loop eventually would."""
        entry = self.scheduled.pop(0)
        entry["fn"]()
        return entry["ms"]


class FakeGameplay:
    def __init__(self, state):
        self.jukebox_state = state
        self.player = SimpleNamespace(name="me", x=0.0, y=0.0, z=0.0)
        self.jukebox_player = None
        self.substates = []
        self.map = None
        self.voice_chat = None

    def add_substate(self, substate):
        if hasattr(substate, "enter"):
            substate.enter()
        self.substates.append(substate)
        return substate

    def pop_last_substate(self):
        if not self.substates:
            return None
        substate = self.substates.pop()
        if hasattr(substate, "exit"):
            substate.exit()
        return substate


def cabinet_state(**box):
    return {"jukeboxes": {"box-1": {"id": "box-1", **box}}}


def song(title="Song A", duration=240):
    return {"title": title, "url": "https://e.com/a", "duration": duration,
            "queuedBy": "alice"}


def playing_gp(needle=40.0, duration=240, received_at=1000.0):
    """A gameplay where the cabinet is playing and this machine hears it."""
    state = cabinet_state(current=song(duration=duration), paused=False,
                          position=0.0)
    gp = FakeGameplay(state)
    gp.jukebox_player = SimpleNamespace(players={
        "box-1": {"play_params": {"start_offset": needle,
                                  "received_at": received_at}},
    })
    return gp


class NeedleTests(unittest.TestCase):
    def test_nothing_playing_is_nothing_to_scrub(self):
        gp = FakeGameplay({"jukeboxes": {}})
        self.assertIsNone(jukebox._scrub_spot(gp, "box-1"))

    def test_a_livestream_has_no_length_to_scrub(self):
        gp = FakeGameplay(cabinet_state(current=song(duration=0)))
        self.assertIsNone(jukebox._scrub_spot(gp, "box-1"))

    def test_a_playing_cabinet_is_read_from_this_machines_own_ears(self):
        # The anchor is the direct path's own arithmetic: where the play event
        # started plus everything elapsed since it arrived.
        clock = FakeClock(1020.0)
        gp = playing_gp(needle=40.0, received_at=1000.0)
        with mock.patch("time.monotonic", clock):
            spot = jukebox._scrub_spot(gp, "box-1")
        self.assertEqual(spot, (60.0, 240))

    def test_a_paused_cabinet_is_read_from_the_servers_own_needle(self):
        gp = FakeGameplay(cabinet_state(current=song(), paused=True, position=91.5))
        self.assertEqual(jukebox._scrub_spot(gp, "box-1"), (91.5, 240))

    def test_a_cabinet_this_machine_is_not_playing_falls_back_to_the_needle(self):
        gp = FakeGameplay(cabinet_state(current=song(), paused=False, position=12.0))
        self.assertEqual(jukebox._scrub_spot(gp, "box-1"), (12.0, 240))

    def test_the_line_reads_the_needle_and_the_song(self):
        gp = FakeGameplay(cabinet_state(current=song(duration=252), paused=True,
                                        position=83.0))
        self.assertEqual(jukebox._scrub_menu_label(gp, "box-1"),
                         "Scrub playback (now: 1:23 of 4:12)")

    def test_a_control_that_is_holding_a_scrub_reads_its_own_needle(self):
        # Between scrubs the label asks the cabinet; while frozen nothing else
        # knows the needle (the Server is told about a move only now and then).
        gp = FakeGameplay(cabinet_state(current=song(), paused=True, position=83.0))
        self.assertEqual(
            jukebox._scrub_menu_label(gp, "box-1", spot=(150.0, 240)),
            "Scrub playback (now: 2:30 of 4:00)")


class FakeMenu:
    """Drop-in for libs.menu.Menu capturing the items it was built with."""

    last = None

    def __init__(self, game, title, *args, **kwargs):
        self.title = title
        self.items = []
        FakeMenu.last = self

    def add_items(self, items):
        self.items = list(items)

    def action(self, label):
        for item in self.items:
            text = item[0]() if callable(item[0]) else item[0]
            if text == label:
                return item[1]
        raise AssertionError(f"no line named {label!r}: "
                             f"{[i[0]() if callable(i[0]) else i[0] for i in self.items]}")

    def labels(self):
        return [label() if callable(label) else label
                for label, _action in self.items]


def open_menu(gp, game=None):
    game = game or FakeGame()
    with mock.patch("libs.menu.Menu", FakeMenu), \
            mock.patch("libs.menus.set_default_sounds"), \
            mock.patch("libs.jukebox.speak"):
        jukebox.open_jukebox_menu(game, gp)
    return game, FakeMenu.last


class CabinetLineTests(unittest.TestCase):
    def test_the_line_is_offered_while_a_song_can_be_scrubbed(self):
        gp = playing_gp()
        with mock.patch("time.monotonic", FakeClock()):
            _game, menu = open_menu(gp)
            # Read inside the frozen clock: the label is a callable, so it
            # measures the needle again every time the menu speaks.
            self.assertIn("Scrub playback (now: 0:40 of 4:00)", menu.labels())

    def test_a_cabinet_with_no_song_offers_no_scrub_line(self):
        gp = FakeGameplay(cabinet_state())
        _game, menu = open_menu(gp)
        self.assertNotIn("Scrub playback", menu.labels())

    def test_a_livestream_offers_no_scrub_line(self):
        gp = FakeGameplay(cabinet_state(current=song(duration=0), paused=False))
        _game, menu = open_menu(gp)
        self.assertNotIn("Scrub playback", menu.labels())

    def test_the_line_is_the_control_the_arrows_are_handed_to(self):
        # This is the whole contract with menu.Menu: a line whose action offers
        # `arrow` answers Left and Right itself, so the scrub needs no Enter.
        gp = playing_gp()
        with mock.patch("time.monotonic", FakeClock()):
            _game, menu = open_menu(gp)
            action = menu.action("Scrub playback (now: 0:40 of 4:00)")
        self.assertIsInstance(action, jukebox._JukeboxScrubControl)
        self.assertTrue(callable(getattr(action, "arrow", None)))

    def test_enter_on_the_line_says_what_it_does_and_where_it_is(self):
        gp = playing_gp(needle=40.0)
        with mock.patch("time.monotonic", FakeClock()), \
                mock.patch("libs.jukebox.speak") as spoken:
            _game, menu = open_menu(gp)
            menu.action("Scrub playback (now: 0:40 of 4:00)")()
        line = spoken.call_args.args[0]
        self.assertIn("The song is at 0:40 of 4:00", line)
        self.assertIn("Left and Right move ten seconds", line)
        self.assertIn("Hold an arrow to skim", line)
        self.assertIn("plays on by itself", line)


class ScrubControlTests(unittest.TestCase):
    """The hold itself: freeze, move for free, play on when the hand stops."""

    def setUp(self):
        # Every tick asks the keyboard whether the arrow is still down (this
        # client has no key repeat). A test that cares holds one of its own.
        held = HeldKeys()
        patcher = mock.patch("pygame.key.get_pressed", lambda: held)
        self.held = patcher.start()
        self.addCleanup(patcher.stop)

    def control(self, game, gp, needle=40.0, duration=240):
        with mock.patch("time.monotonic", FakeClock(1000.0)):
            return jukebox._JukeboxScrubControl(game, gp, "box-1")

    def test_the_first_arrow_freezes_the_cabinet_and_moves_ten_seconds(self):
        game, gp = FakeGame(), playing_gp()
        with mock.patch("time.monotonic", FakeClock()), \
                mock.patch("libs.jukebox.speak"):
            control = self.control(game, gp)
            control.arrow(1, 0)
        self.assertEqual(game.network.events("jukebox_seek_start"),
                         [{"id": "box-1"}])
        self.assertEqual(game.network.events("jukebox_seek"),
                         [{"id": "box-1", "position": 50.0}])
        self.assertEqual(control.needle, 50.0)
        # The hold is armed, and cancelling it is what closes the scrub later.
        self.assertEqual([tick["ms"] for tick in game.scheduled],
                         [int(jukebox._JukeboxScrubControl.TICK_INTERVAL_S * 1000)])

    def test_the_three_steps_are_ten_thirty_and_sixty_seconds(self):
        for mod, expected in ((0, 50.0), (pygame.KMOD_SHIFT, 70.0),
                              (pygame.KMOD_CTRL, 100.0),
                              (pygame.KMOD_CTRL | pygame.KMOD_SHIFT, 100.0)):
            with self.subTest(mod=mod):
                game, gp = FakeGame(), playing_gp()
                with mock.patch("time.monotonic", FakeClock()), \
                        mock.patch("libs.jukebox.speak"):
                    control = self.control(game, gp)
                    control.arrow(1, mod)
                self.assertEqual(control.needle, expected)
                self.assertEqual(game.network.events("jukebox_seek"),
                                 [{"id": "box-1", "position": expected}])

    def test_a_needle_stops_at_both_ends_of_the_song(self):
        game, gp = FakeGame(), playing_gp(needle=3.0)
        with mock.patch("time.monotonic", FakeClock()), \
                mock.patch("libs.jukebox.speak"):
            control = self.control(game, gp, needle=3.0)
            control.arrow(-1, 0)
            self.assertEqual(control.needle, 0.0)
            control.arrow(-1, 0)                     # still answers at the start
            for expected in (60.0, 120.0, 180.0):
                control.arrow(1, pygame.KMOD_CTRL)
                self.assertEqual(control.needle, expected)
            control.arrow(1, pygame.KMOD_CTRL)
            # 238 is the song's own last two seconds, kept behind the needle.
            self.assertEqual(control.needle, 238.0)

    def test_presses_inside_the_hold_are_free_and_silent(self):
        game, gp = FakeGame(), playing_gp()
        clock = FakeClock()
        with mock.patch("time.monotonic", clock), \
                mock.patch("libs.jukebox.speak"):
            control = self.control(game, gp)
            control.arrow(1, 0)                      # 40 -> 50, freezes once
            clock.tick(1.0)
            game.run_next_tick()                     # the hold is still open
            control.arrow(1, pygame.KMOD_SHIFT)      # 50 -> 80
            clock.tick(0.5)
            game.run_next_tick()
            control.arrow(1, 0)                      # 80 -> 90
        # One freeze, three moves: the room hears the cabinet go quiet once.
        self.assertEqual(len(game.network.events("jukebox_seek_start")), 1)
        self.assertEqual([event["position"]
                          for event in game.network.events("jukebox_seek")],
                         [50.0, 80.0, 90.0])
        self.assertEqual(game.network.events("jukebox_seek_end"), [])

    def test_the_hold_plays_the_song_on_by_itself_and_says_nothing(self):
        game, gp = FakeGame(), playing_gp()
        clock = FakeClock()
        with mock.patch("time.monotonic", clock), \
                mock.patch("libs.jukebox.speak") as spoken:
            control = self.control(game, gp)
            control.arrow(1, pygame.KMOD_CTRL)       # 40 -> 100
            spoken.reset_mock()
            clock.tick(jukebox._JukeboxScrubControl.SCRUB_HOLD_S + 0.01)
            game.run_next_tick()
        self.assertEqual(game.network.events("jukebox_seek_end"),
                         [{"id": "box-1", "position": 100.0}])
        # The needle itself was read out on the move; the closing packet is not
        # announced from here, because only the Server knows whether the room
        # hears the song again (and it says so itself).
        self.assertEqual(spoken.call_count, 0)
        self.assertEqual(game.scheduled, [])

    def test_a_press_after_the_hold_opens_a_fresh_scrub(self):
        game, gp = FakeGame(), playing_gp()
        clock = FakeClock()
        with mock.patch("time.monotonic", clock), \
                mock.patch("libs.jukebox.speak"):
            control = self.control(game, gp)
            control.arrow(1, 0)
            clock.tick(jukebox._JukeboxScrubControl.SCRUB_HOLD_S + 0.01)
            game.run_next_tick()                     # hold expired, song plays on
            control.arrow(1, 0)                      # a new hand on the arrows
        self.assertEqual(len(game.network.events("jukebox_seek_start")), 2)
        self.assertEqual(len(game.network.events("jukebox_seek_end")), 1)

    def test_holding_an_arrow_skims_and_keeps_the_hold_open(self):
        game, gp = FakeGame(), playing_gp()
        clock, held = FakeClock(), HeldKeys()
        with mock.patch("time.monotonic", clock), \
                mock.patch("pygame.key.get_pressed", lambda: held), \
                mock.patch("libs.jukebox.speak"):
            control = self.control(game, gp)
            held[pygame.K_RIGHT] = True
            control.arrow(1, 0)                      # 40 -> 50
            clock.tick(0.2)
            game.run_next_tick()                     # inside the tap delay
            self.assertEqual(control.needle, 50.0)
            clock.tick(0.2)                          # past it: start the clock
            game.run_next_tick()
            clock.tick(0.25)
            game.run_next_tick()
            self.assertAlmostEqual(control.needle, 60.0)
            # A held arrow is a hand still on the key however long it runs, so
            # the hold cannot expire under a skim.
            clock.tick(jukebox._JukeboxScrubControl.SCRUB_HOLD_S + 1.0)
            game.run_next_tick()
            self.assertGreater(control.needle, 60.0)
        self.assertEqual(game.network.events("jukebox_seek_end"), [])

    def test_a_released_arrow_stops_skimming(self):
        game, gp = FakeGame(), playing_gp()
        clock, held = FakeClock(), HeldKeys()
        with mock.patch("time.monotonic", clock), \
                mock.patch("pygame.key.get_pressed", lambda: held), \
                mock.patch("libs.jukebox.speak"):
            control = self.control(game, gp)
            held[pygame.K_LEFT] = True
            control.arrow(-1, 0)
            clock.tick(0.4)
            game.run_next_tick()
            clock.tick(0.25)
            game.run_next_tick()
            self.assertLess(control.needle, 30.0)
            still = control.needle
            del held[pygame.K_LEFT]
            clock.tick(0.5)
            game.run_next_tick()
            self.assertEqual(control.needle, still)

    def test_a_skimming_needle_does_not_send_a_packet_per_tick(self):
        game, gp = FakeGame(), playing_gp()
        clock = FakeClock()
        with mock.patch("time.monotonic", clock), \
                mock.patch("libs.jukebox.speak"):
            control = self.control(game, gp)
            control.arrow(1, 0)
            clock.tick(0.05)
            control._move(1)                         # too soon: swallowed
            positions = [event["position"]
                         for event in game.network.events("jukebox_seek")]
            self.assertEqual(positions, [50.0])
            clock.tick(0.2)
            control._move(1)
            self.assertEqual(len(game.network.events("jukebox_seek")), 2)

    def test_a_cabinet_with_nothing_to_scrub_is_told_so_and_freezes_nothing(self):
        game, gp = FakeGame(), FakeGameplay(cabinet_state())
        with mock.patch("time.monotonic", FakeClock()), \
                mock.patch("libs.jukebox.speak") as spoken:
            control = jukebox._JukeboxScrubControl(game, gp, "box-1")
            control.arrow(1, 0)
        self.assertIn("nothing to scrub", spoken.call_args.args[0])
        self.assertEqual(game.network.sent, [])
        self.assertEqual(game.scheduled, [])

    def test_the_end_carries_the_needle_and_no_opinion_of_its_own(self):
        # Whether the room hears the song again is the Server's session to
        # answer: a scrub whose client guesses it is how a cabinet somebody
        # paused gets woken up again.
        game, gp = FakeGame(), playing_gp()
        clock = FakeClock()
        with mock.patch("time.monotonic", clock), \
                mock.patch("libs.jukebox.speak"):
            control = self.control(game, gp)
            control.arrow(1, pygame.KMOD_SHIFT)      # 40 -> 70
            clock.tick(jukebox._JukeboxScrubControl.SCRUB_HOLD_S + 0.01)
            game.run_next_tick()
        self.assertEqual(game.network.events("jukebox_seek_end"),
                         [{"id": "box-1", "position": 70.0}])


class MenuArrowTests(unittest.TestCase):
    """The other half of the contract: the cabinet menu really hands an arrow
    over, and a line without one is left exactly as it was."""

    def setUp(self):
        patcher = mock.patch.object(menu_module.speech, "speak")
        self.speak = patcher.start()
        self.addCleanup(patcher.stop)

    def make_menu(self, items, pos=0):
        class SoundGroup:
            labeled_sources = {}

            def play(self, *args, **kwargs):
                pass

        game = SimpleNamespace(
            direct_soundgroup=SoundGroup(), match_history=[],
            audio_mngr=SimpleNamespace(volume_categories={"ui": [100]}))
        menu = menu_module.Menu(game, "Music Jukebox")
        menu.items = list(items)
        menu.pos = pos
        return menu

    def key(self, name, mod=pygame.KMOD_NONE):
        return pygame.event.Event(pygame.KEYDOWN, key=name, mod=mod, unicode="")

    def test_left_and_right_reach_a_line_that_offers_them(self):
        arrows = []
        line = SimpleNamespace(arrow=lambda direction, mod: arrows.append((direction, mod)),
                               __call__=lambda self=None: None)
        menu = self.make_menu([("Scrub playback (now: 0:40 of 4:00)", line),
                               ("Cancel", lambda: None)])
        menu.update([self.key(pygame.K_LEFT, pygame.KMOD_SHIFT)])
        menu.update([self.key(pygame.K_RIGHT)])
        self.assertEqual(arrows, [(-1, pygame.KMOD_SHIFT), (1, 0)])

    def test_a_line_that_offers_nothing_does_not_answer_the_arrows(self):
        chosen = []
        menu = self.make_menu([("Cancel", lambda: chosen.append("cancel"))])
        menu.update([self.key(pygame.K_LEFT)])
        menu.update([self.key(pygame.K_RIGHT)])
        # Nothing was chosen, nothing was spoken, and the cursor never moved.
        self.assertEqual(chosen, [])
        self.assertEqual(menu.pos, 0)
        self.assertEqual(self.speak.call_count, 0)

    def test_the_line_only_answers_while_it_is_the_one_focused(self):
        arrows = []
        line = SimpleNamespace(arrow=lambda direction, mod: arrows.append(direction))
        menu = self.make_menu([("Cancel", lambda: None),
                               ("Scrub playback", line)], pos=0)
        menu.update([self.key(pygame.K_RIGHT)])
        self.assertEqual(arrows, [])
        menu.pos = 1
        menu.update([self.key(pygame.K_RIGHT)])
        self.assertEqual(arrows, [1])

    def test_the_real_cabinet_menu_scrubs_from_the_keyboard(self):
        game, gp = FakeGame(), playing_gp(needle=40.0)

        class SoundGroup:
            labeled_sources = {}

            def play(self, *args, **kwargs):
                pass

        game.direct_soundgroup = SoundGroup()
        game.match_history = []
        game.audio_mngr = SimpleNamespace(volume_categories={"ui": [100]})
        with mock.patch("time.monotonic", FakeClock()), \
                mock.patch("libs.jukebox.speak"), \
                mock.patch("libs.menus.set_default_sounds"):
            jukebox.open_jukebox_menu(game, gp)
            menu = gp.substates[-1]
            menu.pos = next(
                index for index, item in enumerate(menu.items)
                if "Scrub playback"
                in str(item[0]() if callable(item[0]) else item[0])
            )
            menu.update([self.key(pygame.K_RIGHT, pygame.KMOD_CTRL)])
        self.assertEqual(game.network.events("jukebox_seek_start"),
                         [{"id": "box-1"}])
        self.assertEqual(game.network.events("jukebox_seek"),
                         [{"id": "box-1", "position": 100.0}])


class DirectRebuildTests(unittest.TestCase):
    """A direct listener builds its own stream on every resume and seek, so a
    scrub can only be continuous if that rebuild can heal itself."""

    PAGE = "https://www.youtube.com/watch?v=fixture"
    SIGNED = "https://rr1---sn-x.googlevideo.com/videoplayback?expire=1"

    def player(self):
        game = mock.MagicMock()
        player = jukebox.JukeboxPlayer(game)
        return game, player

    def test_a_direct_stream_is_handed_the_page_it_is_playing(self):
        _game, player = self.player()
        with mock.patch("libs.music_bot.AudioStreamer") as streamer:
            player.play("box-1", 0.0, 0.0, 0.0, "Song", self.SIGNED, 240,
                        transport="direct", canonical_url=self.PAGE)
        self.assertEqual(streamer.call_args.kwargs["canonical_url"], self.PAGE)
        self.assertEqual(
            player.players["box-1"]["play_params"]["canonical_url"], self.PAGE)

    def test_the_emergency_fallback_replays_the_same_page(self):
        # The fallback re-offers the song from its stored play params; the page
        # travels with them, so the direct stream it starts can re-resolve too.
        _game, player = self.player()
        with mock.patch("libs.music_bot.AudioStreamer") as streamer, \
                mock.patch("libs.jukebox.JukeboxRelayReceiver"):
            player.play("box-1", 0.0, 0.0, 0.0, "Song", self.SIGNED, 240,
                        transport="relay", relay_id=1, stream_epoch=2,
                        canonical_url=self.PAGE)
            player._switch_to_direct("box-1",
                                     player.players["box-1"]["play_params"])
        # The relay stream must really have been replaced by a direct one, or
        # this would be checking the relay play event instead.
        self.assertEqual(streamer.call_count, 1)
        self.assertEqual(streamer.call_args.kwargs["canonical_url"], self.PAGE)


if __name__ == "__main__":
    unittest.main()
