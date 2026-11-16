"""The Audio File Converter page of `MusicBotMenus`.

Two doors, the same shape as Record Audio and Music Download Center: a page of
folder and format choices, and a settings page under it. The job itself is
`MapMusicBot._converter()` (`libs/music_bot/converter.py`); this page only
reads its labels and calls it, so the menu can never be holding a second copy
of the batch state.

Files come from three places, because that is what "convert files" means here:
the player's own multi-select (`libs/folder_dialog.py`, asked in its own
process so the song does not hitch), a folder (`music_files_in`, the same scan
`Choose a Folder of Music` uses), or the Picks from the page beside this one.

`self.` is this handler and `self._bot.` is the Music Bot; a page that speaks
through `menus.speak` calls `self._say` (see `menus.py`'s docstring).

Pins: client/tests/test_music_bot_converter.py
Rules: .agents/skills/music_bot_integration/references/audio-converter.md
"""

import os

from .. import menu_prefs


class ConverterMenus:
    """The Audio File Converter: choose files, format and destination."""

    def _open_converter_menu(self):
        from ... import menu as menu_mod, menus
        from .. import gate

        gp = self._bot._find_gameplay()
        if not gp:
            return
        if gate.blocked("converter"):
            # The Mode line is already hidden while the feature is locked; a
            # page still on the stack (locked after it was opened) says whose
            # decision it was rather than running a batch nobody can see.
            return
        converter = self._bot._converter()
        converter.show_progress_bar()

        picks = list(getattr(self._bot, "picks", ()) or ())

        def go_back():
            gp.pop_last_substate()
            self._show_mode_menu()

        def go_settings():
            gp.pop_last_substate()
            self._open_converter_settings_menu()

        def leave_then(action):
            # The player asked for a batch, so the page gets out of the way the
            # way Music Download Center does: the dialog opens outside the game
            # and the conversion runs while they play.
            gp.pop_last_substate()
            action()

        def convert_picks():
            converter.start([pick.get("target", "") for pick in picks], "Picks")

        class ConverterMenu(menu_mod.Menu):
            def exit(menu_self):
                # The bar belongs to the page: the conversion keeps running
                # after it closes, exactly as a download does.
                converter.hide_progress_bar()
                super().exit()

        # The `intro` is what stops the one confusion this page produced: the
        # Convert lines ask which files to convert, and the Output line above
        # them is where those files go. Two folder pickers that ask different
        # questions read as the same question when nothing says so.
        m = ConverterMenu(
            self._bot.game, "Audio File Converter", parrent=gp,
            intro="Output is where converted files go. The two Convert lines "
                  "ask which files to convert, and every original stays "
                  "where it is.",
        )
        items = [
            ("converter_output", converter.output_menu_label(),
             converter.speak_output,
             "Where converted files are written. Next to the Original puts "
             "each one beside the file it came from, and never touches that "
             "file."),
            ("converter_output_set", "Set Output Folder...",
             lambda: leave_then(self._set_converter_output_folder),
             "Chooses one folder for every converted file. The dialog opens "
             "outside the game, so the song keeps playing."),
        ]
        if converter.has_saved_folder():
            items.append((
                "converter_output_clear", "Save Next to the Original",
                converter.clear_folder,
                "Forgets the chosen folder. Files already converted are left "
                "where they are; the next file is written beside its "
                "original."))
        items.extend([
            ("converter_format", converter.format_menu_label(), go_settings,
             "What every converted file becomes: MP3 at three bitrates, OGG, "
             "WAV or FLAC."),
            ("converter_files", "Convert Files I Choose...",
             lambda: leave_then(self._choose_converter_files),
             "Converts files that are already on this computer: pick as many "
             "as you like at once. The originals stay exactly where they are."),
            ("converter_folder", "Convert a Folder of Files...",
             lambda: leave_then(self._choose_converter_folder),
             "Converts every audio file directly inside a folder you pick, in "
             "name order. Subfolders are left alone, the same as Choose a "
             "Folder of Music."),
        ])
        if picks:
            items.append(("converter_picks", f"Convert Picks ({len(picks)})...",
                          lambda: leave_then(convert_picks),
                          "Converts the files you marked while listening, in "
                          "the order you marked them."))
        items.append(("converter_progress", converter.progress_menu_label(),
                      converter.speak_status,
                      "How the batch is going: how many files are done, the "
                      "percentage, and which file is converting now."))
        if converter.is_active():
            items.append((
                "converter_cancel", "Cancel Conversion", converter.cancel,
                "Stops the batch. Files already converted are kept, and the "
                "original that was being read is left alone."))
        items.append((None, "Back", go_back,
                      "Returns to the Music Bot menu. A running conversion "
                      "keeps running."))
        menu_prefs.build(m, items)
        menus.set_default_sounds(m)
        gp.add_substate(m)

    def _open_converter_settings_menu(self):
        """The format catalogue: one line each, with the active one marked."""
        from ... import menu as menu_mod, menus
        from ..converter import FORMATS

        gp = self._bot._find_gameplay()
        if not gp:
            return
        converter = self._bot._converter()

        def go_back():
            gp.pop_last_substate()
            self._open_converter_menu()

        def make_label(fmt):
            def label():
                mark = " (active)" if converter.format_key() == fmt["key"] else ""
                return fmt["label"] + mark
            return label

        def pick(key):
            converter.set_format(key)
            # The page stays open and the mark moves on the page already open,
            # the way every page of values behaves.
            m.speak_current_item()

        m = menu_mod.Menu(self._bot.game, "Conversion Settings", parrent=gp)
        items = [(make_label(fmt), lambda fmt=fmt: pick(fmt["key"]), None,
                  fmt["note"]) for fmt in FORMATS]
        items.append((converter.announce_menu_label,
                      lambda: (converter.toggle_announce(),
                               m.speak_current_item()),
                      None,
                      "Whether the batch says how many files are done as it "
                      "goes. Off keeps the game quiet and only the final "
                      "sentence is spoken."))
        items.append(("Back", go_back, None,
                      "Returns to the Audio File Converter."))
        m.add_items(items)
        menus.set_default_sounds(m)
        gp.add_substate(m)

    def _choose_converter_files(self):
        """Ask for the files to convert, then convert them off this thread."""
        import threading

        from ... import folder_dialog
        from ..track_source import MEDIA_PATTERNS

        converter = self._bot._converter()

        def choose():
            chosen, problem = folder_dialog.ask_files(
                "Which files should be converted?", pattern=MEDIA_PATTERNS)
            if problem:
                self._say(f"Could not open the file dialog: {problem}")
                return
            paths = [line for line in str(chosen or "").splitlines()
                     if line.strip()]
            if not paths:
                self._say("No files selected.")
                return
            converter.start(paths, "your selection")

        # Said before the thread starts: the player hears the line as the
        # dialog opens, and a choose step that finishes at once cannot push it
        # behind its own answer.
        self._say("Opening the file list...")
        threading.Thread(target=choose, daemon=True).start()

    def _choose_converter_folder(self):
        """Convert every audio file in a folder the player picks."""
        import threading

        from ... import folder_dialog
        from ..track_source import music_files_in

        converter = self._bot._converter()

        def choose():
            folder, problem = folder_dialog.ask_folder(
                "Which folder's files should be converted?")
            if problem:
                self._say(f"Could not open the folder dialog: {problem}")
                return
            if not folder:
                self._say("No folder selected.")
                return
            paths = music_files_in(folder)
            if not paths:
                self._say("No audio files were found in that folder.")
                return
            converter.start(paths, os.path.basename(folder.rstrip(os.sep))
                            or folder)

        self._say("Opening the folder list...")
        threading.Thread(target=choose, daemon=True).start()

    def _set_converter_output_folder(self):
        """Ask where converted files go; an empty answer changes nothing."""
        import threading

        from ... import folder_dialog

        converter = self._bot._converter()

        def choose():
            folder, problem = folder_dialog.ask_folder(
                "Where should the converted files be saved?")
            if problem:
                self._say(f"Could not open the folder dialog: {problem}")
                return
            if not folder:
                self._say("No folder selected. Converted files stay next to "
                          "the original.")
                return
            converter.set_folder(folder)

        self._say("Opening the folder list...")
        threading.Thread(target=choose, daemon=True).start()
