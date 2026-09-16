"""Pasting a long text into a field, and what happens to its line breaks.

Reported from a live staff session: a Message of the Day written in another
window could not be pasted into the Staff Form at all -- the answer came back
"this field cannot contain line breaks", because the Server refuses a line
break in every form field. A Message of the Day is a *body* of text, though,
and the file it goes to has always held several lines (the desktop admin tool
has never written it any other way).

So the rule belongs to the field: a field marked multiline keeps a pasted line
break, any other field joins them into spaces rather than let the Server throw
the whole answer away after the confirm step. These tests pin the client half
-- the summary a screen reader hears instead of the whole paste, the cap that
says when it bit, and the wiring that carries the flag from the Server's
prompt into the field it describes.
"""

import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from libs import event_handeler as event_handeler_module
from libs.virtual_input import Virtual_input

MOTD = "Community match tonight at 8 PM\n\nBring your own shield\nGames start in the plaza"


class FakeClock:
    def __init__(self):
        self.elapsed = 0.0

    def restart(self):
        self.elapsed = 0.0


class FakeNetwork:
    def __init__(self):
        self.sent = []

    def send(self, channel, event, data=None, reliable=True):
        self.sent.append((channel, event, data))


class FakeSoundGroup:
    def __init__(self):
        self.played = []

    def play(self, path, *args, **kwargs):
        self.played.append(path)


class FakeGame:
    def __init__(self):
        self.network = FakeNetwork()
        self.input_history = [""]
        self.direct_soundgroup = FakeSoundGroup()

    def new_clock(self):
        return FakeClock()


def make_input(**kwargs):
    return Virtual_input(FakeGame(), **kwargs)


class PasteTextTests(unittest.TestCase):
    """One paste is one edit, and the field decides what a line break is."""

    def setUp(self):
        self.spoken = []
        patch = mock.patch(
            "libs.virtual_input.speak",
            lambda *args, **kwargs: self.spoken.append(args[0] if args else ""),
        )
        patch.start()
        self.addCleanup(patch.stop)

    def summary(self):
        return " ".join(self.spoken)

    def test_a_multiline_field_keeps_the_pasted_message(self):
        v = make_input(multiline=True)
        v.paste_text(MOTD)
        self.assertEqual(v.current_string, MOTD)
        self.assertEqual(len(v.line_list), 4)
        self.assertEqual(v.line_num, 3)
        self.assertEqual(v._cursor, len(MOTD))

    def test_a_one_line_field_joins_the_lines_instead(self):
        """The Server refuses a line break there, so the paste is one line."""
        v = make_input(multiline=False)
        v.paste_text(MOTD)
        self.assertNotIn("\n", v.current_string)
        self.assertEqual(v.current_string, MOTD.replace("\n\n", " ").replace("\n", " "))
        self.assertIn("line breaks joined into spaces", self.summary())

    def test_the_message_is_never_spoken_line_by_line(self):
        v = make_input(multiline=True)
        v.paste_text(MOTD)
        self.assertEqual(len(self.spoken), 1)
        self.assertIn("pasted 4 lines", self.summary())
        self.assertIn(f"{len(MOTD)} characters", self.summary())
        self.assertNotIn("Bring your own shield", self.summary())

    def test_the_paste_lands_where_the_cursor_is(self):
        v = make_input(initial_msg="hello world")
        v._cursor = 5
        v.paste_text("there ")
        self.assertEqual(v.current_string, "hellothere  world")
        self.assertEqual(v._cursor, 11)

    def test_a_pasted_crlf_is_one_line_ending(self):
        v = make_input(multiline=True)
        v.paste_text("one\r\ntwo\rthree")
        self.assertEqual(v.current_string, "one\ntwo\nthree")

    def test_a_tab_becomes_a_space_and_control_characters_are_dropped(self):
        v = make_input(multiline=True)
        v.paste_text("a\tb\x00c\x07d")
        self.assertEqual(v.current_string, "a bcd")

    def test_nothing_worth_pasting_says_so(self):
        v = make_input(multiline=True)
        v.paste_text("\x00\x01")
        self.assertEqual(v.current_string, "")
        self.assertIn("nothing to paste", self.summary())

    def test_a_field_nobody_described_keeps_the_lines_by_default(self):
        """A paste is never destroyed unless the field says it is one line."""
        v = make_input()
        self.assertTrue(v.multiline)
        v.paste_text("one\ntwo")
        self.assertEqual(v.current_string, "one\ntwo")

    def test_the_field_rule_can_be_set_when_the_field_opens(self):
        v = make_input()
        v.run("Ban reason", multiline=False)
        self.assertFalse(v.multiline)
        v.paste_text("one\ntwo")
        self.assertEqual(v.current_string, "one two")


class PasteCapTests(unittest.TestCase):
    """A cap that bites must be heard, or the end of the message is lost."""

    def setUp(self):
        self.spoken = []
        patch = mock.patch(
            "libs.virtual_input.speak",
            lambda *args, **kwargs: self.spoken.append(args[0] if args else ""),
        )
        patch.start()
        self.addCleanup(patch.stop)

    def test_a_paste_longer_than_the_field_is_cut_and_says_so(self):
        v = make_input(multiline=True, msg_length=10)
        v.paste_text("abcdefghijklmno")
        self.assertEqual(v.current_string, "abcdefghij")
        self.assertIn("only the first 10 characters fit", " ".join(self.spoken))

    def test_a_field_that_is_already_full_pastes_nothing(self):
        v = make_input(multiline=True, msg_length=5, initial_msg="12345")
        v.paste_text("more")
        self.assertEqual(v.current_string, "12345")
        self.assertIn("this field is full at 5 characters", " ".join(self.spoken))
        self.assertEqual(v.game.direct_soundgroup.played, ["ui/error.ogg"])

    def test_a_paste_that_fits_is_not_announced_as_cut(self):
        v = make_input(multiline=True, msg_length=2000)
        v.paste_text(MOTD)
        self.assertNotIn("fit", " ".join(self.spoken))


class TypingNoticeTests(unittest.TestCase):
    """A paste is still typing: it opens the notice and sends the tick once."""

    def setUp(self):
        patch = mock.patch("libs.virtual_input.speak", lambda *args, **kwargs: None)
        patch.start()
        self.addCleanup(patch.stop)

    def test_a_paste_opens_the_typing_notice_once(self):
        v = make_input(multiline=True)
        v.paste_text(MOTD)
        self.assertTrue(v.typing)
        typing_events = [entry for entry in v.game.network.sent
                         if entry[1] == "set_typing"]
        self.assertEqual(len(typing_events), 1)
        self.assertEqual(typing_events[0][2], {"typing": True})

    def test_a_pasted_slash_command_is_not_announced_as_typing(self):
        v = make_input(multiline=True)
        v.paste_text("/setmotd hello")
        self.assertFalse(v.typing)
        self.assertEqual(v.game.network.sent, [])


class StaffFormMultilineWiringTests(unittest.TestCase):
    """The flag travels from the Server's prompt to the field it describes."""

    def make_handler(self):
        recorded = {}

        def run(prompt, handeler=None, default="", min_val=None, max_val=None,
                msg_length=-1, multiline=False):
            recorded.update(prompt=prompt, msg_length=msg_length,
                            multiline=multiline)
            return lambda: True

        obj = object.__new__(event_handeler_module.EventHandeler)
        obj.game = SimpleNamespace(input=SimpleNamespace(run=run))
        obj.gameplay = SimpleNamespace(add_substate=lambda _state: None)
        obj.client = SimpleNamespace(send=lambda *args, **kwargs: None)
        return obj, recorded

    def test_a_body_of_text_field_from_the_staff_form_says_so(self):
        obj, recorded = self.make_handler()
        obj.make_input({
            "prompt": "Message of the Day",
            "event": "staff_menu_command_input",
            "data": {"type": "staff_command", "stage": "message",
                     "msg_length": 2000, "multiline": True},
        })
        self.assertIs(recorded["multiline"], True)
        self.assertEqual(recorded["msg_length"], 2000)

    def test_a_field_the_server_did_not_describe_keeps_todays_behaviour(self):
        """No flag means an older Server, which never had a body-of-text field."""
        obj, recorded = self.make_handler()
        obj.make_input({
            "prompt": "Enter some text to search your buffer for",
            "event": "builder_input",
            "data": {"type": "zone", "stage": "find"},
        })
        self.assertIs(recorded["multiline"], True)

    def test_a_one_line_staff_form_field_joins_a_pasted_break(self):
        obj, recorded = self.make_handler()
        obj.make_input({
            "prompt": "Server announcement",
            "event": "staff_menu_command_input",
            "data": {"type": "staff_command", "stage": "message",
                     "msg_length": 1000, "multiline": False},
        })
        self.assertIs(recorded["multiline"], False)


if __name__ == "__main__":
    unittest.main()
