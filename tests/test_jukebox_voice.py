"""The cabinet's own voice: the files, the reach, and the packet.

The Server names a line (``power_on`` / ``power_off`` / ``searching``) and sends
it with the cabinet's coordinates; this client plays that line out of that
cabinet. These tests pin the three facts a regression would break first:

* every line really is an Ogg Vorbis file -- the game's decoder is Vorbis only,
  so a WebM/Opus clip renamed to ``.ogg`` is silence with no error anywhere;
* a line is anchored at the cabinet and shaped by the song's own reach, the
  cabinet's category and the reverb zone the cabinet stands in;
* the packet handler defers the OpenAL work to the main thread and falls back
  to the parsed map element when a payload carries no coordinates.
"""

import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from libs.jukebox import voice


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class FakeSource:
    def __init__(self):
        self.rolloff_factor = 1.0


class FakeSound:
    def __init__(self):
        self.source = FakeSource()


class FakeGroup:
    def __init__(self):
        self.position = (0.0, 0.0, 0.0)
        self.sends = {}
        self.plays = []
        self.sounds = []
        self.destroyed = False

    def play(self, path, **options):
        self.plays.append((path, options))
        self.sounds.append(FakeSound())
        return self.sounds[-1]

    def apply_effect(self, slot, sendnum=0):
        self.sends[sendnum] = slot

    def destroy(self):
        self.destroyed = True


class FakeAudio:
    def __init__(self, position=(0.0, 0.0, 0.0)):
        self.position = position
        self.volume_categories = {"jukebox": [100], "miscelaneous": [100]}
        self.groups = []

    def create_soundgroup(self, **kwargs):
        group = FakeGroup()
        self.groups.append(group)
        return group


class FakeMap:
    def __init__(self, reverb=None):
        self.reverb = reverb
        self.looked_up = []

    def get_reverb_at(self, x, y, z):
        self.looked_up.append((x, y, z))
        return None if self.reverb is None else SimpleNamespace(reverb=self.reverb)


class FakeGame:
    def __init__(self, audio=None, world_map=None):
        self.audio_mngr = audio
        self.queued = []
        self.gameplay = SimpleNamespace(map=world_map or FakeMap())

    def put(self, callback):
        self.queued.append(callback)
        callback()


def voices(audio, world_map=None):
    game = FakeGame(audio, world_map)
    return game, voice.voices_for(game)


class VoiceFileTests(unittest.TestCase):
    def test_every_line_names_a_file_that_ships(self):
        for line, path in voice.LINES.items():
            with self.subTest(line=line):
                full = os.path.join(ROOT, "data", *path.split("/"))
                self.assertTrue(os.path.exists(full), full)

    def test_every_line_is_really_ogg_vorbis(self):
        """Not just a file named ``.ogg``: a WebM/Opus clip is silent here."""
        for line, path in voice.LINES.items():
            with self.subTest(line=line):
                full = os.path.join(ROOT, "data", *path.split("/"))
                with open(full, "rb") as handle:
                    head = handle.read(4)
                self.assertEqual(head, b"OggS", f"{line} is not an Ogg stream")
                # EBML magic: a WebM file somebody renamed. The music bot, the
                # browser and every player on the machine will happily play it,
                # which is exactly why nothing else would notice.
                self.assertNotEqual(head, b"\x1aE\xdf\xa3")

    def test_every_line_decodes_at_the_game_s_rate(self):
        try:
            from libs.safe_vorbis import load_vorbis_pcm
        except Exception as error:  # pragma: no cover - pyogg missing
            self.skipTest(f"Vorbis decoder unavailable: {error}")
        # A relative path from the client root, exactly what the game hands
        # the decoder: pyogg opens through the C runtime's ANSI code page, so an
        # absolute path through a Thai (or otherwise non-ASCII) checkout
        # directory cannot be opened by it, while the game's own ``data/...``
        # path is ASCII and works.
        previous = os.getcwd()
        os.chdir(ROOT)
        try:
            for line, path in voice.LINES.items():
                with self.subTest(line=line):
                    # ``LINES`` is relative to ``data/`` (what the audio
                    # manager prepends); the decoder wants the real file.
                    pcm = load_vorbis_pcm(os.path.join("data", *path.split("/")))
                    self.assertEqual(pcm.channels, 2, line)
                    self.assertEqual(pcm.frequency, 44100, line)
                    self.assertGreater(len(bytes(pcm.buffer)), 0, line)
        finally:
            os.chdir(previous)

    def test_a_line_has_no_path_of_its_own(self):
        """The Server sends words; only this file decides where they live."""
        for path in voice.LINES.values():
            self.assertTrue(path.startswith("jukebox_voice/"))
            self.assertTrue(path.endswith(".ogg"))


def box(x, y, width=1.0, depth=1.0, z=0.0):
    """A footprint centred on (x, y), ``width`` tiles across."""
    return (x - width / 2.0, x + width / 2.0, y - depth / 2.0, y + depth / 2.0, z)


class ReachTests(unittest.TestCase):
    def test_reach_is_the_song_s_own(self):
        tile = box(0, 0)
        self.assertEqual(voice.volume_at((0, 0, 0), tile), 100)
        self.assertEqual(voice.volume_at((0, 8, 0), tile), 100)
        self.assertEqual(voice.volume_at((0, 40, 0), tile), 0)
        self.assertEqual(voice.volume_at((0, 400, 0), tile), 0)
        self.assertEqual(voice.volume_at((0, 24, 0), tile), 50)

    def test_an_unknown_listener_position_does_not_silence_the_cabinet(self):
        self.assertEqual(voice.volume_at(None, box(0, 0)), 100)


class FootprintTests(unittest.TestCase):
    def test_a_wide_cabinet_is_heard_from_the_part_near_the_listener(self):
        wide = box(0, 10, width=4.0)      # four tiles across, x -2 .. 2
        self.assertEqual(voice.nearest_point(wide, (-1.9, 20, 0))[0], -1.9)
        self.assertEqual(voice.nearest_point(wide, (9.0, 20, 0))[0], 2.0)
        self.assertEqual(voice.nearest_point(wide, (-9.0, 20, 0))[0], -2.0)

    def test_a_wide_cabinet_s_own_middle_is_still_its_middle(self):
        wide = box(0, 10, width=4.0)
        self.assertEqual(voice.nearest_point(wide, (0.0, 10.0, 0.0)), (0.0, 10.0, 0.0))

    def test_full_volume_reaches_the_whole_footprint(self):
        """Standing at the end of a long cabinet is as close as the face."""
        wide = box(0, 10, width=8.0)      # x -4 .. 4: 9 m to the centre
        self.assertEqual(voice.volume_at((-12, 10, 0), wide), 100)   # 8 m off the end
        self.assertLess(voice.volume_at((-12, 10, 0), box(0, 10)), 100)

    def test_a_listener_standing_on_the_cabinet_hears_its_middle(self):
        wide = box(0, 10, width=4.0)
        self.assertEqual(voice.nearest_point(wide, (1.0, 10.5, 0)), (0.0, 10.0, 0.0))

    def test_a_one_tile_cabinet_is_one_tile_across(self):
        class Zone:
            minx = maxx = 10.0
            miny = maxy = 6.0
            minz = maxz = 2.0

        class Map(FakeMap):
            def get_jukebox(self, jid):
                return Zone()

        game = FakeGame(None, Map())
        rect = voice.footprint_of(game, "jb1", (10.0, 6.0, 2.0))
        self.assertEqual(rect, (9.5, 10.5, 5.5, 6.5, 2.0))

    def test_the_element_is_preferred_to_the_packet_point(self):
        class Zone:
            minx, maxx = 0.0, 3.0
            miny, maxy = 0.0, 0.0
            minz = maxz = 0.0

        class Map(FakeMap):
            def get_jukebox(self, jid):
                return Zone()

        game = FakeGame(None, Map())
        # The Server still sends the tile centre; the placed box is what sounds.
        rect = voice.footprint_of(game, "jb1", (1.5, 0.0, 0.0))
        self.assertEqual(rect, (-0.5, 3.5, -0.5, 0.5, 0.0))

    def test_a_map_that_has_not_parsed_the_element_falls_back_to_the_point(self):
        game = FakeGame(None)
        rect = voice.footprint_of(game, "jb1", (4.0, 5.0, 1.0))
        self.assertEqual(rect, (4.0, 4.0, 5.0, 5.0, 1.0))


class PlaybackTests(unittest.TestCase):
    def test_a_line_comes_out_of_the_cabinet_s_own_position(self):
        audio = FakeAudio(position=(0.0, 0.0, 0.0))
        game, holder = voices(audio)
        self.assertTrue(holder.speak("jb1", (4.0, 0.0, 3.0), "power_on"))

        group = audio.groups[0]
        self.assertEqual(group.position, (4.0, 0.0, 3.0))
        path, options = group.plays[0]
        self.assertEqual(path, "jukebox_voice/power_on.ogg")
        self.assertEqual(options["cat"], "jukebox")
        self.assertEqual(options["volume"], 100)
        self.assertEqual(options["id"], voice.SOURCE_ID)

    def test_a_wide_cabinet_speaks_from_its_near_edge(self):
        """Dragged four tiles wide, the voice follows the part you stand by."""
        audio = FakeAudio(position=(4.0, 0.0, 0.0))

        class Wide(FakeMap):
            def get_jukebox(self, jid):
                return SimpleNamespace(minx=0.0, maxx=3.0, miny=0.0, maxy=0.0,
                                       minz=0.0, maxz=0.0)

        _game, holder = voices(audio, Wide())
        self.assertTrue(holder.speak("jb1", (1.5, 0.0, 0.0), "searching"))
        self.assertEqual(audio.groups[0].position, (3.5, 0.0, 0.0))

    def test_a_line_is_not_attenuated_twice(self):
        """The reach above already shaped it; OpenAL must not re-apply it."""
        audio = FakeAudio()
        _game, holder = voices(audio)
        self.assertTrue(holder.speak("jb1", (0.0, 0.0, 0.0), "power_off"))
        sound = audio.groups[0].sounds[0]
        self.assertEqual(sound.source.rolloff_factor, 0.0)

    def test_a_far_away_cabinet_says_nothing(self):
        audio = FakeAudio(position=(0.0, 0.0, 0.0))
        _game, holder = voices(audio)
        self.assertFalse(holder.speak("jb1", (0.0, 100.0, 0.0), "power_on"))
        self.assertEqual(audio.groups, [])

    def test_an_unknown_line_says_nothing(self):
        audio = FakeAudio()
        _game, holder = voices(audio)
        self.assertFalse(holder.speak("jb1", (0.0, 0.0, 0.0), "theme_song"))
        self.assertEqual(audio.groups, [])

    def test_a_second_line_takes_over_from_the_first(self):
        audio = FakeAudio()
        _game, holder = voices(audio)
        holder.speak("jb1", (0.0, 0.0, 0.0), "power_on")
        holder.speak("jb1", (0.0, 0.0, 0.0), "searching")
        group = audio.groups[0]
        self.assertEqual(len(audio.groups), 1)
        self.assertEqual([options["id"] for _path, options in group.plays],
                         [voice.SOURCE_ID, voice.SOURCE_ID])

    def test_the_voice_carries_the_room_the_cabinet_stands_in(self):
        audio = FakeAudio()
        world_map = FakeMap(reverb="zone-slot")
        _game, holder = voices(audio, world_map)
        holder.speak("jb1", (4.0, 5.0, 6.0), "searching")
        group = audio.groups[0]
        self.assertEqual(group.sends[voice.REVERB_SEND], "zone-slot")
        self.assertEqual(world_map.looked_up, [(4.0, 5.0, 6.0)])

    def test_a_dry_spot_clears_the_last_room_s_reverb(self):
        audio = FakeAudio()
        world_map = FakeMap(reverb="zone-slot")
        _game, holder = voices(audio, world_map)
        holder.speak("jb1", (0.0, 0.0, 0.0), "power_on")
        world_map.reverb = None
        holder.speak("jb1", (0.0, 0.0, 0.0), "power_off")
        group = audio.groups[0]
        self.assertIsNone(group.sends[voice.REVERB_SEND])

    def test_release_hands_every_cabinet_back(self):
        audio = FakeAudio()
        game, holder = voices(audio)
        holder.speak("jb1", (0.0, 0.0, 0.0), "power_on")
        holder.speak("jb2", (1.0, 0.0, 0.0), "power_on")
        voice.release(game)
        self.assertEqual(audio.groups[0].destroyed, True)
        self.assertEqual(audio.groups[1].destroyed, True)
        self.assertEqual(holder.groups, {})


class SwitchTests(unittest.TestCase):
    """The listener's own switch: saved, off means silence, on is the default."""

    def test_the_switch_is_on_until_somebody_says_otherwise(self):
        with mock.patch("libs.options.get", return_value=None):
            self.assertTrue(voice.voice_enabled())

    def test_the_choice_is_saved(self):
        with mock.patch("libs.options.set") as saved:
            self.assertTrue(voice.set_voice_enabled(False))
        saved.assert_called_once_with(voice.OPTION_ENABLED, False)

    def test_off_silences_the_cabinet(self):
        audio = FakeAudio()
        _game, holder = voices(audio)
        with mock.patch("libs.options.get", return_value=False):
            self.assertFalse(holder.speak("jb1", (0.0, 0.0, 0.0), "power_on"))
        self.assertEqual(audio.groups, [])

    def test_a_broken_settings_back_end_does_not_silence_it(self):
        with mock.patch("libs.options.get", side_effect=OSError("no prefs")):
            self.assertTrue(voice.voice_enabled())


class CabinetMenuTests(unittest.TestCase):
    """The line at the end of the cabinet menu, beside the cinema lock."""

    class FakeMenu:
        def __init__(self, *args, **kwargs):
            self.items = []

        def add_items(self, items):
            self.items = list(items)

        def speak_current_item(self):
            pass

    def make_game(self, options_saved=None, with_audio=True):
        box = {"id": "cab1", "x": 0.0, "y": 0.0, "z": 0.0,
               "cinema_mode": "auto", "current": None, "queue": []}
        gp = SimpleNamespace(
            jukebox_state={"jukeboxes": {"cab1": box}},
            player=SimpleNamespace(name="Kanya", x=0.0, y=0.0, z=0.0),
            substates=[],
            pop_last_substate=lambda: None,
        )
        gp.add_substate = gp.substates.append
        game = SimpleNamespace(gameplay=gp, network=None,
                               audio_mngr=FakeAudio() if with_audio else None)
        gp.game = game
        return game

    def open_menu(self, game):
        from libs import jukebox
        with mock.patch("libs.menu.Menu", self.FakeMenu), \
                mock.patch("libs.menus.set_default_sounds"):
            jukebox.open_jukebox_menu(game, game.gameplay)
        return game.gameplay.substates[-1]

    def labels(self, menu):
        return [label() if callable(label) else label for label, _action in menu.items]

    def test_the_line_is_last_before_cancel_and_available_to_a_player(self):
        with mock.patch("libs.options.get", return_value=None):
            labels = self.labels(self.open_menu(self.make_game()))
        self.assertEqual(labels[-1], "Cancel")
        self.assertIn("Cabinet voice: ON (you hear the cabinet speak)", labels[-2:])

    def test_a_saved_off_reads_as_off(self):
        with mock.patch("libs.options.get", return_value=False):
            labels = self.labels(self.open_menu(self.make_game()))
        self.assertIn("Cabinet voice: OFF (cabinets stay silent)", labels)

    def test_pressing_it_turns_the_cabinets_off_and_stops_the_line_playing(self):
        game = self.make_game()
        menu = self.open_menu(game)
        holder = voice.voices_for(game)
        holder.speak("cab1", (0.0, 0.0, 0.0), "power_on")
        group = game.audio_mngr.groups[0]
        with mock.patch("libs.options.get", return_value=True), \
                mock.patch("libs.options.set") as saved, \
                mock.patch("libs.jukebox.menu.speak") as spoken:
            dict((label() if callable(label) else label, action) for label, action in menu.items)[
                "Cabinet voice: ON (you hear the cabinet speak)"]()
        saved.assert_called_once_with(voice.OPTION_ENABLED, False)
        self.assertTrue(group.destroyed)
        spoken.assert_called_once_with("The cabinets stay silent.")

    def test_pressing_it_again_turns_them_back_on(self):
        game = self.make_game()
        with mock.patch("libs.options.get", return_value=False):
            menu = self.open_menu(game)
        with mock.patch("libs.options.get", return_value=False), \
                mock.patch("libs.options.set") as saved, \
                mock.patch("libs.jukebox.menu.speak") as spoken:
            dict((label() if callable(label) else label, action) for label, action in menu.items)[
                "Cabinet voice: OFF (cabinets stay silent)"]()
        saved.assert_called_once_with(voice.OPTION_ENABLED, True)
        spoken.assert_called_once_with("You hear the cabinet speak again.")


class HandlerTests(unittest.TestCase):
    def _handler(self, game):
        from libs.event_handeler import EventHandeler
        handler = EventHandeler.__new__(EventHandeler)
        handler.game = game
        handler.gameplay = game.gameplay
        return handler

    def test_the_packet_plays_at_the_coordinates_it_carries(self):
        audio = FakeAudio()
        game = FakeGame(audio)
        handler = self._handler(game)
        handler.jukebox_voice({"id": "jb1", "line": "power_on", "x": 7, "y": 8, "z": 1})
        group = audio.groups[0]
        self.assertEqual(group.position, (7.0, 8.0, 1.0))
        self.assertEqual(group.plays[0][0], "jukebox_voice/power_on.ogg")

    def test_a_payload_without_coordinates_falls_back_to_the_map_element(self):
        audio = FakeAudio()
        centre = (3.0, 4.0, 5.0)

        class WorldWithCabinet(FakeMap):
            def get_jukebox(self, jid):
                return SimpleNamespace(center=centre)

        game = FakeGame(audio, WorldWithCabinet())
        handler = self._handler(game)
        handler.jukebox_voice({"id": "jb1", "line": "power_off"})
        self.assertEqual(audio.groups[0].position, centre)

    def test_a_map_change_releases_the_cabinets(self):
        audio = FakeAudio()
        game = FakeGame(audio)
        game.gameplay.jukebox_player = None
        holder = voice.voices_for(game)
        holder.speak("jb1", (0.0, 0.0, 0.0), "power_on")
        from libs.event_handeler import EventHandeler
        EventHandeler._stop_jukebox_players_for_map_change(self._handler(game))
        self.assertTrue(audio.groups[0].destroyed)

    def test_a_packet_with_no_cabinet_is_ignored(self):
        audio = FakeAudio()
        game = FakeGame(audio)
        handler = self._handler(game)
        handler.jukebox_voice({"line": "power_on"})
        handler.jukebox_voice(None)
        self.assertEqual(audio.groups, [])


if __name__ == "__main__":
    unittest.main()
