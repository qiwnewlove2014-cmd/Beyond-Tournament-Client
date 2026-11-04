"""The External audio pages of `MusicBotMenus`.

Five short pages instead of one long one, because the first version put every
program playing sound on the page the moment it opened: a wall of buttons with
the search nowhere in sight. Where it lives moved too -- the Party Sync menu
carries the entry, because that is the session whose listeners it feeds, and
the Music Bot menu is long enough for a screen reader already.

    Party Sync  ->  "External audio from a music program"
    External audio      status, Choose a program, Choose a device or input,
                        whole computer, channel, level, stop, say status, Back
    Choose a program    "Type to search", the matches (at most 24), Back
    Choose a device or input  a playback-device page, an input page, Back
    Capture a playback device  the playback endpoints, at most 8, Back
    Capture an audio input  the recording endpoints, at most 8, Back
    a text box          type part of a program's name, Enter filters

The capture itself is `libs/external_audio.py`; these pages only choose a
source and two numbers.

Two rules the pages keep, both learned from the menus around them:

* **Every line is explainable.** The fourth field of each entry is what Tab
  reads, and it says the effect on the people listening -- not the name of a
  setting. A blind player and a sighted player read the same sentence.
* **A choice is confirmed out loud.** Enter on a program starts capturing it
  and the page says which program and which channel mode, then re-opens with
  the new status at the top; nothing here is silent about what it just did.

Pins: client/tests/test_external_audio.py (the page shape: the root keeps the
hardware routes beside the picker, the picker caps and filters and does not
mix devices into program matches, and the entry does not come back to the
Music Bot menu),
test_music_bot_queue_settings.py (a shim per menu), test_party_sync.py (the
Party Sync page builds).
"""


class ExternalAudioMenus:
    """Capture another program (or the computer) as this bot's live source."""

    LEVELS = (40, 60, 85, 100)
    #: How many programs a picker page shows before it asks for a search word.
    PAGE_MATCHES = 24
    #: Devices stay a short list; a playback endpoint is not a program.
    DEVICE_MATCHES = 8

    def _open_external_audio_menu(self):
        """The short root page: what is captured, and the four things to change."""
        from ... import menu as menu_mod, menus
        from ...external_audio import MODE_MONO, MODE_STEREO

        gp = self._bot._find_gameplay()
        if gp is None:
            return
        m = menu_mod.Menu(self._bot.game, "External audio", parrent=gp)
        items = []

        def close_top():
            if gp.substates and gp.substates[-1] is m:
                gp.pop_last_substate()

        def back_to_party_menu():
            close_top()
            # The page that opened this one is still underneath (the full
            # Party Sync page or the Ctrl+F8 quick menu), so going back is a
            # pop. Only a page opened with no parent needs the fallback.
            if not gp.substates:
                self._open_party_sync_menu()

        def go_pick():
            close_top()
            self._open_external_program_menu()

        def go_sources():
            close_top()
            self._open_external_source_menu()

        def use_system():
            close_top()
            source = self._bot.start_external_audio(
                kind="system", label="This computer",
                mode=self._bot.external_audio_mode,
                gain=self._bot.external_audio_gain,
            )
            if source is None:
                self._say("Could not start capturing this computer's audio.")
            else:
                self._say(
                    f"Capturing everything this computer plays, in "
                    f"{source.mode}. Anything you hear, the people listening "
                    "hear too."
                )
            self._open_external_audio_menu()

        def use_off():
            close_top()
            self._bot.stop_external_audio()
            self._say("External audio stopped. Your music and instruments "
                      "still play as before.")
            self._open_external_audio_menu()

        def toggle_mode():
            if self._bot.external_audio_mode == MODE_STEREO:
                self._bot.set_external_audio_mode(MODE_MONO)
            else:
                self._bot.set_external_audio_mode(MODE_STEREO)
            self._say(self._bot.external_audio_status())
            m.speak_current_item()

        def cycle_level():
            current = int(round(self._bot.external_audio_gain * 100))
            levels = self.LEVELS
            position = 0
            for index, value in enumerate(levels):
                if current >= value:
                    position = index
            nxt = levels[(position + 1) % len(levels)]
            self._bot.set_external_audio_gain(nxt / 100.0)
            self._say(f"External audio level {nxt} percent.")
            m.speak_current_item()

        def say_status():
            self._say(self._bot.external_audio_status())

        def say_signal():
            self._say(self._bot.external_audio_signal_report())

        items.append((
            self._bot.external_audio_status, say_status, None,
            "What is being captured right now, in which channel mode, and "
            "where it is being sent. Enter repeats the description; nothing "
            "changes on this line.",
        ))

        items.append((
            self._bot.external_audio_signal_label, say_signal, None,
            "Whether sound is actually arriving from the program right now: "
            "silent, very quiet, quiet, good, loud or clipping, with the peak "
            "level. Enter repeats the verdict and says what to check when it "
            "disagrees with what you can hear.",
        ))

        items.append((
            "Choose a program", go_pick, None,
            f"Opens a list of up to {self.PAGE_MATCHES} programs playing sound "
            "on this computer, with a search box above it. Choosing one "
            "captures only that program.",
        ))

        items.append((
            "Choose a device or input", go_sources, None,
            "Opens a short page that separates the two hardware routes: a "
            "playback device for a program Windows cannot see, or a recording "
            "input such as a line-in, USB interface or microphone.",
        ))

        source = getattr(self._bot, "external_audio", None)
        if source is not None:
            items.append((
                "Stop capturing", use_off, None,
                "Stops the capture at once. The song you were playing, your "
                "instruments, your party and your broadcast all keep working.",
            ))

        items.append((
            "Capture this whole computer", use_system, None,
            "Captures everything this computer plays, not just one program. "
            "Use it when your music program does not appear in the list; "
            "while it runs, the game's own sounds are captured too, so keep "
            "your speakers low or use headphones.",
        ))

        mode_label = (
            "Channel: Stereo (the program's own left and right)"
            if self._bot.external_audio_mode == MODE_STEREO
            else "Channel: Mono (one centred sound)"
        )
        items.append((
            mode_label, toggle_mode, None,
            "Stereo keeps the program's left and right apart, which is what a "
            "song or a stereo mix wants. Mono puts one centred signal on both "
            "sides, which is what a single instrument wants. Enter switches.",
        ))

        level = int(round(self._bot.external_audio_gain * 100))
        items.append((
            f"Level: {level} percent", cycle_level, None,
            "How loud the captured program is sent compared with your song. "
            "Enter cycles 40, 60, 85 and 100 percent; the change is heard "
            "immediately by everyone listening.",
        ))

        items.append((
            "Back", back_to_party_menu, None,
            "Returns to the Party Sync menu. A running capture keeps running.",
        ))

        m.add_items(items)
        menus.set_default_sounds(m)
        gp.add_substate(m)

    def _open_external_source_menu(self):
        """The short fork: a playback device or a recording input.

        These two used to sit at the bottom of the program picker. That put
        hardware choices among program matches, where a player looking for a
        line-in had to walk past every program first. The picker now only
        chooses a program; this page is the one place for the other routes.
        """
        from ... import menu as menu_mod, menus

        gp = self._bot._find_gameplay()
        if gp is None:
            return
        m = menu_mod.Menu(self._bot.game, "Choose a device or input", parrent=gp)
        items = []

        def close_top():
            if gp.substates and gp.substates[-1] is m:
                gp.pop_last_substate()

        def back_to_root():
            close_top()
            self._open_external_audio_menu()

        def go_playback():
            close_top()
            self._open_external_device_menu()

        def go_input():
            close_top()
            self._open_external_input_menu()

        items.append((
            "Capture a playback device", go_playback, None,
            "Lists the playback devices on this computer, including a "
            "virtual cable. Choose the device your program plays out of when "
            "Windows cannot see the program itself -- an ASIO program has no "
            "audio session, but its sound still reaches a device.",
        ))
        items.append((
            "Capture an audio input", go_input, None,
            "Lists the recording inputs on this computer, such as a USB "
            "interface, a line-in or a microphone. Choose one to send what is "
            "plugged into it; no program needs to be running. A microphone "
            "also hears your speakers, so use headphones to avoid feedback.",
        ))
        items.append((
            "Back", back_to_root, None,
            "Returns to the External audio page. Nothing is changed.",
        ))

        m.add_items(items)
        menus.set_default_sounds(m)
        gp.add_substate(m)

    def _open_external_device_menu(self):
        """Capture a playback device: the route for a program Windows cannot see."""
        from ... import menu as menu_mod, menus
        from ...external_audio import render_devices

        gp = self._bot._find_gameplay()
        if gp is None:
            return
        m = menu_mod.Menu(self._bot.game, "Capture a playback device", parrent=gp)
        items = []

        def close_top():
            if gp.substates and gp.substates[-1] is m:
                gp.pop_last_substate()

        def back_to_root():
            close_top()
            self._open_external_source_menu()

        def use_device(identifier, name):
            close_top()
            source = self._bot.start_external_audio(
                kind="device", target=identifier, label=name,
                mode=self._bot.external_audio_mode,
                gain=self._bot.external_audio_gain,
            )
            if source is None:
                self._say(f"Could not start capturing {name}.")
            else:
                self._say(
                    f"Capturing the device {name} in {source.mode}. Any "
                    "program that plays out of this device is sent with your "
                    "music, so route the program here first."
                )
            self._open_external_audio_menu()

        devices = []
        try:
            devices = render_devices()
        except Exception:
            devices = []
        if not devices:
            self._say("This computer reports no playback devices to capture.")
        for item in devices[:self.DEVICE_MATCHES]:
            identifier = item.get("id")
            name = item.get("name") or "Unnamed device"
            if not identifier:
                continue
            cable = " (virtual cable)" if item.get("virtual") else ""
            items.append((
                f"Capture {name}{cable}",
                lambda i=identifier, n=name: use_device(i, n),
                None,
                "Captures everything this device plays, not one program. The "
                "route for a program on an ASIO driver: set its output to "
                "this device (or to a virtual cable listed here) and its "
                "sound arrives with no session.",
            ))
        if len(devices) > self.DEVICE_MATCHES:
            items.append((
                f"{len(devices) - self.DEVICE_MATCHES} more devices are not shown",
                lambda: None, None,
                f"Only the first {self.DEVICE_MATCHES} devices are listed. "
                "Unplug or disable one you do not use, or set the one you "
                "need as the default playback device and use Capture this "
                "whole computer.",
            ))
        items.append((
            "Back", back_to_root, None,
            "Returns to the device or input page. Nothing is changed.",
        ))

        m.add_items(items)
        menus.set_default_sounds(m)
        gp.add_substate(m)

    def _open_external_input_menu(self):
        """Capture a recording endpoint: a line-in, USB interface or microphone."""
        from ... import menu as menu_mod, menus
        from ...external_audio import capture_devices

        gp = self._bot._find_gameplay()
        if gp is None:
            return
        m = menu_mod.Menu(self._bot.game, "Capture an audio input", parrent=gp)
        items = []

        def close_top():
            if gp.substates and gp.substates[-1] is m:
                gp.pop_last_substate()

        def back_to_source():
            close_top()
            self._open_external_source_menu()

        def use_input(identifier, name):
            close_top()
            source = self._bot.start_external_audio(
                kind="input", target=identifier, label=name,
                mode=self._bot.external_audio_mode,
                gain=self._bot.external_audio_gain,
            )
            if source is None:
                self._say(f"Could not start capturing {name}.")
            else:
                self._say(
                    f"Capturing the input {name} in {source.mode}. Whatever "
                    "is plugged into it is sent with your music. If it is a "
                    "microphone, use headphones so the speakers do not feed "
                    "back."
                )
            self._open_external_audio_menu()

        devices = []
        try:
            devices = capture_devices()
        except Exception:
            devices = []
        if not devices:
            self._say("This computer reports no audio inputs to capture.")
        for item in devices[:self.DEVICE_MATCHES]:
            identifier = item.get("id")
            name = item.get("name") or "Unnamed input"
            if not identifier:
                continue
            items.append((
                f"Capture input {name}",
                lambda i=identifier, n=name: use_input(i, n),
                None,
                "Captures what is plugged into this input, not one program. "
                "Use it for a mixer, a USB audio interface or a line-in. A "
                "microphone also hears the room, so wear headphones.",
            ))
        if len(devices) > self.DEVICE_MATCHES:
            items.append((
                f"{len(devices) - self.DEVICE_MATCHES} more inputs are not shown",
                lambda: None, None,
                f"Only the first {self.DEVICE_MATCHES} inputs are listed. "
                "Disable an input you do not use, or set the one you need as "
                "the default recording device and reopen this page.",
            ))
        items.append((
            "Back", back_to_source, None,
            "Returns to the device or input page. Nothing is changed.",
        ))

        m.add_items(items)
        menus.set_default_sounds(m)
        gp.add_substate(m)

    def _open_external_program_menu(self, query=""):
        """The picker: a search line, then at most PAGE_MATCHES programs."""
        from ... import menu as menu_mod, menus
        from ...external_audio import display_program_name, running_apps

        gp = self._bot._find_gameplay()
        if gp is None:
            return
        m = menu_mod.Menu(self._bot.game, "Choose a program", parrent=gp)
        items = []

        def close_top():
            if gp.substates and gp.substates[-1] is m:
                gp.pop_last_substate()

        def back_to_root():
            close_top()
            self._open_external_audio_menu()

        def ask_for_a_word():
            close_top()

            def submit(text):
                if gp.substates:
                    gp.pop_last_substate()
                word = (text or "").strip()
                if not word:
                    self._say("Showing every program that is playing sound.")
                self._open_external_program_menu(word)

            gp.add_substate(self._bot.game.input.run(
                "Type part of the program's name, then press Enter:",
                handeler=submit,
            ))

        def use_app(pid, name):
            close_top()
            source = self._bot.start_external_audio(
                kind="app", target=pid, label=name,
                mode=self._bot.external_audio_mode,
                gain=self._bot.external_audio_gain,
            )
            if source is None:
                self._say(f"Could not start capturing {name}.")
            else:
                self._say(
                    f"Capturing {name} in {source.mode}. It is sent wherever "
                    "your music is going, so the people listening hear your "
                    "program with you."
                )
            self._open_external_audio_menu()

        def use_system():
            close_top()
            source = self._bot.start_external_audio(
                kind="system", label="This computer",
                mode=self._bot.external_audio_mode,
                gain=self._bot.external_audio_gain,
            )
            if source is None:
                self._say("Could not start capturing this computer's audio.")
            else:
                self._say(
                    f"Capturing everything this computer plays, in "
                    f"{source.mode}. Anything you hear, the people listening "
                    "hear too."
                )
            self._open_external_audio_menu()

        apps = []
        try:
            apps = running_apps()
        except Exception:
            apps = []
        needle = (query or "").strip().casefold()
        matches = []
        for app in apps:
            raw_name = app.get("name") or ""
            label_name = display_program_name(raw_name)
            # Searching still accepts the process name the player sees in
            # Task Manager (``explorer.exe``), while the menu speaks the
            # readable form (``explorer``).
            if (needle in raw_name.casefold()
                    or needle in label_name.casefold()):
                matches.append(app)
        if needle:
            if matches:
                self._say(f"{len(matches)} programs match {query}.")
            else:
                self._say(f"Nothing matches {query}. Press Enter to search "
                          "again, or use the whole computer.")

        items.append((
            "Type to search for a program", ask_for_a_word, None,
            "Opens a text box: type part of the program's name and press "
            "Enter to shorten the list to what matches. Every program that "
            "is open is listed, not only one already playing sound, because "
            "a music program on an ASIO driver has no Windows audio session "
            "until its output is switched. Press Enter with nothing typed to "
            "see every program again.",
        ))

        if not apps:
            items.append((
                "No program is listed right now", ask_for_a_word, None,
                "No program with a window is running, and nothing is "
                "rendering audio. Start the program you want, then search "
                "again.",
            ))
        elif not matches:
            items.append((
                f"Nothing matches: {query}", ask_for_a_word, None,
                "No program playing sound has that in its name. Enter opens "
                "the search box again; the whole-computer line below still "
                "works.",
            ))
        else:
            for app in matches[:self.PAGE_MATCHES]:
                name = display_program_name(app.get("name") or "")
                pid = app.get("pid")
                if not pid:
                    continue
                playing_now = " (playing now)" if app.get("playing") else ""
                items.append((
                    f"Capture {name}{playing_now}",
                    lambda p=pid, n=name: use_app(p, n),
                    None,
                    f"Captures only {name}'s audio, so nothing else on this "
                    "computer is sent with it. Choose this for a music "
                    "program you are playing or playing into.",
                ))
            hidden = len(matches) - self.PAGE_MATCHES
            if hidden > 0:
                items.append((
                    f"{hidden} more -- type to narrow the list",
                    ask_for_a_word, None,
                    f"The list stops at {self.PAGE_MATCHES} names so it stays "
                    "readable. Typing a few letters of the one you want is "
                    "faster than walking past the rest.",
                ))

        items.append((
            "Capture this whole computer",
            use_system, None,
            "Everything this computer plays, for a program that does not "
            "appear above. It also captures the game's own sound, so keep "
            "your speakers low or use headphones.",
        ))
        items.append((
            "Back", back_to_root, None,
            "Returns to the External audio page. Nothing is changed.",
        ))

        m.add_items(items)
        menus.set_default_sounds(m)
        gp.add_substate(m)
