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
from . import gate
from . import output_device
from ..string_utils import friendly_key_name
from . import song_requests
from . import subtitles
from .music_downloader import is_supported_music_url


def stand_on(menu, prefix):
    """Put a rebuilt menu's cursor back on the line whose text starts `prefix`.

    A page that changes shape around a press -- the Auto-Tune dials exist only
    while the switch above them is on -- cannot be updated in place: it is
    popped and built again. The cursor would land on the first line of a page
    the player did not open from the top, so it is put back on the switch they
    just pressed instead, and the line is read with its new label.
    """
    if menu is None:
        return
    for index, item in enumerate(menu.items):
        text = item[0]() if callable(item[0]) else item[0]
        if str(text).startswith(prefix):
            menu.pos = index
            break
    menu.speak_current_item()


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

        entries.append((
            "Nothing, thanks", withdraw, None,
            "Answers the person who asked: there is no song to queue this "
            "time, and nothing is added to the session."))
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
        items.append(("Close", lambda: gp.pop_last_substate(), None,
                      "Closes this list and returns to the Party Sync menu."))
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
            items.append((
                "Start Party Sync session", do_start, None,
                "Starts a session with you as the host: the song you play is "
                "sent to everyone you invite, and they hear it at the moment "
                "you do."))
            items.append((
                "Invite friends anywhere to hear your music privately",
                lambda: None, None,
                "A session is not tied to a map: a listener anywhere hears your "
                "song in their own headphones, never through the map's "
                "speakers.",
            ))
        elif ps.role == "host":
            def guests_label():
                names = ", ".join(
                    g["name"] for g in ps.guests
                ) if ps.guests else "nobody yet"
                return f"Listeners ({len(ps.guests)}): {names}"
            items.append((guests_label, lambda: None, None,
                          "Who is listening right now. Each of them hears what "
                          "you hear, from the moment they joined."))
            if ps.guests:
                def do_kick():
                    close_top()
                    self._open_party_kick_menu(ps)
                items.append((
                    "Kick a listener", do_kick, None,
                    "Removes one listener from the session. They stop hearing "
                    "your song at once and can be invited again."))
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
            items.append((
                "Invite players (any map)", do_invite, None,
                "Opens the list of everyone online. A listener on another map "
                "hears the song as clearly as one standing beside you."))
            # The /p switch sits with the host's listeners, their invite and
            # their End action (one builder: song_requests_switch_item), not
            # in the Music Bot menu -- the queue it fills is this session's,
            # and this is the menu a host opens to run the session.
            item = self._bot.song_requests_switch_item(m.speak_current_item)
            # A locked `requests` feature takes the host's switch away: the line
            # is how a session starts taking requests, so without it there is
            # nothing a listener could ask into.
            if item is not None and not gate.is_locked("requests"):
                items.append(item)
            def do_end():
                close_top()
                self._bot._party_sync_send("party_sync_end")
                ps.end_session()
                self._bot.party_sync_force_upload = False
                self._bot._clear_party_sync_direct()
            items.append((
                "End Party Sync session", do_end, None,
                "Ends the session for everyone. You keep playing on your own "
                "machine; your listeners hear nothing more."))
        elif ps.role == "guest":
            def do_leave():
                close_top()
                self._bot._party_sync_send("party_sync_leave")
                ps.end_session()
                self._bot._clear_party_sync_direct()
            items.append((f"Listening to {ps.host_name}", lambda: None, None,
                          "The host whose song you are hearing. It comes "
                          "straight to your headphones, not through the map's "
                          "speakers."))
            # Read-only: whether this party takes song requests (/p) is the
            # host's decision, and it arrives with the session state. A listener
            # reads it here instead of finding out by asking.
            items.append((self._bot.song_requests_label, lambda: None, None,
                          "Whether the host takes song requests. While it is "
                          "open, type /m and a song name in the party chat."))
            # ...and what the host has waiting, which is the other half of
            # asking for a song: the queue is the host's machine's, relayed to
            # the room (song_requests.queue_share -> parse_queue -> here).
            items.append((self._bot.party_queue_label,
                          self.open_party_queue_view, None,
                          "What the host has waiting behind the song that is "
                          "playing. Enter opens the list to read it."))
            items.append((
                "Leave Party Sync session", do_leave, None,
                "Stops listening and leaves the session. The host keeps "
                "playing, and you can be invited again."))

        items.append(("Back", back_to_bot_menu, None,
                      "Returns to the Music Bot menu. Nothing is ended."))
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
            items.append((f"Kick {name}", make_kick(), None,
                          f"Removes {name} from the session: they stop "
                          f"hearing the song and can be invited again."))
        items.append(("Back", back_to_party, None,
                      "Returns to the Party Sync menu without kicking "
                      "anybody."))
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
        from .. import menu as menu_mod, menus
        from ..game_audio_recorder import FADE_CHOICES, MAX_FADE_SECONDS

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
                speak(f"{title} unchanged, {recorder.fade_time_text(before)}.")
                return
            try:
                value = float(text)
            except ValueError:
                speak(f"'{text}' is not a number of seconds, so "
                      f"{title.lower()} is still {recorder.fade_time_text(before)}.")
                return
            apply(value)
            after = live_seconds()
            if after:
                speak(f"{title}: {recorder.fade_time_text(after)}.")
            else:
                speak(f"{title} off.")

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
            ("Yes, Restore Recording Defaults", restore, None,
             "Puts every recording setting back to the value it ships with. "
             "Recordings already saved are not touched."),
            ("Cancel and Keep Current Settings", cancel, None,
             "Leaves every setting as it is and goes back."),
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
        items.append(("Clear Queue", clear_queue, None,
                      "Removes every waiting song. The song that is playing "
                      "keeps playing."))
        items.append(("Back", go_back, None,
                      "Returns to the Music Bot menu."))
        m.add_items(items)
        menus.set_default_sounds(m)
        gp.add_substate(m)

    def _open_settings_menu(self):
        """Music Bot settings: output device first, then reverb, crossfade, EQ, key, speech key, auto-tune, its three dials (speed, scale, amount), harmony, muffle.

                    Harmony is its own line and its own mode: it is not one of the dials
                    that the Auto-Tune switch adds and removes, and it sings whether the
                    tuner is on or off (`libs/voice_chat.harmony_enabled`). It sits after
                    the tuner's dials so the block the switch opens stays one block.

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
            if getattr(self._bot, "autotune_enabled", False):
                # Not a dead line: the dial really has nothing to move while
                # the grid is chromatic, so the row says who is in charge
                # rather than letting a key press do nothing quietly.
                return "Speech Key: replaced by Auto-Tune"
            return f"Speech Key: {self._bot.speech_transpose_label()}"

        def go_speech_transpose():
            if getattr(self._bot, "autotune_enabled", False):
                speak("Auto-Tune puts the voice on every note by itself, so "
                      "Speech Key has nothing to move. Turn Auto-Tune off first.")
                return
            gp.pop_last_substate()
            self._open_speech_transpose_menu()

        def get_autotune_label():
            return f"Auto-Tune: {self._bot.autotune_label()}"

        def toggle_autotune():
            enabled = self._bot.toggle_autotune()
            speak("Auto-Tune on. The voice is snapped to the nearest note as "
                  "you broadcast it; Retune Speed sets how fast it slides. "
                  "The harmony voices are not affected."
                  if enabled else "Auto-Tune off. Your own voice; the "
                  "harmony voices keep singing.")
            # The dials below this line exist only while it is on, so the page
            # is rebuilt around this press and the cursor is put back on the
            # switch rather than jumping to the top of the list. The Harmony
            # line is NOT one of them: it is a mode of its own and stays.
            gp.pop_last_substate()
            stand_on(self._open_settings_menu(), "Auto-Tune:")

        def get_autotune_speed_label():
            return f"Retune Speed: {self._bot.autotune_speed_label()}"

        def go_autotune_speed():
            gp.pop_last_substate()
            self._open_autotune_speed_menu()

        def get_autotune_scale_label():
            return f"Auto-Tune Scale: {self._bot.autotune_scale_label()}"

        def go_autotune_scale():
            gp.pop_last_substate()
            self._open_autotune_scale_menu()

        def get_autotune_amount_label():
            return f"Tune Amount: {self._bot.autotune_amount_label()}"

        def go_autotune_amount():
            gp.pop_last_substate()
            self._open_autotune_amount_menu()

        def get_harmony_label():
            return f"Harmony: {self._bot.autotune_harmony_summary()}"

        def go_harmony():
            gp.pop_last_substate()
            self._open_harmony_menu()

        # The dials are only in the list while the switch is on: off, they
        # change nothing a player can hear, and Auto-Tune lines that do nothing
        # are noise between the player and the rest of the page. Turning it on
        # puts them there (the switch rebuilds). Harmony is not one of them.
        dials = [] if not getattr(self._bot, "autotune_enabled", False) else [
            (
                "auto_tune", get_autotune_speed_label, go_autotune_speed,
                "How fast Auto-Tune walks the voice to a note: from Instant, "
                "which is the effect, to Very slow, which is a correction you "
                "only notice when the note lands.",
            ),
            (
                "auto_tune", get_autotune_scale_label, go_autotune_scale,
                "Which notes the voice may be moved to: every semitone, a "
                "major key, a natural, harmonic or melodic minor key, or a "
                "pentatonic, in the key you pick. Each one is described on "
                "its own line.",
            ),
            (
                "auto_tune", get_autotune_amount_label, go_autotune_amount,
                "How much of the correction is used: 100% puts the voice on "
                "the notes of the scale, 50% leaves half the error in it and "
                "half the human wobble, 0% is your own voice with Auto-Tune "
                "still switched on.",
            ),
        ]

        def get_output_label():
            return f"Music Output Device: {self._bot.output_device_label()}"

        def go_output():
            gp.pop_last_substate()
            self._open_output_device_menu()

        m = menu_mod.Menu(self._bot.game, "Music Bot Settings", parrent=gp)
        # One line per feature, and every line says what it does when Tab is
        # pressed on it: the labels have to stay short enough to arrow past, so
        # the explanation rides beside the line instead of inside it.
        m.add_items(gate.filter_items([
            ("output_device", get_output_label, go_output,
             "Where the song comes out: this game's device, or another pair of "
             "speakers or a headset. The equalizer and the room reverb stay on "
             "the game's device, so a song sent elsewhere is unshaped."),
            ("reverb", get_reverb_label, toggle_reverb,
             "Adds the room's own reverb to the song, so a song played inside "
             "a hall sounds like it is in the hall."),
            ("crossfade", get_crossfade_label, toggle_crossfade,
             "Starts the next song before this one ends and blends the two, "
             "so there is no silence between songs."),
            ("eq", get_eq_label, go_eq,
             "Shapes the song's bass, mid and treble. A profile is heard as "
             "soon as it is chosen; Custom opens the three bands as sliders."),
            ("song_key", get_transpose_label, go_transpose,
             "Moves the whole song up or down a semitone at a time. The tempo "
             "and the song's position do not change, so a cabinet and a jam "
             "session stay in step."),
            ("speech_key", get_speech_transpose_label, go_speech_transpose,
             "Moves your broadcast voice up or down a semitone; the song "
             "keeps its own key. It is replaced by Auto-Tune while that is on, "
             "because a chromatic grid already has a note every semitone."),
            ("auto_tune", get_autotune_label, toggle_autotune,
             "Snaps the broadcast voice to the nearest note as you speak. "
             "Retune Speed sets how fast it slides, Auto-Tune Scale sets which "
             "notes count, and it costs about a tenth of a second of delay."),
            *dials,
            ("auto_tune", get_harmony_label, go_harmony,
             "A mode of its own, NOT a dial of Auto-Tune: up to two extra "
             "voices, each an interval above yours (a third, a fifth, a sixth, "
             "a seventh, an octave) or a bass interval below it, which hums "
             "under your words. It sings with Auto-Tune on or off -- with the "
             "tuner off each copy is its plain interval from your voice -- and "
             "switching the tuner never silences it. Every copy rides in the "
             "one broadcast you send. Off is your own voice alone."),
            ("muffle", get_water_label, toggle_water,
             "Muffles the song the way being underwater does, for a player "
             "who dives while it plays."),
            (None, "Back", go_back,
             "Returns to the Music Bot menu. Nothing on this page is lost."),
        ]))
        menus.set_default_sounds(m)
        gp.add_substate(m)
        return m

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
            ("Same as the game", lambda: choose(""), None,
             "The song comes out of the same device as the game, with the "
             "equalizer, the room reverb and the underwater muffle."),
        ]
        for device in output_device.available_devices():
            label = output_device.short_name(device)
            if device == self._bot.output_device:
                label = f"{label} (current)"
            items.append((label, functools.partial(choose, device), None,
                          "Sends only the Music Bot's song to this device, so "
                          "the game can stay in the headset. The equalizer "
                          "and the room reverb stay on the game's device."))
        items.append(("Back", go_back, None,
                      "Returns to Music Bot Settings."))
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

        # What each profile does, in the words of somebody who has never met an
        # equalizer (Tab reads it). The profile table itself stays `(id, label)`
        # on the bot -- the effect slot is looked up by it -- so the sentences
        # live here beside the menu that shows them, with a default for a
        # profile added without one.
        profile_help = {
            "normal": "Every band left where the recording has it: no shaping "
                      "at all.",
            "bass_boost": "Raises everything below about 260 Hz: the low end, "
                          "a kick or a bass line, at its own level.",
            "vocal_boost": "Raises the middle, around 500 Hz and 3 kHz: the "
                           "range a voice sits in.",
            "treble_boost": "Raises everything above about 4 kHz: cymbals, "
                            "hi-hats and air.",
            "custom": "Opens Bass, Mid and Treble as three sliders: Up and "
                      "Down adjust, Page Up and Page Down by ten, Home resets "
                      "the band, Enter saves.",
        }
        m = menu_mod.Menu(self._bot.game, "Music Bot Equalizer", parrent=gp)
        items = []
        for profile, label in self._bot.EQ_PROFILES:
            items.append((make_label(profile, label), make_pick(profile, label),
                          None, profile_help.get(
                              profile,
                              "A ready-made shape of the song's bass, mid "
                              "and treble.")))
        items.append(("Back", go_back, None,
                      "Returns to Music Bot Settings."))
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
            ("Up One Semitone", step(1), None,
             "Raises the song by a semitone. A song already playing bends "
             "into the new key, and its tempo does not change."),
            ("Down One Semitone", step(-1), None,
             "Lowers the song by a semitone. A song already playing bends "
             "into the new key, and its tempo does not change."),
            ("Original Key", reset, None,
             "Puts the song back in the key it was recorded in."),
            ("Back", go_back, None,
             "Returns to Music Bot Settings."),
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
            ("Up One Semitone", step(1), None,
             "Raises your broadcast voice by a semitone: the voice that "
             "reaches the PA speakers, a cabinet's room and your own "
             "monitor. The song is not touched."),
            ("Down One Semitone", step(-1), None,
             "Lowers your broadcast voice by a semitone. The song is not "
             "touched."),
            ("Original Voice", reset, None,
             "Your own voice, untransposed."),
            ("Back", go_back, None,
             "Returns to Music Bot Settings."),
        ])
        menus.set_default_sounds(m)
        gp.add_substate(m)

    def _open_autotune_speed_menu(self):
        """How fast Auto-Tune walks the voice to a note.

        One list, fastest first: Instant is the effect (the voice snaps, and a
        slide between two notes is heard as a step), Very slow is a correction
        nobody notices until the note lands. Normal is the speed the project's
        own key changes move at, which is why it is the default. The current
        one is marked, as the equalizer profiles are.
        """
        from .. import menu as menu_mod, menus
        from . import autotune
        gp = self._bot._find_gameplay()
        if not gp:
            return

        def go_back():
            gp.pop_last_substate()
            self._open_settings_menu()

        def pick(index):
            def apply():
                self._bot.set_autotune_speed(index)
                speak(f"Retune speed: {self._bot.autotune_speed_label().lower()}.")
                m.speak_current_item()
            return apply

        def make_label(index, label):
            # A live label, the way the equalizer's profiles are drawn: the line
            # reads the bot each time it is spoken, so the "(active)" mark moves
            # to the speed just chosen instead of sitting on whichever one was
            # current when this list was built.
            def label_fn():
                current = getattr(self._bot, "autotune_speed",
                                  autotune.DEFAULT_SPEED)
                return f"{label} (active)" if index == current else label
            return label_fn

        items = []
        for index, (label, _glide, note) in enumerate(autotune.SPEEDS):
            items.append((make_label(index, label), pick(index), None, note))
        items.append(("Back", go_back, None,
                      "Returns to Music Bot Settings."))
        m = menu_mod.Menu(
            self._bot.game,
            f"Music Bot Retune Speed: {self._bot.autotune_speed_label()}",
            parrent=gp,
        )
        m.add_items(items)
        menus.set_default_sounds(m)
        gp.add_substate(m)

    def _open_autotune_scale_menu(self):
        """Which notes the voice may be moved to: a scale, and its key.

        The Settings list gains ONE line for all of this (`Auto-Tune Scale:`),
        because four lines of Auto-Tune in that list would bury the rest of the
        menu. The key is not part of the list here -- it is one line that opens
        its own twelve, and only while the scale has a key at all: a chromatic
        grid has a note on every semitone, so `Chromatic` names no key and the
        Root Note line is absent rather than present and dead. Choosing a scale
        rebuilds this list (the row comes or goes with the choice), so the key
        is there to press on the next key press instead of being discovered
        only by reopening the menu.
        """
        from .. import menu as menu_mod, menus
        from . import autotune
        gp = self._bot._find_gameplay()
        if not gp:
            return

        def go_back():
            gp.pop_last_substate()
            self._open_settings_menu()

        def pick(mode_id, says):
            def apply():
                self._bot.set_autotune_mode(mode_id)
                speak(f"Auto-tune scale: {self._bot.autotune_scale_label()}. "
                      f"{says}.")
                if autotune.is_chromatic(mode_id) == autotune.is_chromatic(mode):
                    m.speak_current_item()
                    return
                # The key row comes and goes with the scale, so the list this
                # press just changed is rebuilt: choosing Major lands the
                # cursor on the new `Root Note:` line, and choosing Chromatic
                # takes it away again rather than leaving a dead row behind.
                gp.pop_last_substate()
                self._open_autotune_scale_menu()
            return apply

        items = []
        mode = getattr(self._bot, "autotune_mode", autotune.DEFAULT_MODE)
        if not autotune.is_chromatic(mode):
            def go_root():
                gp.pop_last_substate()
                self._open_autotune_root_menu()

            root = getattr(self._bot, "autotune_root", autotune.DEFAULT_ROOT)
            items.append((f"Root Note: {autotune.root_name(root)}", go_root,
                          None,
                          "Which key the scale is in. This line exists only "
                          "while the scale has a key: a chromatic grid has a "
                          "note on every semitone, so it cannot be in D."))
        # Every scale is four fields, and the fourth is the sentence Tab reads
        # on that line: a name like "Harmonic Minor" is not an explanation, and
        # the person choosing a note set is exactly the person who has never
        # met the word.
        def make_label(mode_id, name):
            # The mark is read live, not baked into the line when the list is
            # built: choosing Minor over Major leaves this list standing, so a
            # frozen label would still say "Major (active)" and only a trip back
            # to Music Bot Settings would show the change.
            def label_fn():
                current = getattr(self._bot, "autotune_mode",
                                  autotune.DEFAULT_MODE)
                return (f"{name} (active)"
                        if mode_id == autotune.clamp_mode(current) else name)
            return label_fn

        for mode_id, name, _classes, note in autotune.SCALES:
            items.append((make_label(mode_id, name),
                          pick(mode_id, autotune.scale_says(mode_id)),
                          None, note))

        def clear_tuning():
            """Every Auto-Tune setting back to nothing, and the voice back.

                Asked for as "a button to clear the tuning": a player who has
                moved the scale, the key and the speed around wants one press
                that puts all three back where they started and takes the
                effect off, rather than three pages to undo by hand. The page
                it returns to rebuilds itself, so the three dials are gone from
                the settings list along with the switch being off.

                The HARMONY copies are left exactly as they are: they are a
                mode of their own, and a singer clearing the tuner must not
                lose the voices they set up beside it.
                """
            self._bot.set_autotune_mode(autotune.DEFAULT_MODE)
            self._bot.set_autotune_root(autotune.DEFAULT_ROOT)
            self._bot.set_autotune_speed(autotune.DEFAULT_SPEED)
            self._bot.set_autotune_amount(autotune.DEFAULT_AMOUNT)
            if getattr(self._bot, "autotune_enabled", False):
                self._bot.toggle_autotune()
            speak("Auto-Tune cleared. Your own voice again, and the scale, the "
                  "key, the retune speed and the tune amount are back at their "
                  "defaults: Chromatic in C, Normal speed, 100%. Your harmony "
                  "voices are left as they were.")
            gp.pop_last_substate()
            self._open_settings_menu()

        items.append(("Clear Auto-Tune Tuning", clear_tuning, None,
                      "Puts everything Auto-Tune was set to back the way it "
                      "came -- Chromatic in C at Normal speed -- and turns it "
                      "off, so the next press is your own voice. The harmony "
                      "voices are not touched: they are a mode of their own."))
        items.append(("Back", go_back, None,
                      "Returns to Music Bot Settings."))
        m = menu_mod.Menu(
            self._bot.game,
            f"Music Bot Auto-Tune Scale: {self._bot.autotune_scale_label()}",
            parrent=gp,
        )
        m.add_items(items)
        menus.set_default_sounds(m)
        gp.add_substate(m)

    def _open_autotune_root_menu(self):
        """The twelve keys, in note order, the current one marked."""
        from .. import menu as menu_mod, menus
        from . import autotune
        gp = self._bot._find_gameplay()
        if not gp:
            return

        def go_back():
            gp.pop_last_substate()
            self._open_autotune_scale_menu()

        def pick(index):
            def apply():
                self._bot.set_autotune_root(index)
                speak(f"Key: {autotune.root_name(index)}.")
                m.speak_current_item()
            return apply

        def make_label(index, name):
            # Read live, like the scale list's: picking a key leaves this menu
            # open, so the "(active)" mark has to follow the choice by itself.
            def label_fn():
                current = autotune.clamp_root(
                    getattr(self._bot, "autotune_root", autotune.DEFAULT_ROOT))
                return f"{name} (active)" if index == current else name
            return label_fn

        items = []
        for index, name in enumerate(autotune.ROOT_NAMES):
            items.append((make_label(index, name),
                          pick(index), None,
                          f"Puts the scale in this key: the same notes, "
                          f"started on {name} instead."))
        items.append(("Back", go_back, None,
                      "Returns to the Auto-Tune Scale list."))
        opened_on = autotune.clamp_root(getattr(self._bot, "autotune_root",
                                                 autotune.DEFAULT_ROOT))
        m = menu_mod.Menu(
            self._bot.game,
            f"Music Bot Auto-Tune Root: {autotune.root_name(opened_on)}",
            parrent=gp,
        )
        m.add_items(items)
        menus.set_default_sounds(m)
        gp.add_substate(m)

    def _open_autotune_amount_menu(self):
        """How much of the correction is used: five steps of percent.

        The one Auto-Tune choice that is taste rather than physics. The scale
        decides how far the voice may move and Retune Speed how fast it gets
        there, but a singer may legitimately want half of it -- close to the
        note with the human wobble left in -- and no note set can express that.
        100% is a ceiling, not a dial that goes further: the nearest note is the
        whole correction, so more than all of it would carry the voice past the
        note it was aiming for.

        Five rows are the quick answers and `Custom Percentage` is the rest of
        the range: it opens the client's own text field (`game.input.run`) for
        any whole number, because "63%" is a thing a player can want and no
        list of five can hold every percentage.
        """
        from .. import menu as menu_mod, menus
        from . import autotune
        gp = self._bot._find_gameplay()
        if not gp:
            return

        def go_back():
            gp.pop_last_substate()
            self._open_settings_menu()

        def pick(percent):
            def apply():
                self._bot.set_autotune_amount(percent)
                speak(f"Tune amount: {autotune.amount_label(percent)}. "
                      f"{autotune.amount_note(percent)}")
                m.speak_current_item()
            return apply

        def make_label(percent):
            # Read live, like the scale and key lists: the mark follows the
            # choice on the list that is already open.
            def label_fn():
                current = autotune.clamp_amount(
                    getattr(self._bot, "autotune_amount",
                            autotune.DEFAULT_AMOUNT))
                label = autotune.amount_label(percent)
                return f"{label} (active)" if percent == current else label
            return label_fn

        def current_amount():
            return autotune.clamp_amount(
                getattr(self._bot, "autotune_amount", autotune.DEFAULT_AMOUNT))

        def submitted_amount(text):
            """Take what was typed, or say why it was not taken.

                The field refuses anything outside 0..100 as it is typed, so a
                refusal here is text that was not a number at all -- and it
                says the value did not change instead of guessing what "half"
                meant.

                **The field is a substate and has to be taken off the stack on
                EVERY exit, an Escape with nothing typed included.** Its `ck`
                answers True for every frame it is open (that is how it keeps
                the player's keys), so a handler that forgets this leaves the
                field on top of the stack for good: no key reaches the menu or
                the player again and the game looks frozen. The search box, the
                paste-a-link field and the playlist prompt pop it in the same
                place for the same reason.
                """
            gp.pop_last_substate()
            text = "" if text is None else text
            if not str(text).strip():
                speak(f"Tune amount unchanged, {autotune.amount_label(current_amount())}.")
                return
            value = autotune.parse_amount(text)
            if value is None:
                speak(f"'{text}' is not a percentage, so the tune amount is "
                      f"still {autotune.amount_label(current_amount())}.")
                return
            self._bot.set_autotune_amount(value)
            speak(f"Tune amount: {autotune.amount_label(value)}. "
                  f"{autotune.amount_note(value)}")

        def go_custom():
            # The five rows are the common answers; a percentage between them
            # is what a player who asks for "63%" actually wants, so the row
            # opens the client's own text field (the one the search box uses)
            # rather than adding a sixth step nobody picked. The menu is left
            # underneath and comes back on Enter or Escape.
            gp.add_substate(self._bot.game.input.run(
                f"Enter a tune amount from 0 to 100 percent "
                f"(currently {autotune.amount_label(current_amount())}):",
                handeler=submitted_amount,
                min_val=0, max_val=100, msg_length=3, multiline=False,
            ))

        items = [(make_label(percent), pick(percent), None, note)
                 for percent, note in autotune.TUNE_AMOUNTS]
        items.append(("Custom Percentage", go_custom, None,
                      "Type the amount yourself, from 0 to 100. Any whole "
                      "number works, so 63% is 63% and not the step beside it."))
        items.append(("Back", go_back, None,
                      "Returns to Music Bot Settings."))
        m = menu_mod.Menu(
            self._bot.game,
            f"Music Bot Tune Amount: {self._bot.autotune_amount_label()}",
            parrent=gp,
        )
        m.add_items(items)
        menus.set_default_sounds(m)
        gp.add_substate(m)

    def _open_harmony_menu(self):
        """The Harmony page: the two copies, and which interval each sings.

        Two lines used to sit in Music Bot Settings (`Harmony 1:` and `Harmony
        2:`), and every harmony feature added since wanted a third. A page of
        its own holds them instead: the Settings list keeps one line
        (`Harmony:`), which says in a few words whether any of it is on, and the
        page under it is where the choices -- and whatever comes next -- live.

        The line is NOT one of Auto-Tune's dials. Harmony is a mode of its own:
        the tuner's switch adds and removes the three dials above it and never
        this line, and the copies sing with the tuner off (at their plain
        intervals) exactly as with it on.
        """
        from .. import menu as menu_mod, menus
        gp = self._bot._find_gameplay()
        if not gp:
            return

        def go_back():
            gp.pop_last_substate()
            self._open_settings_menu()

        def go_first():
            gp.pop_last_substate()
            self._open_autotune_harmony_menu()

        def go_second():
            gp.pop_last_substate()
            self._open_autotune_harmony_menu(second=True)

        m = menu_mod.Menu(
            self._bot.game,
            f"Music Bot Harmony: {self._bot.autotune_harmony_summary()}",
            parrent=gp,
        )
        m.add_items([
            (lambda: f"Harmony 1: {self._bot.autotune_harmony_label()}",
             go_first, None,
             "What the first copy sings: Off, a third, fifth, sixth, seventh "
             "or octave ABOVE your voice, or a bass third, fifth or octave "
             "BELOW it. With Auto-Tune tuning you the interval comes from your "
             "scale, so the copy is always in key; with Auto-Tune off it is the "
             "plain interval, measured from your own voice. Either way the "
             "tuner's switch never silences it."),
            (lambda: (f"Harmony 2: "
                      f"{self._bot.autotune_harmony_label(second=True)}"),
             go_second, None,
             "The second copy, on another interval of the same scale. A third "
             "above and a fifth above turn one voice into a three-note chord; "
             "a bass copy under either of them adds a low voice. Off leaves "
             "Harmony 1 singing alone."),
            ("Back", go_back, None,
             "Returns to Music Bot Settings."),
        ])
        menus.set_default_sounds(m)
        gp.add_substate(m)

    def _open_autotune_harmony_menu(self, second=False):
        """Which interval one harmony copy sings: a third, a fifth, an octave.

        Two copies, so two of these pages (`Harmony 1:` and `Harmony 2:` on the
        Harmony page). The interval is counted in the scale, so the copy is
        always on a note the scale has -- a third above C is E, above D it is F
        -- and `Chromatic` falls back to the major scale's spelling of each
        interval, because a grid with a note every semitone has no key to
        harmonise in. With Auto-Tune OFF there is no scale in play at all: the
        copy is the plain size of the interval (`autotune.harmony_plain_offset`),
        sung from the voice as it is, because Harmony is a mode of its own. The
        rows below `Octave` take the copy DOWN instead: a bass
        voice, mixed quieter than the lead and low-passed, so it hums under the
        words rather than doubling them (`autotune.harmony_share`).

        The copy is part of the ONE broadcast voice: it is mixed into the same
        20 ms frame (`libs/voice_chat.HarmonyVoice`), so a listener hears it
        without anything extra being sent, and a PA slot or a cinema room still
        gets exactly the frames it always did.
        """
        from .. import menu as menu_mod, menus
        from . import autotune
        gp = self._bot._find_gameplay()
        if not gp:
            return

        def go_back():
            gp.pop_last_substate()
            self._open_harmony_menu()

        def pick(interval_id):
            def apply():
                self._bot.set_autotune_harmony(interval_id, second=second)
                speak(f"Harmony {'2' if second else '1'}: "
                      f"{self._bot.autotune_harmony_label(second=second)}. "
                      f"{autotune.harmony_note(interval_id)}")
                m.speak_current_item()
            return apply

        def make_label(interval_id):
            # Read live, like the scale, key and amount lists: the mark follows
            # the choice on the list that is already open.
            def label_fn():
                attribute = "autotune_harmony2" if second else "autotune_harmony"
                current = autotune.clamp_harmony(
                    getattr(self._bot, attribute, autotune.DEFAULT_HARMONY))
                label = autotune.harmony_label(interval_id)
                return f"{label} (active)" if interval_id == current else label
            return label_fn

        items = [(make_label(interval_id), pick(interval_id), None, note)
                 for interval_id, _label, _semitones, note
                 in autotune.HARMONY_INTERVALS]
        items.append(("Back", go_back, None,
                      "Returns to the Harmony page."))
        m = menu_mod.Menu(
            self._bot.game,
            f"Music Bot Harmony {'2' if second else '1'}: "
            f"{self._bot.autotune_harmony_label(second=second)}",
            parrent=gp,
        )
        m.add_items(items)
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
            (self._bot.subtitle_label, self._bot.toggle_subtitles, None,
             "Turns the spoken subtitles on or off: the song's own words, "
             "read out as the song reaches them."),
            (self._bot.subtitle_language_label, go_language, None,
             "Which language YouTube is asked for a caption track in. The "
             "first language with a track is the one spoken."),
            (self._bot.subtitle_timing_label, go_timing, None,
             "Moves the spoken line earlier or later against the song, if "
             "the screen reader falls behind or runs ahead."),
            (lambda: self._bot.subtitle_reader.status(), read_status, None,
             "How far the reader has got, and where the words come from: "
             "the uploader's own caption track, or YouTube's automatic one."),
            ("Back", go_back, None,
             "Returns to the Music Bot menu."),
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
        items = [(make_label(key, label), make_pick(key, label), None,
                  "The order YouTube is asked for a caption track in. The "
                  "first language with a track is the one read out; songs "
                  "often have none at all.")
                 for key, label, _languages in subtitles.LANGUAGE_PREFERENCES]
        items.append(("Back", go_back, None,
                      "Returns to the Subtitles menu."))
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
            (self._bot.subtitle_timing_label, speak_timing, None,
             "How far the spoken line sits from the song, and which way it "
             "has been pushed."),
            (f"Speak {step:.1f} seconds earlier",
             shift(subtitles.OFFSET_STEP_MS), None,
             "Speaks each line half a second earlier, for a screen reader "
             "that falls behind the song."),
            (f"Speak {step:.1f} seconds later",
             shift(-subtitles.OFFSET_STEP_MS), None,
             "Speaks each line half a second later, for a screen reader that "
             "runs ahead of the song."),
            ("Reset timing", reset, None,
             "Puts the timing back to exactly where the video's own captions "
             "are timed."),
            ("Back", go_back, None,
             "Returns to the Subtitles menu."),
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
            items.append((name, make_add_cb(name), None,
                          f"Adds {title} to this playlist."))

        items.append(("Cancel", lambda: gp.pop_last_substate(), None,
                      "Leaves your playlists as they are."))
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
