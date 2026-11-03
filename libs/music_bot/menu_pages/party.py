"""The Party Sync pages of `MusicBotMenus`.

The host/guest session controls, the host's kick list, the listener's
read-only view of the host's queue, and the picker a song request is answered
from -- the one page both ends of a request use, which is why it is a page of
the bot and not of a session.

Moved out of `libs/music_bot/menus.py` on 2026-10-26 and mixed into
`MusicBotMenus`, so every caller still says `bot._menus().<method>()` and the
one-line shims in `controller.py` did not move. The state stays on
`MapMusicBot`: `self.` is this handler and `self._bot.` is the Music Bot. A
page that speaks through the module-level `speak` calls `self._say`, because
`libs/music_bot/menus.speak` is the patch point the menu tests use.

Pins: client/tests/test_party_sync.py, test_party_queue_share.py,
test_party_song_pick.py, test_party_song_request.py and
test_music_bot_menu_help.py. Rules: .agents/skills/party-sync-system/.
"""

from .. import gate
from ... import options as _options
from .. import song_requests
from ...party_sync import MAX_JAMMERS, same_player


class PartyMenus:
    """The Party Sync controls, the kick list, the queue view and the picker."""

    def open_song_pick_menu(self, request_id, items, query="", on_pick=None):
        """The picker: which of the results this request may be queued from.

                    Used by both ends - a listener whose own bot may be switched off entirely (no
                    playback, no sources, only a menu) and a host who asked their own bot. One menu and
                    one wording (`song_requests.choice_line`); `on_pick(index)` is what it means here.
                    """
        from ... import menu as menu_mod, menus
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
                self._say(picked)
                if callable(on_pick):
                    on_pick(idx)

            entries.append((label, choose))
        if not entries:
            self._say(song_requests.NO_CHOICES)
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
        from ... import menu as menu_mod, menus
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
        from ... import menu as menu_mod, menus
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
                def do_jam():
                    close_top()
                    self._open_party_jam_menu(ps)
                items.append((
                    "Who may send audio", do_jam, None,
                    "Lets a listener send their own program or instrument "
                    "into this session. Up to %d listeners at once." % MAX_JAMMERS))
            def do_cinema():
                close_top()
                self._open_party_cinema_menu(ps)
            cinema_label = (
                f"Party cinema room: jukebox {ps.cinema_cabinet}"
                if getattr(ps, "cinema_cabinet", "")
                else "Party cinema room: OFF"
            )
            items.append((
                cinema_label, do_cinema, None,
                "Sends the mixed audio of everyone jamming in this party "
                "through one cinema cabinet's speakers. People outside the "
                "party on this map hear it; party members do not, so their "
                "private feed is never doubled."))
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
            if getattr(ps, "cinema_cabinet", ""):
                items.append((
                    f"Party playing in jukebox {ps.cinema_cabinet}",
                    lambda: None, None,
                    "The host sent this party's mixed audio into that "
                    "cinema room. You keep hearing the direct party feed, "
                    "not the room."))
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

        # The external program lives here, not in the Music Bot menu: it is
        # the session's listeners who hear it, and that menu is long enough
        # for a screen reader already. A guest may capture only with the
        # host's grant -- the server drops the frames without it, so offering
        # the page would capture into silence.
        may_stream = getattr(ps, "may_stream", None)
        may_send = (ps is None or ps.role != "guest"
                    or bool(may_stream and may_stream(
                        _options.get("username", ""))))
        if ps is not None and ps.role == "guest" and may_send:
            items.append((
                "The host allows you to send audio", lambda: None, None,
                "Your captured program or instrument goes only to this "
                "Party Sync, never to the map. The host can stop it again."))
        if may_send:
            def do_external_audio():
                close_top()
                self._open_external_audio_menu()
            items.append((
                "External audio from a music program", do_external_audio, None,
                "Sends the sound of another program on this computer -- a "
                "music program, a soft synth, a browser player, or the whole "
                "computer -- to everyone who hears your music, in mono or in "
                "stereo. It opens a short page with a search for the program."))

        items.append(("Back", back_to_bot_menu, None,
                      "Returns to the Music Bot menu. Nothing is ended."))
        m.add_items(items)
        menus.set_default_sounds(m)
        gp.add_substate(m)

    def _open_party_cinema_menu(self, ps):
        """Host only: choose the cabinet that hears the party's mixed audio."""
        from ... import menu as menu_mod, menus
        from ...speech import speak
        gp, _ = self._bot._party_sync_pair()
        if gp is None:
            return
        m = menu_mod.Menu(self._bot.game, "Party cinema room", parrent=gp)
        items = []

        def close_top():
            if gp.substates and gp.substates[-1] is m:
                gp.pop_last_substate()

        def choose(cabinet, label):
            close_top()
            self._bot.party_sync_set_cinema(cabinet)
            speak(label)

        items.append((
            "Off (private party feed only)",
            lambda: choose("", "Party audio is back on the private party feed."),
            None,
            "Stops sending the party to a cinema room. Every party member "
            "keeps hearing the direct private feed."))
        ready = 0
        for cabinet_id, _anchor, room in self._bot.cinema_cabinets():
            if room is None:
                continue
            ready += 1
            try:
                where = room.summary()
            except Exception:
                where = "ready"
            items.append((
                f"Send to jukebox {cabinet_id} ({where})",
                lambda c=cabinet_id: choose(
                    c, f"Party audio is playing in jukebox {c}."),
                None,
                "People on this map who are not in the party hear the mixed "
                "audio through that cabinet's speakers. Party members keep "
                "their direct feed and do not hear the room copy."))
        if not ready:
            items.append((
                "No cinema room on this map has speakers", lambda: None, None,
                "The cabinet needs cinema speakers around it before its room "
                "can be used."))
        items.append(("Back", close_top, None,
                      "Returns to the Party Sync menu without changing the room."))
        m.add_items(items)
        menus.set_default_sounds(m)
        gp.add_substate(m)

    def _open_party_jam_menu(self, ps):
        """Host only: allow or stop each listener's own captured audio."""
        from ... import menu as menu_mod, menus
        from ...speech import speak
        gp, _ = self._bot._party_sync_pair()
        if gp is None:
            return
        if not ps or not ps.guests:
            speak("Nobody is listening right now.")
            self._open_party_sync_menu()
            return
        m = menu_mod.Menu(self._bot.game, "Who may send audio", parrent=gp)
        items = []

        def close_top():
            if gp.substates and gp.substates[-1] is m:
                gp.pop_last_substate()

        def back_to_party():
            close_top()
            self._open_party_sync_menu()

        for g in ps.guests:
            name = g["name"]
            allowed = any(same_player(name, n) for n in ps.jammers)

            def make_toggle(target=name, currently=allowed):
                def cb():
                    close_top()
                    if currently:
                        self._bot.party_sync_revoke_jam(target)
                    else:
                        self._bot.party_sync_grant_jam(target)
                return cb

            if allowed:
                label = "Stop %s sending audio" % name
                desc = ("Stops %s's program or instrument in this session at "
                        "once. They keep hearing the party." % name)
            else:
                label = "Allow %s sending audio" % name
                desc = ("Lets %s send their program or instrument to this "
                        "session only. Up to %d listeners can send at once."
                        % (name, MAX_JAMMERS))
            items.append((label, make_toggle(), None, desc))
        items.append(("Back", back_to_party, None,
                      "Returns to the Party Sync menu without changing anyone."))
        m.add_items(items)
        menus.set_default_sounds(m)
        gp.add_substate(m)

    def _open_party_kick_menu(self, ps):
        """Pick which listener to kick (host only)."""
        from ... import menu as menu_mod, menus
        from ...speech import speak
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
