"""The personal library pages of `MusicBotMenus`.

Playlists and Favorites, the shuffled personal feed, the play-next queue, the
download center and the action page a search result opens (Play Now / Next /
Download / Save / Add to Playlist). Everything here reads or writes the bot's
own library managers; nothing is sent to the Server.

Moved out of `libs/music_bot/menus.py` on 2026-10-26 and mixed into
`MusicBotMenus`, so every caller still says `bot._menus().<method>()` and the
one-line shims in `controller.py` did not move. The state stays on
`MapMusicBot`: `self.` is this handler and `self._bot.` is the Music Bot. A
page that speaks through the module-level `speak` calls `self._say`, because
`libs/music_bot/menus.speak` is the patch point the menu tests use.

Pins: client/tests/test_music_downloader.py, test_party_queue_share.py (the
queue's wording belongs to `song_requests`) and test_music_bot_menu_help.py.
Rules: .agents/skills/music_bot_integration/.
"""

from .. import song_requests
from ..music_downloader import is_supported_music_url


class LibraryMenus:
    """Playlists, Favorites, the feed, the queue and downloads."""

    def _show_personal_feed_menu(self):
        """Open the shuffled Favorites feed controls."""
        from ... import menu as menu_mod, menus
        gp = self._bot._find_gameplay()
        if not gp:
            return

        def start_feed():
            gp.pop_last_substate()
            self._bot._start_personal_feed()

        def next_feed():
            gp.pop_last_substate()
            self._bot._next_personal_feed()

        m = menu_mod.Menu(self._bot.game, "Personal Music Feed", parrent=gp)
        m.add_items([
            ("Start shuffled Favorites feed", start_feed, None,
             "Plays your Favorites in a shuffled order, one song after "
             "another, until you stop it. It takes nothing out of your play "
             "queue."),
            ("Next feed song", next_feed, None,
             "Moves to the next shuffled song now, instead of waiting for "
             "this one to end."),
            ("Back", lambda: (gp.pop_last_substate(),
                              self._show_mode_menu()), None,
             "Returns to the Music Bot menu. The feed keeps playing if it "
             "was started."),
        ])
        menus.set_default_sounds(m)
        gp.add_substate(m)

    def _open_playlists_menu(self):
        """Show main My Playlists & Favorites menu"""
        from ... import menu as menu_mod, menus
        gp = self._bot._find_gameplay()
        if not gp:
            return

        m = menu_mod.Menu(self._bot.game, "My Playlists & Favorites", parrent=gp)
        items = []

        if self._bot.current_title and self._bot.current_target:
            def fav_current():
                gp.pop_last_substate()
                self._bot._save_current_to_favorites()
            items.append((
                "Save Current Song to Favorites", fav_current, None,
                "Saves the song that is playing to your Favorites, so you can "
                "find it again without searching for it."))

        def go_favorites():
            gp.pop_last_substate()
            self._show_favorites_menu()

        def go_create_playlist():
            gp.pop_last_substate()
            self._bot._prompt_create_playlist()

        items.append(("All Favorites", go_favorites, None,
                      "Every song you have saved. Enter opens the list to "
                      "play one, remove one, or play them all."))
        items.append(("Create New Playlist", go_create_playlist, None,
                      "Makes an empty playlist with a name you choose, then "
                      "fills it from the song that is playing or from search "
                      "results."))

        # List custom playlists
        playlist_names = self._bot.playlist_mgr.get_playlist_names()
        for p_name in playlist_names:
            def make_p_cb(name):
                return lambda: (gp.pop_last_substate(), self._show_custom_playlist_menu(name))
            items.append((f"Playlist: {p_name}", make_p_cb(p_name), None,
                          "Opens this playlist: play every song in it, play "
                          "one, or remove one."))

        items.append(("Back", lambda: (gp.pop_last_substate(),
                                       self._show_mode_menu()), None,
                      "Returns to the Music Bot menu."))
        m.add_items(items)
        menus.set_default_sounds(m)
        gp.add_substate(m)

    def _open_download_menu(self):
        """Open private Client-only download sources and job controls."""
        from ... import menu as menu_mod, menus
        gp = self._bot._find_gameplay()
        if not gp:
            return
        self._bot.download_mgr.show_progress_bar()

        items = [
            (self._bot.download_mgr.folder_menu_label,
             self._bot.download_mgr.speak_folder, None,
             "The folder downloaded songs are saved to. Enter reads it out "
             "again."),
            ("Set Download Folder",
             self._bot.download_mgr.choose_default_folder, None,
             "Chooses where downloads are saved. The dialog opens outside the "
             "game, so the song keeps playing."),
        ]
        if self._bot.download_mgr.has_saved_folder_setting():
            items.append((
                "Clear Saved Download Folder",
                self._bot.download_mgr.clear_default_folder, None,
                "Forgets the saved folder. Files already downloaded are not "
                "touched; the next download asks where to save."))
        if (self._bot.current_source != "local"
                and is_supported_music_url(self._bot.current_target)):
            def download_current():
                gp.pop_last_substate()
                self._bot.download_mgr.configure(
                    [{"title": self._bot.current_title, "target": self._bot.current_target}],
                    self._bot.current_title or "current song",
                )
            items.append((
                "Download Current Song", download_current, None,
                "Saves the song that is playing as a file on this computer, at "
                "the quality you pick next."))

        favorites = self._bot.playlist_mgr.get_favorites()
        if favorites:
            def download_favorites():
                gp.pop_last_substate()
                self._bot.download_mgr.configure(favorites, "Favorites")
            items.append((
                "Download All Favorites", download_favorites, None,
                "Saves every song in your Favorites, one after another, at the "
                "quality you pick next."))

        if self._bot.playlist_mgr.get_playlist_names():
            def choose_playlist():
                gp.pop_last_substate()
                self._open_download_playlist_menu()
            items.append((
                "Download a Saved Playlist", choose_playlist, None,
                "Picks one of your playlists and saves every song in it, one "
                "after another."))

        items.append((
            self._bot.download_mgr.parallel_menu_label,
            self._bot.download_mgr.cycle_parallel_downloads, None,
            "How many songs are downloaded at the same time. More is faster on "
            "a fast connection, and can starve the song you are listening to "
            "on a slow one.",
        ))
        items.append((
            self._bot.download_mgr.notification_menu_label,
            self._bot.download_mgr.toggle_file_notifications, None,
            "Whether a sound and a spoken line say when a download finishes.",
        ))

        items.append((
            self._bot.download_mgr.progress_menu_label,
            self._bot.download_mgr.speak_status, None,
            "How the downloads are going: how many, and how far each one has "
            "got.",
        ))
        if self._bot.download_mgr.is_active():
            items.append((
                "Cancel Active Download", self._bot.download_mgr.cancel, None,
                "Stops the downloads running now. Files already finished are "
                "kept."))
        items.append(("Back", lambda: (gp.pop_last_substate(),
                                       self._show_mode_menu()), None,
                      "Returns to the Music Bot menu. A running download keeps "
                      "running."))

        download_mgr = self._bot.download_mgr

        class DownloadMusicMenu(menu_mod.Menu):
            def exit(menu_self):
                download_mgr.hide_progress_bar()
                super().exit()

        m = DownloadMusicMenu(self._bot.game, "Music Download Center", parrent=gp)
        m.add_items(items)
        menus.set_default_sounds(m)
        gp.add_substate(m)

    def _open_download_playlist_menu(self):
        from ... import menu as menu_mod, menus
        gp = self._bot._find_gameplay()
        if not gp:
            return
        m = menu_mod.Menu(self._bot.game, "Select Playlist to Download", parrent=gp)
        items = []
        for name in self._bot.playlist_mgr.get_playlist_names():
            def choose(playlist_name=name):
                gp.pop_last_substate()
                self._bot.download_mgr.configure(
                    self._bot.playlist_mgr.get_playlist_tracks(playlist_name),
                    f"playlist {playlist_name}",
                )
            items.append((name, choose, None,
                          "Saves every song in this playlist as a file on "
                          "this computer."))
        items.append(("Back", lambda: (gp.pop_last_substate(),
                                       self._open_download_menu()), None,
                      "Returns to the Music Download Center."))
        m.add_items(items)
        menus.set_default_sounds(m)
        gp.add_substate(m)

    def _show_favorites_menu(self):
        """Show menu of favorite tracks"""
        from ... import menu as menu_mod, menus
        from ...speech import speak
        gp = self._bot._find_gameplay()
        if not gp:
            return

        favs = self._bot.playlist_mgr.get_favorites()
        if not favs:
            speak("No favorite tracks saved yet.")
            return

        m = menu_mod.Menu(self._bot.game, "All Favorites", parrent=gp)
        items = []

        def play_all():
            gp.pop_last_substate()
            self._bot._start_track_queue(favs, "Favorites")

        items.append(("Play All Favorites", play_all, None,
                      "Plays every favorite from the top of the list, one "
                      "after another."))
        for track in favs:
            title = track.get("title", "Unknown")
            target = track.get("target", "")
            source = track.get("source", "youtube")

            def make_fav_item_cb(t_title, t_target, t_source):
                return lambda: (gp.pop_last_substate(), self._show_track_action_menu(t_title, t_target, t_source, is_favorite=True))

            items.append((title, make_fav_item_cb(title, target, source)))

        items.append(("Back", lambda: (gp.pop_last_substate(),
                                       self._open_playlists_menu()), None,
                      "Returns to My Playlists & Favorites."))
        m.add_items(items)
        menus.set_default_sounds(m)
        gp.add_substate(m)

    def _show_custom_playlist_menu(self, playlist_name):
        """Show tracks inside a custom playlist"""
        from ... import menu as menu_mod, menus
        from ...speech import speak
        gp = self._bot._find_gameplay()
        if not gp:
            return

        tracks = self._bot.playlist_mgr.get_playlist_tracks(playlist_name)
        m = menu_mod.Menu(self._bot.game, f"Playlist: {playlist_name}", parrent=gp)
        items = []

        if tracks:
            def play_all():
                gp.pop_last_substate()
                self._bot._play_playlist_all(playlist_name)

            items.append(("Play All Tracks", play_all, None,
                          "Plays every song in this playlist, in the order "
                          "it is listed."))

        def delete_playlist():
            gp.pop_last_substate()
            self._bot.playlist_mgr.delete_playlist(playlist_name)
            speak(f"Deleted playlist {playlist_name}.")

        items.append(("Delete Playlist", delete_playlist, None,
                      "Deletes this playlist from this computer. The songs "
                      "are not touched, and a favorite stays a favorite."))

        for track in tracks:
            title = track.get("title", "Unknown")
            target = track.get("target", "")
            source = track.get("source", "youtube")

            def make_tr_cb(t_title, t_target, t_source, p_name):
                return lambda: (gp.pop_last_substate(), self._show_track_action_menu(t_title, t_target, t_source, playlist_name=p_name))

            items.append((title, make_tr_cb(title, target, source, playlist_name)))

        items.append(("Back", lambda: (gp.pop_last_substate(),
                                       self._open_playlists_menu()), None,
                      "Returns to My Playlists & Favorites."))
        m.add_items(items)
        menus.set_default_sounds(m)
        gp.add_substate(m)

    def _show_track_action_menu(self, title, target, source, is_favorite=False, playlist_name=None):
        """Show actions for a specific track (Play Now, Remove)"""
        from ... import menu as menu_mod, menus
        from ...speech import speak
        gp = self._bot._find_gameplay()
        if not gp:
            return

        m = menu_mod.Menu(self._bot.game, f"Track: {title}", parrent=gp)
        items = []

        def play_now():
            gp.pop_last_substate()
            self._bot.play_single_track(title, target, source)

        items.append(("Play Now", play_now, None,
                      "Starts this song now, on top of whatever is playing."))

        if source != "local" and is_supported_music_url(target):
            def download_track():
                gp.pop_last_substate()
                self._bot.download_mgr.configure(
                    [{"title": title, "target": target}],
                    title,
                )
            items.append(("Download This Track", download_track, None,
                          "Saves this song as a file on this computer."))

        if is_favorite:
            def remove_fav():
                gp.pop_last_substate()
                self._bot.playlist_mgr.remove_favorite(target)
                speak(f"Removed {title} from favorites.")
            items.append((
                "Remove from Favorites", remove_fav, None,
                "Takes this song out of your Favorites. A file you downloaded "
                "stays where it is."))

        if playlist_name:
            def remove_from_p():
                gp.pop_last_substate()
                self._bot.playlist_mgr.remove_from_playlist(playlist_name, target)
                speak(f"Removed {title} from playlist.")
            items.append((f"Remove from {playlist_name}", remove_from_p, None,
                          "Takes this song out of this playlist. Your other "
                          "playlists and Favorites keep it."))

        def back_action():
            gp.pop_last_substate()
            if playlist_name:
                self._show_custom_playlist_menu(playlist_name)
            else:
                self._show_favorites_menu()

        items.append(("Back", back_action, None,
                      "Returns to the list this song came from."))
        m.add_items(items)
        menus.set_default_sounds(m)
        gp.add_substate(m)

    def _open_queue_menu(self):
        """View / clear the play-next queue (Queue Mode).

                    Every queued song is its own menu item, so the player scrolls through them one at a
                    time and the menu speaks each as it is highlighted; Enter re-reads a line.

                    Every line is composed by `song_requests` (the count is `queue_label`, the body
                    `queue_lines`) - the same strings a Party Sync listener reads - so host and room can
                    never describe one queue two ways. Only the ends differ: Clear Queue and Back here.
                    """
        from ... import menu as menu_mod, menus
        gp = self._bot._find_gameplay()
        if not gp:
            return

        def go_back():
            gp.pop_last_substate()
            self._show_mode_menu()

        def clear_queue():
            self._bot._clear_next_up_queue()
            self._say("Queue cleared.")
            # Rebuild the menu so the removed song items disappear.
            if gp.substates and gp.substates[-1] is m:
                gp.pop_last_substate()
            self._open_queue_menu()

        m = menu_mod.Menu(
            self._bot.game, song_requests.queue_label(len(self._bot.next_up_queue)),
            parrent=gp)
        items = []
        for line in song_requests.queue_lines(
                song_requests.queue_share(self._bot.next_up_queue),
                self._bot._now_playing_title()):
            def read_line(song_line=line):
                self._say(song_line)

            items.append((line, read_line))
        items.append(("Clear Queue", clear_queue, None,
                      "Removes every waiting song. The song that is playing "
                      "keeps playing."))
        items.append(("Back", go_back, None,
                      "Returns to the Music Bot menu."))
        m.add_items(items)
        menus.set_default_sounds(m)
        gp.add_substate(m)

    def _prompt_add_track_to_playlist(self, title, target, source="youtube"):
        """Prompt user to choose which custom playlist to add a track to"""
        from ... import menu as menu_mod, menus
        from ...speech import speak
        gp = self._bot._find_gameplay()
        if not gp:
            return

        names = self._bot.playlist_mgr.get_playlist_names()
        if not names:
            speak("No custom playlists created yet. Please create one first.")
            return

        m = menu_mod.Menu(self._bot.game, f"Add '{title}' to Playlist", parrent=gp)
        items = []
        for name in names:
            def make_add_cb(p_name):
                def do_add():
                    gp.pop_last_substate()
                    added = self._bot.playlist_mgr.add_to_playlist(p_name, title, target, source)
                    if added:
                        speak(f"Added {title} to {p_name}.")
                    else:
                        speak(f"{title} is already in {p_name}.")
                return do_add
            items.append((name, make_add_cb(name), None,
                          f"Adds {title} to this playlist."))

        items.append(("Cancel", lambda: gp.pop_last_substate(), None,
                      "Leaves your playlists as they are."))
        m.add_items(items)
        menus.set_default_sounds(m)
        gp.add_substate(m)
