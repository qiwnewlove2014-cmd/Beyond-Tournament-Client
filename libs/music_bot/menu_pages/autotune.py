"""The Auto-Tune dials and the Harmony pages of `MusicBotMenus`.

The retune speed, the scale and its root, the amount, and the Harmony page with
its two copies and their interval pages -- the vocal chain, opened from Music
Bot Settings.

Moved out of `libs/music_bot/menus.py` on 2026-10-26 and mixed into
`MusicBotMenus`, so every caller still says `bot._menus().<method>()` and the
one-line shims in `controller.py` did not move. The state stays on
`MapMusicBot`: `self.` is this handler and `self._bot.` is the Music Bot. A page
that speaks through the module-level `speak` calls `self._say`, because
`libs/music_bot/menus.speak` is the patch point the menu tests use.

Pins: client/tests/test_music_bot_autotune.py and test_music_bot_menu_help.py.
Rules and the measured DSP: .agents/skills/music_bot_integration/ and
references/autotune.md.
"""


class AutoTuneMenus:
    """The Auto-Tune dials and the Harmony pages."""

    def _open_autotune_speed_menu(self):
        """How fast Auto-Tune walks the voice to a note.

        One list, fastest first: Instant is the effect (the voice snaps, and a
        slide between two notes is heard as a step), Very slow is a correction
        nobody notices until the note lands. Normal is the speed the project's
        own key changes move at, which is why it is the default. The current
        one is marked, as the equalizer profiles are.
        """
        from ... import menu as menu_mod, menus
        from .. import autotune
        gp = self._bot._find_gameplay()
        if not gp:
            return

        def go_back():
            gp.pop_last_substate()
            self._open_settings_menu()

        def pick(index):
            def apply():
                self._bot.set_autotune_speed(index)
                self._say(f"Retune speed: {self._bot.autotune_speed_label().lower()}.")
                m.speak_current_item()
            return apply

        def make_label(index, label):
            # A live label, the way the equalizer's profiles are drawn: the line
            # reads the bot each time it is spoken, so the "(active)" mark moves
            # to the speed just chosen instead of sitting on whichever one was
            # current when this list was built.
            def label_fn():
                current = getattr(self._bot, "autotune_speed",
                                  autotune.DEFAULT_SPEED)
                return f"{label} (active)" if index == current else label
            return label_fn

        items = []
        for index, (label, _glide, note) in enumerate(autotune.SPEEDS):
            items.append((make_label(index, label), pick(index), None, note))
        items.append(("Back", go_back, None,
                      "Returns to Music Bot Settings."))
        m = menu_mod.Menu(
            self._bot.game,
            f"Music Bot Retune Speed: {self._bot.autotune_speed_label()}",
            parrent=gp,
        )
        m.add_items(items)
        menus.set_default_sounds(m)
        gp.add_substate(m)

    def _open_autotune_scale_menu(self):
        """Which notes the voice may be moved to: a scale, and its key.

        The Settings list gains ONE line for all of this (`Auto-Tune Scale:`),
        because four lines of Auto-Tune in that list would bury the rest of the
        menu. The key is not part of the list here -- it is one line that opens
        its own twelve, and only while the scale has a key at all: a chromatic
        grid has a note on every semitone, so `Chromatic` names no key and the
        Root Note line is absent rather than present and dead. Choosing a scale
        rebuilds this list (the row comes or goes with the choice), so the key
        is there to press on the next key press instead of being discovered
        only by reopening the menu.
        """
        from ... import menu as menu_mod, menus
        from .. import autotune
        gp = self._bot._find_gameplay()
        if not gp:
            return

        def go_back():
            gp.pop_last_substate()
            self._open_settings_menu()

        def pick(mode_id, says):
            def apply():
                self._bot.set_autotune_mode(mode_id)
                self._say(f"Auto-tune scale: {self._bot.autotune_scale_label()}. "
                      f"{says}.")
                if autotune.is_chromatic(mode_id) == autotune.is_chromatic(mode):
                    m.speak_current_item()
                    return
                # The key row comes and goes with the scale, so the list this
                # press just changed is rebuilt: choosing Major lands the
                # cursor on the new `Root Note:` line, and choosing Chromatic
                # takes it away again rather than leaving a dead row behind.
                gp.pop_last_substate()
                self._open_autotune_scale_menu()
            return apply

        items = []
        mode = getattr(self._bot, "autotune_mode", autotune.DEFAULT_MODE)
        if not autotune.is_chromatic(mode):
            def go_root():
                gp.pop_last_substate()
                self._open_autotune_root_menu()

            root = getattr(self._bot, "autotune_root", autotune.DEFAULT_ROOT)
            items.append((f"Root Note: {autotune.root_name(root)}", go_root,
                          None,
                          "Which key the scale is in. This line exists only "
                          "while the scale has a key: a chromatic grid has a "
                          "note on every semitone, so it cannot be in D."))
        # Every scale is four fields, and the fourth is the sentence Tab reads
        # on that line: a name like "Harmonic Minor" is not an explanation, and
        # the person choosing a note set is exactly the person who has never
        # met the word.
        def make_label(mode_id, name):
            # The mark is read live, not baked into the line when the list is
            # built: choosing Minor over Major leaves this list standing, so a
            # frozen label would still say "Major (active)" and only a trip back
            # to Music Bot Settings would show the change.
            def label_fn():
                current = getattr(self._bot, "autotune_mode",
                                  autotune.DEFAULT_MODE)
                return (f"{name} (active)"
                        if mode_id == autotune.clamp_mode(current) else name)
            return label_fn

        for mode_id, name, _classes, note in autotune.SCALES:
            items.append((make_label(mode_id, name),
                          pick(mode_id, autotune.scale_says(mode_id)),
                          None, note))

        def clear_tuning():
            """Every Auto-Tune setting back to nothing, and the voice back.

                Asked for as "a button to clear the tuning": a player who has
                moved the scale, the key and the speed around wants one press
                that puts all three back where they started and takes the
                effect off, rather than three pages to undo by hand. The page
                it returns to rebuilds itself, so the three dials are gone from
                the settings list along with the switch being off.

                The HARMONY copies are left exactly as they are: they are a
                mode of their own, and a singer clearing the tuner must not
                lose the voices they set up beside it.
                """
            self._bot.set_autotune_mode(autotune.DEFAULT_MODE)
            self._bot.set_autotune_root(autotune.DEFAULT_ROOT)
            self._bot.set_autotune_speed(autotune.DEFAULT_SPEED)
            self._bot.set_autotune_amount(autotune.DEFAULT_AMOUNT)
            if getattr(self._bot, "autotune_enabled", False):
                self._bot.toggle_autotune()
            self._say("Auto-Tune cleared. Your own voice again, and the scale, the "
                  "key, the retune speed and the tune amount are back at their "
                  "defaults: Chromatic in C, Normal speed, 100%. Your harmony "
                  "voices are left as they were.")
            gp.pop_last_substate()
            self._open_settings_menu()

        items.append(("Clear Auto-Tune Tuning", clear_tuning, None,
                      "Puts everything Auto-Tune was set to back the way it "
                      "came -- Chromatic in C at Normal speed -- and turns it "
                      "off, so the next press is your own voice. The harmony "
                      "voices are not touched: they are a mode of their own."))
        items.append(("Back", go_back, None,
                      "Returns to Music Bot Settings."))
        m = menu_mod.Menu(
            self._bot.game,
            f"Music Bot Auto-Tune Scale: {self._bot.autotune_scale_label()}",
            parrent=gp,
        )
        m.add_items(items)
        menus.set_default_sounds(m)
        gp.add_substate(m)

    def _open_autotune_root_menu(self):
        """The twelve keys, in note order, the current one marked."""
        from ... import menu as menu_mod, menus
        from .. import autotune
        gp = self._bot._find_gameplay()
        if not gp:
            return

        def go_back():
            gp.pop_last_substate()
            self._open_autotune_scale_menu()

        def pick(index):
            def apply():
                self._bot.set_autotune_root(index)
                self._say(f"Key: {autotune.root_name(index)}.")
                m.speak_current_item()
            return apply

        def make_label(index, name):
            # Read live, like the scale list's: picking a key leaves this menu
            # open, so the "(active)" mark has to follow the choice by itself.
            def label_fn():
                current = autotune.clamp_root(
                    getattr(self._bot, "autotune_root", autotune.DEFAULT_ROOT))
                return f"{name} (active)" if index == current else name
            return label_fn

        items = []
        for index, name in enumerate(autotune.ROOT_NAMES):
            items.append((make_label(index, name),
                          pick(index), None,
                          f"Puts the scale in this key: the same notes, "
                          f"started on {name} instead."))
        items.append(("Back", go_back, None,
                      "Returns to the Auto-Tune Scale list."))
        opened_on = autotune.clamp_root(getattr(self._bot, "autotune_root",
                                                 autotune.DEFAULT_ROOT))
        m = menu_mod.Menu(
            self._bot.game,
            f"Music Bot Auto-Tune Root: {autotune.root_name(opened_on)}",
            parrent=gp,
        )
        m.add_items(items)
        menus.set_default_sounds(m)
        gp.add_substate(m)

    def _open_autotune_amount_menu(self):
        """How much of the correction is used: five steps of percent.

        The one Auto-Tune choice that is taste rather than physics. The scale
        decides how far the voice may move and Retune Speed how fast it gets
        there, but a singer may legitimately want half of it -- close to the
        note with the human wobble left in -- and no note set can express that.
        100% is the plain correction and the default; past it the dial spends
        the tuner's own lag instead of a wider move (`autotune._apply_amount`),
        so 200% is a hard lock that still lands only on the scale's notes.

        Six rows are the quick answers and `Custom Percentage` is the rest of
        the range: it opens the client's own text field (`game.input.run`) for
        any whole number, because "63%" is a thing a player can want and no
        list of six can hold every percentage.
        """
        from ... import menu as menu_mod, menus
        from .. import autotune
        gp = self._bot._find_gameplay()
        if not gp:
            return

        def go_back():
            gp.pop_last_substate()
            self._open_settings_menu()

        def pick(percent):
            def apply():
                self._bot.set_autotune_amount(percent)
                self._say(f"Tune amount: {autotune.amount_label(percent)}. "
                      f"{autotune.amount_note(percent)}")
                m.speak_current_item()
            return apply

        def make_label(percent):
            # Read live, like the scale and key lists: the mark follows the
            # choice on the list that is already open.
            def label_fn():
                current = autotune.clamp_amount(
                    getattr(self._bot, "autotune_amount",
                            autotune.DEFAULT_AMOUNT))
                label = autotune.amount_label(percent)
                return f"{label} (active)" if percent == current else label
            return label_fn

        def current_amount():
            return autotune.clamp_amount(
                getattr(self._bot, "autotune_amount", autotune.DEFAULT_AMOUNT))

        def submitted_amount(text):
            """Take what was typed, or say why it was not taken.

                The field refuses anything outside 0..200 as it is typed, so a
                refusal here is text that was not a number at all -- and it
                says the value did not change instead of guessing what "half"
                meant.

                **The field is a substate and has to be taken off the stack on
                EVERY exit, an Escape with nothing typed included.** Its `ck`
                answers True for every frame it is open (that is how it keeps
                the player's keys), so a handler that forgets this leaves the
                field on top of the stack for good: no key reaches the menu or
                the player again and the game looks frozen. The search box, the
                paste-a-link field and the playlist prompt pop it in the same
                place for the same reason.
                """
            gp.pop_last_substate()
            text = "" if text is None else text
            if not str(text).strip():
                self._say(f"Tune amount unchanged, {autotune.amount_label(current_amount())}.")
                return
            value = autotune.parse_amount(text)
            if value is None:
                self._say(f"'{text}' is not a percentage, so the tune amount is "
                      f"still {autotune.amount_label(current_amount())}.")
                return
            self._bot.set_autotune_amount(value)
            self._say(f"Tune amount: {autotune.amount_label(value)}. "
                  f"{autotune.amount_note(value)}")

        def go_custom():
            # The six rows are the common answers; a percentage between them
            # is what a player who asks for "63%" actually wants, so the row
            # opens the client's own text field (the one the search box uses)
            # rather than adding a seventh step nobody picked. The menu is left
            # underneath and comes back on Enter or Escape.
            gp.add_substate(self._bot.game.input.run(
                f"Enter a tune amount from 0 to 200 percent "
                f"(currently {autotune.amount_label(current_amount())}):",
                handeler=submitted_amount,
                min_val=0, max_val=autotune.MAX_AMOUNT, msg_length=3,
                multiline=False,
            ))

        items = [(make_label(percent), pick(percent), None, note)
                 for percent, note in autotune.TUNE_AMOUNTS]
        items.append(("Custom Percentage", go_custom, None,
                      "Type the amount yourself, from 0 to 200. Any whole "
                      "number works, so 63% is 63% and not the step beside it; "
                      "above 100 the extra is a harder lock, not a wider move."))
        items.append(("Back", go_back, None,
                      "Returns to Music Bot Settings."))
        m = menu_mod.Menu(
            self._bot.game,
            f"Music Bot Tune Amount: {self._bot.autotune_amount_label()}",
            parrent=gp,
        )
        m.add_items(items)
        menus.set_default_sounds(m)
        gp.add_substate(m)

    def _open_harmony_menu(self):
        """The Harmony page: the two copies, and which interval each sings.

        Two lines used to sit in Music Bot Settings (`Harmony 1:` and `Harmony
        2:`), and every harmony feature added since wanted a third. A page of
        its own holds them instead: the Settings list keeps one line
        (`Harmony:`), which says in a few words whether any of it is on, and the
        page under it is where the choices -- and whatever comes next -- live.

        The line is NOT one of Auto-Tune's dials. Harmony is a mode of its own:
        the tuner's switch adds and removes the three dials above it and never
        this line, and the copies sing with the tuner off (at their plain
        intervals) exactly as with it on.
        """
        from ... import menu as menu_mod, menus
        gp = self._bot._find_gameplay()
        if not gp:
            return

        def go_back():
            gp.pop_last_substate()
            self._open_settings_menu()

        def go_first():
            gp.pop_last_substate()
            self._open_autotune_harmony_menu()

        def go_second():
            gp.pop_last_substate()
            self._open_autotune_harmony_menu(second=True)

        m = menu_mod.Menu(
            self._bot.game,
            f"Music Bot Harmony: {self._bot.autotune_harmony_summary()}",
            parrent=gp,
        )
        m.add_items([
            (lambda: f"Harmony 1: {self._bot.autotune_harmony_label()}",
             go_first, None,
             "What the first copy sings: Off, a third, fifth, sixth, seventh "
             "or octave ABOVE your voice, or a bass third, fifth or octave "
             "BELOW it. With Auto-Tune tuning you the interval comes from your "
             "scale, so the copy is always in key; with Auto-Tune off it is the "
             "plain interval, measured from your own voice. Either way the "
             "tuner's switch never silences it."),
            (lambda: (f"Harmony 2: "
                      f"{self._bot.autotune_harmony_label(second=True)}"),
             go_second, None,
             "The second copy, on another interval of the same scale. A third "
             "above and a fifth above turn one voice into a three-note chord; "
             "a bass copy under either of them adds a low voice. Off leaves "
             "Harmony 1 singing alone."),
            ("Back", go_back, None,
             "Returns to Music Bot Settings."),
        ])
        menus.set_default_sounds(m)
        gp.add_substate(m)

    def _open_autotune_harmony_menu(self, second=False):
        """Which interval one harmony copy sings: a third, a fifth, an octave.

        Two copies, so two of these pages (`Harmony 1:` and `Harmony 2:` on the
        Harmony page). The interval is counted in the scale, so the copy is
        always on a note the scale has -- a third above C is E, above D it is F
        -- and `Chromatic` falls back to the major scale's spelling of each
        interval, because a grid with a note every semitone has no key to
        harmonise in. With Auto-Tune OFF there is no scale in play at all: the
        copy is the plain size of the interval (`harmony.harmony_plain_offset`),
        sung from the voice as it is, because Harmony is a mode of its own. The
        rows below `Octave` take the copy DOWN instead: a bass
        voice, mixed quieter than the lead and low-passed, so it hums under the
        words rather than doubling them (`harmony.harmony_share`).

        The copy is part of the ONE broadcast voice: it is mixed into the same
        20 ms frame (`libs/music_bot/harmony.py`'s `HarmonyVoice`), so a
        listener hears it
        without anything extra being sent, and a PA slot or a cinema room still
        gets exactly the frames it always did.
        """
        from ... import menu as menu_mod, menus
        from .. import harmony
        gp = self._bot._find_gameplay()
        if not gp:
            return

        def go_back():
            gp.pop_last_substate()
            self._open_harmony_menu()

        def pick(interval_id):
            def apply():
                self._bot.set_autotune_harmony(interval_id, second=second)
                self._say(f"Harmony {'2' if second else '1'}: "
                      f"{self._bot.autotune_harmony_label(second=second)}. "
                      f"{harmony.harmony_note(interval_id)}")
                m.speak_current_item()
            return apply

        def make_label(interval_id):
            # Read live, like the scale, key and amount lists: the mark follows
            # the choice on the list that is already open.
            def label_fn():
                attribute = "autotune_harmony2" if second else "autotune_harmony"
                current = harmony.clamp_harmony(
                    getattr(self._bot, attribute, harmony.DEFAULT_HARMONY))
                label = harmony.harmony_label(interval_id)
                return f"{label} (active)" if interval_id == current else label
            return label_fn

        items = [(make_label(interval_id), pick(interval_id), None, note)
                 for interval_id, _label, _semitones, note
                 in harmony.HARMONY_INTERVALS]
        items.append(("Back", go_back, None,
                      "Returns to the Harmony page."))
        m = menu_mod.Menu(
            self._bot.game,
            f"Music Bot Harmony {'2' if second else '1'}: "
            f"{self._bot.autotune_harmony_label(second=second)}",
            parrent=gp,
        )
        m.add_items(items)
        menus.set_default_sounds(m)
        gp.add_substate(m)
