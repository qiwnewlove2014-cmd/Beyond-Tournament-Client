"""The cinema speaker's delay field owns its own range, and must keep it.

Reported from a live test room: typing 60 ms into Set Speaker Delay was refused
one keystroke at a time, while 0.2 was accepted and then stored as 0 ("0 ms
(aligned)"). The cause was a name, not arithmetic -- the flow reused the
``delay`` input stage, and the virtual input clamps a typed value to that
stage's range (0-0.5) on every keypress, because ``delay`` is the *megaphone*
speaker's propagation delay in SECONDS. So a cinema value above 0.5 could never
be typed at all, and a value below it was read as whole milliseconds and
rounded down to nothing.

Two halves of the contract are pinned here: the cinema stage accepts 0-100 ms
(even when the Server sends no range), and the stage the Server prompts with is
that same stage -- which is what a shared name quietly broke.
"""

import importlib.util
import os
import re
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from libs import event_handeler as event_handeler_module

HERE = os.path.dirname(os.path.abspath(__file__))
MENU_MANAGER = os.path.join(HERE, "..", "..", "server", "libs", "builder",
                            "menu_manager.ts")


def handeler():
    """An EventHandeler with only what ``make_input`` touches, plus a recorder."""
    recorded = {}

    def run(prompt, handeler=None, default="", min_val=None, max_val=None,
            msg_length=-1, multiline=False):
        recorded.update(prompt=prompt, min_val=min_val, max_val=max_val,
                        msg_length=msg_length, multiline=multiline)
        return lambda: True

    obj = object.__new__(event_handeler_module.EventHandeler)
    obj.game = SimpleNamespace(input=SimpleNamespace(run=run))
    obj.gameplay = SimpleNamespace(add_substate=lambda _state: None)
    obj.client = SimpleNamespace(send=lambda *args, **kwargs: None)
    return obj, recorded


def prompt_range(stage, **extra):
    """The range the Client would enforce for a stage the Server prompts with."""
    obj, recorded = handeler()
    data = {"elementId": "spk1", "elementType": "CinemaSpeaker", "stage": stage}
    data.update(extra)
    obj.make_input({"prompt": f"Speaker Delay ({stage})", "default": "",
                    "event": "cinema_speaker_input", "data": data})
    return recorded["min_val"], recorded["max_val"]


class CinemaDelayFieldTests(unittest.TestCase):
    """A trim a builder can actually type."""

    def test_the_cinema_trim_accepts_milliseconds(self):
        # The Server sends this range with the prompt, and the Client must use
        # it rather than falling into the seconds-wide `delay` stage.
        self.assertEqual(prompt_range("cinema_delay", min_val=0, max_val=100),
                         (0, 100))

    def test_the_range_holds_even_without_one_from_the_server(self):
        self.assertEqual(prompt_range("cinema_delay"), (0, 100))

    def test_the_megaphone_delay_stage_still_measures_seconds(self):
        """The stage that caused this is unchanged: it belongs to the megaphone."""
        self.assertEqual(prompt_range("delay"), (0.0, 0.5))


@unittest.skipUnless(os.path.exists(MENU_MANAGER),
                     "the Server side is not in this checkout")
class CinemaDelayPromptTests(unittest.TestCase):
    """The stage a builder is prompted with is one the Client can accept."""

    @classmethod
    def setUpClass(cls):
        with open(MENU_MANAGER, encoding="utf-8") as handle:
            cls.source = handle.read()
        start = cls.source.find("Speaker Delay (was")
        assert start != -1, "the cinema delay prompt is gone from menu_manager"
        cls.block = cls.source[start:start + 700]

    def test_the_prompt_names_the_stage_the_client_lets_a_builder_type(self):
        stage = re.search(r'stage:\s*"([A-Za-z_]+)"', self.block).group(1)
        # Whatever it is called, the Client must not clamp it to the megaphone
        # speaker's half-second: that is the bug this file exists to catch.
        self.assertNotEqual(stage, "delay")
        self.assertEqual(prompt_range(stage), (0, 100))

    def test_the_prompt_carries_the_range_the_builder_can_type(self):
        bounds = {key: int(re.search(rf'{key}:\s*(\d+)', self.block).group(1))
                  for key in ("min_val", "max_val")}
        self.assertEqual(bounds, {"min_val": 0, "max_val": 100})
        # The number in the prompt is the one the field accepts, so a builder is
        # never told to type something the input refuses.
        self.assertIn("0-100 ms", self.block)


if __name__ == "__main__":
    unittest.main()
