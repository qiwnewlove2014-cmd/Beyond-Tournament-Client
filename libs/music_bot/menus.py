"""Every Music Bot menu - the builder half of MapMusicBot (extracted from
controller.py).

Search, the personal library, downloads and recording, Party Sync and its song
requests, the equalizer, spoken subtitles and the help page each open a menu;
this module builds all of them. The menus were written as one vertical per
feature inside the controller, so they moved together rather than by kind.

**The state stays on `MapMusicBot`; the behaviour moved here.** Every queue,
option, bank and reader a menu shows or flips is the bot's own (`next_up_queue`,
`queue_mode`, `feed_tracks`, `cinema_target`, `eq_profile`, `subtitle_reader`),
and the tests read those straight off the bot, so those attributes are still the
bot's, and this class reaches them through its owner. One rule for the reader,
because this file names its owner on every line: **`self.` is this handler,
`self._bot.` is the Music Bot.**

`MapMusicBot` keeps a one-line shim for each method below, so the callers that
were written against the bot -- gameplay's key bindings, event_handeler,
cinema_handler's "Back to Music Bot" line and the tests -- keep working
unchanged. A menu runs when a key is pressed, on the main thread, and never in
the frame loop or the audio pump, so the extra hop costs nothing that matters.

Usage from MapMusicBot::

    self._menus_handler = MusicBotMenus(self)   # or built on first use
    self._menus()._show_mode_menu()

Pins: client/tests/test_game_audio_recorder.py, test_music_downloader.py,
test_music_bot_queue_settings.py, test_party_queue_share.py,
test_party_song_pick.py and test_party_song_request.py read this file's source for
the lines they pinned here, test_music_bot_subtitles.py reads one menu's body
through `inspect.getsource` (`menu_source_of`), and test_music_bot_crossfade_eq.py
opens `_MusicBotEqSlider` itself. Rules: .agents/skills/music_bot_integration/.
"""

import contextlib
import functools

import pygame

from .. import options
from .. import state
from ..speech import speak
from . import output_device
from ..string_utils import friendly_key_name
from . import song_requests
from . import subtitles
from .music_downloader import is_supported_music_url


class MusicBotMenus:
    """Builds and opens every Music Bot menu (see the module docstring)."""

    def __init__(self, bot):
        self._bot = bot          # back-reference to MapMusicBot

    def _show_mode_menu(self):
        """Show menu to choose between YouTube search and Local playlist"""
        from .. import menu as menu_mod, menus

        gp = self._bot._find_gameplay()
        if not gp:
            return

        def go_search():
            gp.pop_last_substate()
            self._bot._open_search_input()

        def go_link():
            gp.pop_last_substate()
            self._bot._open_link_input()

        def go_local():
            gp.pop_last_substate()
            self._bot._open_file_dialog()

        def go_playlists():
            gp.pop_last_substate()
            self._open_playlists_menu()

        def go_personal_feed():
            gp.pop_last_substate()
            self._show_personal_feed_menu()

        def go_downloads():
            gp.pop_last_substate()
            self._open_download_menu()

        def go_record_audio():
            gp.pop_last_substate()
            self._open_recording_menu()

        def go_help():
            gp.pop_last_substate()
            self._show_help_menu()

        def go_party_sync():
            gp.pop_last_substate()
            self._open_party_sync_menu()

        def go_queue():
            gp.pop_last_substate()
            self._open_queue_menu()

        def go_settings():
            gp.pop_last_substate()
            self._open_settings_menu()

        def get_queue_mode_label():
            status = "ON" if self._bot.queue_mode else "OFF"
            return f"Queue Mode: {status}"

        def toggle_queue_mode():
            self._bot.queue_mode = not self._bot.queue_mode
            options.set("music_bot_queue_mode", self._bot.queue_mode)
            status_text = "enabled" if self._bot.queue_mode else "disabled"
            speak(f"Queue mode {status_text}. Search results will be added to the play queue.")
            m.speak_current_item()

        def get_queue_count_label():
            return song_requests.queue_label(len(self._bot.next_up_queue))

        m = menu_mod.Menu(self._bot.game, "Music Bot Mode", parrent=gp)
        items = [
            ("Search YouTube", go_search),
            # Right beside the search: staff who already hold the link should not
            # have to search for the song they are looking at.
            ("Play a YouTube link", go_link),
            ("Choose Local File", go_local),
            ("My Playlists & Favorites", go_playlists),
            ("Personal Music Feed", go_personal_feed),
            ("Music Download Center", go_downloads),
            ("Record Audio", go_record_audio),
            ("Party Sync (Listen Together)", go_party_sync),
        ]
        
        # Show the megaphone routing option only when the server explicitly granted
        # broadcast permission (canBroadcastMegaphone()). The server is the single
        # source of truth; gating on it keeps the client menu and the server lock
        # perfectly in sync (no client-side role guessing).
        can_broadcast_megaphone = getattr(gp, 'can_broadcast_megaphone', False) if gp else False

        if can_broadcast_megaphone:
            def get_megaphone_label():
                status = "ON" if self._bot.broadcast_to_megaphone else "OFF"
                return f"Broadcast to Megaphone: {status}"
                
            def toggle_megaphone_routing():
                # No broadcast_enabled gate: piano broadcast is independent of music
                # playback, so performers can broadcast the piano through PA speakers
                # without starting a music track first.
                self._bot.broadcast_to_megaphone = not self._bot.broadcast_to_megaphone
                status_text = "enabled" if self._bot.broadcast_to_megaphone else "disabled"
                speak(f"Broadcast to megaphone {status_text}.")
                m.speak_current_item()

                # Send lock request to the server
                from .. import consts
                self._bot.game.network.send(
                    consts.CHANNEL_MISC,
                    "megaphone_broadcast_lock",
                    {"locked": self._bot.broadcast_to_megaphone}
                )
                
            items.append((get_megaphone_label, toggle_megaphone_routing))

        # Cinema speaker routing is Developer/Contributor only (see
        # cinema_routing_allowed): everyone else gets a plain Music Bot menu
        # rather than a switch the Server would refuse to honour.
        if self._bot.cinema_routing_allowed():
            def go_cinema():
                gp.pop_last_substate()
                self._bot._open_cinema_menu()

            items.append((self._bot.cinema_target_label, go_cinema))

        # Three listener-side switches, one line below the song's own routing: a room is
        # how *this* listener hears a cabinet, so none of them takes anything from anybody
        # and all three are open to anyone who can open this menu (the song routing above
        # stays Developer/Contributor). Songs, instruments and a voice.
        if self._bot.cinema_listening_allowed():
            items.append((self._bot.cinema_rooms_label, self._bot.toggle_cinema_rooms))
            items.append((self._bot.instruments_cinema_label,
                          self._bot.toggle_instruments_cinema))
            items.append((self._bot.speech_cinema_label, self._bot.toggle_speech_cinema))
            # The three lines above choose how *you* hear a room; this one is the other
            # direction and is not a switch at all - it answers "where do other players hear me
            # from", which is decided by staff. It reads rather than toggles: pressing it says
            # the same sentence the moment of a pan says, so it cannot look like a setting.
            items.append((self._bot.own_sound_label, self._bot.announce_own_sound))

        items.extend([
            (get_queue_mode_label, toggle_queue_mode),
            (get_queue_count_label, go_queue),
            (self._bot.subtitle_label, self._open_subtitle_menu),
            ("Music Bot Settings", go_settings),
            ("Help", go_help),
            ("Cancel", lambda: gp.pop_last_substate())
        ])
        m.add_items(items)
        menus.set_default_sounds(m)
        gp.add_substate(m)

    def open_song_pick_menu(self, request_id, items, query="", on_pick=None):
        """The picker: which of the results this request may be queued from.

                    Used by both ends - a listener whose own bot may be switched off entirely (no
                    playback, no sources, only a menu) and a host who asked their own bot. One menu and
                    one wording (`song_requests.choice_line`); `on_pick(index)` is what it means here.
                    """
        from .. import menu as menu_mod, menus
        gp = self._bot._find_gameplay()
        if gp is None:
            return
        entries = []
        for item in items or ():
            if not isinstance(item, dict):
                continue
            # The position in the list is what the pick means, and it counts
            # what is actually shown: an entry the list skipped must not shift
            # which result a later position points at.
            index = len(entries)
            label = song_requests.choice_line(
                item.get("title"), item.get("duration"))

            def choose(idx=index, picked=label):
                gp.pop_last_substate()
                speak(picked)
                if callable(on_pick):
                    on_pick(idx)

            entries.append((label, choose))
        if not entries:
            speak(song_requests.NO_CHOICES)
            return

        def withdraw():
            gp.pop_last_substate()
            if callable(on_pick):
                on_pick(-1)

        entries.append(("Nothing, thanks", withdraw))
        title = "Pick a song"
        if query:
            title = f"Pick a song: {query}"
        m = menu_mod.Menu(self._bot.game, title, parrent=gp)
        m.add_items(entries)
        menus.set_default_sounds(m)
        gp.add_substate(m)

    def open_party_queue_view(self):
        """Read-only view of the host's queue, for a session listener.

                    Somebody else's queue: every line is a report and there is nothing to press but Close.
                    It opens on top of whatever party menu asked for it.
                    """
        from .. import menu as menu_mod, menus
        gp, ps = self._bot._party_sync_pair()
        if gp is None:
            return
        m = menu_mod.Menu(self._bot.game, self._bot.party_queue_label(), parrent=gp)
        lines = song_requests.queue_lines(
            getattr(ps, "queue", None) or (),
            getattr(ps, "now_playing", ""),
        )
        items = [(line, lambda: None) for line in lines]
        items.append(("Close", lambda: gp.pop_last_substate()))
        m.add_items(items)
        menus.set_default_sounds(m)
        gp.add_substate(m)

    def _open_party_sync_menu(self):
        """Host/guest Party Sync controls (entry from the Music Bot menu)."""
        from .. import menu as menu_mod, menus
        from ..speech import speak
        gp, ps = self._bot._party_sync_pair()
        if gp is None:
            return

        m = menu_mod.Menu(self._bot.game, "Party Sync", parrent=gp)
        items = []

        def close_top():
            if gp.substates and gp.substates[-1] is m:
                gp.pop_last_substate()

        def back_to_bot_menu():
            close_top()
            self._show_mode_menu()

        if ps is None or ps.role is None:
            def do_start():
                close_top()
                self._bot._party_sync_send("party_sync_start")
            items.append(("Start Party Sync session", do_start))
            items.append((
                "Invite friends anywhere to hear your music privately",
                lambda: None,
            ))
        elif ps.role == "host":
            def guests_label():
                names = ", ".join(
                    g["name"] for g in ps.guests
                ) if ps.guests else "nobody yet"
                return f"Listeners ({len(ps.guests)}): {names}"
            items.append((guests_label, lambda: None))
            if ps.guests:
                def do_kick():
                    close_top()
                    self._open_party_kick_menu(ps)
                items.append(("Kick a listener", do_kick))
            def do_invite():
                close_top()
                # A session is not map-scoped, so the picker lists every player
                # online (own map first, each line saying which is which).
                # Back in the invite picker returns to this Party controls menu
                # (the Ctrl+F8 quick menu sets its own target).
                gp._party_sync_invite_back = (
                    lambda: self._open_party_sync_menu()
                )
                self._bot._party_sync_send("party_sync_list")
            items.append(("Invite players (any map)", do_invite))
            # The /p switch sits with the host's listeners, their invite and
            # their End action (one builder: song_requests_switch_item), not
            # in the Music Bot menu -- the queue it fills is this session's,
            # and this is the menu a host opens to run the session.
            item = self._bot.song_requests_switch_item(m.speak_current_item)
            if item is not None:
                items.append(item)
            def do_end():
                close_top()
                self._bot._party_sync_send("party_sync_end")
                ps.end_session()
                self._bot.party_sync_force_upload = False
                self._bot._clear_party_sync_direct()
            items.append(("End Party Sync session", do_end))
        elif ps.role == "guest":
            def do_leave():
                close_top()
                self._bot._party_sync_send("party_sync_leave")
                ps.end_session()
                self._bot._clear_party_sync_direct()
            items.append((f"Listening to {ps.host_name}", lambda: None))
            # Read-only: whether this party takes song requests (/p) is the
            # host's decision, and it arrives with the session state. A listener
            # reads it here instead of finding out by asking.
            items.append((self._bot.song_requests_label, lambda: None))
            # ...and what the host has waiting, which is the other half of
            # asking for a song: the queue is the host's machine's, relayed to
            # the room (song_requests.queue_share -> parse_queue -> here).
            items.append((self._bot.party_queue_label, self.open_party_queue_view))
            items.append(("Leave Party Sync session", do_leave))

        items.append(("Back", back_to_bot_menu))
        m.add_items(items)
        menus.set_default_sounds(m)
        gp.add_substate(m)

    def _open_party_kick_menu(self, ps):
        """Pick which listener to kick (host only)."""
        from .. import menu as menu_mod, menus
        from ..speech import speak
        gp, _ = self._bot._party_sync_pair()
        if gp is None:
            return
        if not ps or not ps.guests:
            speak("Nobody is listening right now.")
            self._open_party_sync_menu()
            return
        m = menu_mod.Menu(self._bot.game, "Kick a Party Sync listener", parrent=gp)
        items = []

        def close_top():
            if gp.substates and gp.substates[-1] is m:
                gp.pop_last_substate()

        def back_to_party():
            close_top()
            self._open_party_sync_menu()

        for g in ps.guests:
            name = g["name"]
            def make_kick(target=name):
                def cb():
                    close_top()
                    self._bot._party_sync_send("party_sync_kick", {"name": target})
                return cb
            items.append((f"Kick {name}", make_kick()))
        items.append(("Back", back_to_party))
        m.add_items(items)
        menus.set_default_sounds(m)
        gp.add_substate(m)

    def _show_personal_feed_menu(self):
        """Open the shuffled Favorites feed controls."""
        from .. import menu as menu_mod, menus
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
            ("Start shuffled Favorites feed", start_feed),
            ("Next feed song", next_feed),
            ("Back", lambda: (gp.pop_last_substate(), self._show_mode_menu())),
        ])
        menus.set_default_sounds(m)
        gp.add_substate(m)

    def _open_playlists_menu(self):
        """Show main My Playlists & Favorites menu"""
        from .. import menu as menu_mod, menus
        gp = self._bot._find_gameplay()
        if not gp:
            return

        m = menu_mod.Menu(self._bot.game, "My Playlists & Favorites", parrent=gp)
        items = []

        if self._bot.current_title and self._bot.current_target:
            def fav_current():
                gp.pop_last_substate()
                self._bot._save_current_to_favorites()
            items.append(("Save Current Song to Favorites", fav_current))

        def go_favorites():
            gp.pop_last_substate()
            self._show_favorites_menu()

        def go_create_playlist():
            gp.pop_last_substate()
            self._bot._prompt_create_playlist()

        items.append(("All Favorites", go_favorites))
        items.append(("Create New Playlist", go_create_playlist))

        # List custom playlists
        playlist_names = self._bot.playlist_mgr.get_playlist_names()
        for p_name in playlist_names:
            def make_p_cb(name):
                return lambda: (gp.pop_last_substate(), self._show_custom_playlist_menu(name))
            items.append((f"Playlist: {p_name}", make_p_cb(p_name)))

        items.append(("Back", lambda: (gp.pop_last_substate(), self._show_mode_menu())))
        m.add_items(items)
        menus.set_default_sounds(m)
        gp.add_substate(m)

    def _open_recording_menu(self):
        """Open the persistent folder and game-audio recording controls."""
        from .. import menu as menu_mod, menus

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
            (self._bot.audio_recorder.folder_menu_label, self._bot.audio_recorder.speak_folder),
            ("Set Recording Folder", self._bot.audio_recorder.choose_folder),
            (self._bot.audio_recorder.menu_label, start_or_stop),
            (self._bot.audio_recorder.status_menu_label, self._bot.audio_recorder.speak_status),
            ("Recording Settings", go_settings),
            ("Open Recording Folder", self._bot.audio_recorder.open_folder),
        ]
        items.append(("Back", go_back))
        m.add_items(items)
        menus.set_default_sounds(m)
        gp.add_substate(m)

    def _open_recording_settings_menu(self):
        """Open accessible, persistent settings that are safe for final-mix capture."""
        from .. import menu as menu_mod, menus

        gp = self._bot._find_gameplay()
        if not gp:
            return

        def go_back():
            gp.pop_last_substate()
            self._open_recording_menu()

        def confirm_restore():
            gp.pop_last_substate()
            self._open_recording_reset_confirmation()

        m = menu_mod.Menu(self._bot.game, "Audio Recording Settings", parrent=gp)
        m.add_items([
            (self._bot.audio_recorder.capture_scope_label, self._bot.audio_recorder.speak_capture_scope),
            (self._bot.audio_recorder.computer_audio_setting_label, self._bot.audio_recorder.toggle_computer_audio),
            (self._bot.audio_recorder.microphone_setting_label, self._bot.audio_recorder.toggle_microphone),
            (self._bot.audio_recorder.countdown_setting_label, self._bot.audio_recorder.cycle_countdown),
            (self._bot.audio_recorder.split_setting_label, self._bot.audio_recorder.cycle_split_minutes),
            (self._bot.audio_recorder.announce_setting_label, self._bot.audio_recorder.toggle_announce_details),
            ("Restore Recording Defaults", confirm_restore),
            ("Back", go_back),
        ])
        menus.set_default_sounds(m)
        gp.add_substate(m)

    def _open_recording_reset_confirmation(self):
        """Require an explicit second action before restoring recording defaults."""
        from .. import menu as menu_mod, menus

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
            ("Yes, Restore Recording Defaults", restore),
            ("Cancel and Keep Current Settings", cancel),
        ])
        menus.set_default_sounds(m)
        gp.add_substate(m)

    def _open_download_menu(self):
        """Open private Client-only download sources and job controls."""
        from .. import menu as menu_mod, menus
        gp = self._bot._find_gameplay()
        if not gp:
            return
        self._bot.download_mgr.show_progress_bar()

        items = [
            (self._bot.download_mgr.folder_menu_label, self._bot.download_mgr.speak_folder),
            ("Set Download Folder", self._bot.download_mgr.choose_default_folder),
        ]
        if self._bot.download_mgr.has_saved_folder_setting():
            items.append(("Clear Saved Download Folder", self._bot.download_mgr.clear_default_folder))
        if (self._bot.current_source != "local"
                and is_supported_music_url(self._bot.current_target)):
            def download_current():
                gp.pop_last_substate()
                self._bot.download_mgr.configure(
                    [{"title": self._bot.current_title, "target": self._bot.current_target}],
                    self._bot.current_title or "current song",
                )
            items.append(("Download Current Song", download_current))

        favorites = self._bot.playlist_mgr.get_favorites()
        if favorites:
            def download_favorites():
                gp.pop_last_substate()
                self._bot.download_mgr.configure(favorites, "Favorites")
            items.append(("Download All Favorites", download_favorites))

        if self._bot.playlist_mgr.get_playlist_names():
            def choose_playlist():
                gp.pop_last_substate()
                self._open_download_playlist_menu()
            items.append(("Download a Saved Playlist", choose_playlist))

        items.append((
            self._bot.download_mgr.parallel_menu_label,
            self._bot.download_mgr.cycle_parallel_downloads,
        ))
        items.append((
            self._bot.download_mgr.notification_menu_label,
            self._bot.download_mgr.toggle_file_notifications,
        ))

        items.append((
            self._bot.download_mgr.progress_menu_label,
            self._bot.download_mgr.speak_status,
        ))
        if self._bot.download_mgr.is_active():
            items.append(("Cancel Active Download", self._bot.download_mgr.cancel))
        items.append(("Back", lambda: (gp.pop_last_substate(), self._show_mode_menu())))

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
        from .. import menu as menu_mod, menus
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
            items.append((name, choose))
        items.append(("Back", lambda: (gp.pop_last_substate(), self._open_download_menu())))
        m.add_items(items)
        menus.set_default_sounds(m)
        gp.add_substate(m)

    def _show_favorites_menu(self):
        """Show menu of favorite tracks"""
        from .. import menu as menu_mod, menus
        from ..speech import speak
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

        items.append(("Play All Favorites", play_all))
        for track in favs:
            title = track.get("title", "Unknown")
            target = track.get("target", "")
            source = track.get("source", "youtube")

            def make_fav_item_cb(t_title, t_target, t_source):
                return lambda: (gp.pop_last_substate(), self._show_track_action_menu(t_title, t_target, t_source, is_favorite=True))

            items.append((title, make_fav_item_cb(title, target, source)))

        items.append(("Back", lambda: (gp.pop_last_substate(), self._open_playlists_menu())))
        m.add_items(items)
        menus.set_default_sounds(m)
        gp.add_substate(m)

    def _show_custom_playlist_menu(self, playlist_name):
        """Show tracks inside a custom playlist"""
        from .. import menu as menu_mod, menus
        from ..speech import speak
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

            items.append(("Play All Tracks", play_all))

        def delete_playlist():
            gp.pop_last_substate()
            self._bot.playlist_mgr.delete_playlist(playlist_name)
            speak(f"Deleted playlist {playlist_name}.")

        items.append(("Delete Playlist", delete_playlist))

        for track in tracks:
            title = track.get("title", "Unknown")
            target = track.get("target", "")
            source = track.get("source", "youtube")

            def make_tr_cb(t_title, t_target, t_source, p_name):
                return lambda: (gp.pop_last_substate(), self._show_track_action_menu(t_title, t_target, t_source, playlist_name=p_name))

            items.append((title, make_tr_cb(title, target, source, playlist_name)))

        items.append(("Back", lambda: (gp.pop_last_substate(), self._open_playlists_menu())))
        m.add_items(items)
        menus.set_default_sounds(m)
        gp.add_substate(m)

    def _show_track_action_menu(self, title, target, source, is_favorite=False, playlist_name=None):
        """Show actions for a specific track (Play Now, Remove)"""
        from .. import menu as menu_mod, menus
        from ..speech import speak
        gp = self._bot._find_gameplay()
        if not gp:
            return

        m = menu_mod.Menu(self._bot.game, f"Track: {title}", parrent=gp)
        items = []

        def play_now():
            gp.pop_last_substate()
            self._bot.play_single_track(title, target, source)

        items.append(("Play Now", play_now))

        if source != "local" and is_supported_music_url(target):
            def download_track():
                gp.pop_last_substate()
                self._bot.download_mgr.configure(
                    [{"title": title, "target": target}],
                    title,
                )
            items.append(("Download This Track", download_track))

        if is_favorite:
            def remove_fav():
                gp.pop_last_substate()
                self._bot.playlist_mgr.remove_favorite(target)
                speak(f"Removed {title} from favorites.")
            items.append(("Remove from Favorites", remove_fav))

        if playlist_name:
            def remove_from_p():
                gp.pop_last_substate()
                self._bot.playlist_mgr.remove_from_playlist(playlist_name, target)
                speak(f"Removed {title} from playlist.")
            items.append((f"Remove from {playlist_name}", remove_from_p))

        def back_action():
            gp.pop_last_substate()
            if playlist_name:
                self._show_custom_playlist_menu(playlist_name)
            else:
                self._show_favorites_menu()

        items.append(("Back", back_action))
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
        from .. import menu as menu_mod, menus
        gp = self._bot._find_gameplay()
        if not gp:
            return

        def go_back():
            gp.pop_last_substate()
            self._show_mode_menu()

        def clear_queue():
            self._bot._clear_next_up_queue()
            speak("Queue cleared.")
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
                speak(song_line)

            items.append((line, read_line))
        items.append(("Clear Queue", clear_queue))
        items.append(("Back", go_back))
        m.add_items(items)
        menus.set_default_sounds(m)
        gp.add_substate(m)

    def _open_settings_menu(self):
        """Music Bot settings: output device first, then reverb, crossfade, EQ, key, speech key, muffle.

                    The output line leads because it is what a player comes here to change;
                    the underwater muffle sits last (just above Back) so the two are never
                    neighbours on the list. `test_the_output_line_leads_and_the_muffle_sits_last`
                    pins that order.

                    Broadcast to Others is deliberately NOT here - it has its own shortcut, and
                    duplicating it caused confusion about which is the source of truth. Every toggle here
                    is persisted in client options.
                    """
        from .. import menu as menu_mod, menus
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
            speak("Underwater muffle enabled." if self._bot.water_muffle_enabled
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
            speak("Room reverb enabled." if self._bot.reverb_enabled
                  else "Room reverb disabled.")
            m.speak_current_item()

        def get_crossfade_label():
            status = "ON" if getattr(self._bot, "crossfade_enabled", False) else "OFF"
            return f"Crossfade Between Songs: {status}"

        def toggle_crossfade():
            enabled = not getattr(self._bot, "crossfade_enabled", False)
            self._bot._set_crossfade_enabled(enabled)
            speak("Crossfade between songs enabled. Tracks overlap smoothly."
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
            return f"Speech Key: {self._bot.speech_transpose_label()}"

        def go_speech_transpose():
            gp.pop_last_substate()
            self._open_speech_transpose_menu()

        def get_output_label():
            return f"Music Output Device: {self._bot.output_device_label()}"

        def go_output():
            gp.pop_last_substate()
            self._open_output_device_menu()

        m = menu_mod.Menu(self._bot.game, "Music Bot Settings", parrent=gp)
        m.add_items([
            (get_output_label, go_output),
            (get_reverb_label, toggle_reverb),
            (get_crossfade_label, toggle_crossfade),
            (get_eq_label, go_eq),
            (get_transpose_label, go_transpose),
            (get_speech_transpose_label, go_speech_transpose),
            (get_water_label, toggle_water),
            ("Back", go_back),
        ])
        menus.set_default_sounds(m)
        gp.add_substate(m)

    def _open_output_device_menu(self):
        """Where this bot's own song is heard: the game's device, or another one.

        The game keeps playing where it always did; this sends the Music Bot's
        own stream somewhere else (the speakers while the game stays in the
        headset). A chosen device is played by a child process of its own
        (`music_bot/output_device.py` says why a process is the honest way to
        do it at all), so the equalizer and the room reverb -- the game's own
        audio engine -- stay on the game's device and the line says so.
        """
        from .. import menu as menu_mod, menus
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
            ("Same as the game", lambda: choose("")),
        ]
        for device in output_device.available_devices():
            label = output_device.short_name(device)
            if device == self._bot.output_device:
                label = f"{label} (current)"
            items.append((label, functools.partial(choose, device)))
        items.append(("Back", go_back))
        m = menu_mod.Menu(self._bot.game, "Music Output Device", parrent=gp)
        m.add_items(items)
        menus.set_default_sounds(m)
        gp.add_substate(m)

    def _open_eq_menu(self):
        """Pick the Music Bot equalizer profile (or open the Custom sliders).
        Profiles apply immediately so the change is heard while browsing.
        """
        from .. import menu as menu_mod, menus
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
                speak(f"{label} equalizer applied.")
                m.speak_current_item()
            return pick

        m = menu_mod.Menu(self._bot.game, "Music Bot Equalizer", parrent=gp)
        items = []
        for profile, label in self._bot.EQ_PROFILES:
            items.append((make_label(profile, label), make_pick(profile, label)))
        items.append(("Back", go_back))
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
        from .. import menu as menu_mod, menus
        gp = self._bot._find_gameplay()
        if not gp:
            return

        def go_back():
            gp.pop_last_substate()
            self._open_settings_menu()

        def step(amount):
            def apply():
                self._bot.set_transpose(self._bot.transpose_semitones + amount)
                speak(f"Key: {self._bot.transpose_label().lower()}.")
                m.speak_current_item()
            return apply

        def reset():
            self._bot.set_transpose(0)
            speak("Key: the song's own.")
            m.speak_current_item()

        m = menu_mod.Menu(
            self._bot.game,
            f"Music Bot Key: {self._bot.transpose_label()}",
            parrent=gp,
        )
        m.add_items([
            ("Up One Semitone", step(1)),
            ("Down One Semitone", step(-1)),
            ("Original Key", reset),
            ("Back", go_back),
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
        from .. import menu as menu_mod, menus
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
                speak(f"Speech key: {self._bot.speech_transpose_label().lower()}.")
                m.speak_current_item()
            return apply

        def reset():
            self._bot.set_speech_transpose(0)
            speak("Speech key: your own voice.")
            m.speak_current_item()

        m = menu_mod.Menu(
            self._bot.game,
            f"Music Bot Speech Key: {self._bot.speech_transpose_label()}",
            parrent=gp,
        )
        m.add_items([
            ("Up One Semitone", step(1)),
            ("Down One Semitone", step(-1)),
            ("Original Voice", reset),
            ("Back", go_back),
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

    def _open_subtitle_menu(self):
        """Everything about spoken subtitles, behind one line of the menu."""
        from .. import menu as menu_mod, menus
        gp = self._bot._find_gameplay()
        if not gp:
            return

        def go_back():
            gp.pop_last_substate()
            self._show_mode_menu()

        def go_language():
            gp.pop_last_substate()
            self._open_subtitle_language_menu()

        def go_timing():
            gp.pop_last_substate()
            self._open_subtitle_timing_menu()

        def read_status():
            speak(self._bot.subtitle_reader.status() +
                  " Subtitles come from the video on YouTube: the uploader's "
                  "own track when there is one, otherwise the automatic "
                  "captions. Songs often have none, because the words of a "
                  "song are rarely a caption track.")

        m = menu_mod.Menu(self._bot.game, "Subtitles", parrent=gp)
        m.add_items([
            (self._bot.subtitle_label, self._bot.toggle_subtitles),
            (self._bot.subtitle_language_label, go_language),
            (self._bot.subtitle_timing_label, go_timing),
            (lambda: self._bot.subtitle_reader.status(), read_status),
            ("Back", go_back),
        ])
        menus.set_default_sounds(m)
        gp.add_substate(m)

    def _open_subtitle_language_menu(self):
        """The order YouTube is asked for a language in."""
        from .. import menu as menu_mod, menus
        gp = self._bot._find_gameplay()
        if not gp:
            return

        def go_back():
            gp.pop_last_substate()
            self._open_subtitle_menu()

        def make_label(key, label):
            def label_fn():
                current = getattr(self._bot, "subtitle_language", None)
                return f"{label} (current)" if current == key else label
            return label_fn

        def make_pick(key, label):
            def pick():
                self._bot.subtitle_language = subtitles.normalize_preference(key)
                options.set("music_bot_subtitle_language",
                            self._bot.subtitle_language)
                speak(f"Subtitle language: {label}.")
                # A track already playing is read again in the new order (the
                # track itself is cached, so only a language change costs a
                # fetch).
                self._bot.subtitle_reader.clear()
                self._bot._begin_subtitles(self._bot._caption_page())
                m.speak_current_item()
            return pick

        m = menu_mod.Menu(self._bot.game, "Subtitle Language", parrent=gp)
        items = [(make_label(key, label), make_pick(key, label))
                 for key, label, _languages in subtitles.LANGUAGE_PREFERENCES]
        items.append(("Back", go_back))
        m.add_items(items)
        menus.set_default_sounds(m)
        gp.add_substate(m)

    def _open_subtitle_timing_menu(self):
        """Shift the captions against the audio, half a second a press.

                    Reading a caption takes the reader its own time and the rate is the player's setting,
                    so only the player can say where the line they hear starts.
                    """
        from .. import menu as menu_mod, menus
        gp = self._bot._find_gameplay()
        if not gp:
            return

        def go_back():
            gp.pop_last_substate()
            self._open_subtitle_menu()

        def speak_timing():
            speak(subtitles.offset_label(self._bot.subtitle_offset))

        def shift(delta):
            def move():
                self._bot.subtitle_offset = subtitles.normalize_offset(
                    self._bot.subtitle_offset + delta)
                options.set("music_bot_subtitle_offset", self._bot.subtitle_offset)
                speak_timing()
                m.speak_current_item()
            return move

        def reset():
            self._bot.subtitle_offset = 0
            options.set("music_bot_subtitle_offset", 0)
            speak_timing()
            m.speak_current_item()

        step = subtitles.OFFSET_STEP_MS / 1000.0
        m = menu_mod.Menu(self._bot.game, "Subtitle Timing", parrent=gp)
        m.add_items([
            (self._bot.subtitle_timing_label, speak_timing),
            (f"Speak {step:.1f} seconds earlier",
             shift(subtitles.OFFSET_STEP_MS)),
            (f"Speak {step:.1f} seconds later",
             shift(-subtitles.OFFSET_STEP_MS)),
            ("Reset timing", reset),
            ("Back", go_back),
        ])
        menus.set_default_sounds(m)
        gp.add_substate(m)

    def _show_help_menu(self):
        """Show scrollable menu containing the Music Bot key controls"""
        from .. import menu as menu_mod, menus

        gp = self._bot._find_gameplay()
        if not gp:
            return

        def go_back():
            gp.pop_last_substate()
            self._show_mode_menu()

        toggle_key = friendly_key_name(
            self._bot.game.keyconfig.get("music_bot_toggle", pygame.K_m)
        )
        volume_down_key = friendly_key_name(
            self._bot.game.keyconfig.get("music_bot_vol_down", pygame.K_F9)
        )
        volume_up_key = friendly_key_name(
            self._bot.game.keyconfig.get("music_bot_vol_up", pygame.K_F10)
        )

        m = menu_mod.Menu(self._bot.game, "Music Bot Controls Help", parrent=gp)
        items = [
            (f"{toggle_key}: Open mode menu", lambda: None),
            (f"Shift + {toggle_key}: Pause / Resume", lambda: None),
            (f"Ctrl + {toggle_key}: Stop / Replay last song", lambda: None),
            (f"Ctrl + Shift + {toggle_key}: Speak status", lambda: None),
            (f"Alt + {toggle_key}: Toggle broadcast (Private/Public)", lambda: None),
            ("Personal Music Feed: Ctrl left bracket for previous; Ctrl right bracket for next", lambda: None),
            (f"{volume_down_key}: Decrease volume", lambda: None),
            (f"{volume_up_key}: Increase volume", lambda: None),
            (f"Ctrl + {volume_down_key}: Rewind 10 seconds (add Shift for 60)", lambda: None),
            (f"Ctrl + {volume_up_key}: Fast-forward 10 seconds (add Shift for 60)", lambda: None),
            ("Back", go_back)
        ]
        m.add_items(items)
        menus.set_default_sounds(m)
        gp.add_substate(m)

    def _show_results_menu(self, results):
        """Show search results as a menu"""
        from .. import menu as menu_mod, menus

        gp = self._bot._find_gameplay()
        if not gp:
            return

        if not results:
            speak("No results found.")
            return

        m = menu_mod.Menu(self._bot.game, "Search Results", parrent=gp)
        items = []
        for i, r in enumerate(results):
            # The same line a requester's picker shows for the same result
            # (`song_requests.choice_line`, "A Song (4:29)").
            label = song_requests.choice_line(
                r.get('title'), r.get('duration'))
            # Use default_factory to capture loop variable
            def make_callback(idx):
                return lambda: self._on_result_selected(idx, gp)
            items.append((label, make_callback(i)))

        items.append(("Cancel", lambda: gp.pop_last_substate()))
        m.add_items(items)
        menus.set_default_sounds(m)
        gp.add_substate(m)

    def _prompt_add_track_to_playlist(self, title, target, source="youtube"):
        """Prompt user to choose which custom playlist to add a track to"""
        from .. import menu as menu_mod, menus
        from ..speech import speak
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
            items.append((name, make_add_cb(name)))

        items.append(("Cancel", lambda: gp.pop_last_substate()))
        m.add_items(items)
        menus.set_default_sounds(m)
        gp.add_substate(m)

    def _on_result_selected(self, index, gp):
        """User selected a search result -> Queue (Queue Mode) or show options
        (Play Now / Play Next / Download / Save to Favorites / Playlist)."""
        gp.pop_last_substate()

        if index >= len(self._bot.search_results):
            return

        result = self._bot.search_results[index]
        title = result.get('title', 'Unknown')
        webpage_url = result.get('webpage_url', '')
        direct_url = result.get('url', '')
        http_headers = result.get('http_headers') or {}
        target = webpage_url or direct_url

        # Queue Mode: pressing Enter on a result queues it to play next
        # without opening the options menu. Toggle Queue Mode off to reach
        # Download / Favorites / Playlist again.
        if self._bot.queue_mode:
            self._bot._enqueue_track(
                title, target, "youtube", http_headers=http_headers,
                webpage_url=webpage_url, direct_url=direct_url,
            )
            return

        from .. import menu as menu_mod, menus
        m = menu_mod.Menu(self._bot.game, title, parrent=gp)
        items = []

        def play_now():
            gp.pop_last_substate()
            self._bot._start_youtube_stream_from_search(
                title, webpage_url, direct_url, http_headers
            )

        def add_to_queue():
            gp.pop_last_substate()
            self._bot._enqueue_track(
                title, target, "youtube", http_headers=http_headers,
                webpage_url=webpage_url, direct_url=direct_url,
            )

        def save_fav():
            gp.pop_last_substate()
            added = self._bot.playlist_mgr.add_favorite(title, target, "youtube")
            if added:
                speak(f"Saved {title} to favorites.")
            else:
                speak(f"{title} is already in favorites.")

        def save_playlist():
            gp.pop_last_substate()
            self._prompt_add_track_to_playlist(title, target, "youtube")

        def download_song():
            gp.pop_last_substate()
            self._bot.download_mgr.configure(
                [{"title": title, "target": target}],
                title,
            )

        items.append(("Play Now", play_now))
        items.append(("Play Next (Add to Queue)", add_to_queue))
        items.append(("Download Song", download_song))
        items.append(("Save to Favorites", save_fav))
        items.append(("Add to Playlist...", save_playlist))
        items.append(("Cancel", lambda: gp.pop_last_substate()))

        m.add_items(items)
        menus.set_default_sounds(m)
        gp.add_substate(m)


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
        speak(
            "Music Bot Custom Equalizer. Tab switches Bass, Mid, and Treble. "
            "Up and Down adjust. Page Up and Page Down adjust by 10. "
            "Home resets the current band to 50. Enter saves."
        )
        self._announce_current()

    def exit(self):
        super().exit()
        speak("Music Bot Equalizer closed.")

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
        speak(f"{label}. Slider: {self.values[band]} percent")

    def _adjust(self, amount):
        band, _ = self.BANDS[self.current_index]
        self._set_current(self.values[band] + amount)

    def _set_current(self, value):
        band, _ = self.BANDS[self.current_index]
        value = max(0, min(100, int(value)))
        if value != self.values[band]:
            self.values[band] = value
            self.bot.set_eq_profile("custom", dict(self.values))
        speak(f"{value} percent")

    def _close_and_commit(self):
        if self._closed:
            return
        self._closed = True
        self.bot.set_eq_profile("custom", dict(self.values))
        gp = self.bot._find_gameplay()
        if gp is not None:
            gp.pop_last_substate()
        self.bot._open_eq_menu()
