"""Favorites: the player's shortlist, and the sound they built getting into it.

The feature is one small idea in two halves, so the checks come in two halves
too:

* the **store** (`voice_effects/favourites.py`) -- what an entry is, that a
  preset and a board snapshot are saved differently and mean the same thing on
  the page, that saving the same sound twice is one row, that a list of twenty
  is a list of twenty, and that a settings file edited by hand cannot make the
  page raise;
* the **round trip** -- a design made on a board, saved, changed, and recalled
  comes back exactly as it was, which is the whole reason a snapshot exists
  rather than a second "favourite this board" toggle.

`options` is the one seam that keeps this out of the real settings file: every
module reads the same `libs.options` object, so patching its `get`/`set` here
gives the whole feature an in-memory file for the length of a test.
"""

import unittest
from unittest import mock

from libs.music_bot.voice_effects import builder
from libs.music_bot.voice_effects import custom
from libs.music_bot.voice_effects import favourites
from libs.music_bot.voice_effects import pan
from libs.music_bot.voice_effects import vocoder
from libs.music_bot import voice_effects


class Prefs:
    """An `options` module in memory: get, set, and nothing on disk."""

    def __init__(self, values=None):
        self.values = dict(values or {})

    def get(self, key, default=None):
        return self.values.get(key, default)

    def set(self, key, value, autosave=True):
        self.values[key] = value


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.prefs = Prefs()
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(favourites.options, "get",
                          side_effect=self.prefs.get).start()
        mock.patch.object(favourites.options, "set",
                          side_effect=self.prefs.set).start()

    def preset(self, effect_id):
        return {"kind": "preset", "id": effect_id}


class TheStoreTests(StoreTest):
    def test_a_preset_is_one_row_and_the_count_says_so(self):
        self.assertEqual(favourites.count(), 0)
        self.assertEqual(favourites.add(self.preset("amp")), "added")
        self.assertEqual(favourites.count(), 1)
        self.assertEqual([entry["id"] for entry in favourites.entries()],
                         ["amp"])

    def test_saving_the_same_preset_twice_is_one_row(self):
        favourites.add(self.preset("amp"))
        self.assertEqual(favourites.add(self.preset("amp")), "exists")
        self.assertEqual(favourites.count(), 1)

    def test_the_order_is_the_order_they_were_saved(self):
        for effect_id in ("amp", "fuzz", "plate"):
            favourites.add(self.preset(effect_id))
        self.assertEqual([entry["id"] for entry in favourites.entries()],
                         ["amp", "fuzz", "plate"])

    def test_off_is_not_a_sound_to_save(self):
        self.assertEqual(favourites.add(self.preset("off")), "unknown")
        self.assertEqual(favourites.count(), 0)
        self.assertIsNone(favourites.snapshot("off"))

    def test_an_id_the_game_never_had_is_refused(self):
        self.assertEqual(favourites.add(self.preset("not_a_pedal")),
                         "unknown")
        self.assertEqual(favourites.count(), 0)

    def test_the_list_holds_twenty_and_then_says_so(self):
        ids = [row[0] for row in voice_effects.VOICE_EFFECTS
               if row[0] != voice_effects.DEFAULT_EFFECT][:favourites.MAX]
        self.assertEqual(len(ids), favourites.MAX)
        for effect_id in ids:
            self.assertEqual(favourites.add(self.preset(effect_id)), "added")
        self.assertEqual(favourites.add(self.preset("plate")), "full")
        self.assertEqual(favourites.count(), favourites.MAX)

    def test_a_broken_settings_file_reads_as_an_empty_list(self):
        self.prefs.values["music_bot_voice_favourites"] = "not a list"
        self.assertEqual(favourites.entries(), ())
        # ... and a list of junk is junk rows, not an exception.
        self.prefs.values["music_bot_voice_favourites"] = [
            5, {"kind": "preset"}, {"kind": "board", "id": "amp"},
            {"kind": "preset", "id": "gone_since_a_patch"}]
        self.assertEqual(favourites.entries(), ())

    def test_a_preset_removed_since_it_was_saved_leaves_the_page(self):
        self.prefs.values["music_bot_voice_favourites"] = [
            {"kind": "preset", "id": "amp"},
            {"kind": "preset", "id": "was_a_pedal_once"}]
        self.assertEqual(len(favourites.entries()), 1)
        self.assertEqual(favourites.entries()[0]["id"], "amp")

    def test_the_line_says_the_name_and_tab_says_what_it_is(self):
        entry = self.preset("amp")
        self.assertEqual(favourites.label(entry),
                         voice_effects.effect_label("amp"))
        self.assertIn("guitar amp", favourites.describe(entry).lower())
        self.assertIn("favourites", favourites.describe(entry).lower())

    def test_remove_takes_one_row_and_clear_takes_the_rest(self):
        favourites.add(self.preset("amp"))
        favourites.add(self.preset("fuzz"))
        self.assertTrue(favourites.remove(
            favourites.key(self.preset("amp"))))
        self.assertFalse(favourites.remove(
            favourites.key(self.preset("amp"))))
        self.assertEqual([entry["id"] for entry in favourites.entries()],
                         ["fuzz"])
        self.assertEqual(favourites.clear(), 1)
        self.assertEqual(favourites.count(), 0)


class TheSnapshotTests(StoreTest):
    """The designer's half: a board saved as what it sounds like."""

    def drive_choice(self, index=1):
        return custom.choices("drive")[index][1]

    def test_a_preset_snapshot_is_just_its_id(self):
        self.assertEqual(favourites.snapshot("amp"),
                         {"kind": "preset", "id": "amp"})

    def test_an_amp_snapshot_carries_every_knob_and_a_name(self):
        custom.set_value("drive", self.drive_choice())
        entry = favourites.snapshot(custom.EFFECT_ID)
        self.assertEqual(entry["kind"], "board")
        self.assertEqual(entry["id"], "custom")
        self.assertEqual(set(entry["values"]), set(custom.values()))
        self.assertEqual(entry["values"]["drive"],
                         custom.values()["drive"])
        self.assertTrue(entry["label"])

    def test_the_same_board_twice_is_one_row_but_two_settings_are_two(self):
        custom.set_value("drive", self.drive_choice(1))
        first = favourites.snapshot(custom.EFFECT_ID)
        self.assertEqual(favourites.add(first), "added")
        self.assertEqual(favourites.add(favourites.snapshot("custom")),
                         "exists")
        custom.set_value("drive", self.drive_choice(2))
        self.assertEqual(favourites.add(favourites.snapshot("custom")),
                         "added")
        self.assertEqual(favourites.count(), 2)

    def test_recalling_a_saved_amp_writes_the_knobs_back(self):
        original = self.drive_choice(2)
        custom.set_value("drive", original)
        entry = favourites.snapshot("custom")
        custom.set_value("drive", self.drive_choice(4))
        self.assertNotEqual(custom.values()["drive"], original)
        self.assertEqual(favourites.restore(entry), "custom")
        self.assertEqual(custom.values()["drive"], original)

    def test_a_recalled_sound_reads_as_active_and_a_changed_one_does_not(self):
        custom.set_value("drive", self.drive_choice(1))
        entry = favourites.snapshot("custom")
        self.assertTrue(favourites.matches(entry))
        custom.set_value("drive", self.drive_choice(3))
        self.assertFalse(favourites.matches(entry))

    def test_a_snapshot_from_before_a_knob_existed_still_recalls(self):
        custom.set_value("drive", self.drive_choice(1))
        entry = favourites.snapshot("custom")
        # A file written by an older build: one knob, no others.
        entry["values"] = {"drive": entry["values"]["drive"]}
        self.assertEqual(favourites.restore(entry), "custom")
        self.assertEqual(set(custom.values()), set(dict(
            (row[0], row[4]) for row in custom.PARAMS)))

    def test_the_pan_delay_saves_both_of_its_settings(self):
        pan.set_delay_ms(450)
        pan.set_motion("off")
        entry = favourites.snapshot("pan_delay")
        self.assertEqual(entry["values"], {"delay_ms": 450, "motion": "off"})
        pan.set_delay_ms(300)
        pan.set_motion("stereo")
        self.assertEqual(favourites.restore(entry), "pan_delay")
        self.assertEqual(pan.delay_ms(), 450)
        self.assertEqual(pan.motion(), "off")

    def test_a_vocoder_snapshot_recalls_a_locked_carrier_not_noise(self):
        vocoder.set_value("mode", "tune")
        entry = favourites.snapshot("vox_lab")
        vocoder.set_values({"mode": "tune", "wave": "noise"})
        self.assertEqual(vocoder.values()["wave"], "saw")
        self.assertEqual(favourites.restore(entry), "vox_lab")
        self.assertEqual(vocoder.values()["wave"], entry["values"]["wave"])

    def test_the_custom_voice_board_snapshot_is_its_slots(self):
        builder.set_value("space", builder.choices("space")[1][1])
        entry = favourites.snapshot(builder.EFFECT_ID)
        self.assertEqual(entry["values"], builder.values())
        self.assertIn(builder.summary(), favourites.label(entry))


if __name__ == "__main__":
    unittest.main()
