"""Every Music Bot menu - the builder half of MapMusicBot (extracted from
controller.py).

This module is the door of the menu layer: the Mode page, the search result
pages and the help page. Every other page group lives in `menu_pages/` beside
this file -- `settings.py` (the Settings hub, output device, equalizer and the
Custom EQ slider, song key and speech key), `autotune.py` (Auto-Tune and
Harmony), `subtitles.py`, `voice_effect.py` (the Voice Effect page and its two
boards), `party.py`, `recording.py` and `library.py` -- and `MusicBotMenus`
inherits one class from each, so every method still answers on the bot.

**The state stays on `MapMusicBot`; the behaviour moved here.** Every queue,
option, bank and reader a menu shows or flips is the bot's own (`next_up_queue`,
`queue_mode`, `feed_tracks`, `cinema_target`, `eq_profile`, `subtitle_reader`),
and the tests read those straight off the bot, so those attributes are still the
bot's, and these classes reach them through their owner. One rule for the
reader, because the menu layer names its owner on every line: **`self.` is this
handler, `self._bot.` is the Music Bot.** A page in `menu_pages/` that
speaks through the module's `speak` calls `self._say`, because `menus.speak` is
the name the menu tests patch.

`MapMusicBot` keeps a one-line shim for each method, so the callers that
were written against the bot -- gameplay's key bindings, event_handeler,
cinema_handler's "Back to Music Bot" line and the tests -- keep working
unchanged. A menu runs when a key is pressed, on the main thread, and never in
the frame loop or the audio pump, so the extra hop costs nothing that matters.

Usage from MapMusicBot::

    self._menus_handler = MusicBotMenus(self)   # or built on first use
    self._menus()._show_mode_menu()

Pins: client/tests/test_music_bot_queue_settings.py's `TestTheMenuModule`
(a shim per menu and a menu per shim, across this file and `menu_pages/`),
test_music_bot_menu_help.py (every line's Tab description), and
test_music_bot_subtitles.py reads one menu's body through `inspect.getsource`
(`menu_source_of`). test_music_bot_crossfade_eq.py opens `_MusicBotEqSlider`
itself, and the menu source reads in test_game_audio_recorder.py,
test_music_downloader.py and test_party_*.py join this file with `menu_pages/`.
Rules: .agents/skills/music_bot_integration/.
"""

import pygame

from . import gate
from .. import options
# Re-exported: the output-device tests patch `menus.output_device` here.
from . import output_device
from ..speech import speak
from ..string_utils import friendly_key_name
from . import song_requests
from .menu_pages.autotune import AutoTuneMenus
from .menu_pages.library import LibraryMenus
from .menu_pages.party import PartyMenus
from .menu_pages.recording import RecordingMenus
from .menu_pages.settings import SettingsMenus, _MusicBotEqSlider
from .menu_pages.subtitles import SubtitleMenus
# Re-exported: `stand_on` was this module's own helper before the page split.
from .menu_pages.support import stand_on
from .menu_pages.voice_effect import VoiceEffectMenus

__all__ = ["MusicBotMenus", "stand_on", "output_device", "_MusicBotEqSlider"]


class MusicBotMenus(SettingsMenus, AutoTuneMenus, SubtitleMenus,
                    VoiceEffectMenus, PartyMenus, RecordingMenus, LibraryMenus):
    """Builds and opens every Music Bot menu (see the module docstring)."""

    def __init__(self, bot):
        self._bot = bot          # back-reference to MapMusicBot

    def _say(self, text, **kwargs):
        """Speak a line through this module's `speak`, looked up at call time.

        `libs.music_bot.menus.speak` is the name the menu tests patch, so a
        page in `menu_pages/` must not bind its own copy: it calls here and the
        patch reaches it wherever the page's code lives.
        """
        return speak(text, **kwargs)

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
        # Every line is ``(feature id, label, action, description)``: the id is
        # the Server's own (libs/music_bot/gate.py) and a locked feature's line
        # is not built at all, so a feature added to the catalogue needs one id
        # here and nothing else; the description is what Tab reads out for the
        # line the cursor is on, and EVERY line of this menu carries one (a
        # feature nobody can explain is a feature only its author uses).
        # `None` is a line that belongs to no single feature: Back, Help, the
        # Settings hub and the listener-side cinema switches are never hidden.
        items = [
            ("search", "Search YouTube", go_search,
             "Searches YouTube and plays what you pick, without downloading "
             "anything. Enter on a result plays it now, or adds it to the "
             "play-next queue while Queue Mode is on."),
            # Right beside the search: staff who already hold the link should not
            # have to search for the song they are looking at.
            ("search", "Play a YouTube link", go_link,
             "Plays a link you already have instead of searching for it: a "
             "youtube.com or youtu.be address, from a chat message or a "
             "browser. Every other site is refused."),
            ("search", "Choose Local File", go_local,
             "Plays a song file already on this computer, through the same "
             "speakers, key and equalizer this bot uses for YouTube."),
            ("playlists", "My Playlists & Favorites", go_playlists,
             "Songs you saved and playlists you made: play them, save the song "
             "that is playing, or take one out again."),
            ("feed", "Personal Music Feed", go_personal_feed,
             "A shuffled stream of your Favorites, one song after another, "
             "kept apart from the play-next queue."),
            ("downloads", "Music Download Center", go_downloads,
             "Downloads songs and playlists to this computer as files, so they "
             "play with no internet and no YouTube."),
            ("recording", "Record Audio", go_record_audio,
             "Records what this machine plays, the game and the song together, "
             "to a file for a final mix."),
            ("party", "Party Sync (Listen Together)", go_party_sync,
             "Invites people on any map to hear your music privately in their "
             "own headphones, at the same moment you hear it."),
        ]
        
        # Show the megaphone routing option only when the server explicitly granted
        # broadcast permission (canBroadcastMegaphone()). The server is the single
        # source of truth; gating on it keeps the client menu and the server lock
        # perfectly in sync (no client-side role guessing).
        can_broadcast_megaphone = getattr(gp, 'can_broadcast_megaphone', False) if gp else False

        if can_broadcast_megaphone and not gate.is_locked("broadcast"):
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
                
            items.append(("broadcast", get_megaphone_label,
                          toggle_megaphone_routing,
                          "Sends the song, an instrument and your voice "
                          "through the map's PA speakers, so everyone in "
                          "earshot hears it. The song's own room routing is a "
                          "different line."))

        # Cinema speaker routing is Developer/Contributor only (see
        # cinema_routing_allowed): everyone else gets a plain Music Bot menu
        # rather than a switch the Server would refuse to honour.
        if self._bot.cinema_routing_allowed() and not gate.is_locked("cinema_speakers"):
            def go_cinema():
                gp.pop_last_substate()
                self._bot._open_cinema_menu()

            items.append((
                "cinema_speakers", self._bot.cinema_target_label, go_cinema,
                "Which cabinet's room this song is played through. A room "
                "plays it from the speakers its builder placed, aimed at the "
                "seats, instead of from the cabinet."))

        # Three listener-side switches, one line below the song's own routing: a room is
        # how *this* listener hears a cabinet, so none of them takes anything from anybody
        # and all three are open to anyone who can open this menu (the song routing above
        # stays Developer/Contributor). Songs, instruments and a voice.
        if self._bot.cinema_listening_allowed():
            items.append((None, self._bot.cinema_rooms_label,
                          self._bot.toggle_cinema_rooms,
                          "Whether a cabinet's room plays into your own ears. "
                          "Off means you hear the cabinet as the plain pair of "
                          "speakers it is."))
            items.append((None, self._bot.instruments_cinema_label,
                          self._bot.toggle_instruments_cinema,
                          "Whether instruments played on this map are heard "
                          "through a cinema room's speakers as well."))
            items.append((None, self._bot.speech_cinema_label,
                          self._bot.toggle_speech_cinema,
                          "Whether a voice talking over the map is heard "
                          "through a cinema room's speakers as well."))
            # The three lines above choose how *you* hear a room; this one is the other
            # direction and is not a switch at all - it answers "where do other players hear me
            # from", which is decided by staff. It reads rather than toggles: pressing it says
            # the same sentence the moment of a pan says, so it cannot look like a setting.
            items.append((None, self._bot.own_sound_label,
                          self._bot.announce_own_sound,
                          "Says where other players hear you from: your own "
                          "body, or the speaker a staff member panned you to. "
                          "It only tells you; staff decide it."))

        items.extend([
            ("queue", get_queue_mode_label, toggle_queue_mode,
             "When it is on, Enter on a search result adds the song to the "
             "play-next list instead of playing it now."),
            ("queue", get_queue_count_label, go_queue,
             "The songs waiting behind the one that is playing. Enter opens "
             "the list to read them, or to clear it."),
            ("subtitles", self._bot.subtitle_label, self._open_subtitle_menu,
             "Reads the song's own words out loud as the song reaches them, "
             "for a player who cannot see the screen. Songs often have none."),
            # The hub itself is never hidden: it is where a Developer looks to
            # see what is left, and its own lines are filtered one at a time.
            (None, "Music Bot Settings", go_settings,
             "Every sound choice: where the song comes out, reverb, "
             "crossfade, the equalizer, the song's key, the voice's key and "
             "Auto-Tune."),
            (None, "Help", go_help,
             "The keys the Music Bot answers to. Tab reads what the line you "
             "are standing on does, on every page of this menu."),
            (None, "Cancel", lambda: gp.pop_last_substate(),
             "Closes this menu. Nothing is changed."),
        ])
        m.add_items(gate.filter_items(items))
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

        def say_tab_help():
            speak("Tab reads what the line you are standing on does. It works "
                  "on every page of the Music Bot menu: arrow to a line, press "
                  "Tab, and it explains the feature behind it -- what it "
                  "changes, what you will hear, and what it costs. Pressing "
                  "Tab never chooses a line or closes the menu.")

        # The Tab line leads, because it is the one thing a first visitor does
        # not know: every other line here is a key to memorise, and this one is
        # how the rest of the bot explains itself.
        m = menu_mod.Menu(
            self._bot.game, "Music Bot Controls Help", parrent=gp,
            intro="Tab reads what the line you are on does, on every page.",
        )
        items = [
            ("Tab: Read what the line you are on does", say_tab_help, None,
             "Every line in the Music Bot's menus carries its own description "
             "and its properties: which scale a key has, what a Retune Speed "
             "does, what a listening switch changes. Arrow to a line and "
             "press Tab to hear it."),
            (f"{toggle_key}: Open mode menu", lambda: None, None,
             "Opens the Music Bot menu: search, local files, playlists, "
             "downloads, recording, Party Sync, the queue and Settings."),
            (f"Shift + {toggle_key}: Pause / Resume", lambda: None, None,
             "Stops the song where it is and starts it again from the same "
             "place. A cabinet, a room and a jam session follow it."),
            (f"Ctrl + {toggle_key}: Stop / Replay last song", lambda: None, None,
             "Stops the song entirely, or starts the last one from the "
             "beginning when nothing is playing."),
            (f"Ctrl + Shift + {toggle_key}: Speak status", lambda: None, None,
             "Says what is playing, how far in it is, and whether it is "
             "private or broadcast."),
            (f"Alt + {toggle_key}: Toggle broadcast (Private/Public)", lambda: None, None,
             "Chooses whether other players hear the song or only you do."),
            ("Personal Music Feed: Ctrl left bracket for previous; Ctrl right bracket for next",
             lambda: None, None,
             "Moves through your shuffled Favorites feed without opening a "
             "menu."),
            (f"{volume_down_key}: Decrease volume", lambda: None, None,
             "Turns the song down, without touching the game's own sounds."),
            (f"{volume_up_key}: Increase volume", lambda: None, None,
             "Turns the song up, without touching the game's own sounds."),
            (f"Ctrl + {volume_down_key}: Rewind 10 seconds (add Shift for 60)",
             lambda: None, None,
             "Goes back in the song. Held with Shift it is a whole minute."),
            (f"Ctrl + {volume_up_key}: Fast-forward 10 seconds (add Shift for 60)",
             lambda: None, None,
             "Goes forward in the song. Held with Shift it is a whole minute."),
            ("Back", go_back, None,
             "Returns to the Music Bot menu."),
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

        items.append(("Cancel", lambda: gp.pop_last_substate(), None,
                      "Closes the results without playing anything."))
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

        items.append(("Play Now", play_now, None,
                      "Starts this song now, on top of whatever is playing."))
        items.append(("Play Next (Add to Queue)", add_to_queue, None,
                      "Puts this song behind the one that is playing, so it "
                      "comes next."))
        items.append(("Download Song", download_song, None,
                      "Saves this song as a file on this computer, at the "
                      "quality you pick next."))
        items.append(("Save to Favorites", save_fav, None,
                      "Adds this song to your Favorites, so you can find it "
                      "again without searching."))
        items.append(("Add to Playlist...", save_playlist, None,
                      "Picks one of your playlists to put this song in."))
        items.append(("Cancel", lambda: gp.pop_last_substate(), None,
                      "Closes this menu without doing anything with the "
                      "song."))

        m.add_items(items)
        menus.set_default_sounds(m)
        gp.add_substate(m)
