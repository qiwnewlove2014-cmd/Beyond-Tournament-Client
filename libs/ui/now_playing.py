"""The playing song's picture on the game's own screen.

`menu_view` gives the picture a column inside a Music Bot menu.  This module is
the other half: a small panel in the bottom-right corner of the gameplay screen,
so a player watching the world sees the cover or animation without opening a
menu.  It draws the same `music_bot.artwork.CoverArt` frames, which means a
still is still and an animated picture keeps moving here too.  The same frames
feed `draw_expanded`, the persistent Cinematic Screen switch in Music Bot
Settings: `Game.update` draws it behind any menu that is open, so the switch
itself stays reachable while the picture fills the window.

The panel is deliberately quiet: a dark plate, a border, the picture and the
track's name.  It is drawn every frame by `Game.update`, after the spoken-text
lines, and a visual menu drawn afterwards covers it.  It is optional in the
same way every visual thing in this client is: a missing picture or a drawing
error returns False and never reaches the game loop.

Pins: client/tests/test_music_bot_artwork.py (the panel and full-screen geometry,
both draws, and `Game._draw_now_playing` keeping the switch on behind a menu).
"""

import contextlib


HUD_SIZE = 176
HUD_MARGIN = 18
HUD_MIN_WIDTH = 420
HUD_MIN_HEIGHT = 420
CAPTION_HEIGHT = 38

EXPANDED_MARGIN = 18
EXPANDED_CAPTION_HEIGHT = 64
EXPANDED_MIN_WIDTH = 360
EXPANDED_MIN_HEIGHT = 300

_BACKGROUND = (10, 10, 16)
_BORDER = (78, 82, 108)
_ACCENT = (150, 150, 220)
_TEXT = (232, 232, 242)
_DIM = (150, 154, 170)

_FONTS = {}


def panel_rect(width, height):
    """The picture plate at the bottom right, or ``None`` when it is too big.

    Kept as pure numbers so a test can prove the plate stays on screen and
    leaves a reasonable amount of the world visible.
    """
    try:
        width = int(width)
        height = int(height)
    except (TypeError, ValueError):
        return None
    if width < HUD_MIN_WIDTH or height < HUD_MIN_HEIGHT:
        return None
    # Keep the plate below the three spoken lines, which sit around a third
    # of the way down and grow by 50px each.  A quarter of the height leaves
    # that text room even on the 500px-tall default window.
    size = min(HUD_SIZE, max(96, width // 5), max(96, height // 4))
    x = width - size - HUD_MARGIN
    y = height - size - CAPTION_HEIGHT - HUD_MARGIN
    return (x, y, size, size + CAPTION_HEIGHT)


def expanded_box(width, height):
    """The picture's square in a full-window view, or ``None``.

    The square is centred, with room under it for the title and the close
    hint.  Kept as pure numbers so the geometry can be tested without a
    display, exactly like the small HUD plate.
    """
    try:
        width = int(width)
        height = int(height)
    except (TypeError, ValueError):
        return None
    if width < EXPANDED_MIN_WIDTH or height < EXPANDED_MIN_HEIGHT:
        return None
    size = min(width - EXPANDED_MARGIN * 2,
               height - EXPANDED_MARGIN * 2 - EXPANDED_CAPTION_HEIGHT)
    if size < 96:
        return None
    return ((width - size) // 2, EXPANDED_MARGIN, size)


def _font(size=13, bold=False):
    import pygame

    key = (size, bold)
    font = _FONTS.get(key)
    if font is None:
        with contextlib.suppress(Exception):
            font = pygame.font.SysFont("calibri", size, bold=bold)
        if font is None:
            font = pygame.font.Font(None, size + 4)
        _FONTS[key] = font
    return font


def _fit(text, size, color, width, bold=False):
    font = _font(size, bold)
    surface = font.render(str(text), True, color)
    if width is not None and surface.get_width() > width:
        trimmed = str(text)
        while trimmed and surface.get_width() > width:
            trimmed = trimmed[:-1]
            surface = font.render(trimmed + "...", True, color)
    return surface


def draw_expanded(screen, picture):
    """Draw the picture across the game window, with its title.

    This is the picture drawn while Music Bot Settings' Cinematic Screen switch
    is on.  It asks the same :meth:`CoverArt.frame` for every draw, so a GIF or
    APNG keeps moving at this size too.  Returns whether anything was drawn;
    a missing picture or a tiny window never raises into the frame loop.
    """
    if screen is None or picture is None:
        return False
    if getattr(picture, "state", None) not in ("loading", "ready"):
        return False
    try:
        import pygame

        width, height = screen.get_size()
        box = expanded_box(width, height)
        if not box:
            return False
        x, y, size = box
        screen.fill(_BACKGROUND)
        frame = picture.frame(size)
        pygame.draw.rect(screen, _BORDER,
                         pygame.Rect(x, y, size, size), 1, border_radius=8)
        if frame is not None:
            screen.blit(frame, (x, y))
        else:
            loading = _fit("Loading picture...", 14, _DIM, size - 16)
            screen.blit(loading, loading.get_rect(
                center=(x + size // 2, y + size // 2)))
        caption = _fit("Cinematic Screen", 12, _ACCENT, size - 12, bold=True)
        screen.blit(caption, (x + 6, y + size + 6))
        title = _fit(getattr(picture, "title", "") or "", 18, _TEXT,
                     size - 12)
        screen.blit(title, (x + 6, y + size + 22))
        hint = _fit("Turn off in Music Bot Settings", 11, _DIM, size - 12)
        screen.blit(hint, (x + 6, y + size + 46))
        return True
    except Exception:
        return False


def draw(screen, picture, rect=None):
    """Draw ``picture`` on ``screen``; never raises at the game loop.

    ``picture`` is a `music_bot.artwork.CoverArt`: its ``state``, ``title`` and
    ``frame(size)`` are the only three things read.  Returns whether anything
    was drawn.
    """
    if screen is None or picture is None:
        return False
    if getattr(picture, "state", None) not in ("loading", "ready"):
        return False
    if rect is None:
        rect = panel_rect(*screen.get_size())
    if not rect:
        return False
    try:
        import pygame

        x, y, size, panel_height = rect
        pygame.draw.rect(screen, _BACKGROUND,
                         pygame.Rect(x, y, size, panel_height), border_radius=8)
        pygame.draw.rect(screen, _BORDER,
                         pygame.Rect(x, y, size, panel_height), 1, border_radius=8)
        frame = picture.frame(size)
        if frame is not None:
            screen.blit(frame, (x, y))
        else:
            loading = _fit("Loading picture...", 12, _DIM, size - 12)
            screen.blit(loading, loading.get_rect(
                center=(x + size // 2, y + size // 2)))
        caption = _fit("Now playing", 10, _ACCENT, size - 12, bold=True)
        screen.blit(caption, (x + 6, y + size + 3))
        title = _fit(getattr(picture, "title", "") or "", 12, _TEXT,
                     size - 12)
        screen.blit(title, (x + 6, y + size + 18))
        return True
    except Exception:
        return False
