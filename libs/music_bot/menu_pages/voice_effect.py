"""The Voice Effect pages of `MusicBotMenus`: the pedals and the three boards.

`_open_voice_effect_menu` is the family list and, given a family id, that
family's pedals. `_open_voice_builder_menu`, `_open_builder_recipes_menu`,
`_open_builder_clear_menu`, `_open_custom_effect_menu` and
`_open_custom_param_menu` are the Custom Voice board, its fifty starting
points, its Clear question and the knob pages every board shares;
`_open_vocoder_lab_menu` is the Vocoder Lab board, the Developer/Contributor
carrier page at the end of that family. `_open_voice_tone_menu` and
`_VoiceToneSlider` are the channel strip one preset carries of its own -- the
low cut, four bands, the mix and the level (`voice_effects/tone.py`): a
`state.State`, not a page, for the same reason
the Music Bot's own equalizer is one -- a slider is Tab, arrows and speech,
not a list of steps.

Moved out of `libs/music_bot/menus.py` on 2026-10-26 and mixed into
`MusicBotMenus`, so every caller still says `bot._menus().<method>()` and the
one-line shims in `controller.py` did not move. The state stays on
`MapMusicBot`: `self.` is this handler and `self._bot.` is the Music Bot. A
page that speaks through the module-level `speak` calls `self._say`, because
`libs/music_bot/menus.speak` is the patch point the menu tests use.

Pins: client/tests/test_music_bot_menu_help.py, test_voice_effects.py,
test_music_bot_autotune.py (the harmony and voice pages) and
test_music_bot_gate*.py. Rules: .agents/skills/music_bot_integration/.
"""

import pygame

from ... import state
from .settings import _speak

#: What Tab says about the `Tone:` line, on every page that offers it.
_TONE_NOTE = (
    "The channel strip for this one effect: Low Cut takes the rumble under "
    "the voice, then four tone bands -- Low for the chest and the room, "
    "Body for the mud under the words, Presence for the clarity of the "
    "words, Air for the breath on top -- then De-ess for the spit on an s, "
    "High Cut for where the top stops, Mix for how much of the effect is "
    "heard, and Level for how loud it leaves. M speaks the level of the "
    "last frame, and Flat is the pedal exactly as it was made.")


class VoiceEffectMenus:
    """The Voice Effect page, its families and the three boards."""

    def _open_voice_effect_menu(self, group=None, standalone=None):
        """The pedals a player can put their broadcast voice through.

        Two levels: the families, then the pedals in one of them. A hundred
        and forty-four rows in one list is a list you scroll rather than read,
        and a name
        like `Plate` says nothing until something explains it -- a family is
        where that explanation fits without every line carrying all of it. Off
        stays on the top page, so turning the effect off never means drilling
        into a family.

        The effect is a WRAPPER around the line a press already has, so choosing
        one here does not replace Auto-Tune or Harmony -- it is measured after
        them -- and the preset is read per frame, so it can be changed
        mid-sentence (crossfaded, never cut).

        Amp & Drive ends with one line that is not a pedal: `Custom Amp` opens
        the board (`_open_custom_effect_menu`), where the drive, the character,
        the three tone knobs, the gate, the air, the supply's sag and the
        wobble are the player's own. It sits at the
        bottom of that family and nowhere else, because a board with no amp
        around it is a page of filters with nothing to filter.

        This page also ends with the two rows that are not families, because
        each is one choice rather than a category
        (`voice_effects.catalogue.STANDALONE_EFFECTS`), and each opens a page
        rather than a list of pedals:

        - `Pan Delay` (`_open_pan_delay_menu`) is a delay whose repeats cross
          the listener's head, and its delay is TYPED, so it cannot be a row of
          fixed steps the way a family's presets are. It sits beside the
          families rather than inside Motion so the one preset with a setting
          of its own is where a player looking for it opens the page.
        - `Custom Voice` (`_open_voice_builder_menu`) assembles a whole effect
          out of the blocks the families are made of -- eleven slots,
          seventy-eight choices, and fifty ready-made voices on the board's
          first line (`voice_effects.recipes`).

        The Vocoder Lab family ends with its own board
        (`_open_vocoder_lab_menu`): the same six-band bank as its twenty
        machines, with the carrier -- note, scale degree, waveform, shift,
        breath and mix -- exposed. It is Developer/Contributor-only, so the
        line is added only when the Server's snapshot allows it, and the board
        itself refuses to open without that answer.

        `standalone` is the world's own door (Alt + the Speak Zone key, V by
        default): the same page, with Back closing to the game instead of
        walking back into Music Bot Settings. Passing None -- what a family,
        a board or Pan Delay passes when it re-opens this page -- inherits the
        visit's own mode; Settings passes False so its visit always ends where
        it began.
        """
        from ... import menu as menu_mod, menus
        from .. import gate as music_bot_gate
        from ..voice_effects import builder
        from ..voice_effects import custom
        from ..voice_effects import pan
        from ..voice_effects import tone
        from ..voice_effects import vocoder
        from .. import voice_effects
        gp = self._bot._find_gameplay()
        if not gp:
            return
        if music_bot_gate.blocked("voice_fx"):
            return
        # The mode belongs to the visit, not to the player: the world's entry
        # sets it, Settings clears it, and every page underneath inherits it
        # rather than guessing from the page it came from.
        if standalone is not None:
            self._voice_effect_standalone = bool(standalone)
        standalone_mode = self._voice_effect_standalone

        def current():
            return voice_effects.clamp_effect(
                getattr(self._bot, "voice_effect",
                        voice_effects.DEFAULT_EFFECT))

        def marks(effect_id):
            """The suffixes one preset's line carries: `tuned`, then `active`.

            `tuned` is this preset's OWN four bands, so a player can see which of
            the hundred and forty-four they have reshaped without opening each
            one -- and it stops being said the moment `R` puts the bands back,
            because flat is no saved key at all rather than four zeros. Both
            words are read live, like the labels around them, so tuning a band
            and closing the slider updates the list that is already open.
            """
            bits = []
            if tone.has_tone(effect_id):
                bits.append("tuned")
            if effect_id == current():
                bits.append("active")
            return f" ({', '.join(bits)})" if bits else ""

        def note_for(effect_id):
            """The preset's own description, and what its tone is doing now.

            Callable because the bands move while the page is open: Tab on a
            preset whose tone was just changed answers with the bands it has
            now, not the ones it had when the list was built.
            """
            def note_fn():
                note = voice_effects.effect_note(effect_id)
                if tone.has_tone(effect_id):
                    note = f"{note} Tone: {tone.summary(effect_id)}."
                return note
            return note_fn

        def go_back():
            gp.pop_last_substate()
            if not standalone_mode:
                self._open_settings_menu()

        def go_to_groups():
            gp.pop_last_substate()
            self._open_voice_effect_menu()

        def open_group(group_id):
            def apply():
                gp.pop_last_substate()
                self._open_voice_effect_menu(group_id)
            return apply

        def pick(effect_id):
            def apply():
                self._bot.set_voice_effect(effect_id)
                self._say(f"Voice effect: {voice_effects.effect_label(effect_id)}. "
                      f"{voice_effects.effect_note(effect_id)}")
                m.speak_current_item()
            return apply

        def make_label(effect_id):
            # Read live, like the Harmony list: the mark follows the choice on
            # the list that is already open.
            def label_fn():
                return (f"{voice_effects.effect_label(effect_id)}"
                        f"{marks(effect_id)}")
            return label_fn

        def make_group_label(group_id, name, members):
            # The chosen pedal is named on its family's line, so the top page
            # answers "where am I" without being opened. The vocoder board is
            # the exception: its catalogue name is the family's own name, so
            # naming it would read "Vocoder Lab: Vocoder Lab" -- the board's
            # live summary (its mode and note) is the useful answer instead.
            def label_fn():
                now = current()
                if now in members:
                    if now == voice_effects.VOX_LAB_EFFECT:
                        return (f"{name}: "
                                f"{vocoder.summary()}{marks(now)}")
                    return (f"{name}: {voice_effects.effect_label(now)}"
                            f"{marks(now)}")
                return name
            return label_fn

        def make_custom_label():
            # The board's own line carries what it is set to, so the family
            # page answers "what am I playing through" without being opened.
            def label_fn():
                label = voice_effects.effect_label(voice_effects.CUSTOM_EFFECT)
                label = f"{label}: {custom.summary()}"
                return f"{label}{marks(voice_effects.CUSTOM_EFFECT)}"
            return label_fn

        def make_pan_label():
            # The page's two settings on the family's own line, like the boards'.
            def label_fn():
                label = voice_effects.effect_label(voice_effects.PAN_EFFECT)
                label = f"{label}: {pan.summary()}"
                return f"{label}{marks(voice_effects.PAN_EFFECT)}"
            return label_fn

        def go_pan():
            # Opening the page is choosing the preset, as it is for the boards:
            # a player who came here to set a delay has already decided.
            gp.pop_last_substate()
            self._bot.set_voice_effect(voice_effects.PAN_EFFECT)
            self._open_pan_delay_menu()

        def go_custom():
            # Opening the board is choosing it: a player who wants their own
            # amp has already decided, and a board that changed nothing until
            # another line was pressed would look broken. The knobs' own page
            # is where the first change lands.
            gp.pop_last_substate()
            self._bot.set_voice_effect(voice_effects.CUSTOM_EFFECT)
            self._open_custom_effect_menu()

        def make_builder_label():
            # Same rule for the whole-voice board: its line carries what the
            # player built, so the top page answers without being opened.
            def label_fn():
                label = voice_effects.effect_label(voice_effects.BUILDER_EFFECT)
                label = f"{label}: {builder.summary()}"
                return f"{label}{marks(voice_effects.BUILDER_EFFECT)}"
            return label_fn

        def go_builder():
            gp.pop_last_substate()
            self._bot.set_voice_effect(voice_effects.BUILDER_EFFECT)
            self._open_voice_builder_menu()

        def make_vocoder_label():
            # Same rule as the other boards: the line carries what the board
            # holds now, so the family page answers without being opened.
            def label_fn():
                label = voice_effects.effect_label(voice_effects.VOX_LAB_EFFECT)
                label = f"{label}: {vocoder.summary()}"
                return f"{label}{marks(voice_effects.VOX_LAB_EFFECT)}"
            return label_fn

        def go_vocoder():
            # Opening the board is choosing it, as for Custom Amp: a player
            # who wants their own carrier has already decided. The Server can
            # take the permission away while this page is open (a staff
            # refresh), and the line is a snapshot: refuse in place and say
            # why rather than popping into a board that would not open.
            if not self._bot.vocoder_lab_allowed():
                self._say("The Vocoder Lab board is for Developer/Contributor "
                          "accounts only.")
                return
            gp.pop_last_substate()
            self._bot.set_voice_effect(voice_effects.VOX_LAB_EFFECT)
            self._open_vocoder_lab_menu()

        def make_tone_label():
            # The line names the effect it will edit, because the tone belongs
            # to one preset: it is read live, so picking a pedal here moves the
            # name on the page already open.
            def label_fn():
                now = current()
                if now == voice_effects.DEFAULT_EFFECT:
                    return "Tone: pick an effect first"
                return (f"Tone ({voice_effects.effect_label(now)}): "
                        f"{tone.summary(now)}")
            return label_fn

        def open_tone():
            self._open_voice_tone_menu(current())

        if standalone_mode:
            title = (f"Voice Effect: {self._bot.voice_effect_label()}"
                     if group is None else
                     f"Voice Effect: {voice_effects.group_label(group)}")
        else:
            title = (f"Music Bot Voice Effect: "
                     f"{self._bot.voice_effect_label()}"
                     if group is None else
                     f"Music Bot Voice Effect: "
                     f"{voice_effects.group_label(group)}")
        m = menu_mod.Menu(self._bot.game, title, parrent=gp)
        if group is None:
            items = [(make_label(voice_effects.DEFAULT_EFFECT),
                      pick(voice_effects.DEFAULT_EFFECT), None,
                      note_for(voice_effects.DEFAULT_EFFECT))]
            for group_id, name, note, members in (
                    voice_effects.VOICE_EFFECT_GROUPS):
                items.append((make_group_label(group_id, name, members),
                              open_group(group_id), None, note))
            # The tone sits above the rows that are not families, so the page
            # still ends with the two things you set yourself (Custom Voice is
            # its last choice above Back, and a pin says so).
            items.append((make_tone_label(), open_tone, None, _TONE_NOTE))
            # The rows that are not families: each opens a page rather than a
            # list of pedals (the Pan Delay types its delay, Custom Voice is a
            # board). They come after the families, so the page reads as the
            # categories first and the two things you set yourself last.
            for effect_id in voice_effects.STANDALONE_EFFECTS:
                if effect_id == voice_effects.PAN_EFFECT:
                    items.append((make_pan_label(), go_pan, None,
                                  note_for(effect_id)))
                elif effect_id == voice_effects.BUILDER_EFFECT:
                    items.append((make_builder_label(), go_builder, None,
                                  note_for(effect_id)))
            items.append(("Back", go_back, None,
                          "Closes the page and returns to the game."
                          if standalone_mode else
                          "Returns to Music Bot Settings."))
        else:
            # The board is in the family (so the top page can name it when it
            # is chosen) but it is not a pedal to pick: it opens a page, so the
            # plain list leaves it out and its own line goes at the bottom.
            items = [(make_label(effect_id), pick(effect_id), None,
                      note_for(effect_id))
                     for effect_id in voice_effects.group_members(group)
                     if effect_id not in (voice_effects.CUSTOM_EFFECT,
                                          voice_effects.VOX_LAB_EFFECT)]
            # Above the board's own line, so the family still ends with the
            # board at the bottom of its list (as its own pins say).
            items.append((make_tone_label(), open_tone, None, _TONE_NOTE))
            if group == "drive":
                items.append((make_custom_label(), go_custom, None,
                              note_for(voice_effects.CUSTOM_EFFECT)))
            elif group == "synth" and self._bot.vocoder_lab_allowed():
                # Hidden for anyone the Server has not allowed: this is the
                # one line in the family that is not a preset but a tool.
                items.append((make_vocoder_label(), go_vocoder, None,
                              note_for(voice_effects.VOX_LAB_EFFECT)))
            items.append(("Back", go_to_groups, None,
                          "Returns to the list of voice effect families."))
        m.add_items(items)
        menus.set_default_sounds(m)
        gp.add_substate(m)

    def _open_voice_tone_menu(self, preset=None):
        """The four tone bands of one preset, as a slider page.

        `preset` is the effect the caller's line names (the live choice on the
        Voice Effect page, the board's own id on a board page); with none it is
        whatever is chosen now. It is pushed ON TOP of the page that opened it,
        never in place of it, because every label on that page reads live --
        closing the slider reveals a `Tone:` line already saying what the bands
        now hold, with nothing rebuilt and nothing spoken over.

        A page for Off would be a slider with no chain behind it (the line holds
        no processor when no pedal is chosen), so it says so instead.

        The values are written live as the bands move, with `autosave=False`:
        the capture thread reads the same option dict, so a decibel lands on
        the next 20 ms frame, and the settings file is written ONCE when the
        page closes (`_VoiceToneSlider._close_and_commit`) rather than once per
        key press.
        """
        from .. import gate as music_bot_gate
        from .. import voice_effects
        gp = self._bot._find_gameplay()
        if not gp:
            return
        if music_bot_gate.blocked("voice_fx"):
            return
        chosen = (voice_effects.clamp_effect(preset) if preset is not None
                  else voice_effects.clamp_effect(
                      getattr(self._bot, "voice_effect",
                              voice_effects.DEFAULT_EFFECT)))
        if chosen == voice_effects.DEFAULT_EFFECT:
            self._say("Pick a voice effect first: the tone belongs to the "
                      "pedal you are using.")
            return
        gp.add_substate(_VoiceToneSlider(self._bot.game, self._bot, chosen))

    def _open_pan_delay_menu(self):
        """Pan Delay: the typed delay and the movement, on one page.

        Two settings, not a board. `Delay` is typed, 20-2000 whole
        milliseconds, because a delay a player hears is a number they choose
        rather than a row of steps; `Motion` is three choices (Stereo,
        Front-Back, Off). The page's title names both, and every change is
        spoken once and lands on the next 20 ms frame.

        `Preset` is the third line: the named settings of
        `voice_effects.pan.PRESETS` -- Ping-Pong, Wide Sweep, Front Walk, Deep
        Behind and Slow Drift, plus `Custom (this page)` for the player's own
        two numbers. Choosing a name writes both settings, so a preset is a
        name for the pair rather than a copy of it, and typing a delay puts the
        mark back to `Custom`. **The same table is the Custom Voice board's Pan
        Delay slot**, so a delay set here can be played by a voice built there.

        Opening the page chooses the preset -- the same rule as the boards: a
        player who came here to set a delay has already decided. The line on the
        Voice Effect page carries the same summary, so that page answers "what
        is it set to" without being opened.
        """
        from ... import menu as menu_mod, menus
        from .. import gate as music_bot_gate
        from ..voice_effects import pan
        from ..voice_effects import tone
        from .. import voice_effects

        gp = self._bot._find_gameplay()
        if not gp:
            return
        if music_bot_gate.blocked("voice_fx"):
            return

        def go_back():
            # The line lives on the Voice Effect page itself, not in a family.
            gp.pop_last_substate()
            self._open_voice_effect_menu()

        def choose():
            self._bot.set_voice_effect(voice_effects.PAN_EFFECT)

        def go_delay():
            def submitted(text):
                # Pop first, always: a field that stays open over a swapped
                # substate is the frozen-looking game the fade field had.
                gp.pop_last_substate()
                text = "" if text is None else str(text).strip()
                before = pan.delay_ms()
                if not text:
                    self._say(f"Delay unchanged, {before} milliseconds.")
                    return
                stored = pan.set_delay_ms(text)
                if stored is None:
                    self._say(f"'{text}' is not a number of milliseconds, so "
                              f"the delay is still {before}.")
                    return
                choose()
                self._say(f"Pan Delay: {stored} milliseconds, "
                          f"{pan.motion_label().lower()}.")

            gp.add_substate(self._bot.game.input.run(
                f"Enter the delay in milliseconds, "
                f"{pan.MIN_DELAY_MS} to "
                f"{pan.MAX_DELAY_MS} "
                f"(currently {pan.delay_ms()}):",
                handeler=submitted,
                min_val=pan.MIN_DELAY_MS,
                max_val=pan.MAX_DELAY_MS,
                msg_length=4, multiline=False,
            ))

        def go_motion():
            page = menu_mod.Menu(
                self._bot.game,
                f"Music Bot Pan Delay: {pan.motion_label()}",
                parrent=gp,
            )
            items = []
            for label, stored, note in pan.MOTIONS:
                def apply(value=stored, name=label):
                    def pressed():
                        pan.set_motion(value)
                        choose()
                        gp.pop_last_substate()
                        self._say(f"Pan Delay: {name.lower()}.")
                    return pressed

                shown = (f"{label} (active)"
                         if stored == pan.motion() else label)
                items.append((shown, apply(), None, note))
            items.append(("Back", lambda: gp.pop_last_substate(), None,
                          "Returns to the Pan Delay page."))
            page.add_items(items)
            menus.set_default_sounds(page)
            gp.add_substate(page)

        def go_preset():
            page = menu_mod.Menu(
                self._bot.game,
                f"Music Bot Pan Delay Preset: "
                f"{pan.preset_label()}",
                parrent=gp,
            )
            items = []
            for stored, label, milliseconds, mode, note in (
                    pan.PRESETS):
                def apply(value=stored, name=label):
                    def pressed():
                        pan.set_preset(value)
                        choose()
                        gp.pop_last_substate()
                        self._say(f"Pan Delay preset: {name.lower()}, "
                                  f"{pan.delay_ms()} "
                                  f"milliseconds, "
                                  f"{pan.motion_label().lower()}.")
                    return pressed

                # A named row says what it would set; the mark says which one
                # the page is on (`Custom (this page)` is the player's own two
                # numbers, so it reads as its own name alone).
                shown = label
                if stored != pan.CUSTOM_PRESET:
                    shown = (f"{label} -- {milliseconds} ms, "
                             f"{pan.motion_label(mode).lower()}")
                if stored == pan.preset():
                    shown = f"{shown} (active)"
                items.append((shown, apply(), None, note))
            items.append(("Back", lambda: gp.pop_last_substate(), None,
                          "Returns to the Pan Delay page."))
            page.add_items(items)
            menus.set_default_sounds(page)
            gp.add_substate(page)

        def reset():
            pan.clear()
            choose()
            page.title = f"Music Bot Pan Delay: {pan.summary()}"
            self._say("Pan Delay reset: 300 milliseconds, Stereo.")

        def make_tone_label():
            # The tone belongs to the preset, and this page is one preset.
            def label_fn():
                label = voice_effects.effect_label(voice_effects.PAN_EFFECT)
                return (f"Tone ({label}): "
                        f"{tone.summary(voice_effects.PAN_EFFECT)}")
            return label_fn

        def open_tone():
            self._open_voice_tone_menu(voice_effects.PAN_EFFECT)

        choose()
        page = menu_mod.Menu(
            self._bot.game,
            f"Music Bot Pan Delay: {pan.summary()}",
            parrent=gp,
        )
        page.add_items([
            (lambda: f"Delay: {pan.delay_ms()} ms", go_delay,
             None,
             "Type the delay yourself, in whole milliseconds from 20 to 2000. "
             "It is both the gap between the repeats and the time one crossing "
             "takes, so a longer delay is a slower movement."),
            (lambda: f"Motion: {pan.motion_label()}", go_motion,
             None,
             "Where the repeats travel: Stereo crosses your ears, Front-Back "
             "passes in front of and behind you, Off keeps the delay still."),
            (lambda: f"Preset: {pan.preset_label()}", go_preset,
             None,
             "Named settings: a delay and a movement under one name. The same "
             "names are the Pan Delay slot of the Custom Voice board, so a "
             "preset chosen here can be played by a voice built there, and "
             "Custom (this page) follows the two lines above."),
            (make_tone_label(), open_tone, None, _TONE_NOTE),
            ("Reset to 300 ms, Stereo", reset, None,
             "Puts both settings back to what the page opens with, and the "
             "preset mark back to Custom. Pan Delay stays chosen."),
            ("Back", go_back, None,
             "Returns to the Voice Effect page."),
        ])
        menus.set_default_sounds(page)
        gp.add_substate(page)

    def _open_voice_builder_menu(self):
        """The Custom Voice board: the last line of the Voice Effect page.

        The same generated page as the amp board, over the eleven slots in
        `voice_effects.builder.PARAMS` -- colour, tone, body, movement, pitch,
        space, pan delay, texture, rhythm, gate and output. Back returns to the
        Voice Effect page it came from, not into Amp & Drive.
        """
        from ..voice_effects import builder
        return self._open_custom_effect_menu(builder,
                                             back_to_top=True)

    def _open_vocoder_lab_menu(self):
        """The Vocoder Lab board: the last line of the family.

        The same generated page as the amp board, over the nine rows in
        `voice_effects.vocoder.PARAMS` -- mode, root, octave, scale, interval,
        wave, shift, breath and mix. Back returns to the Vocoder Lab family,
        not into Amp & Drive. Developer/Contributor only: the Server's
        snapshot is asked here as well as where the line is added, so a direct
        call cannot open the page for an account without it.
        """
        from ..voice_effects import vocoder
        if not self._bot.vocoder_lab_allowed():
            return
        return self._open_custom_effect_menu(vocoder,
                                             back_group="synth")

    def _open_builder_clear_menu(self, board_menu=None):
        """The Custom Voice board's Clear line, behind a question.

        Every other line on that page is a choice that can be changed again by
        pressing the same line; `Clear` is every slot at once, and it sits
        directly above `Back` -- exactly where an overshoot lands. So the press
        opens a two-line question instead, and `No, keep it` is a Back.

        `Yes` clears, speaks once, and returns to the board with its title
        rewritten in place (`board_menu`): the slots' labels are live, and a
        rebuilt page would re-read its own title over the sentence that says
        what just happened. With no board handed in (a direct call), the
        question simply pops and the board underneath is already right.
        """
        from ... import menu as menu_mod, menus
        from .. import gate as music_bot_gate
        from ..voice_effects import builder
        from .. import voice_effects
        gp = self._bot._find_gameplay()
        if not gp:
            return
        if music_bot_gate.blocked("voice_fx"):
            return

        def keep():
            gp.pop_last_substate()

        def do_clear():
            builder.clear()
            if board_menu is not None:
                board_menu.title = (
                    f"Music Bot "
                    f"{voice_effects.effect_label(voice_effects.BUILDER_EFFECT)}: "
                    f"{builder.summary()}")
            gp.pop_last_substate()
            self._say("Custom Voice cleared. Every slot is back at its default, "
                  "and the board says My Own.")

        m = menu_mod.Menu(self._bot.game,
                          "Music Bot Custom Voice: Clear the board?",
                          parrent=gp)
        m.add_items([
            ("Yes, clear the board", do_clear, None,
             "Puts every slot back to its default and forgets the "
             "starting point. Custom Voice stays chosen."),
            ("No, keep it", keep, None,
             "Returns to the board with everything on it kept."),
        ])
        menus.set_default_sounds(m)
        gp.add_substate(m)

    def _open_builder_recipes_menu(self, family_id=None, from_board=False):
        """The Custom Voice board's starting points: five pages of ten.

        The board is eleven slots of seventy-eight choices, and a player
        opening it
        for the first time is looking at ten lines reading their defaults. This
        page is the way in: fifty finished voices (`voice_effects.recipes`),
        each one the same eleven slots with values in them, so a press is a
        sound
        and every slot underneath stays the player's afterwards.

        One method for both levels -- the five families, and then one family's
        ten -- because the family is a field of each recipe's row: the page in
        front of the player and the table behind it are the same object, so a
        recipe cannot be offered under a family it does not belong to. Back
        pops one level and reveals whatever opened it (the family list, or the
        board).

        **A pick does not leave the page.** The fifty exist to be tried one
        after another, and a page that closed on every press turned "listen to
        the Machine family" into fifty round trips through the board; the mark
        on the chosen line is a live label, so it follows the press on the page
        already open. The board underneath is rebuilt on the way back instead
        (`from_board` remembers it is there), because its slots read live but
        its TITLE was written when it opened -- a board titled "Crunch" over a
        vocoder is the kind of lie a live label exists to prevent -- and the
        stack still ends where it began.
        """
        from ... import menu as menu_mod, menus
        from .. import gate as music_bot_gate
        from ..voice_effects import recipes
        from .. import voice_effects
        gp = self._bot._find_gameplay()
        if not gp:
            return
        if music_bot_gate.blocked("voice_fx"):
            return

        def go_back():
            gp.pop_last_substate()
            if family_id is None and from_board:
                # Off the families page and back onto the board it came from:
                # rebuilt, not revealed, for the reason in the docstring -- a
                # recipe picked since then moved the slots under a title that
                # was written when the board opened.
                gp.pop_last_substate()
                self._open_voice_builder_menu()

        def open_family(group_id):
            def apply():
                self._open_builder_recipes_menu(group_id)
            return apply

        def make_family_label(group_id, name):
            # The family names the recipe it is currently playing, so the page
            # answers "which voice am I on" without being opened -- the same
            # rule the pedal families follow.
            def label_fn():
                now = recipes.current_id()
                row = recipes.recipe(now)
                if row is not None and row[2] == group_id:
                    return f"{name}: {row[1]}"
                return name
            return label_fn

        def pick(recipe_id):
            def apply():
                recipes.apply(recipe_id)
                self._bot.set_voice_effect(voice_effects.BUILDER_EFFECT)
                self._say(f"Custom Voice: "
                      f"{recipes.recipe_label(recipe_id)}. "
                      f"{recipes.recipe_note(recipe_id)}")
                # The page stays open -- the mark and the board's first line
                # are live labels, and the board underneath is rebuilt when
                # this page is left (go_back), so nothing said here is stale.
            return apply

        def make_label(recipe_id):
            def label_fn():
                label = recipes.recipe_label(recipe_id)
                return (f"{label} (active)"
                        if recipes.current_id() == recipe_id
                        else label)
            return label_fn

        if family_id is None:
            items = [(make_family_label(group_id, name), open_family(group_id),
                      None, note)
                     for group_id, name, note in recipes.FAMILIES]
            items.append(("Back", go_back, None,
                          "Returns to the Custom Voice board. The board keeps "
                          "everything set here."))
            title = "Music Bot Custom Voice: Start From"
        else:
            items = [(make_label(recipe_id), pick(recipe_id), None,
                      recipes.recipe_note(recipe_id))
                     for recipe_id in
                     recipes.family_members(family_id)]
            items.append(("Back", go_back, None,
                          "Returns to the five families of starting "
                          "points."))
            title = ("Music Bot Custom Voice: "
                     f"{recipes.family_label(family_id)}")
        m = menu_mod.Menu(self._bot.game, title, parrent=gp)
        m.add_items(items)
        menus.set_default_sounds(m)
        gp.add_substate(m)

    def _open_custom_effect_menu(self, board=None, back_to_top=False,
                                 back_group=None):
        """A board page: one line per knob, straight from the board's table.

        Generated rather than written out -- `PARAMS` holds the knobs, their
        choices and every word Tab says, and this method walks it. A new knob
        is one row in that table; a whole new board is a new table and this
        same loop, which is why the menu has no list of its own to fall out of
        step with the DSP, and the DSP has no table of its own to fall out of
        step with the words.

        `board` is either board module (the Custom Amp by default);
        `back_to_top` says its Back returns to the Voice Effect page, and
        `back_group` names the family it returns to otherwise (Amp & Drive by
        default, Vocoder Lab for the vocoder board). A board that hides rows
        by mode does so through its own `rows()`, so the page and the DSP ask
        the same table which rows exist.
        """
        from ... import menu as menu_mod, menus
        from .. import gate as music_bot_gate
        from ..voice_effects import custom
        from ..voice_effects import recipes
        from ..voice_effects import tone
        from .. import voice_effects
        board = board if board is not None else custom
        gp = self._bot._find_gameplay()
        if not gp:
            return
        if music_bot_gate.blocked("voice_fx"):
            return

        def go_back():
            gp.pop_last_substate()
            if back_to_top:
                self._open_voice_effect_menu()
            else:
                self._open_voice_effect_menu(back_group or "drive")

        def open_param(param_id):
            def apply():
                gp.pop_last_substate()
                # The board is handed on so the knob page's Back can put it
                # back: a page of values closes onto the board it came from,
                # never onto the page the board itself was opened from.
                self._open_custom_param_menu(
                    param_id, board, board_back_to_top=back_to_top,
                    board_back_group=back_group)
            return apply

        def make_label(param_id, name):
            def label_fn():
                return f"{name}: {board.choice_label(param_id)}"
            return label_fn

        def open_recipes():
            # The board stays on the stack underneath, and says so: the
            # families page rebuilds it on the way back (a recipe picked in
            # between moved the slots under a title written when it opened).
            self._open_builder_recipes_menu(from_board=True)

        def make_recipes_label():
            def label_fn():
                return ("Start From: "
                        f"{recipes.current_label()}")
            return label_fn

        def open_clear():
            self._open_builder_clear_menu(m)

        def make_tone_label():
            # A board is a preset too, and the tone sits after its whole chain:
            # Air over a vocoder is a different sound from either alone.
            def label_fn():
                return (f"Tone ({voice_effects.effect_label(board.EFFECT_ID)}): "
                        f"{tone.summary(board.EFFECT_ID)}")
            return label_fn

        def open_tone():
            self._open_voice_tone_menu(board.EFFECT_ID)

        # A board may hide rows by mode (the vocoder hides Root and Octave in
        # Current, and Scale and Interval outside Interval). Its `rows()` asks
        # the same table the spec does, so the page cannot offer a row that
        # does nothing. The other boards have no such filter and their PARAMS
        # is the page.
        visible = getattr(board, "rows", None)
        params = visible() if callable(visible) else board.PARAMS
        items = [(make_label(param_id, name), open_param(param_id), None, note)
                 for param_id, name, note, _choices, _default in params]
        # The tone goes above the board's own Clear line, which is the last
        # thing above Back on the board that has one.
        items.append((make_tone_label(), open_tone, None, _TONE_NOTE))
        if board.EFFECT_ID == voice_effects.BUILDER_EFFECT:
            # The whole-voice board's first line is where to start, above the
            # eleven slots it fills: eleven lines reading their defaults is not
            # a choice, and "what is a Colour?" is not the first question a
            # player wants to answer.
            items.insert(0, (make_recipes_label(), open_recipes, None,
                             recipes.start_note()))
            # ... and the board's last line above Back is the way out of one
            # that has been played with: every slot at once is the only change
            # here a single extra press cannot undo, so it asks first.
            items.append(("Clear: back to the defaults", open_clear, None,
                          "Every slot back to its default and the starting "
                          "point forgotten. It asks before it clears."))
        items.append(("Back", go_back, None,
                      "Returns to the Voice Effect page. The board keeps "
                      "everything set here." if back_to_top else
                      f"Returns to the "
                      f"{voice_effects.group_label(back_group or 'drive')} "
                      "list. The board keeps everything set here."))
        m = menu_mod.Menu(
            self._bot.game,
            f"Music Bot {voice_effects.effect_label(board.EFFECT_ID)}: "
            f"{board.summary()}",
            parrent=gp,
        )
        m.add_items(items)
        menus.set_default_sounds(m)
        gp.add_substate(m)

    def _open_custom_param_menu(self, param_id, board=None,
                                board_back_to_top=False,
                                board_back_group=None):
        """One knob's choices, and the only place a board is written to.

        A choice applies the board and leaves this list OPEN. A page of values
        is a set to try -- which body, which room, which texture -- and closing
        it on every press turned one question into a round trip through the
        board for each answer. The choices' own labels are live, so the
        `(active)` mark moves to the press on the page already open; only the
        page's TITLE is a snapshot, and it is rewritten here rather than the
        page being rebuilt, because a rebuild would re-read the title over the
        sentence that names the value.

        Picking a choice here is also what makes the board the chosen pedal: a
        player who turns a knob has chosen it.

        **The line above Back puts this one setting back.** `Clear: back to
        Crunch`, naming the value the board opens with -- a page of choices
        used to answer "what was it before I got here" nowhere, and four of
        the board's slots (and six of the amp's ten knobs) do not open on their
        first row. It leaves the other settings alone and does not ask, which
        is what separates it from the whole board's Clear.

        **Back here is one page, and it is the board.** This page used to pop
        and re-open nothing, so Esc (which presses the last line) landed on
        whatever the board itself had replaced -- Music Bot Settings, two
        levels up, losing the board with every value the player had just set.
        Every other page in this tree re-opens the page beneath it (that is
        also how the page gets spoken again, since a pop itself says nothing):
        this one now does the same, onto the board, titled with what the board
        holds now rather than what it held when it opened.
        """
        from ... import menu as menu_mod, menus
        from .. import gate as music_bot_gate
        from ..voice_effects import custom
        from .. import voice_effects
        board = board if board is not None else custom
        gp = self._bot._find_gameplay()
        if not gp:
            return
        if music_bot_gate.blocked("voice_fx"):
            return
        row = board.param(param_id)
        # (the param page imports its own modules above; the board decides
        # which table it walks and which id it applies)
        if row is None:
            return
        _param_id, name, _note, choices, _default = row

        def go_back():
            gp.pop_last_substate()
            self._open_custom_effect_menu(
                board, back_to_top=board_back_to_top,
                back_group=board_back_group)

        def announce(chosen, prefix=""):
            # Shared by a choice and by the clear line: both are one write the
            # player hears on the next frame, and both say what the board now
            # holds. Nothing is popped: this page is where the next choice is.
            # The mark follows by itself (see `make_label`); the title is the
            # one snapshot on the page, so it is rewritten here.
            told = (f"{name}: {board.choice_label(param_id, chosen)}. "
                    f"{board.choice_note(param_id, chosen)}")
            self._say(f"{prefix} {told}".strip())
            m.title = (
                f"Music Bot "
                f"{voice_effects.effect_label(board.EFFECT_ID)} "
                f"{name}: {board.choice_label(param_id, chosen)}")

        def pick(value):
            def apply():
                before = board.values()
                chosen = board.set_value(param_id, value)
                self._bot.set_voice_effect(board.EFFECT_ID)
                # A board may answer a choice with another value and say why
                # (`voice_effects.vocoder.refusal`). It is one spoken line, not
                # a second line after the announcement: the refusal and the
                # value it fell back to are the same press.
                refusal = getattr(board, "refusal", None)
                prefix = (refusal(param_id, value, chosen)
                          if callable(refusal) else "")
                if (param_id == "mode" and chosen != "current"
                        and before.get("wave") == "noise"):
                    prefix = ("Noise has no note to lock, so the carrier is "
                              "Saw now.")
                announce(chosen, prefix)
            return apply

        def clear_one():
            chosen = board.clear_value(param_id)
            self._bot.set_voice_effect(board.EFFECT_ID)
            announce(chosen)

        def make_label(value):
            def label_fn():
                label = board.choice_label(param_id, value)
                return (f"{label} (active)"
                        if board.values()[param_id] == value else label)
            return label_fn

        items = [(make_label(stored), pick(stored), None, note)
                 for _label, stored, note in choices]
        # The line above Back is this one setting's way out, in the place the
        # board page's own Clear sits: one slot back to what the board opens
        # with, named, because the page has never said which value that is.
        # It does not ask -- every slot at once is a question, one is a press
        # the page already answers with `(active)` and any choice undoes.
        items.append((f"Clear: back to {board.default_label(param_id)}",
                      clear_one, None,
                      "Puts this one back to what the board opens with "
                      f"({board.default_label(param_id)}) and leaves every "
                      "other setting alone. Pick any value above to change "
                      "it again."))
        items.append(("Back", go_back, None,
                      "Returns to the board, which keeps every setting. Esc "
                      "does the same, one page at a time."))
        m = menu_mod.Menu(
            self._bot.game,
            f"Music Bot {voice_effects.effect_label(board.EFFECT_ID)} "
            f"{name}: {board.choice_label(param_id)}",
            parrent=gp,
        )
        m.add_items(items)
        menus.set_default_sounds(m)
        gp.add_substate(m)


class _VoiceToneSlider(state.State):
    """The channel strip of one voice effect: thirteen rows, walked by Tab.

    The same keyboard shape as the Music Bot's own Bass/Mid/Treble slider
    (`_MusicBotEqSlider` in `menu_pages/settings.py`), because a player who has
    set the song's EQ already knows this one: Tab walks the rows, Up and Down
    move the row, Page Up and Down move it further, Home puts the row back to
    what the preset opens with, `R` puts the whole strip back, and Enter keeps
    everything.

    **Tab stops at the ends; it does not wrap.** Thirteen rows is enough that
    the loop back from `Level` to `Low Cut` is read as the page losing the
    place -- the last press of a walk and the first press of a walk sound the
    same, and the row a player was about to set is now three rows away. So the
    walk is a line with two ends: at the last row a Tab reads `Level` again,
    and at the first row Shift+Tab reads `Low Cut` again. It still speaks, so
    the press is never silent, and it still says where the end of the strip
    is. `M` speaks the meter -- the level and peak of the last frame
    that went through the effect -- because a fader nobody can see needs a
    meter to be set against. Every press is spoken.

    The rows are `tone.ROWS`, in the order a mixer would put them: the low cut
    first, because it is what the bands see; the bands and the de-esser next,
    because the de-esser is the answer to what presence and air bring out; the
    high cut, which is where the top stops; then the mix and the compressor,
    which are what the sound does once it has a shape; and the level last,
    because it is the fader after all of it.

    **The working copy is the slider's own.** `values` is what the speech
    reads; `voice_effects.tone` is written on every move with
    `autosave=False`, so the capture thread hears the row on the next frame
    while the settings file is written once, when the page closes. A page
    closed without a key press writes nothing at all.

    Closing pops only: the page underneath reads its labels live, so the
    `Tone:` line it is revealed with already names the new strip.
    """

    def __init__(self, game, bot, preset):
        super().__init__(game, parrent=bot)
        from .. import voice_effects
        from ..voice_effects import tone
        self.bot = bot
        self.preset = preset
        self.name = voice_effects.effect_label(preset)
        self.rows = tone.ROWS
        self.values = tone.state(preset)
        self.current_index = 0
        self._closed = False
        self._touched = False

    def enter(self):
        super().enter()
        _speak(
            f"Channel strip for {self.name}. Tab switches the thirteen rows: "
            "Low Cut, the rumble under the voice; Low, the chest and the "
            "room; Body, where a voice turns muddy; Presence, the clarity "
            "of the words; Air, the breath on top; De-ess, how much of the "
            "s is taken off; High Cut, where the top stops; Mix, how much of "
            "the effect you hear; Comp and Ratio, where the compressor starts "
            "and how hard it leans; Attack and Release, how fast it moves; "
            "and Level, how loud it leaves. Up and Down move a row, Page Up "
            "and Page Down move it further. Tab stops at the last row rather "
            "than jumping back to the first, and Shift plus Tab the same at "
            "the top. Home puts this row back, R puts everything back, M "
            "speaks the level and the compression, Enter keeps it.")
        self._announce_current()

    def update(self, events):
        super().update(events)
        for event in events:
            if event.type != pygame.KEYDOWN:
                continue
            key = event.key
            if key == pygame.K_TAB:
                direction = -1 if event.mod & pygame.KMOD_SHIFT else 1
                moved = self.current_index + direction
                # A line, not a ring: at an end the row under the cursor is
                # read again rather than jumped to the other end. The silence
                # an ignored press would leave is the one thing worse than the
                # wrap, so the end row still speaks.
                if 0 <= moved < len(self.rows):
                    self.current_index = moved
                self._announce_current()
            elif key in (pygame.K_RETURN, pygame.K_KP_ENTER, pygame.K_ESCAPE):
                self._close_and_commit()
                break
            elif key == pygame.K_UP:
                self._adjust(1)
            elif key == pygame.K_DOWN:
                self._adjust(-1)
            elif key == pygame.K_PAGEUP:
                self._adjust(6)
            elif key == pygame.K_PAGEDOWN:
                self._adjust(-6)
            elif key == pygame.K_HOME:
                self._default_current()
            elif key == pygame.K_r:
                self._reset_all()
            elif key == pygame.K_m:
                self._speak_meter()
        return True

    def _current(self):
        """The row under the cursor: `(id, name, kind)`."""
        return self.rows[self.current_index]

    def _announce_current(self):
        from ..voice_effects import tone
        row_id, label, kind = self._current()
        value = self.values[row_id]
        if kind == "db":
            _speak(f"{label}, {int(tone.band(row_id)[2])} hertz. "
                   f"{tone.spoken(value)}.")
            return
        _speak(f"{label}. {tone.spoken_row(row_id, value)}.")

    def _adjust(self, amount):
        from ..voice_effects import tone
        row_id, _label, kind = self._current()
        value = self.values[row_id]
        ladder = tone.LADDERS.get(kind)
        if ladder is not None:
            # A ladder is walked, not added to: the cut, the compressor's
            # threshold, its ratio and its two times are choices, and Page Up
            # is two choices rather than a bigger number.
            stops = 2 if abs(amount) >= 6 else 1
            index = ladder.index(value) if value in ladder else 0
            index = max(0, min(len(ladder) - 1,
                               index + (stops if amount > 0 else -stops)))
            self._set_current(ladder[index])
        elif kind == "mix":
            step = tone.MIX_PAGE if abs(amount) >= 6 else tone.MIX_STEP
            self._set_current(value + (step if amount > 0 else -step))
        else:
            self._set_current(value + amount)

    def _set_current(self, value):
        from ..voice_effects import tone
        row_id = self._current()[0]
        stored = tone.set_row(self.preset, row_id, value, save=False)
        if stored is None:
            return
        self.values[row_id] = stored
        self._touched = True
        _speak(tone.spoken_row(row_id, stored))

    def _default_current(self):
        from ..voice_effects import tone
        self._set_current(tone.default_of(self._current()[0]))

    def _reset_all(self):
        from ..voice_effects import tone
        tone.reset(self.preset, save=False)
        self.values = tone.state(self.preset)
        self._touched = True
        _speak("Every row back flat: the effect is exactly as it was made "
               "again.")

    def _speak_meter(self):
        from ..voice_effects import tone
        _speak(tone.meter_spoken(self.preset))

    def _close_and_commit(self):
        if self._closed:
            return
        self._closed = True
        from ... import options
        from ..voice_effects import tone
        if self._touched:
            # One write for the whole visit: a key press moves the row in
            # memory (the capture thread reads it per frame), and the settings
            # file is encrypted and rewritten here instead of on every arrow.
            options.save()
        gp = self.bot._find_gameplay()
        _speak(f"Channel strip for {self.name}: "
               f"{tone.description(self.preset)}")
        if gp is not None:
            gp.pop_last_substate()
