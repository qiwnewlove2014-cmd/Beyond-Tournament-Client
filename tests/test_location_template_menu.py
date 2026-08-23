"""Regression coverage for the accessible location-announcement editor."""

import os
import sys
import unittest
from unittest import mock
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from libs import menus


class LocationTemplateValidationTests(unittest.TestCase):
    def test_all_presets_validate_and_render(self):
        for name, template in menus.LOCATION_TEMPLATE_PRESETS:
            with self.subTest(name=name):
                valid, error = menus.validate_location_template(template)
                self.assertTrue(valid, error)
                preview = menus.render_location_template_preview(template)
                self.assertNotIn("{", preview)
                self.assertNotIn("}", preview)

    def test_rejects_unknown_or_unsafe_fields(self):
        invalid_templates = (
            "{unknown}",
            "{x.__class__}",
            "{x[0]}",
            "{x!r}",
            "{x:03d}",
            "{}",
            "plain text only",
        )
        for template in invalid_templates:
            with self.subTest(template=template):
                valid, _ = menus.validate_location_template(template)
                self.assertFalse(valid)

    def test_rejects_unmatched_braces(self):
        valid, error = menus.validate_location_template("X {x")
        self.assertFalse(valid)
        self.assertIn("braces", error)

    def test_custom_builder_uses_fixed_component_order(self):
        template = menus.build_custom_location_template(
            {"balanced", "x", "direction"}
        )
        self.assertEqual(
            template,
            "X {x}. Facing {direction}. You are {balanced}.",
        )
        self.assertTrue(menus.validate_location_template(template)[0])


class AdvancedLocationTemplateSaveTests(unittest.TestCase):
    def test_invalid_template_is_not_saved_and_returns_to_editor_menu(self):
        done = mock.Mock()
        return_to_menu = mock.Mock()
        with mock.patch.object(menus.options, "set") as set_option, mock.patch.object(
            menus.speech, "speak"
        ) as speak:
            menus.configure_location_template2(
                object(),
                "Facing {wrong}",
                done,
                return_to_menu,
            )

        set_option.assert_not_called()
        done.assert_not_called()
        return_to_menu.assert_called_once_with()
        self.assertIn("not saved", speak.call_args.args[0].lower())

    def test_valid_template_is_saved_then_returns_to_options(self):
        done = mock.Mock()
        return_to_menu = mock.Mock()
        template = "X {x}. Facing {direction}."
        with mock.patch.object(menus.options, "set") as set_option, mock.patch.object(
            menus.speech, "speak"
        ) as speak:
            menus.configure_location_template2(
                object(),
                template,
                done,
                return_to_menu,
            )

        set_option.assert_called_once_with("location_template", template)
        done.assert_called_once_with()
        return_to_menu.assert_not_called()
        self.assertIn("example", speak.call_args.args[0].lower())

    def test_empty_input_cancels_without_overwriting(self):
        done = mock.Mock()
        return_to_menu = mock.Mock()
        with mock.patch.object(menus.options, "set") as set_option, mock.patch.object(
            menus.speech, "speak"
        ):
            menus.configure_location_template2(
                object(),
                "   ",
                done,
                return_to_menu,
            )

        set_option.assert_not_called()
        done.assert_not_called()
        return_to_menu.assert_called_once_with()


class LocationTemplateMenuFlowTests(unittest.TestCase):
    class FakeMenu:
        def __init__(self, game, title, **kwargs):
            self.game = game
            self.title = title
            self.items = []

        def add_items(self, items):
            self.items.extend(items)

    def test_main_menu_exposes_presets_builder_preview_reset_and_back(self):
        created = []
        with mock.patch.object(menus.menu, "Menu", self.FakeMenu), mock.patch.object(
            menus, "set_default_sounds"
        ), mock.patch.object(
            menus.options,
            "get",
            return_value=menus.DEFAULT_LOCATION_TEMPLATE,
        ):
            menus.configure_location_template(
                object(),
                mock.Mock(),
                replace_call=created.append,
            )

        labels = [item[0] for item in created[0].items]
        self.assertEqual(len([label for label in labels if label.startswith("Use ")]), 5)
        self.assertTrue(any("custom" in label.lower() for label in labels))
        self.assertIn("Preview current announcement", labels)
        self.assertIn("Reset to default Full details", labels)
        self.assertEqual(labels[-1], "Back")

    def test_custom_component_toggle_rebuilds_menu_with_new_status(self):
        created = []
        with mock.patch.object(menus.menu, "Menu", self.FakeMenu), mock.patch.object(
            menus, "set_default_sounds"
        ):
            menus.configure_custom_location_template(
                object(),
                mock.Mock(),
                replace_call=created.append,
                selected_fields={"x"},
            )

            self.assertTrue(created[-1].items[0][0].startswith("X coordinate: Included"))
            created[-1].items[0][1]()

        self.assertTrue(created[-1].items[0][0].startswith("X coordinate: Excluded"))

    def test_in_game_navigation_replaces_location_states_instead_of_stacking(self):
        class ParentState:
            def __init__(self):
                self.substates = []

            def add_substate(self, substate):
                self.substates.append(substate)

            def replace_last_substate(self, substate):
                self.substates[-1] = substate

            def pop_last_substate(self):
                return self.substates.pop()

        class FakeGame:
            audio_mngr = SimpleNamespace(
                hrtf=SimpleNamespace(current_model="system default")
            )

            @staticmethod
            def toggle_item(name, *args):
                return name, lambda: None

        class FakeOptionsMenu(self.FakeMenu):
            turning_sensitivity_item_text = "Turning sensitivity"

            def set_music(self, path):
                self.music = path

        parent = ParentState()
        game = FakeGame()
        exit_options = parent.pop_last_substate

        with mock.patch.object(menus, "OptionsMenu", FakeOptionsMenu), mock.patch.object(
            menus.menu, "Menu", self.FakeMenu
        ), mock.patch.object(
            menus, "set_default_sounds"
        ), mock.patch.object(
            menus.server_config, "is_production_build", return_value=True
        ), mock.patch.object(
            menus.options, "get", side_effect=lambda key, default=None: default
        ):
            menus.options_menu(
                game,
                exit_options,
                replace_call=parent.add_substate,
                parent=parent,
                in_game=True,
            )
            self.assertEqual(len(parent.substates), 1)

            options_state = parent.substates[-1]
            location_item = next(
                item
                for item in options_state.items
                if callable(item[0])
                and item[0]().startswith("Configure location announcement")
            )
            location_item[1]()
            self.assertEqual(len(parent.substates), 2)

            location_state = parent.substates[-1]
            custom_item = next(
                item
                for item in location_state.items
                if isinstance(item[0], str) and "custom announcement" in item[0].lower()
            )
            custom_item[1]()
            self.assertEqual(len(parent.substates), 2)

            parent.substates[-1].items[0][1]()
            self.assertEqual(len(parent.substates), 2)

            parent.substates[-1].items[-1][1]()
            self.assertEqual(len(parent.substates), 2)
            self.assertTrue(parent.substates[-1].title.startswith("Configure location"))

            parent.substates[-1].items[0][1]()
            self.assertEqual(len(parent.substates), 1)

            parent.substates[-1].items[-1][1]()
            self.assertEqual(parent.substates, [])


if __name__ == "__main__":
    unittest.main()
