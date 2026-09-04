"""Music Bot controller - MapMusicBot: playback state, queue, playlists,
favorites, downloads, recording and every menu / keybinding hook that glues
the media + streaming layers into the game."""

import os
import queue
import random
import threading
import time
from collections import deque

import cyal
import cyal.exceptions
import pygame

from .. import options
from ..game_audio_recorder import GameAudioRecorderManager
from .music_downloader import MusicDownloadManager, is_supported_music_url
from ..speech import speak
from ..string_utils import friendly_key_name
from .media import (FFMPEG_PATH, DEFAULT_MAP_MUSIC, FALLBACK_PLAYLIST,
                    clamp_seek_position, format_track_position, YouTubeSearcher)
from .streaming import AudioStreamer, LiveRelayStreamer


class MapMusicBot:
    """Music Bot — searches YouTube and streams audio in real-time.
    Falls back to local files when YouTube is unavailable.
    
    Controls are resolved from the player's key bindings in gameplay.py.
    Modifier combinations continue to use the configured Music Bot key.
    """

    def __init__(self, game):
        self.game = game
        # OpenAL source for streaming (not using soundgroup — direct source for buffer queuing)
        self.stream_source = None
        # Local file playback
        self.soundgroup = game.audio_mngr.create_soundgroup(direct=True)
        self.current_local_sound = None

        # State
        self.current_title = ""
        self.playing = False
        self.paused = False
        self.mode = "idle"  # "idle", "youtube", "local"

        # YouTube streamer thread
        self.streamer = None
        self.live_relay_streamer = None
        self._stream_announced = False

        # Main-thread playback generation. Background URL resolution captures a
        # generation but may never create audio after a newer play or Stop.
        self._playback_generation = 0
        self._playback_generation_lock = threading.Lock()

        # Last played YouTube info (for replay)
        self.last_youtube_url = ""
        self.last_youtube_title = ""

        # Local playlist (fallback)
        self.playlist = []
        self.playlist_index = 0

        # Personal Favorites / custom-playlist queue.  This is separate from
        # map music, which has its own local playlist state above.
        self.play_queue = []
        self.play_queue_index = -1
        self.play_queue_label = ""

        # A shuffled Favorites feed. It preserves the user's current broadcast
        # routing and never changes saved playlists.
        self.feed_tracks = []
        self.feed_index = -1

        # Settings
        self.volume = options.get("music_bot_volume", 50)
        self.enabled = options.get("music_bot_enabled", True)
        self.broadcast_enabled = False  # Disabled by default (Private listening mode)
        self.broadcast_to_megaphone = False
        # Party Sync host session forces an upload so the session guests hear
        # the bot; the server narrows the relay to the guests only, so this
        # stays private even when the public broadcast toggle is off. It never
        # flips the user's own broadcast toggles.
        self.party_sync_force_upload = False
        # Line-in guitar raw PCM queue: the instrument input appends 20 ms
        # mono16 frames while guitar mode is on and this broadcast is enabled;
        # AudioStreamer mixes them into the outgoing stream.
        self.guitar_pcm_queue = deque(maxlen=10)

        # Personal Playlist & Favorites Manager (Stored locally on Client)
        from ..playlist_manager import PlaylistManager
        self.playlist_mgr = PlaylistManager()
        self.download_mgr = MusicDownloadManager(
            game,
            self._find_gameplay,
            ffmpeg_path=FFMPEG_PATH,
        )
        self.audio_recorder = GameAudioRecorderManager(game, self._find_gameplay)
        self.current_target = ""
        self.current_source = "youtube"

        # Unified Last Played Track State (for Ctrl+M Replay and Shift+M Pause/Resume)
        self.last_track_title = ""
        self.last_track_target = ""
        self.last_track_source = "youtube"

        # Search state
        self.searching = False
        self.is_loading_stream = False
        self.search_results = []

        # Environmental reverb tracking
        self._current_reverb_slot = None

    def toggle_broadcast(self):
        """Toggle network broadcasting on/off."""
        if getattr(self.game, 'pong_mode', False) and not getattr(self.game, 'pong_arcade', False):
            from ..speech import speak
            if getattr(self.game, 'pong_training', False):
                speak("Broadcasting is disabled in training mode.")
            else:
                speak("Broadcasting is disabled in competition matches.")
            return

        self.broadcast_enabled = not self.broadcast_enabled
        from ..speech import speak
        if self.broadcast_enabled:
            speak("Music broadcast enabled. Others can hear the music.")
        else:
            speak("Music broadcast disabled. Private listening mode.")
            if self.broadcast_to_megaphone:
                self.broadcast_to_megaphone = False
                from .. import consts
                self.game.network.send(
                    consts.CHANNEL_MISC,
                    "megaphone_broadcast_lock",
                    {"locked": False}
                )

    def _create_stream_source(self):
        """Create a fresh OpenAL source for streaming.
        Uses direct_channels=True for clear stereo, plus EFX reverb send
        for environmental atmosphere.
        """
        self._destroy_stream_source()
        try:
            src = self.game.audio_mngr.context.gen_source()
            src.direct_channels = True
            src.spatialize = False
            music_vol = self.game.audio_mngr.volume_categories.get("music", [100])[0] / 100
            src.gain = (self.volume / 100) * music_vol
            # A track started while the listener is underwater inherits the
            # active global water filter so it is dull from its first frame.
            active = getattr(self.game.audio_mngr, "filter", None)
            if active and active[-1] is not None:
                src.direct_filter = active[-1]
            self.stream_source = src
            # Apply current map reverb immediately
            self._sync_map_reverb()
        except Exception as ex:
            print(f"[MusicBot] Error creating source: {ex}")

    def _destroy_stream_source(self):
        if self.stream_source:
            try:
                self.stream_source.stop()
                drain_limit = 64
                while self.stream_source.buffers_processed > 0 and drain_limit > 0:
                    self.stream_source.unqueue_buffers()
                    drain_limit -= 1
                drain_limit = 64
                while self.stream_source.buffers_queued > 0 and drain_limit > 0:
                    self.stream_source.unqueue_buffers()
                    drain_limit -= 1
                self.stream_source.delete()
            except Exception:
                pass
            self.stream_source = None

    def _fade_out_source(self, source, streamer=None, duration=0.5):
        """Fade an active OpenAL stream source to 0 gain in background and delete."""
        if source is None:
            if streamer is not None:
                try:
                    streamer.stop()
                except Exception:
                    pass
            return

        def _fade_worker():
            try:
                start_gain = float(getattr(source, 'gain', 1.0) or 0.0)
                steps = 10
                step_sleep = duration / steps
                for i in range(steps):
                    fraction = (steps - 1 - i) / steps
                    try:
                        source.gain = max(0.0, start_gain * fraction)
                    except Exception:
                        break
                    time.sleep(step_sleep)
            except Exception:
                pass
            finally:
                if streamer is not None:
                    try:
                        streamer.stop()
                    except Exception:
                        pass
                try:
                    source.stop()
                    drain_limit = 64
                    while source.buffers_processed > 0 and drain_limit > 0:
                        source.unqueue_buffers()
                        drain_limit -= 1
                    while source.buffers_queued > 0 and drain_limit > 0:
                        source.unqueue_buffers()
                        drain_limit -= 1
                    source.delete()
                except Exception:
                    pass

        threading.Thread(target=_fade_worker, daemon=True).start()

    def _begin_playback_generation(self):
        """Invalidate pending starts and reserve a generation for new playback."""
        with self._playback_generation_lock:
            self._playback_generation += 1
            return self._playback_generation

    def _is_current_playback_generation(self, generation):
        with self._playback_generation_lock:
            return generation == self._playback_generation

    # === YouTube Playback ===

    def open_search(self):
        """Open search dialog — music keeps playing until a new song is selected."""
        if not self.enabled:
            speak("Music Bot is off. Press Ctrl Shift M to enable.")
            return
        if self.searching:
            speak("Still searching, please wait. Press Ctrl M to cancel.")
            return

        # Don't stop current music — let it play while user searches
        self.game.put(lambda: self._show_mode_menu())

    def _show_mode_menu(self):
        """Show menu to choose between YouTube search and Local playlist"""
        from .. import menu as menu_mod, menus

        gp = self._find_gameplay()
        if not gp:
            return

        def go_search():
            gp.pop_last_substate()
            self._open_search_input()

        def go_local():
            gp.pop_last_substate()
            self._open_file_dialog()

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

        m = menu_mod.Menu(self.game, "Music Bot Mode", parrent=gp)
        items = [
            ("Search YouTube", go_search),
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
                status = "ON" if self.broadcast_to_megaphone else "OFF"
                return f"Broadcast to Megaphone: {status}"
                
            def toggle_megaphone_routing():
                # No broadcast_enabled gate: piano broadcast is independent of music
                # playback, so performers can broadcast the piano through PA speakers
                # without starting a music track first.
                self.broadcast_to_megaphone = not self.broadcast_to_megaphone
                status_text = "enabled" if self.broadcast_to_megaphone else "disabled"
                speak(f"Broadcast to megaphone {status_text}.")
                m.speak_current_item()

                # Send lock request to the server
                from .. import consts
                self.game.network.send(
                    consts.CHANNEL_MISC,
                    "megaphone_broadcast_lock",
                    {"locked": self.broadcast_to_megaphone}
                )
                
            items.append((get_megaphone_label, toggle_megaphone_routing))

        items.extend([
            ("Help", go_help),
            ("Cancel", lambda: gp.pop_last_substate())
        ])
        m.add_items(items)
        menus.set_default_sounds(m)
        gp.add_substate(m)

    # === Party Sync (listen together with invited friends) ===
    # The server (libs/party_sync.ts) runs the session and gates the relay to
    # session guests only. Host-side, the client only needs to keep uploading
    # its stream while a session is active (party_sync_force_upload) and drive
    # the invite/kick/end controls below. Guests receive the stream through the
    # normal music-source receive leg (host voice_channel -> entity
    # music_source), so no special guest audio code is needed.

    def _party_sync_pair(self):
        """(gameplay, PartySyncState) pair, creating the state lazily."""
        gp = self._find_gameplay()
        if gp is None:
            return None, None
        ps = getattr(gp, "party_sync", None)
        if ps is None:
            from ..party_sync import PartySyncState
            ps = PartySyncState()
            gp.party_sync = ps
        return gp, ps

    def _party_sync_send(self, event, data=None):
        from .. import consts
        self.game.network.send(
            consts.CHANNEL_MISC, event, data if data is not None else {}
        )

    def _clear_party_sync_direct(self):
        """Restore any entity left in Party Sync direct-to-ear audio mode
        (host-music feed AND party team-talk voice)."""
        from ..party_sync import clear_all_party_direct
        gp, _ = self._party_sync_pair()
        if gp is None:
            return
        clear_all_party_direct(gp)

    def _open_party_sync_menu(self):
        """Host/guest Party Sync controls (entry from the Music Bot menu)."""
        from .. import menu as menu_mod, menus
        from ..speech import speak
        gp, ps = self._party_sync_pair()
        if gp is None:
            return

        m = menu_mod.Menu(self.game, "Party Sync", parrent=gp)
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
                self._party_sync_send("party_sync_start")
            items.append(("Start Party Sync session", do_start))
            items.append((
                "Invite friends on this map to hear your music privately",
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
                # Back in the invite picker returns to this Party controls
                # menu (the Ctrl+F8 quick menu sets its own target).
                gp._party_sync_invite_back = (
                    lambda: self._open_party_sync_menu()
                )
                self._party_sync_send("party_sync_list")
            items.append(("Invite players from this map", do_invite))
            def do_end():
                close_top()
                self._party_sync_send("party_sync_end")
                ps.end_session()
                self.party_sync_force_upload = False
                self._clear_party_sync_direct()
            items.append(("End Party Sync session", do_end))
        elif ps.role == "guest":
            def do_leave():
                close_top()
                self._party_sync_send("party_sync_leave")
                ps.end_session()
                self._clear_party_sync_direct()
            items.append((f"Listening to {ps.host_name}", lambda: None))
            items.append(("Leave Party Sync session", do_leave))

        items.append(("Back", back_to_bot_menu))
        m.add_items(items)
        menus.set_default_sounds(m)
        gp.add_substate(m)

    def _open_party_kick_menu(self, ps):
        """Pick which listener to kick (host only)."""
        from .. import menu as menu_mod, menus
        from ..speech import speak
        gp, _ = self._party_sync_pair()
        if gp is None:
            return
        if not ps or not ps.guests:
            speak("Nobody is listening right now.")
            self._open_party_sync_menu()
            return
        m = menu_mod.Menu(self.game, "Kick a Party Sync listener", parrent=gp)
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
                    self._party_sync_send("party_sync_kick", {"name": target})
                return cb
            items.append((f"Kick {name}", make_kick()))
        items.append(("Back", back_to_party))
        m.add_items(items)
        menus.set_default_sounds(m)
        gp.add_substate(m)

    def _save_current_to_favorites(self):
        """Save currently playing track to Favorites"""
        from ..speech import speak
        if not self.current_title or not self.current_target:
            speak("No track is currently playing.")
            return

        added = self.playlist_mgr.add_favorite(self.current_title, self.current_target, self.current_source)
        if added:
            speak(f"Saved {self.current_title} to favorites.")
        else:
            speak(f"{self.current_title} is already in favorites.")

    def _show_personal_feed_menu(self):
        """Open the shuffled Favorites feed controls."""
        from .. import menu as menu_mod, menus
        gp = self._find_gameplay()
        if not gp:
            return

        def start_feed():
            gp.pop_last_substate()
            self._start_personal_feed()

        def next_feed():
            gp.pop_last_substate()
            self._next_personal_feed()

        m = menu_mod.Menu(self.game, "Personal Music Feed", parrent=gp)
        m.add_items([
            ("Start shuffled Favorites feed", start_feed),
            ("Next feed song", next_feed),
            ("Back", lambda: (gp.pop_last_substate(), self._show_mode_menu())),
        ])
        menus.set_default_sounds(m)
        gp.add_substate(m)

    def _clear_personal_feed(self):
        self.feed_tracks = []
        self.feed_index = -1

    def _start_personal_feed(self):
        favorites = [dict(track) for track in self.playlist_mgr.get_favorites() if track.get("target")]
        if not favorites:
            speak("Your Favorites are empty. Save songs first, then start the feed.")
            return

        random.shuffle(favorites)
        self.feed_tracks = favorites
        self.feed_index = 0
        speak(f"Personal Music Feed started. {len(favorites)} songs from Favorites.")
        self._play_personal_feed_track()

    def _next_personal_feed(self):
        if not self.feed_tracks:
            self._start_personal_feed()
            return
        self.feed_index = (self.feed_index + 1) % len(self.feed_tracks)
        self._play_personal_feed_track()

    def previous_feed_track(self):
        """Return to the prior song in an active Personal Music Feed."""
        if not self.feed_tracks:
            speak("Personal Music Feed is not active.")
            return
        self.feed_index = (self.feed_index - 1) % len(self.feed_tracks)
        self._play_personal_feed_track()

    def next_feed_track(self):
        """Advance an active Personal Music Feed without changing normal playlists."""
        if not self.feed_tracks:
            speak("Personal Music Feed is not active.")
            return
        self._next_personal_feed()

    def _play_personal_feed_track(self):
        if not (0 <= self.feed_index < len(self.feed_tracks)):
            return
        track = self.feed_tracks[self.feed_index]
        self.play_single_track(
            track.get("title", "Unknown"),
            track.get("target", ""),
            track.get("source", "youtube"),
            preserve_feed=True,
        )

    def _open_playlists_menu(self):
        """Show main My Playlists & Favorites menu"""
        from .. import menu as menu_mod, menus
        gp = self._find_gameplay()
        if not gp:
            return

        m = menu_mod.Menu(self.game, "My Playlists & Favorites", parrent=gp)
        items = []

        if self.current_title and self.current_target:
            def fav_current():
                gp.pop_last_substate()
                self._save_current_to_favorites()
            items.append(("Save Current Song to Favorites", fav_current))

        def go_favorites():
            gp.pop_last_substate()
            self._show_favorites_menu()

        def go_create_playlist():
            gp.pop_last_substate()
            self._prompt_create_playlist()

        items.append(("All Favorites", go_favorites))
        items.append(("Create New Playlist", go_create_playlist))

        # List custom playlists
        playlist_names = self.playlist_mgr.get_playlist_names()
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

        gp = self._find_gameplay()
        if not gp:
            return

        def start_or_stop():
            # The user returns to gameplay for the countdown/recording. Opening
            # Record Audio again exposes the current status and Stop action.
            gp.pop_last_substate()
            self.audio_recorder.menu_action()

        def go_back():
            gp.pop_last_substate()
            self._show_mode_menu()

        def go_settings():
            gp.pop_last_substate()
            self._open_recording_settings_menu()

        m = menu_mod.Menu(self.game, "Record Audio", parrent=gp)
        items = [
            (self.audio_recorder.folder_menu_label, self.audio_recorder.speak_folder),
            ("Set Recording Folder", self.audio_recorder.choose_folder),
            (self.audio_recorder.menu_label, start_or_stop),
            (self.audio_recorder.status_menu_label, self.audio_recorder.speak_status),
            ("Recording Settings", go_settings),
            ("Open Recording Folder", self.audio_recorder.open_folder),
        ]
        items.append(("Back", go_back))
        m.add_items(items)
        menus.set_default_sounds(m)
        gp.add_substate(m)

    def _open_recording_settings_menu(self):
        """Open accessible, persistent settings that are safe for final-mix capture."""
        from .. import menu as menu_mod, menus

        gp = self._find_gameplay()
        if not gp:
            return

        def go_back():
            gp.pop_last_substate()
            self._open_recording_menu()

        def confirm_restore():
            gp.pop_last_substate()
            self._open_recording_reset_confirmation()

        m = menu_mod.Menu(self.game, "Audio Recording Settings", parrent=gp)
        m.add_items([
            (self.audio_recorder.capture_scope_label, self.audio_recorder.speak_capture_scope),
            (self.audio_recorder.computer_audio_setting_label, self.audio_recorder.toggle_computer_audio),
            (self.audio_recorder.microphone_setting_label, self.audio_recorder.toggle_microphone),
            (self.audio_recorder.countdown_setting_label, self.audio_recorder.cycle_countdown),
            (self.audio_recorder.split_setting_label, self.audio_recorder.cycle_split_minutes),
            (self.audio_recorder.announce_setting_label, self.audio_recorder.toggle_announce_details),
            ("Restore Recording Defaults", confirm_restore),
            ("Back", go_back),
        ])
        menus.set_default_sounds(m)
        gp.add_substate(m)

    def _open_recording_reset_confirmation(self):
        """Require an explicit second action before restoring recording defaults."""
        from .. import menu as menu_mod, menus

        gp = self._find_gameplay()
        if not gp:
            return

        def restore():
            gp.pop_last_substate()
            self.audio_recorder.restore_setting_defaults()
            self._open_recording_settings_menu()

        def cancel():
            gp.pop_last_substate()
            self._open_recording_settings_menu()

        m = menu_mod.Menu(
            self.game,
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
        gp = self._find_gameplay()
        if not gp:
            return
        self.download_mgr.show_progress_bar()

        items = [
            (self.download_mgr.folder_menu_label, self.download_mgr.speak_folder),
            ("Set Download Folder", self.download_mgr.choose_default_folder),
        ]
        if self.download_mgr.has_saved_folder_setting():
            items.append(("Clear Saved Download Folder", self.download_mgr.clear_default_folder))
        if (self.current_source != "local"
                and is_supported_music_url(self.current_target)):
            def download_current():
                gp.pop_last_substate()
                self.download_mgr.configure(
                    [{"title": self.current_title, "target": self.current_target}],
                    self.current_title or "current song",
                )
            items.append(("Download Current Song", download_current))

        favorites = self.playlist_mgr.get_favorites()
        if favorites:
            def download_favorites():
                gp.pop_last_substate()
                self.download_mgr.configure(favorites, "Favorites")
            items.append(("Download All Favorites", download_favorites))

        if self.playlist_mgr.get_playlist_names():
            def choose_playlist():
                gp.pop_last_substate()
                self._open_download_playlist_menu()
            items.append(("Download a Saved Playlist", choose_playlist))

        items.append((
            self.download_mgr.parallel_menu_label,
            self.download_mgr.cycle_parallel_downloads,
        ))
        items.append((
            self.download_mgr.notification_menu_label,
            self.download_mgr.toggle_file_notifications,
        ))

        items.append((
            self.download_mgr.progress_menu_label,
            self.download_mgr.speak_status,
        ))
        if self.download_mgr.is_active():
            items.append(("Cancel Active Download", self.download_mgr.cancel))
        items.append(("Back", lambda: (gp.pop_last_substate(), self._show_mode_menu())))

        download_mgr = self.download_mgr

        class DownloadMusicMenu(menu_mod.Menu):
            def exit(menu_self):
                download_mgr.hide_progress_bar()
                super().exit()

        m = DownloadMusicMenu(self.game, "Music Download Center", parrent=gp)
        m.add_items(items)
        menus.set_default_sounds(m)
        gp.add_substate(m)

    def _open_download_playlist_menu(self):
        from .. import menu as menu_mod, menus
        gp = self._find_gameplay()
        if not gp:
            return
        m = menu_mod.Menu(self.game, "Select Playlist to Download", parrent=gp)
        items = []
        for name in self.playlist_mgr.get_playlist_names():
            def choose(playlist_name=name):
                gp.pop_last_substate()
                self.download_mgr.configure(
                    self.playlist_mgr.get_playlist_tracks(playlist_name),
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
        gp = self._find_gameplay()
        if not gp:
            return

        favs = self.playlist_mgr.get_favorites()
        if not favs:
            speak("No favorite tracks saved yet.")
            return

        m = menu_mod.Menu(self.game, "All Favorites", parrent=gp)
        items = []

        def play_all():
            gp.pop_last_substate()
            self._start_track_queue(favs, "Favorites")

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
        gp = self._find_gameplay()
        if not gp:
            return

        tracks = self.playlist_mgr.get_playlist_tracks(playlist_name)
        m = menu_mod.Menu(self.game, f"Playlist: {playlist_name}", parrent=gp)
        items = []

        if tracks:
            def play_all():
                gp.pop_last_substate()
                self._play_playlist_all(playlist_name)

            items.append(("Play All Tracks", play_all))

        def delete_playlist():
            gp.pop_last_substate()
            self.playlist_mgr.delete_playlist(playlist_name)
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
        gp = self._find_gameplay()
        if not gp:
            return

        m = menu_mod.Menu(self.game, f"Track: {title}", parrent=gp)
        items = []

        def play_now():
            gp.pop_last_substate()
            self.play_single_track(title, target, source)

        items.append(("Play Now", play_now))

        if source != "local" and is_supported_music_url(target):
            def download_track():
                gp.pop_last_substate()
                self.download_mgr.configure(
                    [{"title": title, "target": target}],
                    title,
                )
            items.append(("Download This Track", download_track))

        if is_favorite:
            def remove_fav():
                gp.pop_last_substate()
                self.playlist_mgr.remove_favorite(target)
                speak(f"Removed {title} from favorites.")
            items.append(("Remove from Favorites", remove_fav))

        if playlist_name:
            def remove_from_p():
                gp.pop_last_substate()
                self.playlist_mgr.remove_from_playlist(playlist_name, target)
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

    def play_single_track(self, title, target, source, preserve_queue=False, preserve_feed=False):
        """Play a single track from playlist/favorites"""
        from ..speech import speak
        import threading
        if not preserve_queue:
            self._clear_track_queue()
        if not preserve_feed:
            self._clear_personal_feed()
        playback_generation = self._begin_playback_generation()
        self.current_title = title
        self.current_target = target
        self.current_source = source

        # Save for replay
        self.last_track_title = title
        self.last_track_target = target
        self.last_track_source = source
        self.last_youtube_url = target
        self.last_youtube_title = title

        if source == "local":
            self._start_local_file_stream(
                target,
                title,
                preserve_queue=preserve_queue,
                preserve_feed=preserve_feed,
                playback_generation=playback_generation,
            )
        else:
            if target.startswith("http://") or target.startswith("https://"):
                speak(f"Loading: {title}")
                self.stop(
                    clear_queue=False,
                    clear_feed=not preserve_feed,
                    invalidate_pending=False,
                    fade=True,
                )
                self.is_loading_stream = True

                def do_play():
                    stream_info = YouTubeSearcher.get_stream_info(target,
                        cancelled=lambda: not self._is_current_playback_generation(playback_generation))
                    if not self._is_current_playback_generation(playback_generation):
                        return
                    if not stream_info:
                        if self._is_current_playback_generation(playback_generation):
                            speak("Failed to get audio stream.")
                            self.is_loading_stream = False
                        return
                    self.game.put(lambda: self._start_youtube_stream(
                        stream_info['url'], title, playback_generation,
                        http_headers=stream_info.get('http_headers'),
                        canonical_url=target,
                    ))

                threading.Thread(target=do_play, daemon=True).start()
            else:
                self._on_search_submit(target)

    def _clear_track_queue(self):
        self.play_queue = []
        self.play_queue_index = -1
        self.play_queue_label = ""

    def _start_track_queue(self, tracks, label):
        """Start a personal playlist/favorites queue without mixing map music."""
        from ..speech import speak
        self.play_queue = [dict(track) for track in tracks if track.get("target")]
        self.play_queue_index = 0
        self.play_queue_label = label
        if not self.play_queue:
            speak(f"{label} is empty.")
            self._clear_track_queue()
            return
        speak(f"Playing {label}. {len(self.play_queue)} tracks.")
        self._play_queued_track()

    def _play_queued_track(self):
        if not (0 <= self.play_queue_index < len(self.play_queue)):
            return
        track = self.play_queue[self.play_queue_index]
        self.play_single_track(
            track.get("title", "Unknown"),
            track.get("target", ""),
            track.get("source", "youtube"),
            preserve_queue=True,
        )

    def _advance_track_queue(self):
        from ..speech import speak
        if not self.play_queue:
            return False
        self.play_queue_index += 1
        if self.play_queue_index >= len(self.play_queue):
            speak(f"{self.play_queue_label} finished.")
            self._clear_track_queue()
            return False
        self._play_queued_track()
        return True

    def _play_playlist_all(self, playlist_name):
        """Play all tracks in a custom playlist sequentially"""
        tracks = self.playlist_mgr.get_playlist_tracks(playlist_name)
        self._start_track_queue(tracks, f"playlist {playlist_name}")

    def _prompt_create_playlist(self):
        """Prompt user for a new playlist name"""
        gp = self._find_gameplay()
        if gp:
            gp.add_substate(self.game.input.run(
                "Enter new playlist name:",
                handeler=self._on_create_playlist_submit
            ))

    def _on_create_playlist_submit(self, name):
        from ..speech import speak
        gp = self._find_gameplay()
        if gp:
            gp.pop_last_substate()

        if not name.strip():
            speak("Cancelled.")
            return

        success = self.playlist_mgr.create_playlist(name)
        if success:
            speak(f"Created playlist {name}.")
        else:
            speak(f"Playlist {name} already exists.")
        self._open_playlists_menu()

    def _show_help_menu(self):
        """Show scrollable menu containing the Music Bot key controls"""
        from .. import menu as menu_mod, menus

        gp = self._find_gameplay()
        if not gp:
            return

        def go_back():
            gp.pop_last_substate()
            self._show_mode_menu()

        toggle_key = friendly_key_name(
            self.game.keyconfig.get("music_bot_toggle", pygame.K_m)
        )
        volume_down_key = friendly_key_name(
            self.game.keyconfig.get("music_bot_vol_down", pygame.K_F9)
        )
        volume_up_key = friendly_key_name(
            self.game.keyconfig.get("music_bot_vol_up", pygame.K_F10)
        )

        m = menu_mod.Menu(self.game, "Music Bot Controls Help", parrent=gp)
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

    def _open_file_dialog(self):
        """Open Windows file chooser dialog in a background thread to prevent game freezing"""
        import threading
        from ..speech import speak

        def select_file():
            try:
                import tkinter as tk
                from tkinter import filedialog
                
                root = tk.Tk()
                root.withdraw()  # Hide the main tk window
                root.attributes("-topmost", True)  # Bring file dialog to front
                
                # Audio AND video containers: the music bot decodes whatever
                # ffmpeg can demux (movies, music videos, podcasts...).
                media_types = (
                    "*.mp3 *.wav *.ogg *.flac *.m4a *.aac *.opus *.wma *.mka "
                    "*.mp4 *.mkv *.webm *.mov *.m4v *.avi *.mpg *.mpeg *.wmv "
                    "*.flv *.ts *.m2ts *.mts *.3gp *.3g2 *.ogv *.vob *.rmvb "
                    "*.asf *.f4v *.aiff *.ape *.mid *.midi"
                )
                filepath = filedialog.askopenfilename(
                    title="Select Audio or Video File",
                    filetypes=[
                        ("All Media Files", media_types),
                        ("Audio Files", "*.mp3 *.wav *.ogg *.flac *.m4a *.aac *.opus *.wma *.mka *.aiff *.ape *.mid *.midi"),
                        ("Video Files", "*.mp4 *.mkv *.webm *.mov *.m4v *.avi *.mpg *.mpeg *.wmv *.flv *.ts *.m2ts *.mts *.3gp *.3g2 *.ogv *.vob *.rmvb *.asf *.f4v"),
                        ("All Files", "*.*")
                    ]
                )
                root.destroy()
                
                if filepath:
                    # Resolve base name as title
                    import os
                    title = os.path.splitext(os.path.basename(filepath))[0]
                    # Put stream start callback on the main game thread queue
                    self.game.put(lambda: self._start_local_file_stream(filepath, title))
                else:
                    self.game.put(lambda: speak("No file selected."))
            except Exception as ex:
                print(f"[MusicBot] Error opening file dialog: {ex}")
                self.game.put(lambda: speak("Error opening file dialog."))

        t = threading.Thread(target=select_file, daemon=True)
        t.start()
        speak("Opening file explorer...")

    def _start_local_file_stream(self, filepath, title, preserve_queue=False, preserve_feed=False,
                                 playback_generation=None):
        """Start streaming local file"""
        import os
        if not os.path.exists(filepath):
            speak("File not found.")
            return

        if playback_generation is None:
            playback_generation = self._begin_playback_generation()
        if not self._is_current_playback_generation(playback_generation):
            return

        speak(f"Loading local file: {title}")
        self.current_title = title
        self.current_target = filepath
        self.current_source = "local"

        # Save for replay
        self.last_track_title = title
        self.last_track_target = filepath
        self.last_track_source = "local"
        self.is_loading_stream = True

        # Stop any current playback
        self.stop(
            clear_queue=not preserve_queue,
            clear_feed=not preserve_feed,
            invalidate_pending=False,
        )

        # Start streaming local file via ffmpeg -> AudioStreamer
        self._start_youtube_stream(filepath, title, playback_generation)

    def _open_search_input(self):
        """Open the text input for search query"""
        self._gp = self._find_gameplay()
        if self._gp:
            self._gp.add_substate(self.game.input.run(
                "Enter song name:",
                handeler=self._on_search_submit
            ))

    def _find_gameplay(self):
        """Find the Gameplay state instance"""
        from .. import gameplay
        for st in reversed(self.game.stack):
            if isinstance(st, gameplay.Gameplay):
                return st
        return None

    def _is_music_owner(self):
        """True if this performer holds the single music-bot PA slot.

        The server keeps the music slot single-owner (only one MP3 stream on
        the PA at a time, so two people's music never overlaps); everyone else
        with "Broadcast to Megaphone" still broadcasts their live instruments.
        """
        gp = self._find_gameplay()
        if not gp or not getattr(gp, 'megaphone', None):
            return False
        name = getattr(getattr(gp, 'player', None), 'name', '')
        return bool(name and getattr(gp.megaphone, 'lock_owner', None) == name)

    def _on_search_submit(self, query):
        """Called when user submits search query"""
        # ALWAYS pop the input substate first — otherwise it blocks all events!
        gp = self._gp or self._find_gameplay()
        if gp:
            gp.pop_last_substate()

        if not query.strip():
            speak("Search cancelled.")
            return

        speak(f"Searching: {query}")
        self.searching = True

        # Search in background thread to not block game
        def do_search():
            results = YouTubeSearcher.search(query, count=5)
            self.search_results = results
            self.searching = False
            # Show results menu on main thread
            self.game.put(lambda: self._show_results_menu(results))

        t = threading.Thread(target=do_search, daemon=True)
        t.start()

    def _show_results_menu(self, results):
        """Show search results as a menu"""
        from .. import menu as menu_mod, menus

        gp = self._find_gameplay()
        if not gp:
            return

        if not results:
            speak("No results found.")
            return

        m = menu_mod.Menu(self.game, "Search Results", parrent=gp)
        items = []
        for i, r in enumerate(results):
            dur = int(r.get('duration', 0))
            dur_str = f"{dur // 60}:{dur % 60:02d}" if dur else "?"
            title = r.get('title', 'Unknown')
            # Use default_factory to capture loop variable
            def make_callback(idx):
                return lambda: self._on_result_selected(idx, gp)
            items.append((f"{title} ({dur_str})", make_callback(i)))

        items.append(("Cancel", lambda: gp.pop_last_substate()))
        m.add_items(items)
        menus.set_default_sounds(m)
        gp.add_substate(m)

    def _prompt_add_track_to_playlist(self, title, target, source="youtube"):
        """Prompt user to choose which custom playlist to add a track to"""
        from .. import menu as menu_mod, menus
        from ..speech import speak
        gp = self._find_gameplay()
        if not gp:
            return

        names = self.playlist_mgr.get_playlist_names()
        if not names:
            speak("No custom playlists created yet. Please create one first.")
            return

        m = menu_mod.Menu(self.game, f"Add '{title}' to Playlist", parrent=gp)
        items = []
        for name in names:
            def make_add_cb(p_name):
                def do_add():
                    gp.pop_last_substate()
                    added = self.playlist_mgr.add_to_playlist(p_name, title, target, source)
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
        """User selected a search result -> Show options (Play Now / Save to Favorites / Save to Playlist)"""
        gp.pop_last_substate()

        if index >= len(self.search_results):
            return

        result = self.search_results[index]
        title = result.get('title', 'Unknown')
        webpage_url = result.get('webpage_url', '')
        direct_url = result.get('url', '')
        http_headers = result.get('http_headers') or {}
        target = webpage_url or direct_url

        from .. import menu as menu_mod, menus
        m = menu_mod.Menu(self.game, title, parrent=gp)
        items = []

        def play_now():
            gp.pop_last_substate()
            self._start_youtube_stream_from_search(
                title, webpage_url, direct_url, http_headers
            )

        def save_fav():
            gp.pop_last_substate()
            added = self.playlist_mgr.add_favorite(title, target, "youtube")
            if added:
                speak(f"Saved {title} to favorites.")
            else:
                speak(f"{title} is already in favorites.")

        def save_playlist():
            gp.pop_last_substate()
            self._prompt_add_track_to_playlist(title, target, "youtube")

        def download_song():
            gp.pop_last_substate()
            self.download_mgr.configure(
                [{"title": title, "target": target}],
                title,
            )

        items.append(("Play Now", play_now))
        items.append(("Download Song", download_song))
        items.append(("Save to Favorites", save_fav))
        items.append(("Add to Playlist...", save_playlist))
        items.append(("Cancel", lambda: gp.pop_last_substate()))

        m.add_items(items)
        menus.set_default_sounds(m)
        gp.add_substate(m)

    def _start_youtube_stream_from_search(self, title, webpage_url, direct_url,
                                          http_headers=None):
        from ..speech import speak
        if self.is_loading_stream:
            speak("Please wait, already loading a track.")
            return

        # A manually selected search result leaves the private feed.
        self._clear_personal_feed()
        playback_generation = self._begin_playback_generation()

        speak(f"Loading: {title}")
        self.current_title = title
        self.current_target = webpage_url or direct_url
        self.current_source = "youtube"

        # Save for replay
        self.last_track_title = title
        self.last_track_target = webpage_url or direct_url
        self.last_track_source = "youtube"
        self.last_youtube_url = webpage_url
        self.last_youtube_title = title
        self.is_loading_stream = True

        # Stop any current playback
        self.stop(invalidate_pending=False, fade=True)
        self.is_loading_stream = True

        # Get stream URL in background
        def do_play():
            import threading
            # Resolve again at selection time so URL and authorization headers
            # are fresh. Search result direct URLs are only a fallback for
            # providers that do not expose a canonical webpage URL.
            stream_info = (
                YouTubeSearcher.get_stream_info(webpage_url,
                    cancelled=lambda: not self._is_current_playback_generation(playback_generation))
                if webpage_url else None
            )
            if not self._is_current_playback_generation(playback_generation):
                return
            if not stream_info and direct_url:
                stream_info = {
                    'url': direct_url,
                    'http_headers': dict(http_headers or {}),
                }
            if not stream_info:
                if self._is_current_playback_generation(playback_generation):
                    speak("Failed to get audio stream.")
                    self.is_loading_stream = False
                return
            # Start streaming on main thread
            self.game.put(lambda: self._start_youtube_stream(
                stream_info['url'], title, playback_generation,
                http_headers=stream_info.get('http_headers'),
                canonical_url=webpage_url or direct_url,
            ))

        t = threading.Thread(target=do_play, daemon=True)
        t.start()

    def _start_youtube_stream(self, audio_url, title, playback_generation=None,
                              http_headers=None, canonical_url=None,
                              start_offset=0.0, start_paused=False):
        """Start streaming from a YouTube audio URL or a local media file.

        start_offset seeks the new decode head to a content position in
        seconds (ffmpeg input seek, like the jukebox mid-song path);
        start_paused keeps the pre-buffered head silent until resume.
        """
        if (playback_generation is not None
                and not self._is_current_playback_generation(playback_generation)):
            return
        self.is_loading_stream = False
        self._create_stream_source()
        if not self.stream_source:
            speak("Audio error.")
            return

        self.streamer = AudioStreamer(
            self.game, audio_url, self.stream_source, self.volume, bot=self,
            http_headers=http_headers,
            canonical_url=canonical_url,
            start_offset=start_offset,
            start_paused=start_paused,
        )
        self.streamer.start()

        self.mode = "youtube"
        self.playing = True
        # A seek performed while paused stays paused: the new streamer starts
        # silent and only becomes audible when the bot is resumed.
        self.paused = bool(start_paused)
        self.current_title = title
        self._stream_announced = False

    # === Seeking (fast-forward / rewind) ===

    def track_position(self):
        """Approximate audible position (seconds) of the current stream."""
        streamer = self.streamer
        if streamer is None or not streamer.is_alive():
            return None
        try:
            return max(0.0, float(streamer.content_position() or 0.0))
        except Exception:
            return None

    def seek_by(self, delta):
        """Seek the current Music Bot stream by delta seconds (negative = backward).

        Works for both YouTube links and local files: the current track is
        restarted at the target position via an ffmpeg input seek, so the
        same code path serves movies, video files and songs.  A seek while
        paused keeps the new stream paused.
        """
        if self.is_loading_stream:
            speak("Please wait, the track is still loading.")
            return
        if not self.playing or self.streamer is None or not self.streamer.is_alive():
            speak("No active Music Bot track to seek.")
            return
        position = self.track_position()
        if position is None:
            speak("No active Music Bot track to seek.")
            return
        target = clamp_seek_position(position, delta)
        if target is None:
            speak("Already at the start of the track.")
            return
        self._seek_restart(target)

    def _seek_restart(self, position):
        """Restart the current track at an absolute position (seconds)."""
        title = self.current_title
        target = self.current_target
        source = self.current_source
        was_paused = bool(self.paused)
        if not title or not target:
            speak("Nothing to seek.")
            return
        if source == "local" and not os.path.exists(target):
            speak("File not found.")
            return

        # Snapshot the outgoing stream's direct URL + headers BEFORE stop()
        # nulls self.streamer. A YouTube seek can restart from this still-
        # valid signed URL and skip the slow isolated yt-dlp extraction.
        outgoing = getattr(self, "streamer", None)
        seek_reuse_url = getattr(outgoing, "audio_url", "") or ""
        seek_reuse_headers = dict(getattr(outgoing, "http_headers", None) or {})

        playback_generation = self._begin_playback_generation()
        speak(f"Seeking to {format_track_position(position)}.")
        # Hard cut (no fade) so the new decode head starts at the target
        # position immediately; the track queue and feed survive the seek.
        self.stop(clear_queue=False, clear_feed=False, invalidate_pending=False)
        self.is_loading_stream = True

        if source == "local":
            # Local files restart directly — no URL resolution needed.
            self._start_youtube_stream(
                target, title, playback_generation,
                start_offset=position,
                start_paused=was_paused,
            )
            return

        # Remote (YouTube) tracks normally re-resolve so the seeked range
        # request uses a fresh signed stream URL (an expired googlevideo URL
        # 403s forever) - but that extraction is the SLOW part of a seek. When
        # the outgoing stream still holds a direct URL + headers (they age over
        # hours, not seconds) restart straight from them: the seek then only
        # pays for one ffmpeg restart with a fast range request. A stale URL is
        # self-healing - AudioStreamer.run's startup retry ladder re-resolves a
        # fresh URL from the canonical page when ffmpeg 403s.
        if seek_reuse_url.startswith(("http://", "https://")):
            self._start_youtube_stream(
                seek_reuse_url, title, playback_generation,
                http_headers=seek_reuse_headers,
                canonical_url=target,
                start_offset=position,
                start_paused=was_paused,
            )
            return

        def do_seek():
            stream_info = YouTubeSearcher.get_stream_info(
                target,
                cancelled=lambda: not self._is_current_playback_generation(playback_generation))
            if not self._is_current_playback_generation(playback_generation):
                return
            if not stream_info:
                if self._is_current_playback_generation(playback_generation):
                    speak("Failed to get audio stream.")
                    self.is_loading_stream = False
                return
            self.game.put(lambda: self._start_youtube_stream(
                stream_info['url'], title, playback_generation,
                http_headers=stream_info.get('http_headers'),
                canonical_url=target,
                start_offset=position,
                start_paused=was_paused,
            ))

        threading.Thread(target=do_seek, daemon=True).start()

    def has_last_track(self):
        """Check if any track has been played and is available for replay"""
        return bool(self.last_track_target or self.last_youtube_url)

    def _replay_last(self):
        """Replay the last played track (YouTube, Local, or Playlist)"""
        if self.is_loading_stream:
            return

        if self.last_track_target:
            self.play_single_track(self.last_track_title, self.last_track_target, self.last_track_source)
        elif self.last_youtube_url:
            self.play_single_track(self.last_youtube_title, self.last_youtube_url, "youtube")

    # === Local File Playback (fallback/map music) ===

    def load_map_music(self, map_data):
        """Store playlist based on map data but do NOT auto-play.
        The bot only plays music when the user explicitly searches YouTube.
        Local playlist is kept as a fallback reference only.
        """
        playlist = self._resolve_playlist(map_data)
        if playlist:
            self.playlist = playlist
            self.playlist_index = 0

    def _resolve_playlist(self, map_data):
        if isinstance(map_data, dict):
            # Try music_bot data from server
            mbd = map_data.get("music_bot")
            if mbd and mbd.get("tracks"):
                return mbd["tracks"]
            # Try matching map name
            map_name = ""
            for el in map_data.get("elements", []):
                if el.get("type") == "zone":
                    map_name = el.get("data", {}).get("innerText", "")
                    if map_name:
                        break
            if not map_name:
                map_name = map_data.get("name", "")
            for key, tracks in DEFAULT_MAP_MUSIC.items():
                if key in map_name.lower():
                    return tracks
        return FALLBACK_PLAYLIST.copy()

    def _play_local_current(self):
        if not self.playlist:
            return
        idx = self.playlist_index % len(self.playlist)
        track = self.playlist[idx]
        path = f"music/{track}"

        self._stop_local()
        try:
            snd = self.soundgroup.play(
                path, looping=False, id="music_bot_track", cat="music", volume=self.volume
            )
            if snd is None:
                # File doesn't exist or failed to load — skip to next
                print(f"[MusicBot] Failed to load: {path}, skipping...")
                self.playing = False
                return
            self.current_local_sound = snd
            self.mode = "local"
            self.playing = True
            self.paused = False
            self.current_title = track
        except Exception as ex:
            print(f"[MusicBot] Error playing local: {ex}")
            self.playing = False

    def _stop_local(self):
        if self.current_local_sound:
            try:
                self.current_local_sound.destroy()
            except Exception:
                pass
            self.current_local_sound = None

    # === Common Controls ===

    def stop(self, clear_queue=True, clear_feed=True, invalidate_pending=True, fade=False):
        """Stop all playback and cancel any pending search"""
        if invalidate_pending:
            self._begin_playback_generation()
        # Cancel any ongoing search
        self.searching = False
        self.is_loading_stream = False
        # Stop YouTube streamer
        if fade and self.stream_source:
            old_src = self.stream_source
            old_streamer = self.streamer
            self.stream_source = None
            self.streamer = None
            self._fade_out_source(old_src, old_streamer, duration=0.5)
        else:
            if self.streamer:
                self.streamer.stop()
                self.streamer = None
            self._destroy_stream_source()
        # Stop local playback
        self._stop_local()
        self.playing = False
        self.paused = False
        self.mode = "idle"
        self._stream_announced = False
        self._current_reverb_slot = None
        if clear_queue:
            self._clear_track_queue()
        if clear_feed:
            self._clear_personal_feed()

    def toggle_pause(self):
        from ..speech import speak
        if not self.playing:
            # If we have a last played song, replay it
            if self.has_last_track():
                speak(f"Replaying: {self.last_track_title or self.last_youtube_title}")
                self._replay_last()
            else:
                speak("Nothing is playing. Press M to search.")
            return

        if self.streamer:
            self.paused = not self.paused
            self.streamer.set_pause(self.paused)
            speak("Paused" if self.paused else "Resumed")
        elif self.mode == "local":
            if self.paused:
                self.paused = False
                self.soundgroup.resume()
                speak("Resumed")
            else:
                self.paused = True
                self.soundgroup.pause()
                speak("Paused")
        else:
            speak("Nothing is playing.")

    def next_track(self):
        if self.feed_tracks:
            self._next_personal_feed()
            return
        if self.mode == "local" and self.playlist:
            self.playlist_index = (self.playlist_index + 1) % len(self.playlist)
            self._play_local_current()
            speak(f"Next: {self.current_title}")

    def toggle_enabled(self):
        self.enabled = not self.enabled
        options.set("music_bot_enabled", self.enabled)
        if self.enabled:
            speak("Music Bot: On")
        else:
            speak("Music Bot: Off")
            self.stop()

    def speak_status(self):
        if not self.enabled:
            speak("Music Bot is off")
            return
        status = "paused" if self.paused else ("playing" if self.playing else "stopped")
        mode = "stream" if self.streamer else self.mode
        speak(f"Music Bot: {status}. Mode: {mode}. Track: {self.current_title or 'none'}. Volume: {self.volume}%")

    def set_volume(self, volume):
        self.volume = max(0, min(100, volume))
        if self.streamer:
            self.streamer.volume = self.volume
        options.set("music_bot_volume", self.volume)
        music_vol = self.game.audio_mngr.volume_categories.get("music", [100])[0] / 100
        gain = (self.volume / 100) * music_vol
        if self.stream_source:
            try:
                self.stream_source.gain = gain
            except Exception:
                pass
        if self.current_local_sound and self.current_local_sound.source:
            try:
                self.current_local_sound.source.gain = gain
                self.current_local_sound.volume = self.volume
            except Exception:
                pass

    def loop(self):
        """Called every frame — check if track ended + sync reverb"""
        if not self.enabled:
            return

        # Smooth volume ducking interpolation
        gp = self._find_gameplay()
        is_speaking_on_mega = False
        if gp and gp.voice_chat and gp.voice_chat.recording and getattr(gp, 'voice_chat_using_megaphone', False):
            is_speaking_on_mega = True

        target_duck = 0.2 if (is_speaking_on_mega and self.broadcast_to_megaphone) else 1.0
        
        if not hasattr(self, 'duck_multiplier'):
            self.duck_multiplier = 1.0
        
        # LERP towards target (10% step per frame ~300ms transition)
        self.duck_multiplier += (target_duck - self.duck_multiplier) * 0.1
        
        # Ensure live instrument / mic relay streamer is active if needed
        self._ensure_live_relay_streamer()

        # Apply updated gain to local stream source
        if self.stream_source and (self.playing or self.paused):
            try:
                music_vol = self.game.audio_mngr.volume_categories.get("music", [100])[0] / 100
                self.stream_source.gain = (self.volume / 100) * music_vol * self.duck_multiplier
            except Exception:
                pass

        # Sync reverb even when paused so it matches when resumed
        if self.stream_source and (self.playing or self.paused):
            self._sync_map_reverb()

        if not self.playing or self.paused:
            return

        # Announce playback only after ffmpeg produced PCM and OpenAL accepted
        # the pre-buffer. This prevents the misleading sequence
        # "Now playing" -> "Track finished" when stream startup actually failed.
        if (self.streamer and self.streamer.ready_event.is_set()
                and not self._stream_announced):
            self._stream_announced = True
            speak(f"Now playing: {self.current_title}")

        if self.mode == "local" and self.current_local_sound:
            try:
                if self.current_local_sound.source.state == cyal.SourceState.STOPPED:
                    self.playlist_index = (self.playlist_index + 1) % len(self.playlist)
                    self._play_local_current()
            except Exception:
                pass
        elif self.streamer and not self.streamer.is_alive():
            # Keep startup failures distinct from a real end-of-track.
            finished_streamer = self.streamer
            self.streamer = None
            self.playing = False
            self.mode = "idle"
            if not self._advance_track_queue():
                if finished_streamer.failure_reason:
                    speak("Could not load track.")
                else:
                    speak("Track finished.")

    def recover_output(self):
        """Resume buffered local music after a transient UI/output interruption."""
        streamer = self.streamer
        if not (self.enabled and self.playing and not self.paused and streamer):
            return False
        try:
            return bool(streamer.resume_output_if_buffered())
        except Exception:
            return False

    def performance_timeline_marker(self):
        """Marker attached to this performer's event-driven instruments.

        Only ordinary Music Broadcast has a versioned timeline. Megaphone and
        private playback keep their existing paths and therefore return None.
        """
        if (not self.broadcast_enabled or self.broadcast_to_megaphone
                or self.paused or not self.playing):
            return None
        streamer = self.streamer
        if streamer is None:
            return None
        return streamer.performance_timeline_marker()

    def _ensure_live_relay_streamer(self):
        """Ensure background live relay thread runs when broadcast is enabled and live input exists without an active MP3 stream."""
        if not (self.broadcast_enabled or self.broadcast_to_megaphone):
            if getattr(self, 'live_relay_streamer', None):
                self.live_relay_streamer.stop()
                self.live_relay_streamer = None
            return

        if self.streamer and self.streamer.is_alive():
            if getattr(self, 'live_relay_streamer', None):
                self.live_relay_streamer.stop()
                self.live_relay_streamer = None
            return

        has_guitar = bool(getattr(self, 'guitar_pcm_queue', None) and len(self.guitar_pcm_queue) > 0)
        has_mic = bool(getattr(self, 'mic_pcm_queue', None) and len(self.mic_pcm_queue) > 0)

        if has_guitar or has_mic:
            if getattr(self, 'live_relay_streamer', None) is None or not self.live_relay_streamer.is_alive():
                self.live_relay_streamer = LiveRelayStreamer(self.game, bot=self)
                self.live_relay_streamer.start()

    def _sync_map_reverb(self, force=False):
        """Apply the map's reverb at the player's position to the music source.
        This gives the music an environmental feel — cave echo, outdoor ambience, etc.
        The dry signal stays stereo-direct (headphone quality),
        while the wet signal from the reverb adds the room's atmosphere.
        """
        if not self.stream_source:
            return True
        try:
            gp = self._find_gameplay()
            map_obj = getattr(gp, 'map', None) or getattr(gp, 'world_map', None)
            if not map_obj:
                return True

            player = gp.player
            reverb = map_obj.get_reverb_at(player.x, player.y, player.z)

            if reverb and reverb.reverb:
                # Apply map's reverb to the music via aux send 0
                if force or self._current_reverb_slot != reverb.reverb:
                    self.game.audio_mngr.efx.send(
                        self.stream_source, 0, reverb.reverb
                    )
                    self._current_reverb_slot = reverb.reverb
            else:
                # No reverb zone — remove effect
                if force or self._current_reverb_slot is not None:
                    self.game.audio_mngr.efx.send(
                        self.stream_source, 0, None
                    )
                    self._current_reverb_slot = None
            return True
        except Exception:
            return False

    def _detach_map_reverb(self):
        """Detach the old map slot without interrupting the active stream."""
        if not self.stream_source or self._current_reverb_slot is None:
            self._current_reverb_slot = None
            return
        try:
            self.game.audio_mngr.efx.send(self.stream_source, 0, None)
        except Exception:
            pass
        self._current_reverb_slot = None

    def destroy(self):
        self.download_mgr.close()
        self.audio_recorder.close()
        self.stop()
        if getattr(self, 'live_relay_streamer', None):
            self.live_relay_streamer.stop()
            self.live_relay_streamer = None
        try:
            self.soundgroup.destroy()
        except Exception:
            pass
