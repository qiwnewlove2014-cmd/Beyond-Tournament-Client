"""The Music Bot's now-playing picture: still and animated.

These tests keep the picture's two promises apart.  The source half says where
a picture comes from and how large it may be; the display half says a still is
one frame and an animation is a sequence the menu, HUD or expanded view can
ask for at any moment.
No network, audio device, OpenAL or game is used.
"""

import contextlib
import os
import sys
import tempfile
import types
import unittest
from unittest import mock

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")

import pygame

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from libs import youtube_resolver  # noqa: E402
from libs.music_bot import artwork  # noqa: E402
from libs.music_bot import gate  # noqa: E402
from libs.ui import menu_view  # noqa: E402
from libs.ui import now_playing  # noqa: E402


def setUpModule():
    pygame.font.init()


def tearDownModule():
    pygame.font.quit()


def png_bytes(color=(255, 0, 0)):
    """A real two-pixel PNG, written by pygame so no fixture can rot."""
    surface = pygame.Surface((2, 2))
    surface.fill(color)
    handle = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    handle.close()
    try:
        pygame.image.save(surface, handle.name)
        with open(handle.name, "rb") as image:
            return image.read()
    finally:
        with contextlib.suppress(OSError):
            os.remove(handle.name)


class RunNow:
    """The loader's worker, run on start so a test needs no timing."""

    def __init__(self, target=None, daemon=None, name=None, **kwargs):
        self._target = target

    def start(self):
        if self._target is not None:
            self._target()


class SourceTests(unittest.TestCase):
    def test_a_youtube_page_names_its_still_thumbnail(self):
        for page in (
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=30",
            "https://youtube.com/watch?v=dQw4w9WgXcQ",
            "https://youtu.be/dQw4w9WgXcQ",
            "https://www.youtube.com/shorts/dQw4w9WgXcQ",
        ):
            with self.subTest(page=page):
                self.assertEqual(
                    artwork.youtube_thumbnail(page),
                    "https://i.ytimg.com/vi/dQw4w9WgXcQ/mqdefault.jpg")

    def test_another_host_is_not_guessed_at(self):
        for page in ("https://example.com/watch?v=dQw4w9WgXcQ", "",
                     "https://youtube.com/watch?v=short", None):
            with self.subTest(page=page):
                self.assertEqual(artwork.youtube_thumbnail(page), "")

    def test_a_local_song_finds_the_picture_beside_it(self):
        with tempfile.TemporaryDirectory() as folder:
            song = os.path.join(folder, "Song.mp3")
            with open(song, "wb") as handle:
                handle.write(b"not really audio")
            self.assertEqual(artwork.sidecar_picture(song), "")
            gif = os.path.join(folder, "Song.gif")
            with open(gif, "wb") as handle:
                handle.write(b"GIF89a")
            self.assertEqual(artwork.sidecar_picture(song), gif)
            # A still beside a moving one is the fallback, never the winner.
            jpg = os.path.join(folder, "Song.jpg")
            with open(jpg, "wb") as handle:
                handle.write(b"jpeg")
            self.assertEqual(artwork.sidecar_picture(song), gif)

    def test_the_animated_containers_are_recognised(self):
        self.assertTrue(artwork._is_animated(b"GIF89a" + b"\x00" * 20))
        self.assertFalse(artwork._is_animated(
            b"\x89PNG\r\n\x1a\n" + b"\x00" * 20))
        apng = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8 + b"acTL" + b"\x00" * 8
        self.assertTrue(artwork._is_animated(apng))
        webp = (b"RIFF" + b"\x00" * 4 + b"WEBP" + b"VP8X"
                + b"\x00" * 4 + bytes([2]) + b"\x00" * 10)
        self.assertTrue(artwork._is_animated(webp))
        still_webp = bytearray(webp)
        still_webp[20] = 0
        self.assertFalse(artwork._is_animated(bytes(still_webp)))
        self.assertTrue(artwork._is_webp(bytes(still_webp)))

    def test_a_still_webp_is_converted_instead_of_handed_to_pygame(self):
        webp = b"RIFF" + b"\x00" * 4 + b"WEBP" + b"VP8X" + b"\x00" * 12
        with mock.patch.object(artwork, "_bounded_read", return_value=webp), \
                mock.patch.object(artwork, "_frame_paths",
                                  return_value=[b"png"]) as paths:
            frames = artwork._frames_from_file("song.webp")
        self.assertEqual(frames, [b"png"])
        paths.assert_called_once_with("song.webp", animated=False)

    def test_a_still_png_is_handed_straight_to_pygame(self):
        png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 20
        with mock.patch.object(artwork, "_bounded_read", return_value=png), \
                mock.patch.object(artwork, "_frame_paths") as paths:
            frames = artwork._frames_from_file("song.png")
        self.assertEqual(frames, [png])
        paths.assert_not_called()

    def test_an_animated_file_asks_for_its_frames(self):
        gif = b"GIF89a" + b"\x00" * 20
        with mock.patch.object(artwork, "_bounded_read", return_value=gif), \
                mock.patch.object(artwork, "_frame_paths",
                                  return_value=[b"one", b"two"]) as paths:
            frames = artwork._frames_from_file("song.gif")
        self.assertEqual(frames, [b"one", b"two"])
        paths.assert_called_once_with("song.gif")

    def test_the_resolver_carries_a_thumbnail_only_when_it_is_a_real_url(self):
        thumb = "https://i.ytimg.com/vi/dQw4w9WgXcQ/mqdefault.jpg"
        result = youtube_resolver._validated_info({
            "url": "https://example.invalid/audio",
            "http_headers": {},
            "thumbnail": thumb,
        })
        self.assertEqual(result["thumbnail"], thumb)
        for bad in ("file:///private.png", "not a url", 3, ""):
            with self.subTest(thumbnail=bad):
                self.assertNotIn("thumbnail", youtube_resolver._validated_info({
                    "url": "https://example.invalid/audio",
                    "http_headers": {},
                    "thumbnail": bad,
                }))


class CoverArtTests(unittest.TestCase):
    def setUp(self):
        gate.clear()
        self.addCleanup(gate.clear)
        self.game = types.SimpleNamespace(put=lambda callback: callback())

    def load(self, frames, enabled=True):
        art = artwork.CoverArt(self.game, enabled=enabled)
        with mock.patch.object(artwork.threading, "Thread", RunNow), \
                mock.patch.object(artwork, "_materialize", return_value="picture"), \
                mock.patch.object(artwork, "_frames_from_file", return_value=frames):
            art.set_track("Song", page_url="https://youtu.be/dQw4w9WgXcQ")
        return art

    def test_a_still_picture_becomes_one_ready_frame(self):
        art = self.load([png_bytes()])
        self.assertEqual(art.state, "ready")
        frame = art.frame(64)
        self.assertIsNotNone(frame)
        self.assertEqual(frame.get_size(), (64, 64))
        self.assertEqual(art.title, "Song")

    def test_an_animated_picture_keeps_every_frame(self):
        art = self.load([png_bytes((255, 0, 0)), png_bytes((0, 255, 0))])
        self.assertEqual(art.state, "ready")
        self.assertEqual(len(art._frames), 2)
        self.assertIsNotNone(art.frame(64))

    def test_an_animated_picture_advances_with_the_clock(self):
        art = self.load([png_bytes((255, 0, 0)), png_bytes((0, 255, 0))])
        with mock.patch.object(artwork.time, "monotonic",
                               return_value=art._started):
            first = art.frame(32)
        with mock.patch.object(
                artwork.time, "monotonic",
                return_value=art._started + 1.0 / artwork.FRAME_RATE):
            second = art.frame(32)
        self.assertEqual(tuple(first.get_at((0, 0)))[:3], (255, 0, 0))
        self.assertEqual(tuple(second.get_at((0, 0)))[:3], (0, 255, 0))

    def test_a_picture_that_cannot_be_decoded_is_missing_not_an_error(self):
        art = self.load([b"not an image"])
        self.assertEqual(art.state, "missing")
        self.assertIsNone(art.frame(64))

    def test_the_switch_off_stops_the_picture_and_on_loads_it_again(self):
        art = self.load([png_bytes()])
        art.set_enabled(False)
        self.assertEqual(art.state, "idle")
        self.assertIsNone(art.frame(64))
        with mock.patch.object(artwork.threading, "Thread", RunNow), \
                mock.patch.object(artwork, "_materialize", return_value="picture"), \
                mock.patch.object(artwork, "_frames_from_file", return_value=[png_bytes()]):
            art.set_enabled(True)
        self.assertEqual(art.state, "ready")

    def test_a_track_with_no_picture_is_missing(self):
        art = artwork.CoverArt(self.game, enabled=True)
        art.set_track("Local song", local_path=os.path.join(
            tempfile.gettempdir(), "bt-no-such-song-xyz.mp3"))
        self.assertEqual(art.state, "missing")

    def test_clearing_a_track_releases_its_frames(self):
        art = self.load([png_bytes()])
        art.clear()
        self.assertEqual(art.state, "idle")
        self.assertEqual(art._frames, [])
        self.assertIsNone(art.frame(64))

    def test_a_locked_picture_is_not_active(self):
        from libs.music_bot.controller import MapMusicBot

        bot = MapMusicBot.__new__(MapMusicBot)
        bot.song_picture_enabled = True
        gate.apply_locked({"song_picture": "Song picture"})
        self.assertFalse(bot.song_picture_active)

    def test_the_switch_persists_and_tells_the_loader(self):
        from libs.music_bot.controller import MapMusicBot

        bot = MapMusicBot.__new__(MapMusicBot)
        bot.song_picture_enabled = True
        bot.current_target = ""
        bot.current_source = ""
        bot.current_title = ""
        bot.cover_art = mock.Mock()
        with mock.patch("libs.music_bot.controller.options.set") as save, \
                mock.patch("libs.music_bot.controller.speak"):
            self.assertFalse(bot.toggle_song_picture())
        save.assert_called_once_with(artwork.PICTURE_OPTION, False)
        bot.cover_art.set_enabled.assert_called_once_with(False)
        with mock.patch("libs.music_bot.controller.options.set"), \
                mock.patch("libs.music_bot.controller.speak"):
            self.assertTrue(bot.toggle_song_picture())
        bot.cover_art.set_enabled.assert_called_with(True)


class FakePicture:
    state = "ready"
    title = "A Song That Is Playing"

    def frame(self, size):
        surface = pygame.Surface((size, size), pygame.SRCALPHA)
        surface.fill((40, 80, 140))
        return surface


class FakeBot:
    playing = True
    song_picture_active = True
    cover_art = FakePicture()


class FakeMusicMenu:
    def __init__(self):
        self.title = "Music Bot"
        self.items = [("Search YouTube", lambda: None, None,
                       "Search for a song.")]
        self.pos = 0
        self.game = types.SimpleNamespace(
            screen=pygame.Surface((900, 500)),
            gameplay=types.SimpleNamespace(music_bot=FakeBot()),
        )


class MenuArtworkTests(unittest.TestCase):
    Menu = FakeMusicMenu

    def test_a_music_menu_gives_the_picture_a_column(self):
        menu = self.Menu()
        view = menu_view.MenuView()
        with mock.patch.object(menu_view, "enabled", return_value=True):
            art = view.artwork_for(menu, (900, 500))
            layout = view.layout_for(menu, (900, 500))
        self.assertIsNotNone(art)
        self.assertEqual(art[1], 200)
        self.assertEqual(layout.width, 900 - 200 - menu_view.ARTWORK_GAP)

    def test_a_non_music_menu_keeps_its_full_width(self):
        menu = self.Menu()
        menu.title = "Options"
        view = menu_view.MenuView()
        with mock.patch.object(menu_view, "enabled", return_value=True):
            self.assertIsNone(view.artwork_for(menu, (900, 500)))
            self.assertEqual(view.layout_for(menu, (900, 500)).width, 900)

    def test_a_small_window_keeps_the_lines_alone(self):
        self.assertIsNone(menu_view.artwork_box(500, 400))
        self.assertIsNotNone(menu_view.artwork_box(900, 500))

    def test_drawing_a_music_menu_with_a_picture_succeeds(self):
        menu = self.Menu()
        view = menu_view.MenuView()
        with mock.patch.object(menu_view, "enabled", return_value=True):
            self.assertTrue(view.draw(menu))
        self.assertEqual(view.draws, 1)


class NowPlayingHudTests(unittest.TestCase):
    def test_the_plate_stays_inside_a_small_screen(self):
        self.assertIsNone(now_playing.panel_rect(300, 200))
        self.assertIsNone(now_playing.panel_rect(420, 300))
        rect = now_playing.panel_rect(420, 420)
        self.assertIsNotNone(rect)
        x, y, width, height = rect
        self.assertGreaterEqual(x, 0)
        self.assertGreaterEqual(y, 0)
        self.assertLessEqual(x + width, 420)
        self.assertLessEqual(y + height, 420)

    def test_the_hud_draws_the_ready_picture_and_its_name(self):
        screen = pygame.Surface((900, 500))
        rect = now_playing.panel_rect(900, 500)
        picture = FakePicture()
        self.assertTrue(now_playing.draw(screen, picture, rect))
        x, y, size, _ = rect
        # The fake frame is an opaque blue square filling the plate.
        self.assertEqual(tuple(screen.get_at((x + 10, y + 10)))[:3],
                         (40, 80, 140))

    def test_a_missing_picture_draws_nothing(self):
        picture = FakePicture()
        picture.state = "missing"
        self.assertFalse(now_playing.draw(
            pygame.Surface((900, 500)), picture,
            now_playing.panel_rect(900, 500)))

    def test_the_expanded_box_uses_most_of_the_window(self):
        box = now_playing.expanded_box(900, 500)
        self.assertIsNotNone(box)
        x, y, size = box
        self.assertGreater(size, now_playing.HUD_SIZE)
        self.assertGreaterEqual(x, 0)
        self.assertGreaterEqual(y, 0)
        self.assertLessEqual(x + size, 900)
        self.assertLessEqual(y + size + now_playing.EXPANDED_CAPTION_HEIGHT, 500)
        self.assertIsNone(now_playing.expanded_box(300, 200))

    def test_the_expanded_view_draws_the_picture(self):
        screen = pygame.Surface((900, 500))
        self.assertTrue(now_playing.draw_expanded(screen, FakePicture()))
        x, y, size = now_playing.expanded_box(900, 500)
        self.assertEqual(tuple(screen.get_at((x + 10, y + 10)))[:3],
                         (40, 80, 140))


class CinematicScreenSwitchTests(unittest.TestCase):
    def setUp(self):
        gate.clear()
        self.addCleanup(gate.clear)
        from libs.music_bot.controller import MapMusicBot
        self.bot = MapMusicBot.__new__(MapMusicBot)
        self.bot.playing = True
        self.bot.song_picture_enabled = True
        self.bot.picture_fullscreen = False

    def test_the_switch_stays_on_and_a_second_press_turns_it_off(self):
        with mock.patch("libs.music_bot.controller.speak") as speech:
            self.assertTrue(self.bot.toggle_cinematic_screen())
            self.assertTrue(self.bot.picture_fullscreen)
            self.assertEqual(speech.call_count, 1)
            self.assertIn("Cinematic Screen on", speech.call_args.args[0])
            self.assertFalse(self.bot.toggle_cinematic_screen())
            self.assertFalse(self.bot.picture_fullscreen)
            self.assertEqual(speech.call_count, 2)
            self.assertIn("Cinematic Screen off", speech.call_args.args[0])

    def test_it_says_when_the_picture_will_fill_if_no_song_is_playing(self):
        self.bot.playing = False
        with mock.patch("libs.music_bot.controller.speak") as speech:
            self.assertTrue(self.bot.toggle_cinematic_screen())
        self.assertIn("when a song plays", speech.call_args.args[0])

    def test_song_picture_off_refuses_and_says_why(self):
        self.bot.song_picture_enabled = False
        with mock.patch("libs.music_bot.controller.speak") as speech:
            self.assertIsNone(self.bot.toggle_cinematic_screen())
        self.assertFalse(self.bot.picture_fullscreen)
        self.assertIn("Song Picture is off", speech.call_args.args[0])

    def test_a_staff_lock_refuses_and_says_why(self):
        gate.apply_locked({"song_picture": "Song picture"})
        with mock.patch("libs.music_bot.controller.speak") as speech:
            self.assertIsNone(self.bot.toggle_cinematic_screen())
        self.assertFalse(self.bot.picture_fullscreen)
        self.assertIn("locked", speech.call_args.args[0])

    def test_song_picture_off_takes_the_cinematic_screen_with_it(self):
        self.bot.picture_fullscreen = True
        self.bot.cover_art = types.SimpleNamespace(set_enabled=lambda on: None)
        with mock.patch("libs.music_bot.controller.options.set"), \
                mock.patch("libs.music_bot.controller.speak"):
            self.bot.toggle_song_picture()
        self.assertFalse(self.bot.picture_fullscreen)


class PersistentFullscreenDrawTests(unittest.TestCase):
    def test_the_picture_is_drawn_even_while_a_menu_is_open(self):
        from libs.game import Game

        game = Game.__new__(Game)
        game.screen = pygame.Surface((900, 500))
        bot = types.SimpleNamespace(
            playing=True, song_picture_active=True, picture_fullscreen=True,
            cover_art=FakePicture())
        gameplay = types.SimpleNamespace(
            substates=[object()], music_bot=bot)
        game.gameplay = gameplay
        game.stack = [gameplay]
        self.assertTrue(game._draw_now_playing())
        x, y, _ = now_playing.expanded_box(900, 500)
        self.assertEqual(tuple(game.screen.get_at((x + 10, y + 10)))[:3],
                         (40, 80, 140))


if __name__ == "__main__":
    unittest.main()
