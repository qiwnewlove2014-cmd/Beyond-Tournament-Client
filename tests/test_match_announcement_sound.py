"""The notification sound of the line that opens a buffer.

``buffer.add_item`` plays a line's sound and then files the line away in its
buffer; a buffer that does not exist yet is created first, and that recursive
call dropped the ``sound`` argument -- so the very first line to open a buffer
lost its notification (the round-start sound on a client whose ``match``
buffer had not been created yet).

The words are the other half of the same report: an announcement a spectator
never received was missing from their ears *and* their buffer, which was a
Server-side rule about who a match speaks to (``Game.audience``,
``server/tools/match_audience_test.js``). Here the lines are handed to
``add_item`` directly, so what is pinned is what the client does with a line
once it arrives: the sound plays, and the line is readable in its buffer and
in ``main``.

TTS is left out of these tests (``speak=False``) on purpose -- this file is
about the sound and the filing, not the voice.
"""

import unittest

from libs import buffer as buffer_module


class FakeSoundgroup:
    def __init__(self):
        self.played = []

    def play(self, sound, *args, **kwargs):
        self.played.append(sound)


class FakeGame:
    def __init__(self):
        self.direct_soundgroup = FakeSoundgroup()


class MatchAnnouncementSoundTests(unittest.TestCase):
    NAME = "match-announce-test"

    def setUp(self):
        self.game = FakeGame()
        # add_item works on the module's own buffer list, so put it back the
        # way it was: other tests in the same run share this module.
        self._saved_buffers = list(buffer_module.buffers)
        self._main_items = len(buffer_module.buffers[0].items)

    def tearDown(self):
        buffer_module.buffers[:] = self._saved_buffers
        del buffer_module.buffers[0].items[self._main_items :]

    def _buffer(self):
        for item in buffer_module.buffers:
            if item.name == self.NAME:
                return item
        return None

    def test_the_line_that_opens_a_buffer_keeps_its_sound(self):
        self.assertIsNone(self._buffer())
        buffer_module.add_item(
            self.game,
            self.NAME,
            "Round 7!",
            speak=False,
            sound="rounds/theme5/start.ogg",
        )
        self.assertEqual(self.game.direct_soundgroup.played, ["rounds/theme5/start.ogg"])
        created = self._buffer()
        self.assertIsNotNone(created)
        self.assertEqual([item.text for item in created.items], ["Round 7!"])

    def test_the_opening_line_is_readable_in_its_buffer_and_in_main(self):
        buffer_module.add_item(
            self.game,
            self.NAME,
            "End of round 6! ann: 900 points",
            speak=False,
            sound="rounds/theme5/end.ogg",
        )
        self.assertIn("End of round 6! ann: 900 points", [i.text for i in self._buffer().items])
        self.assertIn(
            "End of round 6! ann: 900 points",
            [i.text for i in buffer_module.buffers[0].items],
        )

    def test_a_line_in_an_existing_buffer_still_plays_its_sound(self):
        buffer_module.add_item(self.game, self.NAME, "one", speak=False)
        buffer_module.add_item(
            self.game,
            self.NAME,
            "Round 8!",
            speak=False,
            sound="rounds/theme5/start.ogg",
        )
        self.assertEqual(self.game.direct_soundgroup.played, ["rounds/theme5/start.ogg"])
        self.assertEqual([i.text for i in self._buffer().items], ["one", "Round 8!"])

    def test_a_line_with_no_sound_plays_nothing(self):
        buffer_module.add_item(self.game, self.NAME, "Game starting!", speak=False)
        self.assertEqual(self.game.direct_soundgroup.played, [])

    def test_a_muted_buffer_stays_silent(self):
        buffer_module.add_item(self.game, self.NAME, "one", speak=False)
        created = self._buffer()
        created.muted = True
        buffer_module.add_item(
            self.game,
            self.NAME,
            "Round 9!",
            speak=False,
            sound="rounds/theme5/start.ogg",
        )
        self.assertEqual(self.game.direct_soundgroup.played, [])
        # ...and unmuting brings the notification back.
        created.muted = False
        buffer_module.add_item(
            self.game,
            self.NAME,
            "Round 10!",
            speak=False,
            sound="rounds/theme5/start.ogg",
        )
        self.assertEqual(self.game.direct_soundgroup.played, ["rounds/theme5/start.ogg"])

    def test_the_sound_is_not_played_twice_for_one_line(self):
        buffer_module.add_item(
            self.game,
            self.NAME,
            "Round 7!",
            speak=False,
            sound="rounds/theme5/start.ogg",
        )
        self.assertEqual([i.text for i in self._buffer().items], ["Round 7!"])
        self.assertEqual(self.game.direct_soundgroup.played, ["rounds/theme5/start.ogg"])


if __name__ == "__main__":
    unittest.main()
