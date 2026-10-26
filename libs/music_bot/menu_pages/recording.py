"""The Record Audio pages of `MusicBotMenus`.

The folder and start/stop controls, the settings page, the two fade pages
(quick times plus a typed value) and the question that restores the defaults.

Moved out of `libs/music_bot/menus.py` on 2026-10-26 and mixed into
`MusicBotMenus`, so every caller still says `bot._menus().<method>()` and the
one-line shims in `controller.py` did not move. The state stays on
`MapMusicBot`: `self.` is this handler and `self._bot.` is the Music Bot. A
page that speaks through the module-level `speak` calls `self._say`, because
`libs/music_bot/menus.speak` is the patch point the menu tests use.

Pins: client/tests/test_game_audio_recorder.py and
test_music_bot_menu_help.py. Rules and the fade writer:
.agents/skills/music_bot_integration/references/recording.md.
"""


class RecordingMenus:
    """The Record Audio page and its settings."""

    def _open_recording_menu(self):
        """Open the persistent folder and game-audio recording controls."""
        from ... import menu as menu_mod, menus

        gp = self._bot._find_gameplay()
        if not gp:
            return

        def start_or_stop():
            # The user returns to gameplay for the countdown/recording. Opening
            # Record Audio again exposes the current status and Stop action.
            gp.pop_last_substate()
            self._bot.audio_recorder.menu_action()

        def go_back():
            gp.pop_last_substate()
            self._show_mode_menu()

        def go_settings():
            gp.pop_last_substate()
            self._open_recording_settings_menu()

        m = menu_mod.Menu(self._bot.game, "Record Audio", parrent=gp)
        items = [
            (self._bot.audio_recorder.folder_menu_label,
             self._bot.audio_recorder.speak_folder, None,
             "The folder recordings are saved to. Enter reads it out again."),
            ("Set Recording Folder", self._bot.audio_recorder.choose_folder,
             None,
             "Chooses where recordings are saved. The dialog opens outside "
             "the game, so the audio keeps running."),
            (self._bot.audio_recorder.menu_label, start_or_stop, None,
             "Starts a recording after a countdown, or stops the one that is "
             "running. A recording cannot be started during a match."),
            (self._bot.audio_recorder.status_menu_label,
             self._bot.audio_recorder.speak_status, None,
             "The current state: what is being captured, how long it has run, "
             "and where the file is going."),
            ("Recording Settings", go_settings, None,
             "What goes into the recording, and how long it runs before it "
             "splits into another file."),
            ("Open Recording Folder", self._bot.audio_recorder.open_folder,
             None,
             "Opens the recording folder on this computer, to listen to or "
             "edit what was captured."),
        ]
        items.append(("Back", go_back, None,
                      "Returns to the Music Bot menu."))
        m.add_items(items)
        menus.set_default_sounds(m)
        gp.add_substate(m)

    def _open_recording_settings_menu(self):
        """Open accessible, persistent settings that are safe for final-mix capture."""
        from ... import menu as menu_mod, menus

        gp = self._bot._find_gameplay()
        if not gp:
            return

        def go_back():
            gp.pop_last_substate()
            self._open_recording_menu()

        def confirm_restore():
            gp.pop_last_substate()
            self._open_recording_reset_confirmation()

        def go_fade_in():
            gp.pop_last_substate()
            self._open_recording_fade_menu("in")

        def go_fade_out():
            gp.pop_last_substate()
            self._open_recording_fade_menu("out")

        m = menu_mod.Menu(self._bot.game, "Audio Recording Settings", parrent=gp)
        m.add_items([
            (self._bot.audio_recorder.capture_scope_label,
             self._bot.audio_recorder.speak_capture_scope, None,
             "What a recording captures: this computer's game audio, the "
             "microphone, or both."),
            (self._bot.audio_recorder.computer_audio_setting_label,
             self._bot.audio_recorder.toggle_computer_audio, None,
             "Whether the game's own audio -- the song, the instruments, the "
             "room -- is in the recording."),
            (self._bot.audio_recorder.microphone_setting_label,
             self._bot.audio_recorder.toggle_microphone, None,
             "Whether this computer's microphone is in the recording, for a "
             "mix with the voice in it."),
            (self._bot.audio_recorder.recorded_voice_label,
             self._bot.audio_recorder.cycle_recorded_voice, None,
             "What your own voice sounds like in the file: the voice as it "
             "was transmitted, with your Voice Effect, Auto-Tune and "
             "Harmony, or your microphone through the Studio Condenser. "
             "The second is for a microphone that needs help; other "
             "players still hear your live voice. It is used only while "
             "Include My Transmitted Voice is On."),
            (self._bot.audio_recorder.countdown_setting_label,
             self._bot.audio_recorder.cycle_countdown, None,
             "How long the countdown is before a recording starts, so there "
             "is time to get ready."),
            (self._bot.audio_recorder.fade_in_setting_label,
             go_fade_in, None,
             "How long the recording takes to come up at its start, so the "
             "file opens on a fade instead of a cut. The fade is written "
             "into the file itself."),
            (self._bot.audio_recorder.fade_out_setting_label,
             go_fade_out, None,
             "How long the recording falls away at its end, so the file "
             "closes on a fade instead of a cut. Written into the file, and "
             "nothing extra happens at Stop."),
            (self._bot.audio_recorder.split_setting_label,
             self._bot.audio_recorder.cycle_split_minutes, None,
             "How long one file runs before the next one starts: a long mix "
             "is easier to edit in pieces."),
            (self._bot.audio_recorder.announce_setting_label,
             self._bot.audio_recorder.toggle_announce_details, None,
             "Whether each finished file, and its size, is spoken -- for a "
             "player who cannot see the folder."),
            ("Restore Recording Defaults", confirm_restore, None,
             "Puts every recording setting back to the value it ships with. "
             "It asks once more before doing it."),
            ("Back", go_back, None,
             "Returns to the Record Audio menu."),
        ])
        menus.set_default_sounds(m)
        gp.add_substate(m)

    def _open_recording_fade_menu(self, kind="in"):
        """How long one end of the recording takes to fade: quick times, then any.

        A recording of a live room almost never starts or ends on silence -- a
        session that begins on a downbeat and stops mid-chord leaves a click at
        each end -- so the fade is written into the file, and what leaves the
        game is what any editor or player hears. It changes nothing about
        playback, the mix or the microphone.

        The quick rows are the times people ask for; `Custom Seconds` opens the
        client's own text field for any whole second up to `MAX_FADE_SECONDS`,
        because "12 seconds" is not a value worth arrowing seven rows to find.
        """
        from ... import menu as menu_mod, menus
        from ...game_audio_recorder import FADE_CHOICES, MAX_FADE_SECONDS

        gp = self._bot._find_gameplay()
        if not gp:
            return

        recorder = self._bot.audio_recorder
        fade_in = kind != "out"
        title = "Fade In at Start" if fade_in else "Fade Out at Stop"

        def live_seconds():
            return recorder.fade_in_seconds() if fade_in else recorder.fade_out_seconds()

        def go_back():
            gp.pop_last_substate()
            self._open_recording_settings_menu()

        def apply(seconds):
            if fade_in:
                recorder.set_fade_in(seconds)
            else:
                recorder.set_fade_out(seconds)

        def pick(seconds):
            def chosen():
                apply(seconds)
                m.speak_current_item()
            return chosen

        def make_label(seconds):
            # Read live, like the scale and tune amount lists: the mark follows
            # the choice on the page that is already open.
            def label_fn():
                label = recorder.fade_time_text(seconds)
                if abs(live_seconds() - seconds) < 1e-9:
                    return f"{label} (active)"
                return label
            return label_fn

        def submitted(text):
            """Take what was typed, or say why nothing changed.

            **The field is a substate and is taken off the stack on EVERY exit,**
            Escape with nothing typed included: its `ck` answers True whenever
            it is open, so a handler that forgets this leaves it holding every
            key for good and the game looks frozen. The tune amount field and
            the song search pop it in the same place for the same reason.
            """
            gp.pop_last_substate()
            text = "" if text is None else str(text).strip()
            before = live_seconds()
            if not text:
                self._say(f"{title} unchanged, {recorder.fade_time_text(before)}.")
                return
            try:
                value = float(text)
            except ValueError:
                self._say(f"'{text}' is not a number of seconds, so "
                      f"{title.lower()} is still {recorder.fade_time_text(before)}.")
                return
            apply(value)
            after = live_seconds()
            if after:
                self._say(f"{title}: {recorder.fade_time_text(after)}.")
            else:
                self._say(f"{title} off.")

        def go_custom():
            gp.add_substate(self._bot.game.input.run(
                f"Enter a fade of 0 to {int(MAX_FADE_SECONDS)} whole seconds "
                f"(currently {recorder.fade_time_text(live_seconds())}):",
                handeler=submitted,
                min_val=0, max_val=int(MAX_FADE_SECONDS),
                msg_length=2, multiline=False,
            ))

        notes = {
            0.0: "No fade: the file begins (or ends) exactly where the sound does.",
            0.5: "Half a second: enough to soften a cut without losing any of the take.",
            1.0: "One second: a short fade a listener does not notice.",
            2.0: "Two seconds: a fade you can hear, still short enough to keep the take.",
            3.0: "Three seconds: a deliberate fade, for a session that starts or ends on a chord.",
            5.0: "Five seconds: an effect rather than a guard against a click.",
            10.0: "Ten seconds: a long fade, for a recording with a tail on it.",
        }
        items = [(make_label(seconds), pick(seconds), None, notes[seconds])
                 for seconds in FADE_CHOICES]
        items.append(("Custom Seconds", go_custom, None,
                      f"Type the fade yourself, in whole seconds from 0 to "
                      f"{int(MAX_FADE_SECONDS)}. 0 is off, the same as no fade."))
        items.append(("Back", go_back, None,
                      "Returns to Audio Recording Settings."))
        m = menu_mod.Menu(
            self._bot.game,
            f"Music Bot {title}: {recorder.fade_time_text(live_seconds())}",
            parrent=gp,
        )
        m.add_items(items)
        menus.set_default_sounds(m)
        gp.add_substate(m)

    def _open_recording_reset_confirmation(self):
        """Require an explicit second action before restoring recording defaults."""
        from ... import menu as menu_mod, menus

        gp = self._bot._find_gameplay()
        if not gp:
            return

        def restore():
            gp.pop_last_substate()
            self._bot.audio_recorder.restore_setting_defaults()
            self._open_recording_settings_menu()

        def cancel():
            gp.pop_last_substate()
            self._open_recording_settings_menu()

        m = menu_mod.Menu(
            self._bot.game,
            "Restore all audio recording settings to their defaults?",
            parrent=gp,
        )
        m.add_items([
            ("Yes, Restore Recording Defaults", restore, None,
             "Puts every recording setting back to the value it ships with. "
             "Recordings already saved are not touched."),
            ("Cancel and Keep Current Settings", cancel, None,
             "Leaves every setting as it is and goes back."),
        ])
        menus.set_default_sounds(m)
        gp.add_substate(m)
