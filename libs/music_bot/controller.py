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
from ..audio.cinema import (ROOM_MAX_DISTANCE, ROOM_REFERENCE_DISTANCE,
                            acquire_bank as cinema_acquire_bank,
                            live_instruments_enabled, plan_reach,
                            preview_room,
                            release_renderer as cinema_release,
                            room_diagnosis as cinema_diagnosis, rooms_enabled,
                            set_enabled as cinema_set_enabled,
                            set_live_instruments, set_rooms_enabled,
                            set_speech_enabled as set_cinema_speech,
                            speech_enabled as cinema_speech_enabled)
from ..audio.cinema import pan as cinema_pan
from ..audio.cinema import peer as cinema_peer
from .cinema_handler import CinemaHandler
from .crossfade_handler import CrossfadeHandler
from ..game_audio_recorder import GameAudioRecorderManager
from .music_downloader import MusicDownloadManager, is_supported_music_url
from ..speech import speak
from ..string_utils import friendly_key_name
from . import song_requests
from . import subtitles
from .media import (FFMPEG_PATH, DEFAULT_MAP_MUSIC, FALLBACK_PLAYLIST,
                    clamp_seek_position, format_track_position, YouTubeSearcher)
from .streaming import AudioStreamer, LiveRelayStreamer


class MapMusicBot:
    """Music Bot — searches YouTube and streams audio in real-time (local files as a
        fallback). Controls come from the player's key bindings in gameplay.py. Rules, paths
        and every measured number: .agents/skills/music_bot_integration/.
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
        # Song requests (/p) in a Party Sync session: OFF until the host turns
        # them on, and the switch only exists while this client is the host
        # (see song_requests_switch_item). `song_requests` is the host's own
        # bookkeeping of who asked for what, kept out of the queue itself.
        self.song_requests_open = options.get("music_bot_song_requests", False)
        self.song_requests = song_requests.SongRequestBoard()
        # Searches waiting for their asker to pick a version (the host's own
        # /p keyed under LOCAL_REQUEST_ID, a listener's under the server's
        # request id). See song_requests.PendingPicks.
        self.song_picks = song_requests.PendingPicks()
        # A host's own /p is keyed locally and needs its own id per search (see
        # _offer_song_choices): reusing one key would let a menu left open from
        # an earlier request resolve its index against a newer list.
        self._local_pick_seq = 0
        self.water_muffle_enabled = options.get("music_bot_water_muffle", True)
        self.reverb_enabled = options.get("music_bot_reverb", True)

        # A shuffled Favorites feed. It preserves the user's current broadcast
        # routing and never changes saved playlists.
        self.feed_tracks = []
        self.feed_index = -1

        # Settings
        # Cinema speaker output (opt-in, for testing and for rooms a builder built around a
        # cabinet): with a cabinet selected this bot's audio plays through that jukebox's
        # speakers instead of the listener's ears, using the exact same room it feeds.
        self.cinema_target = str(options.get("music_bot_cinema_target", "") or "") or None
        # Live instruments through the nearest cabinet's room: the listener's own setting,
        # on for everyone (see libs/audio/cinema/live.py) - a live note is one sample per
        # speaker on this client and takes nothing from anybody. Kept here so the menu has
        # something to flip; the instruments read the option itself.
        self.instruments_cinema = live_instruments_enabled()
        self.cinema_bank = None
        self.cinema_bank_key = None
        self.cinema_cabinet = None
        # What the map has last been told about this bot's routing, and when:
        # the announcement is repeated so a listener who joined mid-song (or
        # missed a packet) hears the song out of the room like everyone else.
        self._cinema_announced = None
        self._cinema_announced_at = 0.0
        # The room's behaviour lives in cinema_handler.py; the state it works on
        # is the block above, and stays here for the callers that read it.
        self._cinema_handler = CinemaHandler(self)

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

        # Crossfade between auto-advanced tracks (queue / playlists): the current track's
        # duration is remembered per YouTube page URL whenever a resolve or a search result
        # exposes it, so the next stream can be pre-rolled while the outro still plays.
        self.crossfade_enabled = bool(options.get("music_bot_crossfade", True))
        self.current_duration = None       # seconds, when known (None = no crossfade)
        self._known_durations = {}         # youtube page URL -> duration seconds
        self._crossfade = None             # active roll/fade state (see _update_crossfade)
        # The machine that drives it lives in crossfade_handler.py;
        # the state above stays here for the callers that read it.
        self._crossfade_handler = CrossfadeHandler(self)

        # Spoken subtitles (the video's own YouTube captions, read out loud by
        # this machine's screen reader -- libs/music_bot/subtitles.py). The
        # switch, the language order and the sync offset are the listener's and
        # persist across restarts; the cues themselves are per track.
        self.subtitles_enabled = bool(options.get("music_bot_subtitles", False))
        self.subtitle_language = subtitles.normalize_preference(
            options.get("music_bot_subtitle_language"))
        self.subtitle_offset = subtitles.normalize_offset(
            options.get("music_bot_subtitle_offset"))
        self.subtitle_reader = subtitles.SubtitleReader()
        self._caption_fetcher = subtitles.CaptionFetcher()
        # Which stream a fetch belongs to: a caption track that arrives after
        # the player skipped on is dropped, and the fetch itself is cancelled
        # between its two network steps.
        self._subtitle_generation = 0
        self._subtitle_page = ""

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

                    With a cinema cabinet selected this makes NO source at all: the room owns one source
                    per speaker and the stream is handed to it instead.
                    """
        self._destroy_stream_source()
        if self.cinema_active_target():
            if self._ensure_cinema_bank() is not None:
                # stream_source stays None on purpose: every gain, EQ and
                # reverb call site below already skips a missing source, and
                # the bank is what carries them for the room.
                return
            speak("Cinema speakers unavailable; playing at your ears.")
        src = self._new_bot_source()
        if src is None:
            return
        self.stream_source = src
        # Apply current map reverb immediately
        self._sync_map_reverb()

    def _output_source(self):
        """The source the running stream is written into, or None.

                    With cinema routing it does not exist *by design* - the room's bank carries the audio
                    - so a caller asking "is there anywhere for this stream to play" has to accept the
                    room as an answer (skipping that made a track through a room report "Audio error.").
                    """
        if self.stream_source is not None:
            return self.stream_source
        bank = getattr(self, "cinema_bank", None)
        return getattr(bank, "primary_source", None) if bank is not None else None

    def _destroy_stream_source(self):
        if self.stream_source:
            self._delete_source(self.stream_source)
            self.stream_source = None

    # === Cinema speakers (the room a cabinet's speakers make) ===
    # The room's behaviour lives in music_bot/cinema_handler.py; the state it
    # works on stays here, because the tests, the streamer's upload gate and
    # this class's own call sites read it off the bot (the handler's docstring
    # says which and why). Every name below is a one-line shim, so a menu, a
    # test and the frame loop keep calling the bot exactly as they always did.
    # Rules: .agents/skills/cinema_speaker_system/.

    # How often a playing room re-resolves the map it is in, so a speaker a
    # builder places or deletes mid-song is heard without touching the menu.
    CINEMA_RESHAPE_INTERVAL = 1.0

    # How often the routing is re-announced while the song plays. A listener
    # who joined the map, returned from another map, or lost the packet picks
    # the room up within this long; the cost is one small reliable packet
    # every few seconds, and nothing at all when the bot is not routed.
    CINEMA_ANNOUNCE_INTERVAL = 3.0

    # How often a Party Sync host re-sends its play queue to the session while
    # there is anything in it. A listener cannot see this machine's queue, so
    # the host says it: sent the moment it changes, and repeated this often so
    # a lost packet or somebody who joined late catches up.
    PARTY_QUEUE_INTERVAL = 5.0

    def _cinema(self):
        """The cinema-room handler, built on first use.

                    A bot built without __init__ (a test fixture, a restored session) has none
                    yet, so every shim below asks for it through here rather than holding a
                    reference that may never have been made.
                    """
        handler = getattr(self, "_cinema_handler", None)
        if handler is None:
            handler = CinemaHandler(self)
            self._cinema_handler = handler
        return handler

    def cinema_cabinets(self):
        return self._cinema().cinema_cabinets()

    def cinema_target_label(self):
        return self._cinema().cinema_target_label()

    def cinema_routing_allowed(self):
        return self._cinema().cinema_routing_allowed()

    def cinema_active_target(self):
        return self._cinema().cinema_active_target()

    def cinema_listening_allowed(self):
        return self._cinema().cinema_listening_allowed()

    def instruments_cinema_active(self):
        return self._cinema().instruments_cinema_active()

    def instruments_cinema_label(self):
        return self._cinema().instruments_cinema_label()

    def toggle_instruments_cinema(self):
        return self._cinema().toggle_instruments_cinema()

    def speech_cinema_label(self):
        return self._cinema().speech_cinema_label()

    def toggle_speech_cinema(self):
        return self._cinema().toggle_speech_cinema()

    def cinema_rooms_label(self):
        return self._cinema().cinema_rooms_label()

    def toggle_cinema_rooms(self):
        return self._cinema().toggle_cinema_rooms()

    def own_sound_label(self):
        return self._cinema().own_sound_label()

    def announce_own_sound(self):
        return self._cinema().announce_own_sound()

    def _cinema_problem(self, anchor, cabinet_id=None):
        return self._cinema()._cinema_problem(anchor, cabinet_id)

    def _cinema_map_help(self):
        return self._cinema()._cinema_map_help()

    @property
    def cinema_force_upload(self):
        return self._cinema().cinema_force_upload()

    def announce_cinema_target(self, force=False):
        return self._cinema().announce_cinema_target(force)

    def set_cinema_target(self, cabinet_id):
        return self._cinema().set_cinema_target(cabinet_id)

    def _open_cinema_menu(self):
        return self._cinema()._open_cinema_menu()

    def _ensure_cinema_bank(self):
        return self._cinema()._ensure_cinema_bank()

    def _release_cinema_bank(self):
        return self._cinema()._release_cinema_bank()

    def _update_cinema_output(self):
        return self._cinema()._update_cinema_output()

    def _cinema_occlusion(self, position, listener, max_distance):
        return self._cinema()._cinema_occlusion(position, listener, max_distance)

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
            return song_requests.queue_label(len(self.next_up_queue))

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

        # Cinema speaker routing is Developer/Contributor only (see
        # cinema_routing_allowed): everyone else gets a plain Music Bot menu
        # rather than a switch the Server would refuse to honour.
        if self.cinema_routing_allowed():
            def go_cinema():
                gp.pop_last_substate()
                self._open_cinema_menu()

            items.append((self.cinema_target_label, go_cinema))

        # Three listener-side switches, one line below the song's own routing: a room is
        # how *this* listener hears a cabinet, so none of them takes anything from anybody
        # and all three are open to anyone who can open this menu (the song routing above
        # stays Developer/Contributor). Songs, instruments and a voice.
        if self.cinema_listening_allowed():
            items.append((self.cinema_rooms_label, self.toggle_cinema_rooms))
            items.append((self.instruments_cinema_label,
                          self.toggle_instruments_cinema))
            items.append((self.speech_cinema_label, self.toggle_speech_cinema))
            # The three lines above choose how *you* hear a room; this one is the other
            # direction and is not a switch at all - it answers "where do other players hear me
            # from", which is decided by staff. It reads rather than toggles: pressing it says
            # the same sentence the moment of a pan says, so it cannot look like a setting.
            items.append((self.own_sound_label, self.announce_own_sound))

        items.extend([
            (get_queue_mode_label, toggle_queue_mode),
            (get_queue_count_label, go_queue),
            (self.subtitle_label, self._open_subtitle_menu),
            ("Music Bot Settings", go_settings),
            ("Help", go_help),
            ("Cancel", lambda: gp.pop_last_substate())
        ])
        m.add_items(items)
        menus.set_default_sounds(m)
        gp.add_substate(m)

    # === Party Sync (listen together with invited friends) ===
    # The server (libs/party_sync.ts) runs the session and gates the relay to guests; the client
    # keeps uploading while a session is active and drives the controls, and guests receive the
    # stream through the normal music-source receive leg. Rules: .agents/skills/party-sync-system/.

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

    # ── Song requests (/p) ──────────────────────────────────────────────
    # The queue this fills is THIS machine's, so the host owns the switch and
    # the answer; a guest only asks (see libs/music_bot/song_requests.py for
    # the rules, and Gameplay.party_sync_chat2 for the /p input).

    def song_requests_switch_item(self, on_toggle=None):
        """The host's /p switch as a menu item, or None for a listener.

                    It lives in the Party Sync menus, NOT in the Music Bot menu: parked among the bot's
                    own settings it read like a bot setting. One builder feeds both session menus.
                    """
        _gp, ps = self._party_sync_pair()
        if ps is None or getattr(ps, "role", None) != "host":
            return None

        def label():
            status = "ON" if self.song_requests_open else "OFF"
            return f"Song Requests: {status} (listeners type /m)"

        def action():
            self.toggle_song_requests()
            if on_toggle is not None:
                on_toggle()

        return (label, action)

    def toggle_song_requests(self):
        """Host: flip the switch, remember it, tell the session."""
        self.song_requests_open = not self.song_requests_open
        options.set("music_bot_song_requests", self.song_requests_open)
        self.announce_song_requests()
        if self.song_requests_open:
            speak("Song requests open. Listeners can type /m and a song name "
                  "in the Party Sync chat.")
        else:
            speak("Song requests closed.")
        return self.song_requests_open

    def announce_song_requests(self):
        """Tell the server whether this host takes requests (host only).

                    It travels with the session state, so a guest who joins later still knows, and the
                    server can refuse a request from a client whose flag is stale.
                    """
        gp, ps = self._party_sync_pair()
        if ps is None or getattr(ps, "role", None) != "host":
            return
        self._party_sync_send("party_sync_song_requests",
                              {"open": bool(self.song_requests_open)})

    def song_requests_label(self):
        """Read-only answer for a listener (a host has the switch instead)."""
        gp, ps = self._party_sync_pair()
        open_ = bool(getattr(ps, "song_requests", False)) if ps else False
        if open_:
            return "Song requests: open - type /m and a song name"
        return "Song requests: closed"

    def queue_song_request(self, requester, query, request_id=None):
        """MAIN THREAD: serve one listener's request, or say why not.

                    Everything the host can refuse is decided before the search (which costs about a
                    second of yt-dlp). `request_id` is None for the host's own /p: nobody to answer, so
                    the outcome is spoken here. The search offers CANDIDATE_LIMIT results and THE ASKER
                    PICKS ONE - only the person who asked knows which of five ways to play a song they
                    meant, and nobody picks for somebody else.
                    """
        from ..party_sync import clean_song_query
        requester = str(requester or "").strip()
        query = clean_song_query(query)
        reason = self.song_requests.refusal(
            requester, query, self.next_up_queue,
            open_=self.song_requests_open, bot_running=self.enabled,
        )
        if reason is not None:
            self._answer_song_request(requester, request_id, ok=False,
                                      reason=reason)
            return False
        speak(f"Looking for {query} for {requester}...")

        def do_search():
            results = YouTubeSearcher.search(query,
                                             count=song_requests.CANDIDATE_LIMIT)
            self.game.put(
                lambda: self._offer_song_choices(
                    requester, query, request_id, results
                )
            )

        threading.Thread(target=do_search, daemon=True).start()
        return True

    def _offer_song_choices(self, requester, query, request_id, results):
        """MAIN THREAD: the search came back - offer it, or say there is none.

                    The host keeps the results (`song_picks`) and only the names travel; the answer comes
                    back as an index, so no client can name a song into this queue. A host's own /p opens
                    its picker locally with no round trip.
                    """
        if request_id is None:
            self._local_pick_seq += 1
            key = f"{song_requests.LOCAL_REQUEST_ID}:{self._local_pick_seq}"
        else:
            key = request_id
        found = self.song_picks.add(key, requester, query, results)
        if not found:
            self._answer_song_request(requester, request_id, ok=False,
                                      reason=f'No song found for "{query}".')
            return
        if request_id is None:
            self.open_song_pick_menu(
                key, song_requests.offered(found), query,
                on_pick=lambda index: self.serve_song_pick(requester, key, index),
            )
            return
        self._party_sync_send("party_sync_song_choices", {
            "to": requester,
            "request_id": request_id,
            "query": query,
            "items": song_requests.offered(found),
        })

    def serve_song_pick(self, requester, request_id, index):
        """MAIN THREAD: one pick (or a withdrawal) for a request offered here.

                    Resolved against the host's own list of results, and a pick may only ever answer the
                    request of the person who made it.
                    """
        local = str(request_id).startswith(song_requests.LOCAL_REQUEST_ID)
        query = self.song_picks.query(request_id)
        found, note = self.song_picks.pick(request_id, requester, index)
        if note is song_requests.WITHDRAWN:
            return False
        if found is None:
            self._answer_song_request(requester, None if local else request_id,
                                      ok=False, reason=note)
            return False
        title = found.get("title") or query or "the song"
        waiting = self._enqueue_track(
            title,
            found.get("webpage_url") or found.get("direct_url") or "",
            "youtube",
            http_headers=found.get("http_headers") or {},
            webpage_url=found.get("webpage_url") or "",
            direct_url=found.get("direct_url") or "",
            requested_by=requester,
        )
        self.song_requests.note_served(requester, query or title)
        self._answer_song_request(requester, None if local else request_id,
                                  ok=True, title=title, waiting=waiting)
        return True

    def open_song_pick_menu(self, request_id, items, query="", on_pick=None):
        """The picker: which of the results this request may be queued from.

                    Used by both ends - a listener whose own bot may be switched off entirely (no
                    playback, no sources, only a menu) and a host who asked their own bot. One menu and
                    one wording (`song_requests.choice_line`); `on_pick(index)` is what it means here.
                    """
        from .. import menu as menu_mod, menus
        gp = self._find_gameplay()
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
        m = menu_mod.Menu(self.game, title, parrent=gp)
        m.add_items(entries)
        menus.set_default_sounds(m)
        gp.add_substate(m)

    def _answer_song_request(self, requester, request_id, ok, title="",
                             reason="", waiting=0):
        """MAIN THREAD: hand one result back to the session (or just speak it).

                    The server decides who hears it: the requester always, and the room when it was
                    really queued. Only the host knows the resolved title, so the line is composed here.
                    """
        if request_id is None:
            if ok:
                speak(song_requests.request_line(
                    title, requester, waiting=waiting, started=waiting == 0
                ))
            elif reason:
                speak(reason)
            return
        payload = {
            "to": requester,
            "request_id": request_id,
            "ok": bool(ok),
        }
        if ok:
            payload["title"] = str(title)
            payload["waiting"] = int(waiting)
        else:
            payload["reason"] = str(reason)[:160]
        self._party_sync_send("party_sync_song_result", payload)

    # ── the play queue the session can read ─────────────────────────────
    # A listener hears this machine's music but cannot see what is coming (next_up_queue is
    # client-side, the server has no music-bot queue), so the HOST relays a bounded snapshot
    # (song_requests.queue_share). One direction only - nothing a listener reads changes it.

    def _now_playing_title(self):
        """The title of the song this bot has on right now ("" when idle).

                    One reader for "what is playing": the session relay and the Play Queue menu both lead
                    with the same line (`song_requests.now_playing_line`), or a host would describe a
                    queue nobody is playing.
                    """
        if not (getattr(self, "playing", False)
                or getattr(self, "paused", False)):
            return ""
        return " ".join(
            str(getattr(self, "current_title", "") or "").split())

    def announce_party_queue(self, force=False):
        # Host: tell the session what is playing and what is waiting.
        # Called every frame: it sends the moment the queue changes and repeats at
        # :data:`PARTY_QUEUE_INTERVAL` while there is anything to say, which covers a lost packet and
        # a late joiner. Reads the session without creating one (this runs for every bot that loads).
        gp = self._find_gameplay()
        if gp is None:
            return False
        ps = getattr(gp, "party_sync", None)
        if ps is None or getattr(ps, "role", None) != "host":
            return False
        now_playing = self._now_playing_title()
        items = song_requests.queue_share(self.next_up_queue)
        signature = (now_playing,
                     tuple((item["title"], item["by"]) for item in items))
        sent = getattr(self, "_party_queue_sent", None)
        now = time.monotonic()
        if not force and signature == sent:
            # Nothing changed and nothing is waiting: a session with an empty
            # queue and a stopped bot has no news, so stay quiet.
            if not now_playing and not items:
                return False
            if now - getattr(self, "_party_queue_sent_at", 0.0) \
                    < self.PARTY_QUEUE_INTERVAL:
                return False
        self._party_sync_send("party_sync_queue",
                              {"now_playing": now_playing, "items": items})
        self._party_queue_sent = signature
        self._party_queue_sent_at = now
        return True

    def party_queue_label(self):
        """What this session's host has waiting (read-only, for a listener)."""
        _gp, ps = self._party_sync_pair()
        waiting = len(getattr(ps, "queue", None) or ()) if ps is not None else 0
        return song_requests.queue_label(waiting)

    def open_party_queue_view(self):
        """Read-only view of the host's queue, for a session listener.

                    Somebody else's queue: every line is a report and there is nothing to press but Close.
                    It opens on top of whatever party menu asked for it.
                    """
        from .. import menu as menu_mod, menus
        gp, ps = self._party_sync_pair()
        if gp is None:
            return
        m = menu_mod.Menu(self.game, self.party_queue_label(), parrent=gp)
        lines = song_requests.queue_lines(
            getattr(ps, "queue", None) or (),
            getattr(ps, "now_playing", ""),
        )
        items = [(line, lambda: None) for line in lines]
        items.append(("Close", lambda: gp.pop_last_substate()))
        m.add_items(items)
        menus.set_default_sounds(m)
        gp.add_substate(m)

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
                self._party_sync_send("party_sync_list")
            items.append(("Invite players (any map)", do_invite))
            # The /p switch sits with the host's listeners, their invite and
            # their End action (one builder: song_requests_switch_item), not
            # in the Music Bot menu -- the queue it fills is this session's,
            # and this is the menu a host opens to run the session.
            item = self.song_requests_switch_item(m.speak_current_item)
            if item is not None:
                items.append(item)
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
            # Read-only: whether this party takes song requests (/p) is the
            # host's decision, and it arrives with the session state. A listener
            # reads it here instead of finding out by asking.
            items.append((self.song_requests_label, lambda: None))
            # ...and what the host has waiting, which is the other half of
            # asking for a song: the queue is the host's machine's, relayed to
            # the room (song_requests.queue_share -> parse_queue -> here).
            items.append((self.party_queue_label, self.open_party_queue_view))
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
                       webpage_url="", direct_url="", requested_by=""):
        """Queue a track to play next (Queue Mode / Add to Queue).

                    With nothing playing the earliest "next" is right now, so the track starts
                    immediately; otherwise it is appended and auto-plays at the end. Returns how many
                    tracks are waiting (0 when it started right away). `requested_by` marks a Party Sync
                    listener's /p, so the menu can say whose song it is and the quota counts only the
                    listeners' own slots.
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
            "requested_by": str(requested_by or ""),
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
    # The pre-roll / fade state machine lives in crossfade_handler.py; the state
    # it drives stays here, because the tests read `crossfade_enabled`,
    # `current_duration`, `_known_durations` and `_crossfade` off the bot and
    # committing a fade writes this object's own playback state (title, source,
    # streamer, playback generation) -- the handler's docstring says which and
    # why. These names stay as one-line shims, so the frame loop, Stop and Seek
    # keep calling the bot exactly as they always did. The three windows
    # (`CROSSFADE_SECONDS`, `CROSSFADE_PREP_SECONDS`, `CROSSFADE_LAUNCH_SECONDS`)
    # are declared above because a test reads one of them off the bot.

    def _crossfade_machine(self):
        """The crossfade handler, built on first use.

                    A bot built without __init__ (a test fixture, a restored session) has none
                    yet, so every shim below asks for it through here rather than holding a
                    reference that may never have been made.
                    """
        handler = getattr(self, "_crossfade_handler", None)
        if handler is None:
            handler = CrossfadeHandler(self)
            self._crossfade_handler = handler
        return handler

    def _remaining_seconds(self):
        return self._crossfade_machine()._remaining_seconds()

    def _peek_next_track(self):
        return self._crossfade_machine()._peek_next_track()

    def _consume_next_track(self):
        return self._crossfade_machine()._consume_next_track()

    def _cancel_crossfade(self):
        return self._crossfade_machine()._cancel_crossfade()

    def _start_crossfade_roll(self):
        return self._crossfade_machine()._start_crossfade_roll()

    def _update_crossfade(self):
        return self._crossfade_machine()._update_crossfade()

    def _update_fade_gains(self, base_gain):
        return self._crossfade_machine()._update_fade_gains(base_gain)

    def _set_crossfade_enabled(self, enabled):
        return self._crossfade_machine()._set_crossfade_enabled(enabled)

    def _open_queue_menu(self):
        """View / clear the play-next queue (Queue Mode).

                    Every queued song is its own menu item, so the player scrolls through them one at a
                    time and the menu speaks each as it is highlighted; Enter re-reads a line.

                    Every line is composed by `song_requests` (the count is `queue_label`, the body
                    `queue_lines`) - the same strings a Party Sync listener reads - so host and room can
                    never describe one queue two ways. Only the ends differ: Clear Queue and Back here.
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

        m = menu_mod.Menu(
            self.game, song_requests.queue_label(len(self.next_up_queue)),
            parrent=gp)
        items = []
        for line in song_requests.queue_lines(
                song_requests.queue_share(self.next_up_queue),
                self._now_playing_title()):
            def read_line(song_line=line):
                speak(song_line)

            items.append((line, read_line))
        items.append(("Clear Queue", clear_queue))
        items.append(("Back", go_back))
        m.add_items(items)
        menus.set_default_sounds(m)
        gp.add_substate(m)

    def _open_settings_menu(self):
        """Music Bot settings: underwater muffle and room reverb.

                    Broadcast to Others is deliberately NOT here - it has its own shortcut, and
                    duplicating it caused confusion about which is the source of truth. Every toggle here
                    is persisted in client options.
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

                    Matters when the toggle flips while the listener is underwater at a constant depth -
                    no camera automation tick runs then to re-apply it.
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

    # === Spoken subtitles (YouTube captions) ===
    # A blind player cannot read a caption, so the bot reads the video's own caption track out
    # loud while the audio keeps playing (libs/music_bot/subtitles.py). Per listener and per
    # machine: every cue is aimed at this machine's own audible position.

    def subtitle_label(self):
        if not getattr(self, "subtitles_enabled", False):
            return "Subtitles: OFF"
        return f"Subtitles: {subtitles.preference_label(self.subtitle_language)}"

    def toggle_subtitles(self):
        self.subtitles_enabled = not getattr(self, "subtitles_enabled", False)
        options.set("music_bot_subtitles", self.subtitles_enabled)
        if not self.subtitles_enabled:
            self._stop_subtitles()
            speak("Subtitles off. Songs and videos play without them.")
            return
        speak("Subtitles on. Captions are read aloud while the song or video "
              "plays.")
        # A track already playing gets its captions now instead of at the next
        # one: the page is the one this bot is playing from.
        self._begin_subtitles(self._caption_page())

    def _caption_page(self):
        """The YouTube page this bot is playing from, when there is one."""
        target = self.current_target if self.mode == "youtube" else ""
        return target if subtitles.is_caption_page(target) else ""

    def _begin_subtitles(self, page_url):
        """Start reading one page's captions, or leave the reader empty.

                    Called whenever a stream starts (a fresh song, a replayed one, a seek's restart), so
                    the reader always aims at the track really playing. The fetch runs on its own thread:
                    the yt-dlp import behind it is ~700 ms and ~24 MB and must never sit in the frame loop.
                    """
        self._subtitle_generation += 1
        generation = self._subtitle_generation
        self.subtitle_reader.clear()
        self._subtitle_page = page_url or ""
        if not getattr(self, "subtitles_enabled", False) or not self._subtitle_page:
            return
        languages = subtitles.languages_for(self.subtitle_language)

        def load():
            result = self._caption_fetcher.fetch(
                self._subtitle_page, languages,
                cancelled=lambda: generation != self._subtitle_generation)
            self.game.put(lambda: self._on_subtitles_loaded(generation, result))

        threading.Thread(target=load, daemon=True).start()

    def _on_subtitles_loaded(self, generation, load):
        """Main thread: a caption answer for the track that asked for it."""
        if generation != self._subtitle_generation:
            return
        if not getattr(self, "subtitles_enabled", False):
            return
        if load.reason == subtitles.REASON_CANCELLED:
            return
        if not load.cues:
            # Silence here is indistinguishable from a broken feature, so a
            # track that could not be read says so (once per track).
            speak(subtitles.reason_sentence(load.reason))
            return
        self.subtitle_reader.load(load.cues, load.language, load.automatic)
        kind = "automatic captions" if load.automatic else "captions"
        speak(f"{subtitles.language_label(load.language)} {kind} for this "
              f"track. {len(load.cues)} lines.")

    def _stop_subtitles(self):
        """Forget the track being read and any fetch still running for it."""
        self._subtitle_generation += 1
        self._subtitle_page = ""
        self.subtitle_reader.clear()

    def _pump_subtitles(self):
        """Read whatever the song has reached (called every playing frame)."""
        if not getattr(self, "subtitles_enabled", False):
            return
        reader = self.subtitle_reader
        if not reader.ready:
            return
        position = self.track_position()
        if position is None:
            return
        # Queued, never interrupting: a caption must not cut off the menu or a
        # game announcement, and the reader's own bound is what keeps the queue
        # from growing (subtitles.SubtitleReader.pump).
        for line in reader.pump(position * 1000.0,
                                offset_ms=self.subtitle_offset):
            speak(line, interupt=False)

    def _open_subtitle_menu(self):
        """Everything about spoken subtitles, behind one line of the menu."""
        from .. import menu as menu_mod, menus
        gp = self._find_gameplay()
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
            speak(self.subtitle_reader.status() +
                  " Subtitles come from the video on YouTube: the uploader's "
                  "own track when there is one, otherwise the automatic "
                  "captions. Songs often have none, because the words of a "
                  "song are rarely a caption track.")

        m = menu_mod.Menu(self.game, "Subtitles", parrent=gp)
        m.add_items([
            (self.subtitle_label, self.toggle_subtitles),
            (self.subtitle_language_label, go_language),
            (self.subtitle_timing_label, go_timing),
            (lambda: self.subtitle_reader.status(), read_status),
            ("Back", go_back),
        ])
        menus.set_default_sounds(m)
        gp.add_substate(m)

    def subtitle_language_label(self):
        return ("Language: "
                + subtitles.preference_label(self.subtitle_language))

    def subtitle_timing_label(self):
        return subtitles.offset_label(self.subtitle_offset)

    def _open_subtitle_language_menu(self):
        """The order YouTube is asked for a language in."""
        from .. import menu as menu_mod, menus
        gp = self._find_gameplay()
        if not gp:
            return

        def go_back():
            gp.pop_last_substate()
            self._open_subtitle_menu()

        def make_label(key, label):
            def label_fn():
                current = getattr(self, "subtitle_language", None)
                return f"{label} (current)" if current == key else label
            return label_fn

        def make_pick(key, label):
            def pick():
                self.subtitle_language = subtitles.normalize_preference(key)
                options.set("music_bot_subtitle_language",
                            self.subtitle_language)
                speak(f"Subtitle language: {label}.")
                # A track already playing is read again in the new order (the
                # track itself is cached, so only a language change costs a
                # fetch).
                self.subtitle_reader.clear()
                self._begin_subtitles(self._caption_page())
                m.speak_current_item()
            return pick

        m = menu_mod.Menu(self.game, "Subtitle Language", parrent=gp)
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
        gp = self._find_gameplay()
        if not gp:
            return

        def go_back():
            gp.pop_last_substate()
            self._open_subtitle_menu()

        def speak_timing():
            speak(subtitles.offset_label(self.subtitle_offset))

        def shift(delta):
            def move():
                self.subtitle_offset = subtitles.normalize_offset(
                    self.subtitle_offset + delta)
                options.set("music_bot_subtitle_offset", self.subtitle_offset)
                speak_timing()
                m.speak_current_item()
            return move

        def reset():
            self.subtitle_offset = 0
            options.set("music_bot_subtitle_offset", 0)
            speak_timing()
            m.speak_current_item()

        step = subtitles.OFFSET_STEP_MS / 1000.0
        m = menu_mod.Menu(self.game, "Subtitle Timing", parrent=gp)
        m.add_items([
            (self.subtitle_timing_label, speak_timing),
            (f"Speak {step:.1f} seconds earlier",
             shift(subtitles.OFFSET_STEP_MS)),
            (f"Speak {step:.1f} seconds later",
             shift(-subtitles.OFFSET_STEP_MS)),
            ("Reset timing", reset),
            ("Back", go_back),
        ])
        menus.set_default_sounds(m)
        gp.add_substate(m)

    # === Equalizer (personal Music Bot) ===
    # Same approach as the jukebox: preset slots cached per profile, a custom profile owning one
    # slot mutated in place, and "normal" detaching the aux send so the stream stays perfectly flat.

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
                    slot = audio.gen_effect(
                        "EQUALIZER", *params,
                        hold=("music_bot_eq", "custom", None),
                    )
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
                    "EQUALIZER", *self.EQ_PRESETS[profile],
                    hold=("music_bot_eq", f"preset:{profile}", None))
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
        if getattr(self, "cinema_bank", None) is not None:
            # The room carries the EQ on every speaker, not on a source the
            # bot owns (in cinema mode the bot owns no source at all).
            with contextlib.suppress(Exception):
                self.cinema_bank.set_eq_slot(slot)
            return
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

                    The server keeps the slot single-owner so two people's music never overlaps; everyone
                    else with "Broadcast to Megaphone" still broadcasts their live instruments.
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

                    start_offset seeks the new decode head to a content position in seconds (ffmpeg input
                    seek, like the jukebox mid-song path); start_paused keeps the pre-buffered head silent.
                    """
        if (playback_generation is not None
                and not self._is_current_playback_generation(playback_generation)):
            return
        self.is_loading_stream = False
        self._create_stream_source()
        if not self._output_source():
            speak("Audio error.")
            return

        self.current_duration = (
            self._known_durations.get(canonical_url)
            if canonical_url else None
        )
        self.streamer = AudioStreamer(
            self.game, audio_url,
            self._output_source(),
            self.volume, bot=self,
            http_headers=http_headers,
            canonical_url=canonical_url,
            start_offset=start_offset,
            start_paused=start_paused,
            # A room replaces the ear source entirely: the same interleaved
            # stereo decode is rendered into one feed per speaker.
            cinema=self.cinema_bank,
        )
        self.streamer.start()

        self.mode = "youtube"
        self.playing = True
        # A seek performed while paused stays paused: the new streamer starts
        # silent and only becomes audible when the bot is resumed.
        self.paused = bool(start_paused)
        self.current_title = title
        # Captions for the page this stream came from. A seek restarts the same
        # track through here, and that is on purpose: the cues are unchanged
        # and the pump reads the new stream's own position, so a seek needs no
        # caption bookkeeping at all.
        self._begin_subtitles(canonical_url)
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

                    Works for YouTube links and local files: the track is restarted at the target
                    position via an ffmpeg input seek. A seek while paused keeps the new stream paused.
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

        # Remote (YouTube) tracks normally re-resolve so a seek uses a fresh signed URL (an expired
        # googlevideo URL 403s forever) - but that extraction is the SLOW part. With a direct URL +
        # headers still held (they age over hours, not seconds) restart straight from them; a stale
        # URL is self-healing via AudioStreamer.run's startup retry ladder.
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
        # A room fed by this bot goes quiet with it (the sources are the
        # room's, so they are deleted here rather than by _destroy_stream_source).
        self._release_cinema_bank()
        # Stop local playback
        self._stop_local()
        self.playing = False
        self.paused = False
        self.mode = "idle"
        self._stream_announced = False
        self._current_reverb_slot = None
        # A track this bot was reading captions for is over: the lines and any
        # fetch still running for them stop here, rather than speak into
        # whatever plays next.
        self._stop_subtitles()
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
        if getattr(self, "cinema_bank", None) is not None:
            try:
                self.cinema_bank.set_volume(self.volume)
            except Exception:
                pass
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

        # Also duck while OTHERS talk on the PA: remote megaphone frames are stamped in
        # event_handeler.process_voice_data, and the server never echoes our own broadcast back, so
        # any recent frame means someone else is speaking. The multiplier scales BOTH the local source
        # and the uploaded PCM, so every listener hears the music dip together.
        remote_speaking = False
        if gp:
            last_remote = getattr(gp, '_last_remote_megaphone_voice_ts', 0)
            remote_speaking = (time.monotonic() - last_remote) < 0.6

        target_duck = 0.2 if (
            (is_speaking_on_mega or remote_speaking)
            and self.broadcast_to_megaphone
        ) else 1.0
        
        if not hasattr(self, 'duck_multiplier'):
            self.duck_multiplier = 1.0
        
        # LERP towards target (10% step per frame ~300ms transition)
        self.duck_multiplier += (target_duck - self.duck_multiplier) * 0.1
        
        # Ensure live instrument / mic relay streamer is active if needed
        self._ensure_live_relay_streamer()

        # Apply updated gain to local stream source. During a crossfade the
        # two overlapping sources are ramped against each other instead. With
        # cinema routing there is no ear source to write a gain to -- the
        # room's speakers carry it -- so the duck is handed to the bank.
        bank = getattr(self, "cinema_bank", None)
        if bank is not None and (self.playing or self.paused):
            with contextlib.suppress(Exception):
                bank.set_duck(self.duck_multiplier)
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
        if (self.stream_source or bank is not None) and (self.playing or self.paused):
            self._sync_map_reverb()

        # A cinema room needs its own upkeep every frame (per-speaker gains, occlusion, and
        # survival across a jukebox stop or map reload). This reads the raw attribute on
        # purpose: whether the routing is actually allowed is decided inside, so a bot whose
        # account lost the permission still runs the release path that hands the track back.
        if getattr(self, "cinema_target", None):
            self._update_cinema_output()
            # Keep the routing on the map fresh (a joiner, a return from
            # another map, a lost packet), at most every few seconds.
            self.announce_cinema_target()

        # A Party Sync host keeps the room's view of this queue fresh: sent on
        # every change (so a request appears for everybody as soon as it is
        # queued) and repeated every PARTY_QUEUE_INTERVAL. Quiet for any client
        # that is not hosting, which is the cheap majority of the time.
        self.announce_party_queue()

        if not self.playing or self.paused:
            return

        # Crossfade state machine: pre-roll and overlap the next queued track
        # when the current one is about to end.
        self._update_crossfade()

        # Spoken subtitles: read whatever position this machine's ears have
        # reached (nothing to do unless the player asked for them).
        self._pump_subtitles()

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

                    Only ordinary Music Broadcast has a versioned timeline; megaphone and private playback
                    return None. A song routed into a cabinet's room counts as an ordinary broadcast -
                    other people hear the room and play their notes against this very clock - so the
                    marker travels even when the account's own Broadcast switch is off.
                    """
        if (not (self.broadcast_enabled or self.cinema_force_upload)
                or self.broadcast_to_megaphone
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

                    Cave echo, outdoor ambience: the dry signal stays stereo-direct (headphone quality)
                    while the wet signal adds the room's atmosphere. Skipped when Room Reverb is disabled.
                    """
        bank = getattr(self, "cinema_bank", None)
        if not self.stream_source and bank is None:
            return True
        if not getattr(self, "reverb_enabled", True):
            # Setting off — detach any slot that was applied before it flipped.
            if force or self._current_reverb_slot is not None:
                slot = None
                if bank is not None:
                    self.cinema_bank.set_reverb(None)
                elif self.stream_source is not None:
                    self.game.audio_mngr.efx.send(self.stream_source, 0, slot)
                self._current_reverb_slot = None
            return True
        try:
            gp = self._find_gameplay()
            map_obj = getattr(gp, 'map', None) or getattr(gp, 'world_map', None)
            if not map_obj:
                return True

            player = gp.player
            reverb = map_obj.get_reverb_at(player.x, player.y, player.z)
            if bank is not None:
                # The room sits in the map too, so it takes the zone the
                # LISTENER is standing in -- the same atmosphere the deck
                # plays dry into, applied to every speaker.
                slot = reverb.reverb if (reverb and reverb.reverb) else None
                if force or self._current_reverb_slot != slot:
                    bank.set_reverb(slot)
                    self._current_reverb_slot = slot
                return True

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

                    The Custom profile owns one effect slot mutated in place on every tick, so
                    adjustments are audible immediately and no EFX slots leak.
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
