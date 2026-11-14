"""A picture of whatever menu the game is showing, for a player who can see.

Every menu in this client is the same object: `libs/menu.py::Menu`, a list of
``(label, action, preview_sound[, help])`` tuples with one focused line
(``pos``) and one action per line. That is the whole reason this can be a single
file: a renderer that reads that state covers every menu the game builds --
Options, the music bot's pages, the jukebox, the Party Sync pages, an
instrument's board, the Server-driven builder lists -- with no per-menu code and
no second description of what a menu is.

Three rules, and they are the reason this is safe to add to a game made for
blind players:

* **It never changes what a menu does.** A click becomes the same call a key
  makes: :meth:`Menu.select_current_item`, :meth:`Menu.move_up` /
  ``move_down``, a line's own ``arrow`` callback, :meth:`Menu.speak_current_help`.
  Hovering moves the focus *silently*, so pointing at a line cannot talk over
  somebody's screen reader.
* **It never speaks.** ``speech.speak`` is not imported here. The spoken client
  is untouched, line for line.
* **It is optional and it disappears quietly.** ``options["visual_menus"]``
  turns it off, a game object without a screen (a test, a headless run) makes
  every call a no-op, and a renderer error is logged once and then shelves
  itself rather than taking the menu down with it.

The layout is computed by :func:`build_layout` and :class:`Layout`, which are
pure numbers: hit-testing is testable without a display, and the tests in
``client/tests/test_visual_menu_view.py`` drive it that way (a fake menu, a
900x500 surface, real click coordinates).

Purely visual for now: this draws a menu, it does not draw the world.
``libs/world_map.py`` already knows every tile and every body's position, which
is what a map view would read later.
"""

import contextlib

from .. import options

# Geometry, all in pixels. A row is one menu line and is deliberately tall
# enough to hit with a mouse without precision (a sighted player is not aiming
# at a text baseline), while the number of rows still fits the 900x500 window
# the client opens.
PAD = 16
TITLE_HEIGHT = 46
ROW_HEIGHT = 34
ROW_GAP = 3
HELP_HEIGHT = 58
ARROW_WIDTH = 38
SCROLLBAR_WIDTH = 8
MIN_WIDTH = 240
MIN_HEIGHT = 160

# Colours. High contrast on purpose (the point is a player who can see at all,
# on whatever monitor): near-black paper, light text, and a focus bar that
# differs from the row colour by lightness, not only by hue.
COLOR_BACKGROUND = (12, 12, 16)
COLOR_ROW = (28, 28, 36)
COLOR_ROW_FOCUS = (58, 92, 150)
COLOR_BORDER = (74, 74, 90)
COLOR_TEXT = (232, 232, 240)
COLOR_TEXT_FOCUS = (255, 255, 255)
COLOR_TEXT_DIM = (138, 146, 160)
COLOR_HELP = (176, 184, 198)
COLOR_SCROLL = (96, 96, 116)
COLOR_HOVER = (120, 132, 152)

TEXT_SIZE = 20
TITLE_SIZE = 25
HELP_SIZE = 16

OPTION_KEY = "visual_menus"
DEFAULT_ENABLED = True

# Text surfaces are cached because the same labels are redrawn every frame; the
# cap keeps a long session in a big menu from growing without bound.
_TEXT_CACHE_LIMIT = 600


def enabled():
    """Whether a player asked to see menus. On unless they turned it off."""
    with contextlib.suppress(Exception):
        return bool(options.get(OPTION_KEY, DEFAULT_ENABLED))
    return DEFAULT_ENABLED


def label_of(item):
    """A menu line's text. A line may compute its label lazily (a callable)."""
    text = item[0] if item else ""
    if callable(text):
        try:
            text = text()
        except Exception:
            text = ""
    return str(text or "")


def has_help(item):
    """Whether Tab has anything to say about this line."""
    return len(item) > 3 and bool(item[3])


def help_of(item):
    return str(item[3] or "") if len(item) > 3 else ""


def has_arrow(item):
    """Whether this line is a control that answers Left/Right (a scrub, a gain)."""
    return callable(getattr(item[1] if len(item) > 1 else None, "arrow", None))


def has_preview(item):
    """Whether focusing this line plays a sound sample."""
    return len(item) > 2 and bool(item[2])


class Layout:
    """Where every visible line sits, for one menu of ``count`` lines.

    Pure numbers on purpose: the rectangle of each row, the title, the help
    panel, the scroll offset that keeps ``focus`` on screen. Hit-testing is
    :meth:`hit`.
    """

    __slots__ = ("count", "focus", "scroll", "width", "height", "rows",
                 "title_rect", "help_rect", "max_rows", "scrollable", "arrows")

    def __init__(self, count, focus, scroll, width, height, want_help=False,
                 arrows=()):
        self.arrows = frozenset(arrows or ())
        self.count = max(0, int(count))
        # Not clamped to a comfortable size: a window smaller than a row still
        # has to produce a layout (one line, clipped by the surface), because
        # the alternative is a menu that draws nothing at all.
        self.width = max(1, int(width))
        self.height = max(1, int(height))
        self.focus = int(focus) if focus is not None else -1
        self.title_rect = (0, 0, self.width, TITLE_HEIGHT)
        help_h = HELP_HEIGHT if want_help else 0
        self.help_rect = (
            (0, self.height - help_h, self.width, help_h) if want_help else None
        )

        rows_top = TITLE_HEIGHT + PAD
        rows_bottom = self.height - help_h - PAD
        usable = max(ROW_HEIGHT, rows_bottom - rows_top)
        # Rows that fit, and never fewer than one: a window smaller than a row
        # still has to draw the focused line rather than an empty screen.
        self.max_rows = max(1, usable // (ROW_HEIGHT + ROW_GAP))
        self.scrollable = self.count > self.max_rows
        self.scroll = self._clamped_scroll(scroll, self.max_rows)

        first = self.scroll
        last = min(self.count, first + self.max_rows)
        self.rows = []
        row_width = self.width - PAD * 2 - (SCROLLBAR_WIDTH if self.scrollable else 0)
        y = rows_top
        for index in range(first, last):
            self.rows.append((index, (PAD, y, row_width, ROW_HEIGHT)))
            y += ROW_HEIGHT + ROW_GAP

    def _clamped_scroll(self, scroll, max_rows):
        try:
            scroll = int(scroll or 0)
        except (TypeError, ValueError):
            scroll = 0
        highest = max(0, self.count - max_rows)
        scroll = max(0, min(scroll, highest))
        # Keep the focused line on screen by moving the window, never by moving
        # the focus: the focus is the game's, not the picture's.
        if self.focus >= 0:
            if self.focus < scroll:
                scroll = self.focus
            elif self.focus >= scroll + max_rows:
                scroll = self.focus - max_rows + 1
        return max(0, min(scroll, highest))

    def row_of(self, index):
        for row_index, rect in self.rows:
            if row_index == index:
                return rect
        return None

    def hit(self, x, y):
        """What is under this point.

        Returns ``("item", index)``, ``("arrow", index, +1|-1)`` for a control's
        own left/right zones, ``("help", None)``, ``("title", None)``, or
        ``(None, None)`` for empty space.
        """
        for index, (rx, ry, rw, rh) in self.rows:
            if rx <= x < rx + rw and ry <= y < ry + rh:
                if index in self.arrows:
                    if x >= rx + rw - ARROW_WIDTH:
                        return ("arrow", index, 1)
                    if x < rx + ARROW_WIDTH:
                        return ("arrow", index, -1)
                return ("item", index)
        if self.help_rect:
            hx, hy, hw, hh = self.help_rect
            if hx <= x < hx + hw and hy <= y < hy + hh:
                return ("help", None)
        if 0 <= y < TITLE_HEIGHT:
            return ("title", None)
        return (None, None)

def build_layout(count, focus, scroll, width, height, want_help=False, arrows=()):
    """The layout for a menu, with the rows that own a Left/Right control."""
    return Layout(count, focus, scroll, width, height, want_help, arrows)


def wrap_text(text, width_chars):
    """Split a description into lines no wider than ``width_chars``.

    A crude wrap on purpose: the help text is a sentence or two for a player
    reading along, and any real layout engine here would be more code than the
    thing it wraps.
    """
    words = str(text or "").split()
    lines = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if len(candidate) <= width_chars or not current:
            current = candidate
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


class MenuView:
    """Draws one :class:`~libs.menu.Menu` and turns mouse events into its calls."""

    def __init__(self):
        self._fonts = {}
        self._text_cache = {}
        self._scroll = {}
        self._hover = {}
        self._broken = False
        self.draws = 0
        self.clicks = 0

    # -- fonts and text ---------------------------------------------------

    def _font(self, size, bold=False):
        key = (size, bold)
        font = self._fonts.get(key)
        if font is None:
            import pygame
            with contextlib.suppress(Exception):
                font = pygame.font.SysFont("calibri", size, bold=bold)
            if font is None:
                font = pygame.font.Font(None, size + 4)
            self._fonts[key] = font
        return font

    def _surface(self, text, size, color, bold=False, width=None):
        """One rendered line, cached; ``width`` truncates with an ellipsis."""
        key = (text, size, color, bold, width)
        surface = self._text_cache.get(key)
        if surface is not None:
            return surface
        font = self._font(size, bold)
        surface = font.render(str(text), True, color)
        if width is not None and surface.get_width() > width:
            # Walk the text down until it fits; a menu line is never allowed to
            # run over the scrollbar.
            trimmed = str(text)
            while trimmed and surface.get_width() > width:
                trimmed = trimmed[:-1]
                surface = font.render(trimmed + "...", True, color)
        if len(self._text_cache) > _TEXT_CACHE_LIMIT:
            self._text_cache.clear()
        self._text_cache[key] = surface
        return surface

    # -- state ------------------------------------------------------------

    def scroll_of(self, menu):
        return self._scroll.get(id(menu), 0)

    def set_scroll(self, menu, value):
        self._scroll[id(menu)] = max(0, int(value))

    def hover_of(self, menu):
        """The line under the pointer, or None -- a *picture*, never a focus.

        Hover is kept here and not on the menu because the two are different
        things. SDL reports a cursor position as soon as the window exists, so
        writing hover into ``menu.pos`` meant a menu that was opened with no
        focus (the main menu) silently gained the line under wherever the
        pointer happened to rest -- before the player touched anything.
        """
        index = self._hover.get(id(menu))
        if index is None:
            return None
        if not 0 <= index < len(getattr(menu, "items", []) or []):
            return None
        return index

    def set_hover(self, menu, index):
        if len(self._hover) > 64:
            self._hover.clear()
        self._hover[id(menu)] = index

    def help_index_for(self, menu):
        """Which line the description panel describes.

        The pointer wins while it is over a line (so a player who sees can read
        a line they are only pointing at), and the focused line answers
        otherwise -- which is the only case a screen-reader player is ever in.
        """
        hovered = self.hover_of(menu)
        if hovered is not None:
            return hovered
        pos = getattr(menu, "pos", -1)
        try:
            return int(pos) if pos is not None else -1
        except (TypeError, ValueError):
            return -1

    def arrows_of(self, menu):
        return {index for index, item in enumerate(menu.items) if has_arrow(item)}

    def layout_for(self, menu, size):
        """The layout this view would draw for ``menu`` on a ``size`` window."""
        width, height = size
        # `pos` is a real zero more often than not, so it is not allowed to
        # fall through a `or` into "no focus": that turned every first line
        # into a menu with no focused line, and with it no description panel.
        pos = getattr(menu, "pos", -1)
        try:
            focus = int(pos) if pos is not None else -1
        except (TypeError, ValueError):
            focus = -1
        items = getattr(menu, "items", [])
        answered = self.help_index_for(menu)
        described = items[answered] if 0 <= answered < len(items) else None
        layout = build_layout(
            len(items), focus, self.scroll_of(menu), width, height,
            want_help=bool(described and has_help(described)),
            arrows=self.arrows_of(menu),
        )
        return layout

    # -- events -----------------------------------------------------------

    @staticmethod
    def handles(event):
        """Whether this is a mouse event the picture is responsible for."""
        import pygame
        return getattr(event, "type", None) in (
            pygame.MOUSEMOTION, pygame.MOUSEBUTTONDOWN, pygame.MOUSEBUTTONUP,
            getattr(pygame, "MOUSEWHEEL", -1),
        )

    def handle_event(self, menu, event, size=None):
        """Apply a mouse event to the menu. Returns True when it was ours.

        Every branch calls the menu's own method or the line's own callback, so
        the keyboard path and the picture cannot drift apart.
        """
        import pygame
        items = getattr(menu, "items", []) or []
        if not items:
            return False
        kind = getattr(event, "type", None)

        # The wheel first: it carries no cursor position, so it must not wait
        # on a point that a headless window cannot supply.
        if kind == getattr(pygame, "MOUSEWHEEL", -1):
            if getattr(event, "y", 0) > 0:
                menu.move_up()
            elif getattr(event, "y", 0) < 0:
                menu.move_down()
            return True

        screen = getattr(getattr(menu, "game", None), "screen", None)
        if size is None:
            size = screen.get_size() if screen is not None else (900, 500)
        x, y = self._point(event)
        if x is None:
            return False
        layout = self.layout_for(menu, size)

        if kind == pygame.MOUSEMOTION:
            what, index = self._target(layout, x, y)
            if what in ("item", "arrow") and 0 <= index < len(items):
                # The picture follows the pointer; the game does not. Pointing
                # must not move a menu's focus (see `hover_of`) and must not
                # talk over a screen reader.
                self.set_hover(menu, index)
            else:
                self.set_hover(menu, None)
            return True

        if kind == pygame.MOUSEBUTTONDOWN:
            button = getattr(event, "button", 1)
            if button in (4, 5):  # older wheel events
                menu.move_up() if button == 4 else menu.move_down()
                return True
            if button != 1:
                return False
            what, index, direction = self._target_full(layout, x, y)
            if what == "item" and 0 <= index < len(items):
                # A click is the deliberate act, and this is the one place the
                # pointer is allowed to set the focus -- because the player has
                # just chosen the line with their own hand.
                menu.pos = index
                self.set_hover(menu, index)
                self.clicks += 1
                menu.select_current_item()
                return True
            if what == "arrow" and 0 <= index < len(items):
                menu.pos = index
                self.set_hover(menu, index)
                arrow = getattr(items[index][1], "arrow", None)
                if callable(arrow):
                    arrow(direction, 0)
                    self.clicks += 1
                return True
            if what == "help":
                with contextlib.suppress(Exception):
                    menu.speak_current_help()
                return True
            return True

        return False

    @staticmethod
    def _point(event):
        pos = getattr(event, "pos", None)
        if pos is None:
            import pygame
            with contextlib.suppress(Exception):
                pos = pygame.mouse.get_pos()
        if pos is None:
            return (None, None)
        try:
            return (int(pos[0]), int(pos[1]))
        except (TypeError, ValueError, IndexError):
            return (None, None)

    @staticmethod
    def _target(layout, x, y):
        hit = layout.hit(x, y)
        return (hit[0], hit[1] if len(hit) > 1 else None)

    @staticmethod
    def _target_full(layout, x, y):
        hit = layout.hit(x, y)
        what = hit[0]
        index = hit[1] if len(hit) > 1 else None
        direction = hit[2] if len(hit) > 2 else 0
        return (what, index, direction)

    # -- drawing ----------------------------------------------------------

    def draw(self, menu, screen=None):
        """Draw ``menu`` on ``screen`` (the game's window by default).

        Returns whether anything was drawn. Never raises: a picture that cannot
        be drawn shelves itself, because a menu the player cannot see must still
        work for the player who cannot see it.
        """
        if self._broken:
            return False
        if screen is None:
            screen = getattr(getattr(menu, "game", None), "screen", None)
        if screen is None:
            return False
        if not enabled():
            return False
        try:
            return self._draw(menu, screen)
        except Exception as exc:
            self._broken = True
            with contextlib.suppress(Exception):
                from ..logger import log_exception
                log_exception(exc, "menu view drawing (visual menus now off)")
            return False

    def _draw(self, menu, screen):
        import pygame
        width, height = screen.get_size()
        layout = self.layout_for(menu, (width, height))
        screen.fill(COLOR_BACKGROUND)

        title = self._surface(getattr(menu, "title", "") or "", TITLE_SIZE,
                              COLOR_TEXT, bold=True, width=width - PAD * 2)
        screen.blit(title, (PAD, 12))
        self._rule(screen, TITLE_HEIGHT - 6, width)

        items = menu.items
        hovered = self.hover_of(menu)
        for index, (rx, ry, rw, rh) in layout.rows:
            item = items[index] if index < len(items) else None
            focused = index == int(getattr(menu, "pos", -1))
            pygame.draw.rect(
                screen,
                COLOR_ROW_FOCUS if focused else COLOR_ROW,
                pygame.Rect(rx, ry, rw, rh),
                border_radius=5,
            )
            if focused:
                pygame.draw.rect(screen, COLOR_BORDER,
                                 pygame.Rect(rx, ry, rw, rh), 1, border_radius=5)
            elif index == hovered:
                # The pointer's own mark: an outline, not the focus bar, so a
                # player can tell what they are pointing at from what the keys
                # are on.
                pygame.draw.rect(screen, COLOR_HOVER,
                                 pygame.Rect(rx, ry, rw, rh), 1, border_radius=5)
            marker = ">" if focused else " "
            counter = f"{index + 1}."
            prefix = f"{marker} {counter} "
            arrow = has_arrow(item)
            text_width = rw - PAD - (ARROW_WIDTH if arrow else 0)
            label = self._surface(
                f"{prefix}{label_of(item)}", TEXT_SIZE,
                COLOR_TEXT_FOCUS if focused else COLOR_TEXT,
                width=text_width,
            )
            screen.blit(label, (rx + 10, ry + (rh - label.get_height()) // 2))
            if has_preview(item):
                dot = self._surface("*", TEXT_SIZE, COLOR_TEXT_DIM)
                screen.blit(dot, (rx + rw - (ARROW_WIDTH if arrow else 0) - 18,
                                  ry + (rh - dot.get_height()) // 2))
            if arrow:
                left = self._surface("<", TEXT_SIZE, COLOR_TEXT_FOCUS if focused else COLOR_TEXT)
                right = self._surface(">", TEXT_SIZE, COLOR_TEXT_FOCUS if focused else COLOR_TEXT)
                cy = ry + (rh - left.get_height()) // 2
                screen.blit(left, (rx + rw - ARROW_WIDTH + 4, cy))
                screen.blit(right, (rx + rw - 14, cy))

        if layout.scrollable:
            self._scrollbar(screen, layout)

        self._draw_help(screen, menu, layout, width, height)
        self.draws += 1
        with contextlib.suppress(Exception):
            pygame.display.update()
        return True

    def _draw_help(self, screen, menu, layout, width, height):
        rect = layout.help_rect
        if not rect:
            return
        import pygame
        hx, hy, hw, hh = rect
        pygame.draw.line(screen, COLOR_BORDER, (PAD, hy), (width - PAD, hy))
        answered = self.help_index_for(menu)
        items = menu.items
        item = items[answered] if 0 <= answered < len(items) else None
        text = help_of(item) if item else ""
        if not text:
            return
        chars = max(24, int((width - PAD * 2) / (HELP_SIZE * 0.52)))
        lines = wrap_text(text, chars)[:3]
        y = hy + 8
        for line in lines:
            surface = self._surface(line, HELP_SIZE, COLOR_HELP, width=width - PAD * 2)
            screen.blit(surface, (PAD, y))
            y += surface.get_height() + 2

    @staticmethod
    def _rule(screen, y, width):
        import pygame
        pygame.draw.line(screen, COLOR_BORDER, (PAD, y), (width - PAD, y))

    @staticmethod
    def _scrollbar(screen, layout):
        import pygame
        total = max(1, layout.count)
        visible = min(layout.max_rows, total)
        track_top = TITLE_HEIGHT + PAD
        bottom = layout.height - PAD - (HELP_HEIGHT if layout.help_rect else 0)
        track_height = max(1, bottom - track_top)
        thumb_height = max(18, int(track_height * visible / total))
        travel = max(0, track_height - thumb_height)
        offset = int(travel * layout.scroll / max(1, total - visible)) if total > visible else 0
        x = layout.width - PAD - SCROLLBAR_WIDTH + 4
        pygame.draw.rect(screen, COLOR_ROW, pygame.Rect(x, track_top, SCROLLBAR_WIDTH, track_height),
                         border_radius=4)
        pygame.draw.rect(screen, COLOR_SCROLL,
                         pygame.Rect(x, track_top + offset, SCROLLBAR_WIDTH, thumb_height),
                         border_radius=4)
