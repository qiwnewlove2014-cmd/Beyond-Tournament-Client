"""The Picks page of `MusicBotMenus`.

Mark the local file that is playing, then copy everything marked into one
folder: a cull-by-ear pass over a folder of songs without leaving the game.
The state is the bot's (`picks`, saved as the option `music_bot_picks`, and the
one place a pick is added or dropped is `MapMusicBot.toggle_current_pick`), so
this page only shows the list, plays it and asks for the destination folder.

`self.` is this handler and `self._bot.` is the Music Bot, and a page that
speaks through `menus.speak` calls `self._say`, because that name is what the
menu tests patch (see `menus.py`'s docstring).

Pins: client/tests/test_music_bot_picks.py.
Rules: .agents/skills/music_bot_integration/references/queue-and-local-files.md.
"""


class PicksMenus:
    """The Picks page: what was marked, played as a batch, and copied out."""

    def _open_picks_menu(self):
        from ... import menu as menu_mod, menus

        gp = self._bot._find_gameplay()
        if not gp:
            return

        def go_back():
            gp.pop_last_substate()
            self._show_mode_menu()

        def rebuild():
            # The list just changed, so the page is rebuilt from the bot's own
            # picks rather than edited: one authority, one rendering.
            if gp.substates and gp.substates[-1] is m:
                gp.pop_last_substate()
            self._open_picks_menu()

        def go_copy():
            gp.pop_last_substate()
            self._choose_picks_folder()

        def clear_all():
            self._bot.clear_picks()
            rebuild()

        def say_empty():
            self._say("Nothing picked yet. Mark a local file while it plays, "
                      "from the Music Bot menu.")

        m = menu_mod.Menu(self._bot.game, self._bot.picks_label(), parrent=gp)
        items = []
        for index, pick in enumerate(getattr(self._bot, "picks", ()) or ()):
            def play(index=index):
                # The rest of the list follows the file that was chosen, so one
                # Enter plays the cull in the order it was listened to.
                tracks = [dict(track, source="local")
                          for track in self._bot.picks[index:]]
                if not tracks:
                    return
                gp.pop_last_substate()
                self._bot._start_track_queue(tracks, "Picks")

            items.append((f"{index + 1}. {pick.get('title', 'Unknown')}", play,
                          None,
                          "Plays this file now, with every pick below it queued "
                          "behind it."))
        if not items:
            items.append(("Nothing picked yet", say_empty, None,
                          "Mark a local file while it plays, and it appears "
                          "here to be copied out."))
        items.append(("Copy Picks to a Folder...", go_copy, None,
                      "Copies every picked file into a folder you choose. The "
                      "originals stay where they are."))
        items.append(("Clear Picks", clear_all, None,
                      "Forgets every pick. The files themselves are untouched."))
        items.append(("Back", go_back, None,
                      "Returns to the Music Bot menu."))
        m.add_items(items)
        menus.set_default_sounds(m)
        gp.add_substate(m)

    def _choose_picks_folder(self):
        """Ask where the picks go, then copy off the main thread."""
        import threading

        from ... import folder_dialog

        def choose():
            folder, problem = folder_dialog.ask_folder("Copy picks to a folder")
            if problem:
                self._say(f"Could not open the folder dialog: {problem}")
                return
            if not folder:
                self._say("No folder selected.")
                return
            self._bot._copy_picks(folder)

        threading.Thread(target=choose, daemon=True).start()
        self._say("Opening the folder list...")
