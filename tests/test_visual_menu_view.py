"""The picture of a menu: layout, hit-testing, and what a mouse does to a menu.

The renderer is deliberately split in two -- pure numbers (`build_layout`,
`Layout.hit`) and a drawing layer that reads them -- so most of what a sighted
player can do with a menu is testable without a display, without OpenAL, and
without the game. The fake menu below answers exactly the four calls the view is
allowed to make, which is also the contract that keeps the picture from
inventing a second way to use a menu:

    select_current_item()      a click on a line
    move_up() / move_down()    the wheel
    speak_current_help()       a click on the description panel
    item[1].arrow(direction, 0)  a click on a line's own < >
    menu.pos = index           hover (silent, on purpose)

Pins: nothing here may call speech.speak, and nothing here may make a menu fail.
"""

import os
import types
import unittest
from unittest import mock

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")

import pygame

from libs import options
from libs import speech
from libs.ui import menu_view


def setUpModule():
    pygame.font.init()


def tearDownModule():
    pygame.font.quit()


class FakeMenu:
    """A menu that records what was done to it -- the four calls, and nothing else."""

    def __init__(self, items=None, title="Test menu", pos=0, screen=True):
        self.items = list(items or [])
        self.title = title
        self.pos = pos
        self.selected = []
        self.moves = []
        self.help_spoken = 0
        self.game = types.SimpleNamespace(
            screen=pygame.Surface((900, 500)) if screen else None)

    def select_current_item(self):
        self.selected.append(self.pos)
        self.items[self.pos][1]()

    def move_up(self):
        self.moves.append(-1)
        self.pos = max(0, self.pos - 1)

    def move_down(self):
        self.moves.append(1)
        self.pos = min(len(self.items) - 1, self.pos + 1)

    def speak_current_help(self):
        self.help_spoken += 1


def plain_items(count=5):
    items = []
    for index in range(count):
        items.append((f"Line {index + 1}", (lambda i=index: None), None))
    return items


def click_event(x, y, button=1):
    return pygame.event.Event(pygame.MOUSEBUTTONDOWN,
                              {"pos": (x, y), "button": button})


def motion_event(x, y):
    return pygame.event.Event(pygame.MOUSEMOTION, {"pos": (x, y), "rel": (0, 0),
                                                   "buttons": (0, 0, 0)})


def wheel_event(y):
    return pygame.event.Event(pygame.MOUSEWHEEL, {"y": y, "x": 0,
                                                 "flipped": False,
                                                 "which": 0})


def row_center(layout, index):
    rect = layout.row_of(index)
    x, y, width, height = rect
    return (x + width // 2, y + height // 2)


class LayoutGeometryTests(unittest.TestCase):
    """Where a line sits, for one menu of N lines in one window."""

    def test_the_first_line_sits_below_the_title(self):
        layout = menu_view.build_layout(3, 0, 0, 900, 500)
        top = layout.rows[0][1][1]
        self.assertGreaterEqual(top, menu_view.TITLE_HEIGHT)

    def test_a_short_menu_shows_every_line_without_a_scrollbar(self):
        layout = menu_view.build_layout(3, 0, 0, 900, 500)
        self.assertEqual([index for index, _ in layout.rows], [0, 1, 2])
        self.assertFalse(layout.scrollable)

    def test_a_long_menu_scrolls_and_shows_only_what_fits(self):
        layout = menu_view.build_layout(80, 0, 0, 900, 500)
        self.assertTrue(layout.scrollable)
        self.assertLess(len(layout.rows), 80)

    def test_the_window_moves_to_keep_the_focus_visible_below(self):
        layout = menu_view.build_layout(40, 20, 0, 900, 500)
        self.assertIsNotNone(layout.row_of(20))

    def test_the_window_moves_to_keep_the_focus_visible_above(self):
        layout = menu_view.build_layout(40, 2, 30, 900, 500)
        self.assertEqual(layout.scroll, 2)
        self.assertIsNotNone(layout.row_of(2))

    def test_the_scroll_offset_cannot_go_negative(self):
        layout = menu_view.build_layout(40, 0, -12, 900, 500)
        self.assertEqual(layout.scroll, 0)

    def test_the_scroll_offset_cannot_run_past_the_last_page(self):
        # No focus, so nothing pulls the window back to a line.
        layout = menu_view.build_layout(40, -1, 999, 900, 500)
        self.assertEqual(layout.scroll, 40 - layout.max_rows)

    def test_a_window_smaller_than_a_row_still_draws_one_line(self):
        layout = menu_view.build_layout(10, 3, 0, 200, 40)
        self.assertEqual(layout.max_rows, 1)
        self.assertEqual(len(layout.rows), 1)
        self.assertEqual(layout.rows[0][0], 3)

    def test_a_window_smaller_than_the_minimum_still_lays_out_a_line(self):
        layout = menu_view.build_layout(2, 0, 0, 10, 10)
        self.assertGreaterEqual(layout.width, 1)
        self.assertGreaterEqual(layout.height, 1)
        self.assertEqual(len(layout.rows), 1)

    def test_an_empty_menu_has_no_rows(self):
        layout = menu_view.build_layout(0, -1, 0, 900, 500)
        self.assertEqual(layout.rows, [])

    def test_a_description_panel_costs_rows(self):
        without = menu_view.build_layout(30, 0, 0, 900, 500, want_help=False)
        with_help = menu_view.build_layout(30, 0, 0, 900, 500, want_help=True)
        self.assertLess(with_help.max_rows, without.max_rows)
        self.assertIsNotNone(with_help.help_rect)
        self.assertIsNone(without.help_rect)

    def test_the_description_panel_sits_at_the_bottom_of_the_window(self):
        layout = menu_view.build_layout(5, 0, 0, 900, 500, want_help=True)
        x, y, width, height = layout.help_rect
        self.assertEqual(y + height, 500)

    def test_rows_never_overlap(self):
        layout = menu_view.build_layout(20, 0, 0, 900, 500)
        rects = [rect for _, rect in layout.rows]
        for above, below in zip(rects, rects[1:]):
            self.assertLessEqual(above[1] + above[3], below[1])


class LayoutHitTests(unittest.TestCase):
    """What is under a point -- the only thing a mouse click consults."""

    def setUp(self):
        self.layout = menu_view.build_layout(6, 0, 0, 900, 500, want_help=True)

    def test_a_point_inside_a_line_is_that_line(self):
        self.assertEqual(self.layout.hit(*row_center(self.layout, 3)), ("item", 3))

    def test_the_top_edge_of_a_line_belongs_to_it(self):
        x, y, width, height = self.layout.row_of(2)
        self.assertEqual(self.layout.hit(x + 4, y), ("item", 2))

    def test_the_pixel_below_a_line_is_not_that_line(self):
        x, y, width, height = self.layout.row_of(2)
        self.assertNotEqual(self.layout.hit(x + 4, y + height), ("item", 2))

    def test_the_gap_between_two_lines_belongs_to_neither(self):
        x, y, width, height = self.layout.row_of(1)
        self.assertEqual(self.layout.hit(x + 4, y + height + 1), (None, None))

    def test_the_title_area_is_the_title(self):
        self.assertEqual(self.layout.hit(450, 4), ("title", None))

    def test_the_description_panel_is_the_help(self):
        x, y, width, height = self.layout.help_rect
        self.assertEqual(self.layout.hit(x + 20, y + 20), ("help", None))

    def test_empty_space_below_the_lines_is_nothing(self):
        x, y, width, height = self.layout.row_of(5)
        self.assertEqual(self.layout.hit(600, y + height + 40), (None, None))

    def test_a_point_outside_the_window_is_nothing(self):
        self.assertEqual(self.layout.hit(-10, 100), (None, None))
        self.assertEqual(self.layout.hit(2000, 100), (None, None))

    def test_a_control_line_owns_its_right_arrow(self):
        layout = menu_view.build_layout(4, 0, 0, 900, 500, arrows={1})
        x, y, width, height = layout.row_of(1)
        self.assertEqual(layout.hit(x + width - 5, y + 5), ("arrow", 1, 1))

    def test_a_control_line_owns_its_left_arrow(self):
        layout = menu_view.build_layout(4, 0, 0, 900, 500, arrows={1})
        x, y, width, height = layout.row_of(1)
        self.assertEqual(layout.hit(x + 2, y + 5), ("arrow", 1, -1))

    def test_the_middle_of_a_control_line_is_the_line_itself(self):
        layout = menu_view.build_layout(4, 0, 0, 900, 500, arrows={1})
        x, y, width, height = layout.row_of(1)
        self.assertEqual(layout.hit(x + width // 2, y + 5), ("item", 1))

    def test_a_plain_line_has_no_arrow_zones(self):
        x, y, width, height = self.layout.row_of(0)
        self.assertEqual(self.layout.hit(x + 2, y + 5), ("item", 0))
        self.assertEqual(self.layout.hit(x + width - 3, y + 5), ("item", 0))


class ItemReadingTests(unittest.TestCase):
    """What a line says and what it is, for a renderer that must never guess."""

    def test_a_plain_label_is_its_text(self):
        self.assertEqual(menu_view.label_of(("Hello", lambda: None)), "Hello")

    def test_a_lazy_label_is_called(self):
        self.assertEqual(menu_view.label_of((lambda: "Computed", lambda: None)),
                         "Computed")

    def test_a_lazy_label_that_raises_says_nothing_instead_of_crashing(self):
        def broken():
            raise RuntimeError("label")
        self.assertEqual(menu_view.label_of((broken, lambda: None)), "")

    def test_no_item_is_no_text(self):
        self.assertEqual(menu_view.label_of(None), "")

    def test_a_three_field_line_has_no_description(self):
        self.assertFalse(menu_view.has_help(("Line", lambda: None, None)))
        self.assertEqual(menu_view.help_of(("Line", lambda: None, None)), "")

    def test_a_four_field_line_has_one(self):
        item = ("Line", lambda: None, None, "What it does.")
        self.assertTrue(menu_view.has_help(item))
        self.assertEqual(menu_view.help_of(item), "What it does.")

    def test_a_line_with_a_preview_is_marked(self):
        self.assertTrue(menu_view.has_preview(("Line", lambda: None, "a.ogg")))
        self.assertFalse(menu_view.has_preview(("Line", lambda: None, None)))

    def test_a_line_that_answers_left_and_right_is_a_control(self):
        def action():
            pass
        action.arrow = lambda direction, mod: None
        self.assertTrue(menu_view.has_arrow(("Scrub", action, None)))
        self.assertFalse(menu_view.has_arrow(("Line", lambda: None, None)))

    def test_long_text_wraps_into_several_lines(self):
        lines = menu_view.wrap_text("one two three four five six", 9)
        self.assertGreater(len(lines), 1)
        for line in lines:
            self.assertLessEqual(len(line), 9)

    def test_short_text_wraps_into_one_line(self):
        self.assertEqual(menu_view.wrap_text("hello there", 40), ["hello there"])

    def test_nothing_wraps_into_nothing(self):
        self.assertEqual(menu_view.wrap_text("", 10), [])

    def test_a_word_longer_than_the_line_still_gets_a_line(self):
        self.assertEqual(menu_view.wrap_text("supercalifragilistic", 4),
                         ["supercalifragilistic"])


class MouseTests(unittest.TestCase):
    """What a click, a hover and a wheel do to a menu -- through its own calls."""

    def setUp(self):
        self.view = menu_view.MenuView()
        self.menu = FakeMenu(plain_items(6))
        self.size = (900, 500)
        self.layout = self.view.layout_for(self.menu, self.size)

    def test_a_click_on_a_line_runs_that_line(self):
        ran = []
        self.menu.items[4] = ("Line 5", lambda: ran.append("five"), None)
        self.view.handle_event(self.menu, click_event(*row_center(self.layout, 4)),
                               size=self.size)
        self.assertEqual(ran, ["five"])
        self.assertEqual(self.menu.pos, 4)

    def test_a_click_on_the_focused_line_runs_it_too(self):
        ran = []
        self.menu.items[0] = ("Line 1", lambda: ran.append("first"), None)
        self.view.handle_event(self.menu, click_event(*row_center(self.layout, 0)),
                               size=self.size)
        self.assertEqual(ran, ["first"])

    def test_a_click_in_empty_space_does_nothing(self):
        self.view.handle_event(self.menu, click_event(880, 470), size=self.size)
        self.assertEqual(self.menu.selected, [])

    def test_a_click_on_the_title_does_nothing(self):
        self.view.handle_event(self.menu, click_event(450, 6), size=self.size)
        self.assertEqual(self.menu.selected, [])

    def test_a_click_on_the_description_panel_asks_for_it(self):
        item = ("Line", lambda: None, None, "Explains itself.")
        self.menu.items = [item]
        self.layout = self.view.layout_for(self.menu, self.size)
        x, y, width, height = self.layout.help_rect
        self.view.handle_event(self.menu, click_event(x + 20, y + 20), size=self.size)
        self.assertEqual(self.menu.help_spoken, 1)

    def test_a_click_on_a_controls_right_arrow_steps_it_up(self):
        steps = []
        def action():
            pass
        action.arrow = lambda direction, mod: steps.append((direction, mod))
        self.menu.items = [("Scrub", action, None)]
        self.layout = self.view.layout_for(self.menu, self.size)
        x, y, width, height = self.layout.row_of(0)
        self.view.handle_event(self.menu, click_event(x + width - 4, y + 5),
                               size=self.size)
        self.assertEqual(steps, [(1, 0)])

    def test_a_click_on_a_controls_left_arrow_steps_it_down(self):
        steps = []
        def action():
            pass
        action.arrow = lambda direction, mod: steps.append((direction, mod))
        self.menu.items = [("Scrub", action, None)]
        self.layout = self.view.layout_for(self.menu, self.size)
        x, y, width, height = self.layout.row_of(0)
        self.view.handle_event(self.menu, click_event(x + 2, y + 5), size=self.size)
        self.assertEqual(steps, [(-1, 0)])

    def test_hovering_a_line_marks_it_without_moving_the_focus(self):
        self.view.handle_event(self.menu, motion_event(*row_center(self.layout, 3)),
                               size=self.size)
        self.assertEqual(self.view.hover_of(self.menu), 3)
        self.assertEqual(self.menu.pos, 0)
        self.assertEqual(self.menu.selected, [])

    def test_a_menu_opened_with_no_focus_keeps_no_focus_while_the_pointer_rests(self):
        # The reported bug: the main menu opens with no focused line, SDL
        # reports a cursor position on the first frame, and the pointer was
        # writing itself into the menu's focus -- so the menu opened on
        # "Test Speakers and Headphones" before the player touched anything.
        self.menu.pos = -1
        self.view.handle_event(self.menu, motion_event(*row_center(self.layout, 5)),
                               size=self.size)
        self.assertEqual(self.menu.pos, -1)
        self.assertEqual(self.view.hover_of(self.menu), 5)

    def test_pressing_down_after_a_hover_starts_from_no_focus(self):
        self.menu.pos = -1
        self.view.handle_event(self.menu, motion_event(*row_center(self.layout, 5)),
                               size=self.size)
        self.menu.move_down()
        self.assertEqual(self.menu.pos, 0)

    def test_the_pointer_leaving_the_lines_clears_its_mark(self):
        self.view.handle_event(self.menu, motion_event(*row_center(self.layout, 2)),
                               size=self.size)
        self.view.handle_event(self.menu, motion_event(890, 490), size=self.size)
        self.assertIsNone(self.view.hover_of(self.menu))

    def test_the_description_follows_the_pointer_then_the_focus(self):
        described = ("Described", lambda: None, None, "What it does.")
        self.menu.items = [described, ("Plain", lambda: None, None), described]
        self.menu.pos = 1
        self.layout = self.view.layout_for(self.menu, self.size)
        self.view.handle_event(self.menu, motion_event(*row_center(self.layout, 0)),
                               size=self.size)
        self.assertEqual(self.view.help_index_for(self.menu), 0)
        self.view.handle_event(self.menu, motion_event(890, 490), size=self.size)
        self.assertEqual(self.view.help_index_for(self.menu), 1)

    def test_a_click_marks_the_line_it_chose(self):
        self.view.handle_event(self.menu, click_event(*row_center(self.layout, 3)),
                               size=self.size)
        self.assertEqual(self.view.hover_of(self.menu), 3)
        self.assertEqual(self.menu.pos, 3)

    def test_hovering_says_nothing(self):
        with mock.patch.object(speech, "speak") as speak:
            self.view.handle_event(self.menu, motion_event(*row_center(self.layout, 2)),
                                   size=self.size)
        self.assertFalse(speak.called)

    def test_hovering_the_description_panel_leaves_the_focus_and_mark_alone(self):
        # Both lines describe themselves, so the panel is there whatever the
        # focus is -- which is the point: the panel is not a line.
        item = ("Line", lambda: None, None, "Explains itself.")
        self.menu.items = [item, ("Other", lambda: None, None, "So does this one.")]
        self.menu.pos = 1
        self.layout = self.view.layout_for(self.menu, self.size)
        x, y, width, height = self.layout.help_rect
        self.view.handle_event(self.menu, motion_event(x + 20, y + 20), size=self.size)
        self.assertEqual(self.menu.pos, 1)

    def test_the_wheel_up_moves_the_focus_up(self):
        self.menu.pos = 3
        self.view.handle_event(self.menu, wheel_event(1), size=self.size)
        self.assertEqual(self.menu.moves, [-1])
        self.assertEqual(self.menu.pos, 2)

    def test_the_wheel_down_moves_the_focus_down(self):
        self.menu.pos = 3
        self.view.handle_event(self.menu, wheel_event(-1), size=self.size)
        self.assertEqual(self.menu.moves, [1])
        self.assertEqual(self.menu.pos, 4)

    def test_the_older_wheel_buttons_work_too(self):
        self.menu.pos = 3
        self.view.handle_event(self.menu, click_event(10, 10, button=4), size=self.size)
        self.view.handle_event(self.menu, click_event(10, 10, button=5), size=self.size)
        self.assertEqual(self.menu.moves, [-1, 1])

    def test_a_right_click_is_not_ours(self):
        self.assertFalse(
            self.view.handle_event(self.menu, click_event(10, 10, button=3),
                                   size=self.size))
        self.assertEqual(self.menu.selected, [])

    def test_a_button_release_is_not_ours(self):
        event = pygame.event.Event(pygame.MOUSEBUTTONUP, {"pos": (10, 10), "button": 1})
        self.assertFalse(self.view.handle_event(self.menu, event, size=self.size))

    def test_an_empty_menu_ignores_the_mouse(self):
        self.menu.items = []
        self.assertFalse(
            self.view.handle_event(self.menu, click_event(100, 100), size=self.size))

    def test_the_view_claims_only_mouse_events(self):
        self.assertTrue(menu_view.MenuView.handles(click_event(0, 0)))
        self.assertTrue(menu_view.MenuView.handles(motion_event(0, 0)))
        self.assertTrue(menu_view.MenuView.handles(wheel_event(1)))
        key = pygame.event.Event(pygame.KEYDOWN, {"key": pygame.K_RETURN,
                                                  "mod": 0, "unicode": ""})
        self.assertFalse(menu_view.MenuView.handles(key))


class DrawingTests(unittest.TestCase):
    """A picture that cannot be drawn must never take the menu with it."""

    def setUp(self):
        self.view = menu_view.MenuView()
        self.surface = pygame.Surface((900, 500))
        self.menu = FakeMenu(plain_items(5))
        self.menu.game.screen = self.surface

    def test_drawing_a_menu_succeeds(self):
        self.assertTrue(self.view.draw(self.menu))
        self.assertEqual(self.view.draws, 1)

    def test_drawing_twice_is_still_fine(self):
        self.view.draw(self.menu)
        self.view.draw(self.menu)
        self.assertEqual(self.view.draws, 2)

    def test_a_game_without_a_screen_draws_nothing(self):
        self.menu.game = types.SimpleNamespace()
        self.assertFalse(self.view.draw(self.menu))

    def test_the_option_turning_pictures_off_is_honoured(self):
        with mock.patch.dict(options.prefs, {"visual_menus": False}):
            self.assertFalse(self.view.draw(self.menu))
        self.assertEqual(self.view.draws, 0)

    def test_the_option_is_on_by_default(self):
        self.assertTrue(options.prefs.get("visual_menus", None) is not None
                        or "visual_menus" in options.prefs)
        self.assertTrue(menu_view.DEFAULT_ENABLED)

    def test_a_renderer_error_shelves_the_picture_instead_of_raising(self):
        with mock.patch.object(self.view, "_surface", side_effect=RuntimeError("font")):
            self.assertFalse(self.view.draw(self.menu))
            # And it stays shelved: the next frame does not retry a broken font.
            self.assertFalse(self.view.draw(self.menu))

    def test_a_label_wider_than_the_row_is_cut_short(self):
        self.menu.items = [("x" * 500, lambda: None, None)]
        self.assertTrue(self.view.draw(self.menu))

    def test_an_empty_menu_still_draws_its_title(self):
        self.menu.items = []
        self.assertTrue(self.view.draw(self.menu))

    def test_a_description_that_is_very_long_does_not_crash(self):
        self.menu.items = [("Line", lambda: None, None, "word " * 400)]
        self.assertTrue(self.view.draw(self.menu))

    def test_the_renderer_never_speaks(self):
        source = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                              "libs", "ui", "menu_view.py")
        with open(source, "r", encoding="utf-8") as handle:
            text = handle.read()
        self.assertNotIn("speech.speak(", text)
        self.assertNotIn("from .. import speech", text)
        self.assertNotIn("import speech", text.replace("not imported", ""))


class SimulatedSightedSessionTests(unittest.TestCase):
    """A player with a mouse, doing the things a player does, end to end."""

    def setUp(self):
        self.view = menu_view.MenuView()
        self.size = (900, 500)
        self.log = []

    def menu(self, items, title="Menu", pos=0):
        return FakeMenu(items, title=title, pos=pos)

    def test_a_two_level_session_reaches_the_right_actions(self):
        submenu = self.menu([("Back", lambda: self.log.append("back"), None)])
        items = [("Start a match", lambda: self.log.append("match"), None),
                 ("Open submenu", lambda: self.log.append("open"), None)]
        menu = self.menu(items)
        layout = self.view.layout_for(menu, self.size)

        self.view.handle_event(menu, motion_event(*row_center(layout, 0)), size=self.size)
        self.view.handle_event(menu, click_event(*row_center(layout, 0)), size=self.size)
        self.view.handle_event(menu, click_event(*row_center(layout, 1)), size=self.size)

        self.assertEqual(self.log, ["match", "open"])
        self.assertEqual(submenu.items[0][0], "Back")

    def test_wheel_scrolling_a_long_menu_then_clicking_the_last_line(self):
        ran = []
        items = [(f"Line {index}", (lambda i=index: ran.append(i)), None)
                 for index in range(60)]
        menu = self.menu(items)
        for _ in range(80):
            self.view.handle_event(menu, wheel_event(-1), size=self.size)
        self.assertEqual(menu.pos, 59)
        layout = self.view.layout_for(menu, self.size)
        self.view.handle_event(menu, click_event(*row_center(layout, 59)), size=self.size)
        self.assertEqual(ran, [59])

    def test_clicking_the_same_line_three_times_runs_it_three_times(self):
        ran = []
        menu = self.menu([("Repeat", lambda: ran.append(1), None)])
        layout = self.view.layout_for(menu, self.size)
        for _ in range(3):
            self.view.handle_event(menu, click_event(*row_center(layout, 0)),
                                   size=self.size)
        self.assertEqual(len(ran), 3)

    def test_hovering_down_a_list_then_clicking_runs_the_hovered_line(self):
        ran = []
        items = [(f"Line {index}", (lambda i=index: ran.append(i)), None)
                 for index in range(4)]
        menu = self.menu(items)
        layout = self.view.layout_for(menu, self.size)
        for index in range(4):
            self.view.handle_event(menu, motion_event(*row_center(layout, index)),
                                   size=self.size)
        self.view.handle_event(menu, click_event(*row_center(layout, 3)), size=self.size)
        self.assertEqual(ran, [3])

    def test_a_click_beyond_the_last_line_is_harmless(self):
        menu = self.menu(plain_items(2))
        layout = self.view.layout_for(menu, self.size)
        x, y, width, height = layout.row_of(1)
        self.view.handle_event(menu, click_event(x + 4, y + height + 60), size=self.size)
        self.assertEqual(menu.selected, [])

    def test_scrolling_a_control_line_with_the_wheel_does_not_turn_its_knob(self):
        steps = []
        def action():
            pass
        action.arrow = lambda direction, mod: steps.append(direction)
        menu = self.menu([("Gain", action, None), ("Other", lambda: None, None)])
        self.view.handle_event(menu, wheel_event(-1), size=self.size)
        self.assertEqual(steps, [])

    def test_a_click_on_a_line_with_a_label_callable_runs_its_action(self):
        ran = []
        menu = self.menu([(lambda: "Dynamic", lambda: ran.append("dyn"), None)])
        layout = self.view.layout_for(menu, self.size)
        self.view.handle_event(menu, click_event(*row_center(layout, 0)), size=self.size)
        self.assertEqual(ran, ["dyn"])

    def test_clicking_the_description_then_a_line_does_both(self):
        ran = []
        menu = self.menu([("Line", lambda: ran.append("line"), None, "What it does.")])
        layout = self.view.layout_for(menu, self.size)
        x, y, width, height = layout.help_rect
        self.view.handle_event(menu, click_event(x + 10, y + 10), size=self.size)
        self.view.handle_event(menu, click_event(*row_center(layout, 0)), size=self.size)
        self.assertEqual(menu.help_spoken, 1)
        self.assertEqual(ran, ["line"])

    def test_a_click_on_a_menu_whose_focus_is_unset_still_runs_the_clicked_line(self):
        ran = []
        menu = self.menu([("A", lambda: ran.append("a"), None),
                          ("B", lambda: ran.append("b"), None)], pos=-1)
        layout = self.view.layout_for(menu, self.size)
        self.view.handle_event(menu, click_event(*row_center(layout, 1)), size=self.size)
        self.assertEqual(ran, ["b"])
        self.assertEqual(menu.pos, 1)

    def test_the_window_size_the_view_is_given_is_the_one_it_uses(self):
        menu = self.menu(plain_items(3))
        narrow = self.view.layout_for(menu, (400, 300))
        wide = self.view.layout_for(menu, (1200, 800))
        self.assertLess(narrow.width, wide.width)
        self.assertGreaterEqual(wide.max_rows, narrow.max_rows)


class ThemeTests(unittest.TestCase):
    """Which palette a menu gets, and that every palette is usable."""

    def pick(self, title, event=""):
        return menu_view.theme_for(types.SimpleNamespace(title=title, menu_event=event))

    def test_the_main_menu_is_its_own_blue(self):
        self.assertEqual(self.pick("Main menu.").name, "main")

    def test_options_is_the_settings_family(self):
        self.assertEqual(self.pick("Options menu").name, "settings")

    def test_the_music_bot_is_its_own_family(self):
        self.assertEqual(self.pick("Music Bot Mode").name, "music")

    def test_a_party_page_is_the_party_family(self):
        self.assertEqual(self.pick("Party Sync").name, "party")

    def test_a_session_page_is_the_party_family_too(self):
        self.assertEqual(self.pick("Session jam").name, "party")

    def test_the_builder_menu_is_the_builder_family(self):
        self.assertEqual(self.pick("Builder Menu", "builder_menu_select").name, "builder")

    def test_the_technician_menu_shares_the_builder_family(self):
        self.assertEqual(self.pick("Technician Menu", "builder_menu_select").name,
                         "builder")

    def test_an_element_list_is_the_builder_family_from_its_event(self):
        self.assertEqual(self.pick("Select Element to Edit", "edit_element_select").name,
                         "builder")

    def test_a_jukebox_page_is_the_media_family(self):
        self.assertEqual(self.pick("Jukebox cabinet").name, "media")

    def test_a_cinema_page_is_the_cinema_family(self):
        self.assertEqual(self.pick("Cinema room 2").name, "cinema")

    def test_an_instrument_page_is_the_instrument_family(self):
        self.assertEqual(self.pick("Piano settings").name, "instrument")

    def test_an_unknown_title_falls_back_to_the_default_palette(self):
        self.assertEqual(self.pick("Something else").name, "default")

    def test_a_menu_without_a_title_still_gets_a_palette(self):
        self.assertEqual(menu_view.theme_for(types.SimpleNamespace()).name, "default")
        self.assertEqual(menu_view.theme_for(object()).name, "default")

    def test_every_family_has_a_different_page_and_a_different_focus(self):
        backgrounds = {theme.background for theme in menu_view.THEMES.values()}
        focuses = {theme.row_focus for theme in menu_view.THEMES.values()}
        self.assertEqual(len(backgrounds), len(menu_view.THEMES))
        self.assertEqual(len(focuses), len(menu_view.THEMES))

    def test_every_family_is_readable_and_its_focus_stands_out(self):
        def lightness(colour):
            return 0.299 * colour[0] + 0.587 * colour[1] + 0.114 * colour[2]
        for theme in menu_view.THEMES.values():
            self.assertGreater(lightness(theme.text) - lightness(theme.background), 90,
                               f"{theme.name}: text on page")
            self.assertGreater(lightness(theme.row_focus) - lightness(theme.row), 25,
                               f"{theme.name}: focused row over a plain row")
            self.assertGreater(lightness(theme.help) - lightness(theme.panel), 60,
                               f"{theme.name}: description on its footer")

    def test_a_themed_menu_draws(self):
        for name, theme in menu_view.THEMES.items():
            menu = FakeMenu([("Line", lambda: None, None, "Described.")],
                            title=name + " menu")
            view = menu_view.MenuView()
            self.assertTrue(view.draw(menu, pygame.Surface((900, 500))), name)


class RealMenuIntegrationTests(unittest.TestCase):
    """The picture on the real `Menu`, including that the keys still work."""

    def setUp(self):
        self.sounds = types.SimpleNamespace(play=lambda *a, **k: None,
                                            labeled_sources={},
                                            parent=types.SimpleNamespace(
                                                volume_categories={"ui": [100]}))
        self.screen = pygame.Surface((900, 500))
        self.game = types.SimpleNamespace(screen=self.screen,
                                          direct_soundgroup=self.sounds)
        self.spoken = []
        self._speak = mock.patch.object(speech, "speak",
                                        side_effect=lambda *a, **k: self.spoken.append(a))
        self._speak.start()

    def tearDown(self):
        self._speak.stop()

    def build(self):
        from libs import menu as menu_mod
        menu = menu_mod.Menu(self.game, "Real menu")
        ran = []
        menu.add_item("First", lambda: ran.append("first"))
        menu.add_item("Second", lambda: ran.append("second"))
        return menu, ran

    def test_a_real_menu_builds_a_picture_and_draws_it(self):
        menu, _ = self.build()
        view = menu.visual_view()
        self.assertIsNotNone(view)
        self.assertTrue(view.draw(menu))
        self.assertEqual(view.draws, 1)

    def test_a_click_on_a_real_menu_runs_the_line_and_keeps_the_keys_working(self):
        menu, ran = self.build()
        view = menu.visual_view()
        layout = view.layout_for(menu, (900, 500))
        x, y, width, height = layout.row_of(1)
        self.assertTrue(view.handle_event(menu, click_event(x + 10, y + 5),
                                          size=(900, 500)))
        self.assertEqual(ran, ["second"])
        self.assertEqual(menu.pos, 1)

    def test_a_real_menu_with_pictures_turned_off_draws_nothing(self):
        menu, _ = self.build()
        view = menu.visual_view()
        with mock.patch.dict(options.prefs, {"visual_menus": False}):
            self.assertFalse(view.draw(menu))
        self.assertEqual(view.draws, 0)

    def test_a_menu_update_draws_once_per_frame_and_consumes_mouse_events(self):
        menu, _ = self.build()
        view = menu.visual_view()
        before = view.draws
        menu.update([])
        self.assertEqual(view.draws, before + 1)
        menu.update([click_event(5, 495)])
        self.assertEqual(view.draws, before + 2)


if __name__ == "__main__":
    unittest.main()
