"""The speaker test: one voice, one side, from the main menu.

A player wondering whether their headphones are on the right way round (or
whether a surround setting did what it promised) has nowhere to stand to find
out: the game's own sounds are all around them in a map, and every one of them
is mixed for a room. So the main menu -- beside Check for Updates, which asks
the same kind of question about the same machine -- carries a test that plays one
short voice hard-panned to the left, the right, or each in turn.

These cover both halves, because a test that lies is worse than no test:

  * the menu line exists, sits where somebody would look for it, and each line
    asks for *its* placement's sample: left means left, centre means straight
    ahead, and the walk follows each voice's own measured length instead of
    guessing a gap;
  * the voice itself is a *relative* source one unit to the listener's own left,
    right or front, with distance attenuation off, so "left" is this listener's
    left wherever they are standing and the answer never depends on which way
    they face -- the pan is the placement and nothing else;
  * one side at a time: whatever was sounding is stopped before the next thing
    is played, and a finished test leaves no source behind in a menu that may
    never run the frame loop's own cleanup;
  * a test that cannot be heard says why (the game is muted, the sample could
    not be played) rather than playing nothing in silence;
  * and the two samples are really on disk under the names the menu asks for, so
    a rename cannot quietly turn a side into silence.

The samples are one voice per side; if they are ever re-recorded, they are
dropped in beside these two names and nothing here changes.
"""

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from libs import menus
from libs.audio.sound import Sound
from libs.audio_manager import AudioManager

CLIENT = Path(__file__).resolve().parents[1]
TEST_SOUNDS = {"left": "ui/speaker_test_left.ogg",
               "centre": "ui/speaker_test_center.ogg",
               "right": "ui/speaker_test_right.ogg"}


# ─── The menu half: what each line asks the audio manager for ───


class FakeListener:
    def __init__(self):
        self.gain = 0.0


class FakeSoundGroup:
    def play(self, *args, **kwargs):
        return None


class FakeAudioMngr:
    """Records the speaker-test calls; the real ones are covered below."""

    def __init__(self, muted=False, seconds=2.0):
        self.volume_categories = {"master": [70, None], "ui": [100, None]}
        self.listener = FakeListener()
        self.muted = muted
        self.seconds = seconds
        self.played = []
        self.stopped = 0

    def create_soundgroup(self, direct=False):
        return FakeSoundGroup()

    def play_headphone_test(self, path, side, volume=100):
        if self.muted:
            return 0.0
        self.played.append((path, side))
        return self.seconds

    def stop_headphone_test(self):
        self.stopped += 1
        return True


class FakeGame:
    """Enough of Game for menus.main_menu and the test menu it opens."""

    def __init__(self, audio=None):
        # A real one, not a sentinel: entering a menu plays its "open" sound.
        self.direct_soundgroup = FakeSoundGroup()
        self.audio_mngr = audio if audio is not None else FakeAudioMngr()
        self.replaced = None
        self.delayed = []
        self.fade_out_and_exit = object()
        self.ask_to_restart_client = object()
        self.set_account = lambda: None
        self.create_account = lambda: None

    def replace(self, state):
        self.replaced = state

    def call_after(self, time, function):
        self.delayed.append((time, function))
        return len(self.delayed)

    def run_delayed(self):
        scheduled, self.delayed = self.delayed, []
        for _time, function in scheduled:
            function()


def open_test_menu(game):
    """Walk the real main menu into the speaker test, as a player does."""
    menus.main_menu(game)
    item = next(item for item in game.replaced.items
                if item[0] == "Test Speakers and Headphones")
    item[1]()
    return game.replaced


def press(menu, label):
    action = next(item[1] for item in menu.items if item[0] == label)
    action()


class TheLineIsWhereAPlayerLooksTests(unittest.TestCase):
    def test_it_sits_beside_check_for_updates(self):
        game = FakeGame()
        menus.main_menu(game)
        titles = [item[0] for item in game.replaced.items]
        self.assertEqual(titles[titles.index("Check for Updates") + 1],
                         "Test Speakers and Headphones")
        # The menu's own shape is untouched: Restart Client is still the item
        # Esc-on-the-root-menu and the exit fade both reach for.
        self.assertEqual(titles.index("Restart Client"), titles.index("Exit") - 1)

    def test_the_menu_says_how_it_works_before_it_reads_its_lines(self):
        """The test is silent by design until Enter lands on a line, so the menu
        has to say that on the way in -- otherwise it reads as a broken one.
        The intro and the title are ONE utterance: speak() interrupts, so a
        second call would cut the title off mid-word."""
        game = FakeGame()
        menu = open_test_menu(game)
        with mock.patch("libs.speech.speak") as speak:
            menu.enter()
        self.assertEqual(speak.call_count, 1)
        spoken = speak.call_args.args[0]
        self.assertEqual(spoken, f"{menu.title} {menus.SPEAKER_TEST_INTRO}")
        self.assertTrue(spoken.startswith("Speaker test."), spoken)
        # The two questions a listener asks are answered in it: what the arrows
        # do, and what the key that plays does.
        self.assertIn("Up and Down move between the lines", spoken)
        self.assertIn("Enter plays the position the line names", spoken)
        self.assertIn("Nothing is heard until you press Enter", spoken)

    def test_a_menu_without_an_intro_is_spoken_exactly_as_before(self):
        """The speaker test opened a door in the shared Menu class; every other
        menu must still introduce itself as a title and nothing else."""
        from libs import menu as menu_mod
        plain = menu_mod.Menu(FakeGame(), "Options menu")
        self.assertEqual(plain.intro, "")
        with mock.patch("libs.speech.speak") as speak:
            plain.enter()
        self.assertEqual(speak.call_count, 1)
        self.assertEqual(speak.call_args.args[0], "Options menu")

    def test_it_opens_a_test_menu_that_offers_every_way_to_test(self):
        game = FakeGame()
        menu = open_test_menu(game)
        self.assertEqual(menu.title, "Speaker test.")
        self.assertEqual([item[0] for item in menu.items],
                         ["Left speaker only", "Centre speaker only",
                          "Right speaker only", "Left, centre, then right",
                          "Stop the test", "Back"])
        # Back is last, so Esc reaches it.
        self.assertEqual(menu.items[-1][0], "Back")


class EachLineAsksForItsOwnPlacementTests(unittest.TestCase):
    def test_left_sounds_the_left_sample_on_the_left(self):
        game = FakeGame()
        menu = open_test_menu(game)
        press(menu, "Left speaker only")
        self.assertEqual(game.audio_mngr.played, [(TEST_SOUNDS["left"], "left")])

    def test_centre_sounds_the_centre_sample_straight_ahead(self):
        game = FakeGame()
        menu = open_test_menu(game)
        press(menu, "Centre speaker only")
        self.assertEqual(game.audio_mngr.played, [(TEST_SOUNDS["centre"], "centre")])

    def test_right_sounds_the_right_sample_on_the_right(self):
        game = FakeGame()
        menu = open_test_menu(game)
        press(menu, "Right speaker only")
        self.assertEqual(game.audio_mngr.played, [(TEST_SOUNDS["right"], "right")])

    def test_the_walk_visits_left_centre_and_right_measured_each_time(self):
        """Every step follows the voice before it, so the gap is the length of
        what just sounded -- a constant typed in as the gap would clip a voice
        or leave a hole in it, and the three samples are different lengths."""
        game = FakeGame(FakeAudioMngr(seconds=2.5))
        menu = open_test_menu(game)
        press(menu, "Left, centre, then right")
        self.assertEqual(game.audio_mngr.played, [(TEST_SOUNDS["left"], "left")])
        self.assertEqual(len(game.delayed), 1, "the centre must be scheduled")
        delay, _function = game.delayed[0]
        self.assertEqual(delay, 2750)  # 2.5s of voice, then a quarter-second gap
        game.run_delayed()
        self.assertEqual(game.audio_mngr.played[-1], (TEST_SOUNDS["centre"], "centre"))
        game.run_delayed()
        self.assertEqual(game.audio_mngr.played[-1], (TEST_SOUNDS["right"], "right"))
        self.assertEqual(game.delayed, [], "the walk ends after the right side")

    def test_each_step_of_the_walk_waits_on_its_own_sample(self):
        """The samples differ in length, so a walk that reused the first one's
        gap would cut the second voice short."""
        class MeasuringMngr(FakeAudioMngr):
            def __init__(self, lengths):
                super().__init__()
                self.lengths, self.index = lengths, 0

            def play_headphone_test(self, path, placement, volume=100):
                self.played.append((path, placement))
                seconds = self.lengths[self.index]
                self.index += 1
                return seconds

        game = FakeGame(MeasuringMngr([1.0, 3.0, 0.5]))
        menu = open_test_menu(game)
        press(menu, "Left, centre, then right")
        self.assertEqual(game.delayed[0][0], 1250)
        game.run_delayed()
        self.assertEqual(game.delayed[0][0], 3250)
        game.run_delayed()
        self.assertEqual(game.delayed, [])

    def test_enter_adds_no_sound_of_its_own(self):
        """The centred select sound landing on a hard-panned voice is exactly
        what the test is trying to tell apart, so Enter is silent here."""
        game = FakeGame()
        menu = open_test_menu(game)
        self.assertEqual(menu.enter_sound, "")
        self.assertEqual(menu.open, "menu/open.ogg", "the menu itself is unchanged")
        self.assertEqual(menu.click, "menu/move.ogg")

    def test_stop_takes_the_voice_off_the_air(self):
        game = FakeGame()
        menu = open_test_menu(game)
        press(menu, "Stop the test")
        self.assertEqual(game.audio_mngr.stopped, 1)


class OneWalkAtATimeTests(unittest.TestCase):
    """Press the walk again and it starts over, instead of a second one running
    behind the first.

    Every step is a callback on the game clock and nothing took the steps of a
    replaced walk back, so a second press left two chains stepping over each
    other's voices -- each sample cut short by the other chain's next one, which
    is heard as the test playing on top of itself rather than as one walk from
    the left to the right.
    """

    def test_pressing_the_walk_again_starts_it_over_from_the_left(self):
        game = FakeGame()
        menu = open_test_menu(game)
        press(menu, "Left, centre, then right")
        press(menu, "Left, centre, then right")
        self.assertEqual(
            game.audio_mngr.played,
            [(TEST_SOUNDS["left"], "left"), (TEST_SOUNDS["left"], "left")],
            "the left side sounds again: the walk restarts rather than stacks",
        )
        self.assertEqual(len(game.delayed), 2, "each press owns one pending step")
        game.run_delayed()
        centres = [call for call in game.audio_mngr.played if call[1] == "centre"]
        self.assertEqual(len(centres), 1, "the replaced walk must not sound too")
        self.assertEqual(game.audio_mngr.played[-1], (TEST_SOUNDS["centre"], "centre"))
        game.run_delayed()
        rights = [call for call in game.audio_mngr.played if call[1] == "right"]
        self.assertEqual(len(rights), 1, "one walk, one visit to each side")
        self.assertEqual(game.delayed, [], "the surviving walk ends on the right")

    def test_a_single_placement_ends_the_walk_still_stepping_behind_it(self):
        game = FakeGame()
        menu = open_test_menu(game)
        press(menu, "Left, centre, then right")
        press(menu, "Right speaker only")
        game.run_delayed()
        self.assertEqual(
            game.audio_mngr.played,
            [(TEST_SOUNDS["left"], "left"), (TEST_SOUNDS["right"], "right")],
            "the walk's centre must not step over the side that was asked for",
        )

    def test_stopping_the_test_takes_the_pending_walk_with_it(self):
        game = FakeGame()
        menu = open_test_menu(game)
        press(menu, "Left, centre, then right")
        press(menu, "Stop the test")
        game.run_delayed()
        self.assertEqual(game.audio_mngr.played, [(TEST_SOUNDS["left"], "left")],
                         "a walk that was stopped says nothing more")

    def test_leaving_the_test_leaves_the_walk_and_the_voice_behind(self):
        game = FakeGame()
        menu = open_test_menu(game)
        press(menu, "Left, centre, then right")
        press(menu, "Back")
        self.assertEqual(game.replaced.title, "Main menu.")
        self.assertEqual(game.audio_mngr.stopped, 1, "the voice goes with the menu")
        game.run_delayed()
        self.assertEqual(game.audio_mngr.played, [(TEST_SOUNDS["left"], "left")],
                         "no step of the walk sounds into the main menu")


class ATestThatCannotBeHeardSaysWhyTests(unittest.TestCase):
    def test_a_muted_game_says_so_instead_of_playing_nothing(self):
        game = FakeGame(FakeAudioMngr(muted=True))
        menu = open_test_menu(game)
        with mock.patch("libs.speech.speak") as speak:
            press(menu, "Left speaker only")
            press(menu, "Left, centre, then right")
        self.assertEqual(game.audio_mngr.played, [], "nothing may be played")
        self.assertEqual(speak.call_count, 2, "each line must say why it was silent")
        self.assertIn("muted", speak.call_args.args[0].lower())
        self.assertEqual(game.delayed, [], "a silent step schedules nothing")

    def test_a_sample_that_cannot_play_is_reported(self):
        """A side with no sample behind it is a build problem, not a hearing
        problem, so it is never a button that does nothing."""
        game = FakeGame(FakeAudioMngr(seconds=0.0))
        menu = open_test_menu(game)
        with mock.patch("libs.speech.speak") as speak:
            press(menu, "Right speaker only")
            press(menu, "Left, centre, then right")
        self.assertIn("could not be played", speak.call_args.args[0])
        self.assertEqual(len(game.delayed), 0, "a failed voice schedules nothing")

    def test_stop_with_nothing_playing_says_so(self):
        game = FakeGame()
        menu = open_test_menu(game)
        game.audio_mngr.stop_headphone_test = lambda: False
        with mock.patch("libs.speech.speak") as speak:
            press(menu, "Stop the test")
        self.assertIn("Nothing is playing", speak.call_args.args[0])

    def test_a_stopped_voice_says_nothing(self):
        game = FakeGame()
        menu = open_test_menu(game)
        with mock.patch("libs.speech.speak") as speak:
            press(menu, "Stop the test")
        self.assertEqual(speak.call_count, 0, "silence is the whole answer")


# ─── The audio half: the pan itself, on the real AudioManager ───


class FakeSource:
    def __init__(self, **kwargs):
        for name, value in kwargs.items():
            setattr(self, name, value)
        self.buffer = None
        self.played = False
        self.stopped = False

    def set(self, name, value):
        setattr(self, name, value)

    def play(self):
        self.played = True

    def stop(self):
        self.stopped = True


class FakeBuffer:
    def __init__(self, seconds):
        self.sec_length = seconds


class FakeContext:
    def __init__(self):
        self.created = []

    def gen_source(self, **kwargs):
        source = FakeSource(**kwargs)
        self.created.append(source)
        return source


def fake_manager(seconds=2.0, muted=False, ui_volume=100, buffer=None, raises=False):
    """The real AudioManager with the engine replaced by recording fakes."""
    audio = AudioManager.__new__(AudioManager)
    audio.muted = muted
    audio.volume_categories = {"master": [100, set()], "ui": [ui_volume, set()],
                               "miscelaneous": [100, set()]}
    audio.filter = []
    audio.sends = []
    audio.unbound_sources = []
    audio._headphone_test_sound = None
    audio.context = FakeContext()

    def load_buffer(path, as_mono=False):
        if raises:
            raise IOError("no such sample")
        return buffer if buffer is not None else FakeBuffer(seconds)

    audio.load_buffer = load_buffer
    return audio


class ThePanIsThePlacementAndNothingElseTests(unittest.TestCase):
    def test_left_is_a_relative_source_on_the_listener_s_own_left(self):
        audio = fake_manager(seconds=1.75)
        seconds = audio.play_headphone_test(TEST_SOUNDS["left"], "left")
        self.assertEqual(seconds, 1.75, "the caller gets the sample's own length")
        source = audio.context.created[0]
        # Relative: the position is measured from the listener's own ears, so
        # this works in a menu with no map and never depends on which way the
        # player happens to be facing.
        self.assertTrue(source.relative)
        self.assertEqual(source.position, (-1.0, 0.0, 0.0))
        self.assertTrue(source.spatialize, "the panner must be in the path")
        self.assertFalse(source.direct_channels, "a direct channel skips the pan")
        # One unit away with no rolloff: distance may not change the volume, or
        # a test of the *sides* would be a test of the volume.
        self.assertEqual(source.reference_distance, 1.0)
        self.assertEqual(source.rolloff_factor, 0.0)
        self.assertIsNotNone(source.buffer, "the voice must have its sample")
        self.assertTrue(source.played)

    def test_right_mirrors_it(self):
        audio = fake_manager()
        audio.play_headphone_test(TEST_SOUNDS["right"], "right")
        self.assertEqual(audio.context.created[0].position, (1.0, 0.0, 0.0))

    def test_centre_is_straight_ahead(self):
        """Not a pan at all: one unit along the listener's own forward, which
        OpenAL measures as -z. On headphones that is the phantom centre between
        the ears; on a surround system it is the front-centre channel -- the one
        a left/right pair of samples can never check."""
        audio = fake_manager()
        audio.play_headphone_test(TEST_SOUNDS["centre"], "centre")
        source = audio.context.created[0]
        self.assertEqual(source.position, (0.0, 0.0, -1.0))
        self.assertTrue(source.relative)
        self.assertTrue(source.spatialize)

    def test_an_unknown_placement_plays_nothing(self):
        audio = fake_manager()
        self.assertEqual(audio.play_headphone_test(TEST_SOUNDS["left"], "behind"), 0.0)
        self.assertEqual(audio.context.created, [])

    def test_the_voice_sits_under_the_ui_volume(self):
        audio = fake_manager(ui_volume=60)
        audio.play_headphone_test(TEST_SOUNDS["left"], "left", volume=50)
        self.assertAlmostEqual(audio.context.created[0].gain, 0.3)  # 0.5 * 0.6
        self.assertEqual(audio.unbound_sources[0].cat, "ui")

    def test_one_side_at_a_time(self):
        """Two voices landing together would answer nothing."""
        audio = fake_manager()
        audio.play_headphone_test(TEST_SOUNDS["left"], "left")
        first = audio.context.created[0]
        audio.play_headphone_test(TEST_SOUNDS["right"], "right")
        self.assertTrue(first.stopped, "the side that was sounding must stop")
        self.assertEqual(len(audio.context.created), 2)
        self.assertEqual(len(audio.unbound_sources), 1,
                         "the finished test leaves no source behind")

    def test_stop_is_honest_about_having_nothing_to_stop(self):
        audio = fake_manager()
        self.assertFalse(audio.stop_headphone_test())
        audio.play_headphone_test(TEST_SOUNDS["left"], "left")
        self.assertTrue(audio.stop_headphone_test())
        self.assertTrue(audio.context.created[0].stopped)

    def test_a_muted_game_creates_no_source(self):
        audio = fake_manager(muted=True)
        self.assertEqual(audio.play_headphone_test(TEST_SOUNDS["left"], "left"), 0.0)
        self.assertEqual(audio.play_headphone_test(TEST_SOUNDS["centre"], "centre"), 0.0)
        self.assertEqual(audio.play_headphone_test(TEST_SOUNDS["right"], "right"), 0.0)
        self.assertEqual(audio.context.created, [])

    def test_a_sample_that_cannot_be_decoded_plays_nothing(self):
        audio = fake_manager(raises=True)
        self.assertEqual(audio.play_headphone_test(TEST_SOUNDS["left"], "left"), 0.0)
        self.assertEqual(audio.context.created, [])


class TheSamplesAreWhereTheMenuSaysTests(unittest.TestCase):
    def test_every_sample_is_on_disk_under_those_names(self):
        self.assertEqual(menus.SPEAKER_TEST_SOUNDS, TEST_SOUNDS)
        contents = {}
        for side, relative in menus.SPEAKER_TEST_SOUNDS.items():
            path = CLIENT / "data" / relative
            self.assertTrue(path.is_file(), f"missing speaker test sample: {relative}")
            contents[side] = path.read_bytes()
            self.assertGreater(len(contents[side]), 0, f"empty sample: {relative}")
        self.assertEqual(len(contents), 3, "one sample per placement")
        self.assertEqual(len(set(contents.values())), 3,
                         "one sample cannot stand for two placements")

    def test_the_samples_stay_out_of_the_data_root(self):
        """They live with the other UI sounds, not loose beside the game's own
        top-level sounds (intro.ogg, created.ogg...)."""
        self.assertEqual(len(list((CLIENT / "data").glob("Lumina-tts-*.ogg"))), 0)

    def test_every_sample_is_what_the_game_s_own_decoder_reads(self):
        """They are game sound files, not streams: every sound this game loads
        from disk is Ogg *Vorbis* (an Opus export opens as nothing at all, which
        would be heard as a side that cannot be heard) and a panned side has to
        be mono to be a side."""
        from libs.safe_vorbis import load_vorbis_pcm
        for side, relative in menus.SPEAKER_TEST_SOUNDS.items():
            path = CLIENT / "data" / relative
            # Decoded through the relative path on purpose: libvorbisfile's
            # Windows fopen cannot open an absolute UTF-8 path (this tree lives
            # under a Thai directory name), which is exactly why
            # AudioManager._decode_instrument_sample does the same thing.
            pcm = load_vorbis_pcm(os.path.relpath(str(path)))
            self.assertEqual(pcm.channels, 1, f"the {side} sample must be mono")
            self.assertEqual(pcm.frequency, 48000,
                             f"the {side} sample must be at the engine's own rate")
            self.assertGreater(len(bytes(pcm.buffer)), 0, f"empty sample: {relative}")


if __name__ == "__main__":
    unittest.main()
