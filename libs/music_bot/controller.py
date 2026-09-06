"""Music Bot controller - MapMusicBot: playback state, queue, playlists,
favorites, downloads, recording and every menu / keybinding hook that glues
the media + streaming layers into the game."""

import contextlib
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
from .. import state
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

    # Crossfade window (seconds): the outgoing outro fades down while the
    # next track's intro fades up. Tuning this only shifts the overlap, never
    # the audio pipeline.
    CROSSFADE_SECONDS = 3.0
    # How long before the end of the current track we begin resolving the
    # NEXT track's fresh stream URL (the slow, variable part).
    CROSSFADE_PREP_SECONDS = 3.5
    # Once resolved, the next streamer is launched this close to the end so
    # its ffmpeg + pre-buffer finishes right around the fade window.
    CROSSFADE_LAUNCH_SECONDS = 1.2

    # Equalizer profiles shown in the Music Bot Equalizer menu. "normal"
    # means flat (no effect slot). Preset tuples mirror the jukebox's
    # OpenAL EQUALIZER parameter pairs.
    EQ_PROFILES = (
        ("normal", "Normal (Flat)"),
        ("bass_boost", "Bass Boost"),
        ("vocal_boost", "Vocal Boost"),
        ("treble_boost", "Treble Boost"),
        ("custom", "Custom (Bass/Mid/Treble)"),
    )
    EQ_PRESETS = {
        "bass_boost": (
            ("low_gain", 7.0),
            ("low_cutoff", 260.0),
            ("mid1_gain", 0.9),
            ("high_gain", 1.0),
            ("high_cutoff", 4000.0),
        ),
        "vocal_boost": (
            ("mid1_gain", 3.2),
            ("mid1_center", 500.0),
            ("mid1_width", 1.0),
            ("mid2_gain", 3.2),
            ("mid2_center", 3000.0),
            ("mid2_width", 1.0),
        ),
        "treble_boost": (
            ("high_gain", 3.5),
            ("high_cutoff", 4000.0),
            ("mid2_gain", 1.4),
            ("mid2_center", 3000.0),
            ("mid2_width", 1.0),
        ),
    }

    @classmethod
    def _normalize_eq_values(cls, values):
        """Clamp arbitrary custom-EQ input into the 0-100 bass/mid/treble map."""
        values = values if isinstance(values, dict) else {}
        normalized = {}
        for band in ("bass", "mid", "treble"):
            try:
                value = int(values.get(band, 50))
            except (TypeError, ValueError):
                value = 50
            normalized[band] = max(0, min(100, value))
        return normalized

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

        # Play-next queue (Queue Mode). Search results selected while Queue
        # Mode is ON (or via "Play Next (Add to Queue)") land here and play
        # automatically when the current track ends, BEFORE the favorites /
        # playlist queue continues. Explicit Stop clears it too.
        self.next_up_queue = []

        # Music Bot settings (persisted in client options)
        self.queue_mode = options.get("music_bot_queue_mode", False)
        self.water_muffle_enabled = options.get("music_bot_water_muffle", True)
        self.reverb_enabled = options.get("music_bot_reverb", True)

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

        # Equalizer (per-listener, mirrors the jukebox's OpenAL EQUALIZER
        # slots). "normal" detaches the send entirely; every other profile
        # owns one cached effect slot mutated in place.
        self.eq_profile = str(options.get("music_bot_eq_profile", "normal")).lower()
        self.eq_values = self._normalize_eq_values(options.get("music_bot_eq_values"))
        self._eq_slots = {}   # preset profile -> effect slot
        self._custom_eq_slot = None  # custom profile's single live slot

        # Crossfade between auto-advanced tracks (queue / playlists). A
        # smooth transition needs the CURRENT track's duration so the next
        # stream can be pre-rolled and faded in while the outro still plays;
        # it is remembered per YouTube page URL whenever a resolve or a
        # search result exposes it.
        self.crossfade_enabled = bool(options.get("music_bot_crossfade", True))
        self.current_duration = None       # seconds, when known (None = no crossfade)
        self._known_durations = {}         # youtube page URL -> duration seconds
        self._crossfade = None             # active roll/fade state (see _update_crossfade)

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

    def _new_bot_source(self):
        """Build a configured OpenAL source for Music Bot playback.
        Uses direct_channels=True for clear stereo, plus the EQ aux send and
        an inherited underwater filter when the player is submerged.
        """
        try:
            audio = self.game.audio_mngr
            src = audio.context.gen_source()
            src.direct_channels = True
            src.spatialize = False
            music_vol = audio.volume_categories.get("music", [100])[0] / 100
            src.gain = (self.volume / 100) * music_vol
            # A track started while the listener is underwater inherits the
            # active global water filter so it is dull from its first frame
            # (unless the player disabled the underwater muffle in settings).
            active = getattr(audio, "filter", None)
            if (active and active[-1] is not None
                    and getattr(self, "water_muffle_enabled", True)):
                src.direct_filter = active[-1]
            self._apply_eq_to_source(src)
            return src
        except Exception as ex:
            print(f"[MusicBot] Error creating source: {ex}")
            return None

    def _delete_source(self, src):
        """Stop, drain and delete one OpenAL source (never the live stream)."""
        if src is None:
            return
        try:
            src.stop()
            drain_limit = 64
            while src.buffers_processed > 0 and drain_limit > 0:
                src.unqueue_buffers()
                drain_limit -= 1
            drain_limit = 64
            while src.buffers_queued > 0 and drain_limit > 0:
                src.unqueue_buffers()
                drain_limit -= 1
            src.delete()
        except Exception:
            pass

    def _create_stream_source(self):
        """Create a fresh OpenAL source for streaming.
        Uses direct_channels=True for clear stereo, plus EFX reverb send
        for environmental atmosphere.
        """
        self._destroy_stream_source()
        src = self._new_bot_source()
        if src is None:
            return
        self.stream_source = src
        # Apply current map reverb immediately
        self._sync_map_reverb()

    def _destroy_stream_source(self):
        if self.stream_source:
            self._delete_source(self.stream_source)
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

        def go_queue():
            gp.pop_last_substate()
            self._open_queue_menu()

        def go_settings():
            gp.pop_last_substate()
            self._open_settings_menu()

        def get_queue_mode_label():
            status = "ON" if self.queue_mode else "OFF"
            return f"Queue Mode: {status}"

        def toggle_queue_mode():
            self.queue_mode = not self.queue_mode
            options.set("music_bot_queue_mode", self.queue_mode)
            status_text = "enabled" if self.queue_mode else "disabled"
            speak(f"Queue mode {status_text}. Search results will be added to the play queue.")
            m.speak_current_item()

        def get_queue_count_label():
            n = len(self.next_up_queue)
            return f"Play Queue ({n} waiting)" if n else "Play Queue (empty)"

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
            (get_queue_mode_label, toggle_queue_mode),
            (get_queue_count_label, go_queue),
            ("Music Bot Settings", go_settings),
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
        self.current_duration = None  # refreshed by the resolve below

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
                    self._note_track_duration(target, stream_info.get("duration"))
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
        # Songs the player queued explicitly (Queue Mode / Add to Queue) play
        # BEFORE the favorites/playlist queue continues.
        if self.next_up_queue:
            self._play_queued_next()
            return True
        if not self.play_queue:
            return False
        self.play_queue_index += 1
        if self.play_queue_index >= len(self.play_queue):
            speak(f"{self.play_queue_label} finished.")
            self._clear_track_queue()
            return False
        self._play_queued_track()
        return True

    def _enqueue_track(self, title, target, source="youtube", http_headers=None,
                       webpage_url="", direct_url=""):
        """Queue a track to play next (Queue Mode / Add to Queue).

        When nothing is playing the earliest possible "next" is right now, so
        the track starts immediately; otherwise it is appended and auto-plays
        when the current track ends.        Returns how many tracks are waiting (0
        when it started right away).
        """
        if not target:
            speak("Cannot queue this track.")
            return 0
        self.next_up_queue.append({
            "title": title,
            "target": target,
            "source": source,
            "http_headers": dict(http_headers or {}),
            "webpage_url": webpage_url,
            "direct_url": direct_url,
        })
        if not self.playing and not self.is_loading_stream:
            self._play_queued_next()
            return 0
        speak(f"Added {title} to the queue. {len(self.next_up_queue)} waiting.")
        return len(self.next_up_queue)

    def _play_queued_next(self, track=None):
        """Start the first queued track (or a specific one) and remove it from
        the queue, preserving any favorites/playlist queue underneath so it
        resumes after the queued song ends."""
        if track is None:
            if not self.next_up_queue:
                return False
            track = self.next_up_queue.pop(0)
        elif self.next_up_queue and self.next_up_queue[0] is track:
            self.next_up_queue.pop(0)
        source = track.get("source", "youtube")
        title = track.get("title", "Unknown")
        target = track.get("target", "")
        if source == "local":
            self.play_single_track(title, target, "local", preserve_queue=True)
        else:
            self._start_youtube_stream_from_search(
                title,
                track.get("webpage_url") or target,
                track.get("direct_url", ""),
                track.get("http_headers") or {},
                preserve_queue=True,
            )
        return True

    def _clear_next_up_queue(self):
        self.next_up_queue = []

    # === Crossfade between auto-advanced tracks ===
    # When the current track's duration is known, the NEXT queued track is
    # pre-rolled (URL resolve + a paused ffmpeg streamer on its own source)
    # during the last seconds of the current song. Right before the outro
    # runs out the candidate is unpaused under gain 0 and both sources ramp
    # against each other over CROSSFADE_SECONDS — a real overlapping
    # fade-out/fade-in instead of a silent gap while the next track loads.

    def _remaining_seconds(self):
        """Seconds left on the current track, when its duration is known."""
        if not self.current_duration:
            return None
        position = self.track_position()
        if position is None:
            return None
        return float(self.current_duration) - position

    def _peek_next_track(self):
        """The track the normal end-of-song path would play next (no consume)."""
        if self.next_up_queue:
            return self.next_up_queue[0]
        if self.play_queue and 0 <= self.play_queue_index + 1 < len(self.play_queue):
            return self.play_queue[self.play_queue_index + 1]
        return None

    def _consume_next_track(self):
        """Consume the next track exactly like the end-of-song advance does."""
        if self.next_up_queue:
            return self.next_up_queue.pop(0)
        if self.play_queue and 0 <= self.play_queue_index + 1 < len(self.play_queue):
            self.play_queue_index += 1
            return self.play_queue[self.play_queue_index]
        return None

    def _cancel_crossfade(self):
        """Abandon any pre-roll / in-progress fade and free its resources.

        Never touches the CURRENT streamer/source (the caller owns those); it
        only tears down the extra candidate pipeline and, when a fade was
        already underway, the retired outgoing stream that was being faded.
        """
        state = getattr(self, "_crossfade", None)
        if not state:
            return False
        self._crossfade = None
        pairs = (
            (state.get("candidate"), state.get("candidate_source")),
            (state.get("old_streamer"), state.get("old_source")),
        )
        for streamer, source in pairs:
            if streamer is not None and streamer is not self.streamer:
                try:
                    streamer.stop()
                except Exception:
                    pass
            if source is not None and source is not self.stream_source:
                self._delete_source(source)
        return True

    def _start_crossfade_roll(self):
        """Begin pre-rolling the next queued track (called each frame)."""
        if not self.crossfade_enabled or self.is_loading_stream:
            return
        remaining = self._remaining_seconds()
        if remaining is None or remaining <= 0.0:
            return
        if remaining > self.CROSSFADE_SECONDS + self.CROSSFADE_PREP_SECONDS:
            return
        streamer = self.streamer
        if streamer is None or not streamer.is_alive():
            return
        track = self._peek_next_track()
        if not track or not track.get("target"):
            return
        state = {
            "phase": "resolving",
            "track": dict(track),
            "old_streamer": streamer,
            "old_source": self.stream_source,
            "stream_info": None,
            "resolve_done": False,
            "candidate": None,
            "candidate_source": None,
            "duration": None,
            "fade_started_at": None,
            "old_gain0": None,
        }
        self._crossfade = state
        source = track.get("source", "youtube")
        target = str(track.get("target", ""))
        if source != "local" and not target.startswith(("http://", "https://")):
            self._crossfade = None
            return
        if source != "local":
            webpage = str(track.get("webpage_url") or target)

            def do_resolve():
                info = YouTubeSearcher.get_stream_info(
                    webpage,
                    cancelled=lambda: self._crossfade is not state,
                )
                if self._crossfade is not state:
                    return
                state["stream_info"] = info if info else None
                if info:
                    self._note_track_duration(webpage, info.get("duration"))
                    state["duration"] = self._known_durations.get(
                        webpage) or info.get("duration")
                state["resolve_done"] = True

            threading.Thread(target=do_resolve, daemon=True).start()
        else:
            # Local files need no URL resolution; launch straight away.
            state["resolve_done"] = True
            state["stream_info"] = {"url": target, "http_headers": {}}

    def _create_crossfade_candidate(self, state):
        """Launch the pre-rolled next streamer, silent and network-muted."""
        src = self._new_bot_source()
        if src is None:
            self._cancel_crossfade()
            return
        info = state.get("stream_info") or {}
        track = state["track"]
        source = track.get("source", "youtube")
        url = info.get("url") or track.get("target", "")
        canonical = None
        headers = {}
        if source != "local":
            canonical = str(track.get("webpage_url") or track.get("target", ""))
            headers = dict(info.get("http_headers") or {})
        try:
            cand = AudioStreamer(
                self.game, url, src, self.volume, bot=self,
                http_headers=headers,
                canonical_url=canonical,
                start_paused=True,
            )
        except Exception:
            self._delete_source(src)
            self._cancel_crossfade()
            return
        cand.network_muted = True  # silent until the fade actually hands over
        cand.start()
        state["candidate"] = cand
        state["candidate_source"] = src
        # Keep the candidate source silent while it pre-rolls.
        with contextlib.suppress(Exception):
            src.gain = 0.0

    def _commit_crossfade(self, state):
        """Make the pre-rolled candidate the current track and fade it in
        while the outgoing stream fades out. Returns True on success."""
        cand = state.get("candidate")
        cand_src = state.get("candidate_source")
        old_streamer = state.get("old_streamer")
        old_source = state.get("old_source")
        if cand is None or cand_src is None:
            return False
        if self.streamer is not old_streamer or self.stream_source is not old_source:
            return False
        if not cand.prebuffer_event.is_set():
            return False
        track = self._consume_next_track()
        if track is None:
            self._cancel_crossfade()
            return False
        self._begin_playback_generation()
        title = track.get("title", "Unknown")
        target = str(track.get("target", ""))
        source = track.get("source", "youtube")
        webpage = target if source != "local" else ""
        self.current_title = title
        self.current_target = webpage or target
        self.current_source = source
        self.last_track_title = title
        self.last_track_target = webpage or target
        self.last_track_source = source
        if source != "local":
            self.last_youtube_url = webpage
            self.last_youtube_title = title
        self.current_duration = state.get("duration")
        self.mode = "youtube"
        self.playing = True
        self.paused = False
        self._stream_announced = False
        self._current_reverb_slot = None
        self.streamer = cand
        self.stream_source = cand_src
        self._apply_eq_to_source(cand_src)
        state["old_gain0"] = 0.0
        if old_source is not None:
            with contextlib.suppress(Exception):
                state["old_gain0"] = float(getattr(old_source, "gain", 0.0) or 0.0)
        # Hand the room the same overlap the performer hears: the outgoing
        # network leg blends into the incoming one (old fades out, new fades
        # in) instead of hard-switching. The candidate stays network-muted
        # while the blend runs and only takes over the leg when the fade ends.
        if old_streamer is not None:
            # Drop any frames the candidate queued while pre-rolling so the
            # blend starts at the live position, not seconds behind.
            try:
                while True:
                    cand.network_queue.get_nowait()
            except Exception:
                pass
            if hasattr(old_streamer, "begin_network_crossfade"):
                old_streamer.begin_network_crossfade(
                    cand, self.CROSSFADE_SECONDS)
            else:
                old_streamer.network_muted = True
        with contextlib.suppress(Exception):
            cand_src.gain = 0.0
        cand.set_pause(False)
        state["phase"] = "fading"
        state["fade_started_at"] = time.monotonic()
        return True

    def _update_fade_gains(self, base_gain):
        """Per-frame gain ramp during the overlap (main thread only)."""
        state = self._crossfade
        if state is None or state.get("phase") != "fading":
            return
        elapsed = time.monotonic() - (state.get("fade_started_at") or time.monotonic())
        progress = min(1.0, max(0.0, elapsed / self.CROSSFADE_SECONDS))
        src = self.stream_source
        old_source = state.get("old_source")
        if src is not None:
            with contextlib.suppress(Exception):
                src.gain = base_gain * progress
        if old_source is not None:
            with contextlib.suppress(Exception):
                old_source.gain = (state.get("old_gain0") or 0.0) * (1.0 - progress)
        if progress >= 1.0:
            if src is not None:
                with contextlib.suppress(Exception):
                    src.gain = base_gain
            old_streamer = state.get("old_streamer")
            if old_streamer is not None and old_streamer is not self.streamer:
                with contextlib.suppress(Exception):
                    old_streamer.stop()
            # The blend is over: the incoming stream's own network leg takes
            # over the broadcast now (it stayed muted through the overlap).
            if self.streamer is not None:
                with contextlib.suppress(Exception):
                    self.streamer.network_muted = False
            self._delete_source(old_source)
            self._crossfade = None

    def _update_crossfade(self):
        """Drive the crossfade state machine (called every frame while playing)."""
        streamer = self.streamer
        if streamer is None or not streamer.is_alive():
            # The song ended while we were pre-rolling: abandon the candidate;
            # the normal end-of-song advance plays the next track instead.
            self._cancel_crossfade()
            return
        if not self.crossfade_enabled or self.is_loading_stream:
            return
        state = self._crossfade
        if state is None:
            self._start_crossfade_roll()
            state = self._crossfade
            if state is None:
                return
        if state.get("phase") == "fading":
            # The overlap is already underway; the gain ramp in
            # _update_fade_gains drives the rest of the transition.
            return
        remaining = self._remaining_seconds()
        if remaining is None or remaining <= 0.0:
            return
        if state.get("phase") == "resolving":
            if not state.get("resolve_done"):
                return
            if not state.get("stream_info"):
                # Resolution failed: let the normal end-of-song retry it.
                self._cancel_crossfade()
                return
            if (state.get("candidate") is None
                    and remaining <= self.CROSSFADE_SECONDS + self.CROSSFADE_LAUNCH_SECONDS):
                self._create_crossfade_candidate(state)
                if state.get("candidate") is not None:
                    # Pre-roll launched: wait for its pre-buffer, then hand over.
                    state["phase"] = "waiting"
            return
        cand = state.get("candidate")
        if cand is None:
            return
        if cand.failure_reason is not None and not cand.prebuffer_event.is_set():
            # The candidate stream failed to start; fall back to the normal
            # end-of-song path so the next track still plays.
            self._cancel_crossfade()
            return
        if cand.prebuffer_event.is_set() and remaining <= self.CROSSFADE_SECONDS:
            if not self._commit_crossfade(state):
                self._cancel_crossfade()

    def _set_crossfade_enabled(self, enabled):
        """Persist the crossfade toggle; abort any roll started under it."""
        self.crossfade_enabled = bool(enabled)
        options.set("music_bot_crossfade", self.crossfade_enabled)
        if not self.crossfade_enabled:
            self._cancel_crossfade()

    def _open_queue_menu(self):
        """View / clear the play-next queue (Queue Mode).

        Every queued song is its own menu item so the player scrolls through
        them one at a time with the arrow keys / wheel — the menu speaks each
        item as it is highlighted, instead of reading the whole list at once.
        Enter on a song re-reads its title.
        """
        from .. import menu as menu_mod, menus
        gp = self._find_gameplay()
        if not gp:
            return

        def go_back():
            gp.pop_last_substate()
            self._show_mode_menu()

        def clear_queue():
            self._clear_next_up_queue()
            speak("Queue cleared.")
            # Rebuild the menu so the removed song items disappear.
            if gp.substates and gp.substates[-1] is m:
                gp.pop_last_substate()
            self._open_queue_menu()

        def queue_count():
            n = len(self.next_up_queue)
            return f"Play Queue ({n} waiting)" if n else "Play Queue (empty)"

        m = menu_mod.Menu(self.game, "Play Queue", parrent=gp)
        items = [(queue_count, lambda: None)]
        for i, t in enumerate(self.next_up_queue, 1):
            title = t.get("title", "Unknown")

            def read_song(idx=i, song_title=title):
                speak(f"{idx}. {song_title}")

            items.append((f"{i}. {title}", read_song))
        items.append(("Clear Queue", clear_queue))
        items.append(("Back", go_back))
        m.add_items(items)
        menus.set_default_sounds(m)
        gp.add_substate(m)

    def _open_settings_menu(self):
        """Music Bot settings: underwater muffle and room reverb.

        Broadcast to Others is deliberately NOT here — it has its own keyboard
        shortcut, and duplicating it in the menu caused confusion about which
        one is the source of truth.
        Every toggle here is persisted in client options, so a player sets it
        once and the game remembers it across restarts.
        """
        from .. import menu as menu_mod, menus
        gp = self._find_gameplay()
        if not gp:
            return

        def go_back():
            gp.pop_last_substate()
            self._show_mode_menu()

        def get_water_label():
            status = "ON" if self.water_muffle_enabled else "OFF"
            return f"Underwater Muffle: {status}"

        def toggle_water():
            self.water_muffle_enabled = not self.water_muffle_enabled
            options.set("music_bot_water_muffle", self.water_muffle_enabled)
            self._reapply_bot_water_filter()
            speak("Underwater muffle enabled." if self.water_muffle_enabled
                  else "Underwater muffle disabled.")
            m.speak_current_item()

        def get_reverb_label():
            status = "ON" if self.reverb_enabled else "OFF"
            return f"Room Reverb (Realism): {status}"

        def toggle_reverb():
            self.reverb_enabled = not self.reverb_enabled
            options.set("music_bot_reverb", self.reverb_enabled)
            if not self.reverb_enabled:
                self._detach_map_reverb()
            speak("Room reverb enabled." if self.reverb_enabled
                  else "Room reverb disabled.")
            m.speak_current_item()

        def get_crossfade_label():
            status = "ON" if getattr(self, "crossfade_enabled", False) else "OFF"
            return f"Crossfade Between Songs: {status}"

        def toggle_crossfade():
            enabled = not getattr(self, "crossfade_enabled", False)
            self._set_crossfade_enabled(enabled)
            speak("Crossfade between songs enabled. Tracks overlap smoothly."
                  if enabled else "Crossfade between songs disabled.")
            m.speak_current_item()

        def get_eq_label():
            return f"Equalizer: {self._eq_profile_label()}"

        def go_eq():
            gp.pop_last_substate()
            self._open_eq_menu()

        m = menu_mod.Menu(self.game, "Music Bot Settings", parrent=gp)
        m.add_items([
            (get_water_label, toggle_water),
            (get_reverb_label, toggle_reverb),
            (get_crossfade_label, toggle_crossfade),
            (get_eq_label, go_eq),
            ("Back", go_back),
        ])
        menus.set_default_sounds(m)
        gp.add_substate(m)

    def _open_eq_menu(self):
        """Pick the Music Bot equalizer profile (or open the Custom sliders).
        Profiles apply immediately so the change is heard while browsing.
        """
        from .. import menu as menu_mod, menus
        gp = self._find_gameplay()
        if not gp:
            return

        def go_back():
            gp.pop_last_substate()
            self._open_settings_menu()

        def make_label(profile, label):
            def label_fn():
                return f"{label} (active)" if self.eq_profile == profile else label
            return label_fn

        def make_pick(profile, label):
            def pick():
                if profile == "custom":
                    gp.pop_last_substate()
                    self._open_custom_eq_sliders()
                    return
                self.set_eq_profile(profile)
                speak(f"{label} equalizer applied.")
                m.speak_current_item()
            return pick

        m = menu_mod.Menu(self.game, "Music Bot Equalizer", parrent=gp)
        items = []
        for profile, label in self.EQ_PROFILES:
            items.append((make_label(profile, label), make_pick(profile, label)))
        items.append(("Back", go_back))
        m.add_items(items)
        menus.set_default_sounds(m)
        gp.add_substate(m)

    def _open_custom_eq_sliders(self):
        """Open the Bass/Mid/Treble sliders for the Custom profile."""
        gp = self._find_gameplay()
        if not gp:
            return
        self.set_eq_profile("custom", self.eq_values)
        gp.add_substate(_MusicBotEqSlider(self.game, self))

    def _reapply_bot_water_filter(self):
        """Apply the underwater-muffle setting to the bot's live stream source.

        Matters when the toggle flips while the listener is underwater at a
        constant depth — no camera automation tick runs then to re-apply it.
        """
        src = self.stream_source
        if src is None:
            return
        audio = self.game.audio_mngr
        with contextlib.suppress(Exception):
            if self.water_muffle_enabled:
                active = getattr(audio, "filter", None)
                if active and active[-1] is not None:
                    src.direct_filter = active[-1]
            else:
                del src.direct_filter

    # === Equalizer (personal Music Bot) ===
    # Same OpenAL EQUALIZER approach as the jukebox: preset slots are cached
    # per profile, the custom profile owns one slot that is mutated in place
    # while its sliders move (no slot leaks, audible in real time), and
    # "normal" detaches the aux send so the stream stays perfectly flat.

    def _eq_profile_label(self):
        for profile, label in self.EQ_PROFILES:
            if profile == self.eq_profile:
                return label
        return "Normal (Flat)"

    def _get_bot_eq_slot(self, profile, eq_values=None):
        """Return the cached effect slot for a profile (None = flat)."""
        profile = str(profile or "normal").lower()
        if profile not in ("normal",) and profile not in self.EQ_PRESETS \
                and profile != "custom":
            return None
        if profile == "normal":
            return None
        audio = getattr(self.game, "audio_mngr", None)
        if audio is None or getattr(audio, "efx", None) is None \
                or not hasattr(audio, "gen_effect"):
            return None
        if profile == "custom":
            values = self._normalize_eq_values(eq_values)
            params = self._custom_eq_parameters(values)
            slot = self._custom_eq_slot
            if slot is None:
                try:
                    slot = audio.gen_effect("EQUALIZER", *params)
                except Exception:
                    slot = None
                self._custom_eq_slot = slot
            elif slot is not None:
                effect = getattr(slot, "effect", None)
                if effect is not None:
                    for param in params:
                        try:
                            effect.set(*param)
                        except Exception:
                            pass
                    try:
                        # EFX implementations may snapshot parameters when an
                        # effect is attached; reattach the same object so the
                        # in-place edits become audible without a new slot.
                        slot.effect = effect
                    except Exception:
                        pass
            return slot
        if profile not in self._eq_slots:
            try:
                self._eq_slots[profile] = audio.gen_effect(
                    "EQUALIZER", *self.EQ_PRESETS[profile])
            except Exception:
                self._eq_slots[profile] = None
        return self._eq_slots.get(profile)

    @staticmethod
    def _custom_eq_parameters(values):
        """Map accessible 0-100 Bass/Mid/Treble sliders to OpenAL EQUALIZER
        gains. Kept identical to the jukebox's curve (one shared implementation
        so both EQs always sound the same)."""
        from ..jukebox import JukeboxPlayer
        return JukeboxPlayer._custom_eq_parameters(values)

    def set_eq_profile(self, profile, eq_values=None):
        """Switch the Music Bot equalizer and re-apply the EFX send live."""
        profile = str(profile or "normal").lower()
        allowed = {p for p, _ in self.EQ_PROFILES}
        if profile not in allowed:
            profile = "normal"
        was_custom = str(getattr(self, "eq_profile", "normal")) == "custom"
        self.eq_profile = profile
        self.eq_values = self._normalize_eq_values(eq_values)
        options.set("music_bot_eq_profile", profile)
        options.set("music_bot_eq_values", dict(self.eq_values))
        slot = self._get_bot_eq_slot(profile, self.eq_values)
        self._apply_bot_eq(slot)
        if was_custom and profile != "custom":
            old_slot = self._custom_eq_slot
            self._custom_eq_slot = None
            if old_slot is not None:
                audio = getattr(self.game, "audio_mngr", None)
                if audio is not None and hasattr(audio, "release_effect_slot"):
                    with contextlib.suppress(Exception):
                        audio.release_effect_slot(old_slot)

    def _apply_eq_to_source(self, src):
        """Attach (or detach, when flat) the EQ aux send on one source."""
        if src is None:
            return
        audio = getattr(self.game, "audio_mngr", None)
        if audio is None or getattr(audio, "efx", None) is None:
            return
        slot = self._get_bot_eq_slot(self.eq_profile, self.eq_values)
        with contextlib.suppress(Exception):
            audio.efx.send(src, 1, slot)

    def _apply_bot_eq(self, slot=None):
        """Re-apply the current EQ to every live bot source."""
        if slot is None:
            slot = self._get_bot_eq_slot(self.eq_profile, self.eq_values)
        audio = getattr(self.game, "audio_mngr", None)
        if audio is None or getattr(audio, "efx", None) is None:
            return
        sources = [getattr(self, "stream_source", None)]
        state = getattr(self, "_crossfade", None)
        if state:
            sources.append(state.get("candidate_source"))
        local_sound = getattr(self, "current_local_sound", None)
        if local_sound is not None:
            sources.append(getattr(local_sound, "source", None))
        for src in sources:
            if src is None:
                continue
            with contextlib.suppress(Exception):
                audio.efx.send(src, 1, slot)

    def _note_track_duration(self, target, duration):
        """Remember a resolved/search duration for a canonical target URL."""
        if not target:
            return
        try:
            value = float(duration)
        except (TypeError, ValueError):
            return
        if value > 0.0 and value <= 86400.0:
            self._known_durations[str(target)] = value

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
            # Remember search durations so queued / replayed songs can later
            # crossfade even before their own URL is resolved.
            for r in results or ():
                target = r.get("webpage_url") or r.get("url") or ""
                self._note_track_duration(target, r.get("duration"))
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
        """User selected a search result -> Queue (Queue Mode) or show options
        (Play Now / Play Next / Download / Save to Favorites / Playlist)."""
        gp.pop_last_substate()

        if index >= len(self.search_results):
            return

        result = self.search_results[index]
        title = result.get('title', 'Unknown')
        webpage_url = result.get('webpage_url', '')
        direct_url = result.get('url', '')
        http_headers = result.get('http_headers') or {}
        target = webpage_url or direct_url

        # Queue Mode: pressing Enter on a result queues it to play next
        # without opening the options menu. Toggle Queue Mode off to reach
        # Download / Favorites / Playlist again.
        if self.queue_mode:
            self._enqueue_track(
                title, target, "youtube", http_headers=http_headers,
                webpage_url=webpage_url, direct_url=direct_url,
            )
            return

        from .. import menu as menu_mod, menus
        m = menu_mod.Menu(self.game, title, parrent=gp)
        items = []

        def play_now():
            gp.pop_last_substate()
            self._start_youtube_stream_from_search(
                title, webpage_url, direct_url, http_headers
            )

        def add_to_queue():
            gp.pop_last_substate()
            self._enqueue_track(
                title, target, "youtube", http_headers=http_headers,
                webpage_url=webpage_url, direct_url=direct_url,
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
        items.append(("Play Next (Add to Queue)", add_to_queue))
        items.append(("Download Song", download_song))
        items.append(("Save to Favorites", save_fav))
        items.append(("Add to Playlist...", save_playlist))
        items.append(("Cancel", lambda: gp.pop_last_substate()))

        m.add_items(items)
        menus.set_default_sounds(m)
        gp.add_substate(m)

    def _start_youtube_stream_from_search(self, title, webpage_url, direct_url,
                                          http_headers=None, preserve_queue=False):
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
        self.current_duration = None  # refreshed by the resolve below
        self.is_loading_stream = True

        # Stop any current playback (preserve the favorites/playlist queue
        # when this track came from the play-next queue).
        self.stop(invalidate_pending=False, fade=True,
                  clear_queue=not preserve_queue)
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
            self._note_track_duration(webpage_url or direct_url,
                                      stream_info.get("duration"))
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

        self.current_duration = (
            self._known_durations.get(canonical_url)
            if canonical_url else None
        )
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
            self._note_track_duration(target, stream_info.get("duration"))
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
        self._cancel_crossfade()
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
            self._clear_next_up_queue()
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

        # Apply updated gain to local stream source. During a crossfade the
        # two overlapping sources are ramped against each other instead.
        if self.stream_source and (self.playing or self.paused):
            try:
                music_vol = self.game.audio_mngr.volume_categories.get("music", [100])[0] / 100
                base_gain = (self.volume / 100) * music_vol * self.duck_multiplier
                fade = getattr(self, "_crossfade", None)
                if fade is not None and fade.get("phase") == "fading":
                    self._update_fade_gains(base_gain)
                else:
                    self.stream_source.gain = base_gain
            except Exception:
                pass

        # Sync reverb even when paused so it matches when resumed
        if self.stream_source and (self.playing or self.paused):
            self._sync_map_reverb()

        if not self.playing or self.paused:
            return

        # Crossfade state machine: pre-roll and overlap the next queued track
        # when the current one is about to end.
        self._update_crossfade()

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
            # Any pre-rolled crossfade candidate is abandoned here: the normal
            # advance below consumes the next queue entry itself.
            self._cancel_crossfade()
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
        Skipped entirely when the player disabled Room Reverb in settings.
        """
        if not self.stream_source:
            return True
        if not getattr(self, "reverb_enabled", True):
            # Setting off — detach any slot that was applied before it flipped.
            if force or self._current_reverb_slot is not None:
                with contextlib.suppress(Exception):
                    self.game.audio_mngr.efx.send(self.stream_source, 0, None)
                self._current_reverb_slot = None
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


class _MusicBotEqSlider(state.State):
    """Accessible Bass/Mid/Treble sliders for the personal Music Bot EQ.

    The Custom profile owns one effect slot that is mutated in place on every
    tick, so adjustments are audible immediately and no EFX slots leak.
    """

    BANDS = (("bass", "Bass"), ("mid", "Mid"), ("treble", "Treble"))

    def __init__(self, game, bot):
        super().__init__(game, parrent=bot)
        self.bot = bot
        self.values = dict(MapMusicBot._normalize_eq_values(
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
