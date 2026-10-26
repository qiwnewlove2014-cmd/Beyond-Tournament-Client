"""The Settings hub and its sound pages of `MusicBotMenus`.

The Settings list itself, the output-device picker, the equalizer profiles and
the Custom Bass/Mid/Treble slider (`_MusicBotEqSlider`, the one menu item that is
a `state.State` rather than a page), and the two key dials -- the song's key and
the broadcast voice's key.

Moved out of `libs/music_bot/menus.py` on 2026-10-26 and mixed into
`MusicBotMenus`, so every caller still says `bot._menus().<method>()` and the
one-line shims in `controller.py` did not move. The state stays on
`MapMusicBot`: `self.` is this handler and `self._bot.` is the Music Bot. A page
that speaks through the module-level `speak` calls `self._say`, because
`libs/music_bot/menus.speak` is the patch point the menu tests use; the slider
does the same through `_speak`, since it is not a handler.

Pins: client/tests/test_music_bot_crossfade_eq.py's `TestTheCustomEqSlider`
(opens the slider, moves a band, commits), test_music_bot_output_device.py and
test_music_bot_transpose.py / test_music_bot_speech_key.py.
Rules: .agents/skills/music_bot_integration/.
"""

import contextlib
import functools

import pygame

from .. import gate
from ... import options
from .. import output_device
from ... import state
from .support import stand_on


def _speak(text):
    """Speak through `menus.speak`, looked up at call time.

    The slider is a `state.State`, not a menu, and the menu tests patch
    `libs.music_bot.menus.speak`; reading the name there keeps the patch
    effective wherever the class lives.
    """
    from .. import menus
    return menus.speak(text)



class SettingsMenus:
    """The Settings hub, its sound pages and the Custom EQ slider."""

    def _open_settings_menu(self):
        """Music Bot settings: output device first, then reverb, crossfade, EQ, key, speech key, auto-tune, its three dials (speed, scale, amount), harmony, voice effect, muffle.

                    Harmony is its own line and its own mode: it is not one of the dials
                    that the Auto-Tune switch adds and removes, and it sings whether the
                    tuner is on or off (`libs/voice_chat.harmony_enabled`). It sits after
                    the tuner's dials so the block the switch opens stays one block.

                    The output line leads because it is what a player comes here to change;
                    the underwater muffle sits last (just above Back) so the two are never
                    neighbours on the list. `test_the_output_line_leads_and_the_muffle_sits_last`
                    pins that order.

                    Broadcast to Others is deliberately NOT here - it has its own shortcut, and
                    duplicating it caused confusion about which is the source of truth. Every toggle here
                    is persisted in client options.
                    """
        from ... import menu as menu_mod, menus
        gp = self._bot._find_gameplay()
        if not gp:
            return

        def go_back():
            gp.pop_last_substate()
            self._show_mode_menu()

        def get_water_label():
            status = "ON" if self._bot.water_muffle_enabled else "OFF"
            return f"Underwater Muffle: {status}"

        def toggle_water():
            self._bot.water_muffle_enabled = not self._bot.water_muffle_enabled
            options.set("music_bot_water_muffle", self._bot.water_muffle_enabled)
            self._bot._reapply_bot_water_filter()
            self._say("Underwater muffle enabled." if self._bot.water_muffle_enabled
                  else "Underwater muffle disabled.")
            m.speak_current_item()

        def get_reverb_label():
            status = "ON" if self._bot.reverb_enabled else "OFF"
            return f"Room Reverb (Realism): {status}"

        def toggle_reverb():
            self._bot.reverb_enabled = not self._bot.reverb_enabled
            options.set("music_bot_reverb", self._bot.reverb_enabled)
            if not self._bot.reverb_enabled:
                self._bot._detach_map_reverb()
            self._say("Room reverb enabled." if self._bot.reverb_enabled
                  else "Room reverb disabled.")
            m.speak_current_item()

        def get_crossfade_label():
            status = "ON" if getattr(self._bot, "crossfade_enabled", False) else "OFF"
            return f"Crossfade Between Songs: {status}"

        def toggle_crossfade():
            enabled = not getattr(self._bot, "crossfade_enabled", False)
            self._bot._set_crossfade_enabled(enabled)
            self._say("Crossfade between songs enabled. Tracks overlap smoothly."
                  if enabled else "Crossfade between songs disabled.")
            m.speak_current_item()

        def get_eq_label():
            return f"Equalizer: {self._bot._eq_profile_label()}"

        def go_eq():
            gp.pop_last_substate()
            self._open_eq_menu()

        def get_transpose_label():
            return f"Song Key: {self._bot.transpose_label()}"

        def go_transpose():
            gp.pop_last_substate()
            self._open_transpose_menu()

        def get_speech_transpose_label():
            if getattr(self._bot, "autotune_enabled", False):
                # Not a dead line: the dial really has nothing to move while
                # the grid is chromatic, so the row says who is in charge
                # rather than letting a key press do nothing quietly.
                return "Speech Key: replaced by Auto-Tune"
            return f"Speech Key: {self._bot.speech_transpose_label()}"

        def go_speech_transpose():
            if getattr(self._bot, "autotune_enabled", False):
                self._say("Auto-Tune puts the voice on every note by itself, so "
                      "Speech Key has nothing to move. Turn Auto-Tune off first.")
                return
            gp.pop_last_substate()
            self._open_speech_transpose_menu()

        def get_autotune_label():
            return f"Auto-Tune: {self._bot.autotune_label()}"

        def toggle_autotune():
            enabled = self._bot.toggle_autotune()
            self._say("Auto-Tune on. The voice is snapped to the nearest note as "
                  "you broadcast it; Retune Speed sets how fast it slides. "
                  "The harmony voices are not affected."
                  if enabled else "Auto-Tune off. Your own voice; the "
                  "harmony voices keep singing.")
            # The dials below this line exist only while it is on, so the page
            # is rebuilt around this press and the cursor is put back on the
            # switch rather than jumping to the top of the list. The Harmony
            # line is NOT one of them: it is a mode of its own and stays.
            gp.pop_last_substate()
            stand_on(self._open_settings_menu(), "Auto-Tune:")

        def get_autotune_speed_label():
            return f"Retune Speed: {self._bot.autotune_speed_label()}"

        def go_autotune_speed():
            gp.pop_last_substate()
            self._open_autotune_speed_menu()

        def get_autotune_scale_label():
            return f"Auto-Tune Scale: {self._bot.autotune_scale_label()}"

        def go_autotune_scale():
            gp.pop_last_substate()
            self._open_autotune_scale_menu()

        def get_autotune_amount_label():
            return f"Tune Amount: {self._bot.autotune_amount_label()}"

        def go_autotune_amount():
            gp.pop_last_substate()
            self._open_autotune_amount_menu()

        def get_harmony_label():
            return f"Harmony: {self._bot.autotune_harmony_summary()}"

        def go_harmony():
            gp.pop_last_substate()
            self._open_harmony_menu()

        def get_voice_effect_label():
            return f"Voice Effect: {self._bot.voice_effect_label()}"

        def go_voice_effect():
            gp.pop_last_substate()
            self._open_voice_effect_menu()

        # The dials are only in the list while the switch is on: off, they
        # change nothing a player can hear, and Auto-Tune lines that do nothing
        # are noise between the player and the rest of the page. Turning it on
        # puts them there (the switch rebuilds). Harmony is not one of them.
        dials = [] if not getattr(self._bot, "autotune_enabled", False) else [
            (
                "auto_tune", get_autotune_speed_label, go_autotune_speed,
                "How fast Auto-Tune walks the voice to a note: from Instant, "
                "which is the effect, to Very slow, which is a correction you "
                "only notice when the note lands.",
            ),
            (
                "auto_tune", get_autotune_scale_label, go_autotune_scale,
                "Which notes the voice may be moved to: every semitone, a "
                "major key, a natural, harmonic or melodic minor key, or a "
                "pentatonic, in the key you pick. Each one is described on "
                "its own line.",
            ),
            (
                "auto_tune", get_autotune_amount_label, go_autotune_amount,
                "How much of the correction is used: 100% puts the voice on "
                "the notes of the scale, 50% leaves half the error in it and "
                "half the human wobble, 0% is your own voice with Auto-Tune "
                "still switched on.",
            ),
        ]

        def get_output_label():
            return f"Music Output Device: {self._bot.output_device_label()}"

        def go_output():
            gp.pop_last_substate()
            self._open_output_device_menu()

        m = menu_mod.Menu(self._bot.game, "Music Bot Settings", parrent=gp)
        # One line per feature, and every line says what it does when Tab is
        # pressed on it: the labels have to stay short enough to arrow past, so
        # the explanation rides beside the line instead of inside it.
        m.add_items(gate.filter_items([
            ("output_device", get_output_label, go_output,
             "Where the song comes out: this game's device, or another pair of "
             "speakers or a headset. The equalizer and the room reverb stay on "
             "the game's device, so a song sent elsewhere is unshaped."),
            ("reverb", get_reverb_label, toggle_reverb,
             "Adds the room's own reverb to the song, so a song played inside "
             "a hall sounds like it is in the hall."),
            ("crossfade", get_crossfade_label, toggle_crossfade,
             "Starts the next song before this one ends and blends the two, "
             "so there is no silence between songs."),
            ("eq", get_eq_label, go_eq,
             "Shapes the song's bass, mid and treble. A profile is heard as "
             "soon as it is chosen; Custom opens the three bands as sliders."),
            ("song_key", get_transpose_label, go_transpose,
             "Moves the whole song up or down a semitone at a time. The tempo "
             "and the song's position do not change, so a cabinet and a jam "
             "session stay in step."),
            ("speech_key", get_speech_transpose_label, go_speech_transpose,
             "Moves your broadcast voice up or down a semitone; the song "
             "keeps its own key. It is replaced by Auto-Tune while that is on, "
             "because a chromatic grid already has a note every semitone."),
            ("auto_tune", get_autotune_label, toggle_autotune,
             "Snaps the broadcast voice to the nearest note as you speak. "
             "Retune Speed sets how fast it slides, Auto-Tune Scale sets which "
             "notes count, and it costs about a tenth of a second of delay."),
            *dials,
            ("auto_tune", get_harmony_label, go_harmony,
             "A mode of its own, NOT a dial of Auto-Tune: up to two extra "
             "voices, each an interval above yours (a third, a fifth, a sixth, "
             "a seventh, an octave) or a bass interval below it, which hums "
             "under your words. It sings with Auto-Tune on or off -- with the "
             "tuner off each copy is its plain interval from your voice -- and "
             "switching the tuner never silences it. Every copy rides in the "
             "one broadcast you send. Off is your own voice alone."),
            ("voice_fx", get_voice_effect_label, go_voice_effect,
             "Turns your broadcast voice into an instrument: a distorted "
             "guitar amp, a clean amp with a chorus, a fuzz pedal, a small "
             "radio or a bitcrusher. Your words are still words -- it is the "
             "voice through the pedal, not a guitar -- and Auto-Tune and "
             "Harmony keep working underneath it. Off is your own voice."),
            ("muffle", get_water_label, toggle_water,
             "Muffles the song the way being underwater does, for a player "
             "who dives while it plays."),
            (None, "Back", go_back,
             "Returns to the Music Bot menu. Nothing on this page is lost."),
        ]))
        menus.set_default_sounds(m)
        gp.add_substate(m)
        return m

    def _open_output_device_menu(self):
        """Where this bot's own song is heard: the game's device, or another one.

        The game keeps playing where it always did; this sends the Music Bot's
        own stream somewhere else (the speakers while the game stays in the
        headset). A chosen device is played by a child process of its own
        (`music_bot/output_device.py` says why a process is the honest way to
        do it at all), so the equalizer and the room reverb -- the game's own
        audio engine -- stay on the game's device and the line says so.
        """
        from ... import menu as menu_mod, menus
        gp = self._bot._find_gameplay()
        if not gp:
            return

        def go_back():
            gp.pop_last_substate()
            self._open_settings_menu()

        def choose(specifier):
            self._bot.select_output_device(specifier)
            with contextlib.suppress(Exception):
                m.speak_current_item()

        items = [
            ("Same as the game", lambda: choose(""), None,
             "The song comes out of the same device as the game, with the "
             "equalizer, the room reverb and the underwater muffle."),
        ]
        for device in output_device.available_devices():
            label = output_device.short_name(device)
            if device == self._bot.output_device:
                label = f"{label} (current)"
            items.append((label, functools.partial(choose, device), None,
                          "Sends only the Music Bot's song to this device, so "
                          "the game can stay in the headset. The equalizer "
                          "and the room reverb stay on the game's device."))
        items.append(("Back", go_back, None,
                      "Returns to Music Bot Settings."))
        m = menu_mod.Menu(self._bot.game, "Music Output Device", parrent=gp)
        m.add_items(items)
        menus.set_default_sounds(m)
        gp.add_substate(m)

    def _open_eq_menu(self):
        """Pick the Music Bot equalizer profile (or open the Custom sliders).
        Profiles apply immediately so the change is heard while browsing.
        """
        from ... import menu as menu_mod, menus
        gp = self._bot._find_gameplay()
        if not gp:
            return

        def go_back():
            gp.pop_last_substate()
            self._open_settings_menu()

        def make_label(profile, label):
            def label_fn():
                return f"{label} (active)" if self._bot.eq_profile == profile else label
            return label_fn

        def make_pick(profile, label):
            def pick():
                if profile == "custom":
                    gp.pop_last_substate()
                    self._open_custom_eq_sliders()
                    return
                self._bot.set_eq_profile(profile)
                self._say(f"{label} equalizer applied.")
                m.speak_current_item()
            return pick

        # What each profile does, in the words of somebody who has never met an
        # equalizer (Tab reads it). The profile table itself stays `(id, label)`
        # on the bot -- the effect slot is looked up by it -- so the sentences
        # live here beside the menu that shows them, with a default for a
        # profile added without one.
        profile_help = {
            "normal": "Every band left where the recording has it: no shaping "
                      "at all.",
            "bass_boost": "Raises everything below about 260 Hz: the low end, "
                          "a kick or a bass line, at its own level.",
            "vocal_boost": "Raises the middle, around 500 Hz and 3 kHz: the "
                           "range a voice sits in.",
            "treble_boost": "Raises everything above about 4 kHz: cymbals, "
                            "hi-hats and air.",
            "custom": "Opens Bass, Mid and Treble as three sliders: Up and "
                      "Down adjust, Page Up and Page Down by ten, Home resets "
                      "the band, Enter saves.",
        }
        m = menu_mod.Menu(self._bot.game, "Music Bot Equalizer", parrent=gp)
        items = []
        for profile, label in self._bot.EQ_PROFILES:
            items.append((make_label(profile, label), make_pick(profile, label),
                          None, profile_help.get(
                              profile,
                              "A ready-made shape of the song's bass, mid "
                              "and treble.")))
        items.append(("Back", go_back, None,
                      "Returns to Music Bot Settings."))
        m.add_items(items)
        menus.set_default_sounds(m)
        gp.add_substate(m)

    def _open_transpose_menu(self):
        """Move the song's key, a semitone a press, an octave each way.

        This changes the SONG, not the room: the transposer
        (`libs/music_bot/transpose.py`) works on the decoded frames and returns
        them in the same quantity, so the tempo, the song's position and the
        beat a cabinet or a jam session is keeping are all untouched. A key
        that moves while the song plays bends into the new key rather than
        clicking into it.
        """
        from ... import menu as menu_mod, menus
        gp = self._bot._find_gameplay()
        if not gp:
            return

        def go_back():
            gp.pop_last_substate()
            self._open_settings_menu()

        def step(amount):
            def apply():
                self._bot.set_transpose(self._bot.transpose_semitones + amount)
                self._say(f"Key: {self._bot.transpose_label().lower()}.")
                m.speak_current_item()
            return apply

        def reset():
            self._bot.set_transpose(0)
            self._say("Key: the song's own.")
            m.speak_current_item()

        m = menu_mod.Menu(
            self._bot.game,
            f"Music Bot Key: {self._bot.transpose_label()}",
            parrent=gp,
        )
        m.add_items([
            ("Up One Semitone", step(1), None,
             "Raises the song by a semitone. A song already playing bends "
             "into the new key, and its tempo does not change."),
            ("Down One Semitone", step(-1), None,
             "Lowers the song by a semitone. A song already playing bends "
             "into the new key, and its tempo does not change."),
            ("Original Key", reset, None,
             "Puts the song back in the key it was recorded in."),
            ("Back", go_back, None,
             "Returns to Music Bot Settings."),
        ])
        menus.set_default_sounds(m)
        gp.add_substate(m)

    def _open_speech_transpose_menu(self):
        """Move the SPEECH key; the song keeps the key its own dial chose.

        Same transposer (`libs/music_bot/transpose.py`), applied to the
        microphone's frames instead of the song's (`libs/voice_chat.py`). A
        broadcast voice reaches the map's PA speakers, a cabinet's room and
        the talker's own monitor in the key chosen here, and it is the voice
        that was sent -- a listener's client retunes nothing.
        """
        from ... import menu as menu_mod, menus
        gp = self._bot._find_gameplay()
        if not gp:
            return

        def go_back():
            gp.pop_last_substate()
            self._open_settings_menu()

        def step(amount):
            def apply():
                current = int(getattr(self._bot, "speech_transpose_semitones", 0) or 0)
                self._bot.set_speech_transpose(current + amount)
                self._say(f"Speech key: {self._bot.speech_transpose_label().lower()}.")
                m.speak_current_item()
            return apply

        def reset():
            self._bot.set_speech_transpose(0)
            self._say("Speech key: your own voice.")
            m.speak_current_item()

        m = menu_mod.Menu(
            self._bot.game,
            f"Music Bot Speech Key: {self._bot.speech_transpose_label()}",
            parrent=gp,
        )
        m.add_items([
            ("Up One Semitone", step(1), None,
             "Raises your broadcast voice by a semitone: the voice that "
             "reaches the PA speakers, a cabinet's room and your own "
             "monitor. The song is not touched."),
            ("Down One Semitone", step(-1), None,
             "Lowers your broadcast voice by a semitone. The song is not "
             "touched."),
            ("Original Voice", reset, None,
             "Your own voice, untransposed."),
            ("Back", go_back, None,
             "Returns to Music Bot Settings."),
        ])
        menus.set_default_sounds(m)
        gp.add_substate(m)

    def _open_custom_eq_sliders(self):
        """Open the Bass/Mid/Treble sliders for the Custom profile."""
        gp = self._bot._find_gameplay()
        if not gp:
            return
        self._bot.set_eq_profile("custom", self._bot.eq_values)
        gp.add_substate(_MusicBotEqSlider(self._bot.game, self._bot))


class _MusicBotEqSlider(state.State):
    """Accessible Bass/Mid/Treble sliders for the personal Music Bot EQ.

                    The Custom profile owns one effect slot mutated in place on every tick, so
                    adjustments are audible immediately and no EFX slots leak.
                    """

    BANDS = (("bass", "Bass"), ("mid", "Mid"), ("treble", "Treble"))

    def __init__(self, game, bot):
        super().__init__(game, parrent=bot)
        self.bot = bot
        # The normalization is the bot's own rule (once MapMusicBot was the
        # neighbouring class; the slider is the bot's guest in this module).
        self.values = dict(bot._normalize_eq_values(
            getattr(bot, "eq_values", None)))
        self.current_index = 0
        self._closed = False

    def enter(self):
        super().enter()
        _speak(
            "Music Bot Custom Equalizer. Tab switches Bass, Mid, and Treble. "
            "Up and Down adjust. Page Up and Page Down adjust by 10. "
            "Home resets the current band to 50. Enter saves."
        )
        self._announce_current()

    def exit(self):
        super().exit()
        _speak("Music Bot Equalizer closed.")

    def update(self, events):
        super().update(events)
        for event in events:
            if event.type != pygame.KEYDOWN:
                continue
            key = event.key
            if key == pygame.K_TAB:
                direction = -1 if event.mod & pygame.KMOD_SHIFT else 1
                self.current_index = (self.current_index + direction) % len(self.BANDS)
                self._announce_current()
            elif key in (pygame.K_RETURN, pygame.K_KP_ENTER, pygame.K_ESCAPE):
                self._close_and_commit()
                break
            elif key == pygame.K_UP:
                self._adjust(1)
            elif key == pygame.K_DOWN:
                self._adjust(-1)
            elif key == pygame.K_PAGEUP:
                self._adjust(10)
            elif key == pygame.K_PAGEDOWN:
                self._adjust(-10)
            elif key == pygame.K_HOME:
                self._set_current(50)
        return True

    def _announce_current(self):
        band, label = self.BANDS[self.current_index]
        _speak(f"{label}. Slider: {self.values[band]} percent")

    def _adjust(self, amount):
        band, _ = self.BANDS[self.current_index]
        self._set_current(self.values[band] + amount)

    def _set_current(self, value):
        band, _ = self.BANDS[self.current_index]
        value = max(0, min(100, int(value)))
        if value != self.values[band]:
            self.values[band] = value
            self.bot.set_eq_profile("custom", dict(self.values))
        _speak(f"{value} percent")

    def _close_and_commit(self):
        if self._closed:
            return
        self._closed = True
        self.bot.set_eq_profile("custom", dict(self.values))
        gp = self.bot._find_gameplay()
        if gp is not None:
            gp.pop_last_substate()
        self.bot._open_eq_menu()
