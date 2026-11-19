"""Prove the Cinematic Screen is a real, moving picture -- headless.

This is the test a player can run when the screen is not something they can
look at.  It builds a real animated GIF beside a dummy local song, lets the
same `music_bot.artwork.CoverArt` the game uses decode it, then samples the
actual clock through the same `frame(size)` call the full-screen view uses.

It checks three separate promises:

1. A sidecar GIF beside a local song is found, decoded into more than one
   frame, and reaches ``state == "ready"``.  A still picture would be one
   frame; this one has to move.
2. Two samples taken at different real times return different colours.
3. `Game._draw_now_playing` draws the full-window picture while
   `MapMusicBot.picture_fullscreen` is on, and the drawn pixels change too.
   Turning the switch off clears the state; nothing here closes a modal with
   Escape, because there is no modal -- the switch is the setting.

The run is silent and uses the dummy SDL drivers.  It writes only to a
temporary folder and uses the bundled ffmpeg by absolute path.

Run from ``client/``:

    python tools/cinematic_picture_sim.py

Exit codes: 0 the picture is real and moving; 1 a check failed; 2 the
simulator could not start (ffmpeg/pygame missing).
"""

import contextlib
import os
import subprocess
import sys
import tempfile
import time
import types
import warnings
from unittest import mock

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

warnings.filterwarnings("ignore", message="pkg_resources is deprecated.*")

import pygame  # noqa: E402
pygame.init()

from libs import game as game_module  # noqa: E402
from libs.music_bot import artwork  # noqa: E402
from libs.music_bot import gate  # noqa: E402
from libs.music_bot.controller import MapMusicBot  # noqa: E402
from libs.music_bot.media import FFMPEG_PATH  # noqa: E402
from libs.ui import now_playing  # noqa: E402


COLORS = (
    (255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0),
    (0, 255, 255), (255, 0, 255), (255, 128, 0), (128, 0, 255),
)
SAMPLE_SECONDS = 1.4
SAMPLE_INTERVAL = 0.03
WINDOW = (900, 500)
FRAME_SIZE = 240


class SimGame:
    """A game shell whose `put` queues the loader's main-thread callback."""

    def __init__(self):
        self.callbacks = []

    def put(self, callback):
        self.callbacks.append(callback)

    def pump(self):
        while self.callbacks:
            self.callbacks.pop(0)()


def make_gif(folder):
    """Write eight coloured PNGs and encode them into a looping GIF."""
    for index, color in enumerate(COLORS, start=1):
        surface = pygame.Surface((64, 64))
        surface.fill(color)
        pygame.image.save(surface, os.path.join(folder, f"source-{index:02d}.png"))
    target = os.path.join(folder, "Song.gif")
    command = [
        str(FFMPEG_PATH), "-v", "error", "-y",
        "-framerate", "8",
        "-i", os.path.join(folder, "source-%02d.png"),
        "-loop", "0",
        "-vf", "scale=64:64:flags=neighbor",
        target,
    ]
    result = subprocess.run(
        command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        timeout=30, check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode != 0 or not os.path.isfile(target):
        raise RuntimeError(
            "ffmpeg could not build the test GIF: "
            + result.stderr.decode("utf-8", "replace").strip())
    return target


def load_picture(folder):
    """Load the sidecar through the real CoverArt, with a real clock."""
    song = os.path.join(folder, "Song.mp3")
    with open(song, "wb") as handle:
        handle.write(b"headless simulation; no audio is decoded")
    game = SimGame()
    picture = artwork.CoverArt(game, enabled=True)
    picture.set_track("Simulation Song", local_path=song)
    deadline = time.monotonic() + artwork.REQUEST_TIMEOUT + artwork.DECODE_TIMEOUT
    while time.monotonic() < deadline:
        game.pump()
        if picture.state in ("ready", "missing"):
            break
        time.sleep(0.01)
    return picture


def sample_picture(picture):
    """Sample the real `frame(size)` clock and return its colour changes."""
    changes = []
    last = None
    started = time.monotonic()
    while time.monotonic() - started < SAMPLE_SECONDS:
        frame = picture.frame(FRAME_SIZE)
        if frame is not None:
            color = tuple(frame.get_at((FRAME_SIZE // 2, FRAME_SIZE // 2)))[:3]
            if color != last:
                changes.append((round(time.monotonic() - started, 3), color))
                last = color
        time.sleep(SAMPLE_INTERVAL)
    return changes


def make_switch_screen(picture):
    """A real MapMusicBot switch and the real Game full-screen draw."""
    bot = MapMusicBot.__new__(MapMusicBot)
    bot.playing = True
    bot.song_picture_enabled = True
    bot.picture_fullscreen = False
    bot.cover_art = picture
    with mock.patch("libs.music_bot.controller.speak"):
        turned_on = bot.toggle_cinematic_screen()
    if not turned_on:
        raise RuntimeError("the Cinematic Screen switch did not turn on")

    game = game_module.Game.__new__(game_module.Game)
    game.screen = pygame.Surface(WINDOW)
    # A menu is open here: the full-screen picture must still be drawn first.
    gameplay = types.SimpleNamespace(substates=[object()], music_bot=bot)
    game.gameplay = gameplay
    game.stack = [gameplay]
    return bot, game


def sample_screen(game):
    """Sample what `_draw_now_playing` puts on the screen, over real time."""
    changes = []
    last = None
    started = time.monotonic()
    x, y, size = now_playing.expanded_box(*WINDOW)
    point = (x + size // 2, y + size // 2)
    while time.monotonic() - started < SAMPLE_SECONDS:
        game._draw_now_playing()
        color = tuple(game.screen.get_at(point))[:3]
        if color != last:
            changes.append((round(time.monotonic() - started, 3), color))
            last = color
        time.sleep(SAMPLE_INTERVAL)
    return changes


def main():
    if not FFMPEG_PATH or not os.path.isfile(str(FFMPEG_PATH)):
        print("SKIP: bundled ffmpeg is not available, so no real GIF can be decoded.")
        return 2
    try:
        pygame.display.init()
        pygame.display.set_mode((1, 1))
        pygame.font.init()
        gate.clear()
        with tempfile.TemporaryDirectory(prefix="bt-cinematic-sim-") as folder:
            make_gif(folder)
            picture = load_picture(folder)

            print("Cinematic Screen - headless real-time simulation")
            print(f"  window: {WINDOW[0]}x{WINDOW[1]}, picture: {FRAME_SIZE}x{FRAME_SIZE}")
            print(f"  state: {picture.state}, decoded frames: {len(picture._frames)}")
            print("  note: a YouTube thumbnail is one still frame; only a GIF/APNG sidecar moves.")
            if picture.state != "ready":
                print("  RESULT: FAIL - the sidecar GIF never became a ready picture.")
                return 1
            if len(picture._frames) < 2:
                print("  RESULT: FAIL - the animated GIF decoded to one frame.")
                return 1

            changes = sample_picture(picture)
            colors = {color for _, color in changes}
            print(f"  picture changes over {SAMPLE_SECONDS:.1f}s: {len(changes)}, "
                  f"colours: {len(colors)}")
            for elapsed, color in changes:
                print(f"    t={elapsed:.3f}s  RGB={color}")
            if len(changes) < 2 or len(colors) < 2:
                print("  RESULT: FAIL - the picture did not change over time.")
                return 1

            bot, game = make_switch_screen(picture)
            if not game._draw_now_playing():
                print("  RESULT: FAIL - the full-screen draw refused a ready picture.")
                return 1
            screen_changes = sample_screen(game)
            screen_colors = {color for _, color in screen_changes}
            print(f"  Cinematic Screen switch: {bot.picture_fullscreen}; "
                  f"menu open; screen changes: {len(screen_changes)}, "
                  f"colours: {len(screen_colors)}")
            for elapsed, color in screen_changes:
                print(f"    t={elapsed:.3f}s  screen RGB={color}")
            if len(screen_changes) < 2 or len(screen_colors) < 2:
                print("  RESULT: FAIL - the full-screen pixels did not change.")
                return 1

            with mock.patch("libs.music_bot.controller.speak"):
                bot.toggle_cinematic_screen()
            if bot.picture_fullscreen:
                print("  RESULT: FAIL - the switch did not turn off.")
                return 1

        print("RESULT: PASS - the picture exists and is moving in real time.")
        return 0
    except Exception as error:
        print(f"RESULT: FAIL - {type(error).__name__}: {error}")
        return 1
    finally:
        with contextlib.suppress(Exception):
            pygame.quit()


if __name__ == "__main__":
    raise SystemExit(main())
