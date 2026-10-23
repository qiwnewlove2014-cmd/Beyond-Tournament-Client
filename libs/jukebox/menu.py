"""The cabinet's own menu and every line hanging off it.

Play, queue, search, a direct URL, skip, pause, repeat, shuffle, the EQ and volume
sliders, scrub, the Party Sync requests and My Song all branch from here."""

import threading

from ..audio.cinema import neighbour_line, neighbour_note
from ..speech import speak

from .cinema import (_cabinet_cinema_lock, _cabinet_cinema_mode,
                     _cabinet_cinema_reach, _cabinet_title, _cinema_detail,
                     _cinema_mode_label, _may_lock_cinema_mode,
                     _open_cinema_lock_menu, _open_cinema_mode_menu,
                     _open_cinema_reach_menu)
from . import voice
from .view import (_closest_jukebox, _current_state, _get_eq_values,
                   _get_jukebox_volume, _get_repeat_mode, _own_name,
                   _pause_menu_label, _scrub_spot)
from .widgets import _JukeboxScrubControl, _open_eq_menu, _open_volume_menu


def open_jukebox_menu(game, gp):
    """Open the jukebox song-queue menu."""
    from .. import menu as menu_mod, menus

    jb = _closest_jukebox(gp)
    if jb is None:
        speak("There is no jukebox available here.")
        return
    jukebox_id = jb.get("id")

    # Who is opening this cabinet, and what stands near it: both are asked
    # here, before the menu below, and both are reused further down (the menu
    # lines a staff member gets, and the read-out of a neighbouring cabinet).
    is_staff = bool(
        getattr(gp, "is_staff", False)
        or getattr(gp, "is_builder", False)
        or getattr(gp, "is_technician", False)
        or getattr(gp, "can_broadcast_megaphone", False)
    )
    nearby = neighbour_line(game, jukebox_id)

    def go_cancel():
        gp.pop_last_substate()

    def go_search():
        gp.pop_last_substate()
        _open_search_input(game, gp, jukebox_id)

    def go_direct_url():
        gp.pop_last_substate()
        _open_url_input(game, gp, jukebox_id)

    def go_skip():
        gp.pop_last_substate()
        _skip_song(game, gp, jukebox_id)

    def go_stop():
        gp.pop_last_substate()
        _stop_playback(game, gp, jukebox_id)

    def go_pause():
        gp.pop_last_substate()
        _toggle_pause(game, gp, jukebox_id)

    def go_repeat():
        gp.pop_last_substate()
        _toggle_repeat(game, gp, jukebox_id)

    def go_shuffle():
        gp.pop_last_substate()
        _shuffle_queue(game, gp, jukebox_id)

    def go_volume():
        gp.pop_last_substate()
        _open_volume_menu(game, gp, jukebox_id)

    def go_queue():
        gp.pop_last_substate()
        _view_queue(game, gp)

    def go_remove():
        gp.pop_last_substate()
        _remove_my_song(game, gp)

    def go_clear_all():
        gp.pop_last_substate()
        _clear_all(game, gp, jukebox_id)

    def go_eq():
        gp.pop_last_substate()
        _open_eq_menu(game, gp, jukebox_id)

    repeat_names = {"off": "off", "one": "repeat one", "all": "repeat all"}

    def _repeat_label():
        # Callable label: evaluated when the menu speaks, so it always shows
        # the latest repeat mode from the cached server state.
        mode = _get_repeat_mode(gp, jukebox_id)
        return f"Toggle repeat mode (now: {repeat_names.get(mode, 'off')})"

    def _eq_label():
        values = _get_eq_values(gp, jukebox_id)
        return (
            "Sound profile EQ "
            f"(Bass {values['bass']}%, Mid {values['mid']}%, "
            f"Treble {values['treble']}%)"
        )

    def _volume_label():
        vol = _get_jukebox_volume(gp, jukebox_id)
        return f"Adjust jukebox volume (now: {vol}%)"

    def go_cinema_status():
        speak(_cinema_detail(game, gp, jukebox_id))

    def go_cinema_mode():
        gp.pop_last_substate()
        _open_cinema_mode_menu(game, gp, jukebox_id)

    def go_cinema_reach():
        gp.pop_last_substate()
        _open_cinema_reach_menu(game, gp, jukebox_id)

    def go_cinema_lock():
        gp.pop_last_substate()
        _open_cinema_lock_menu(game, gp, jukebox_id)

    def speak_lock():
        owner, _coded = lock_now
        speak(f"This cabinet's cinema mode is locked by {owner}. Staff who know "
              "its code can change the mode; the room is heard exactly as it is.")

    def _voice_label():
        # The listener's own switch, not the cabinet's: a cabinet says its lines
        # to whoever wants to hear them. Callable like the other (now: …) lines,
        # so the menu always reads the saved choice, and worded like the Music
        # Bot's ``Cinema rooms:`` pair.
        if voice.voice_enabled():
            return "Cabinet voice: ON (you hear the cabinet speak)"
        return "Cabinet voice: OFF (cabinets stay silent)"

    def go_voice():
        enabled = not voice.voice_enabled()
        voice.set_voice_enabled(enabled)
        if not enabled:
            # The line this cabinet is saying right now stops with the switch,
            # rather than finishing on a listener who just asked for silence.
            voice.release(game)
            speak("The cabinets stay silent.")
        else:
            speak("You hear the cabinet speak again.")

    mode_now = _cabinet_cinema_mode(gp, jukebox_id)
    lock_now = _cabinet_cinema_lock(gp, jukebox_id)

    menu_items = [
        ("Search YouTube and queue a song", go_search),
        ("Queue by YouTube URL or livestream", go_direct_url),
        (lambda: _pause_menu_label(gp, jukebox_id), go_pause),
    ]

    # The scrub sits under Pause/Resume because that is what it is built on: reaching for an arrow
    # pauses the cabinet for the room, the needle moves while nothing is sounding (the only way a
    # move costs nothing), and it plays on by itself once the hand stops. The line IS the control,
    # and it is offered only when there is a needle to move.
    if _scrub_spot(gp, jukebox_id) is not None:
        scrub = _JukeboxScrubControl(game, gp, jukebox_id)
        menu_items.append((scrub.label, scrub))

    menu_items.extend([
        ("Skip current song", go_skip),
        ("Stop playback", go_stop),
        (_repeat_label, go_repeat),
        ("Shuffle queue", go_shuffle),
        (_volume_label, go_volume),
        ("View queue", go_queue),
        ("Remove my queued song", go_remove),
    ])

    if is_staff:
        menu_items.append((_eq_label, go_eq))
        menu_items.append(("Clear queue and stop (Staff only)", go_clear_all))
        # What this cabinet plays through, said at the cabinet itself - a *staff* read-out, like the
        # lines under it. A room is heard, and hearing it needs no account of its mode or plumbing.
        menu_items.append((f"Cinema: {_cinema_mode_label(mode_now)}", go_cinema_status))
        # The cabinets around this one, read where a person is standing: two cabinets close enough to
        # share the speakers between them is the one thing a map does not show by itself. Staff's
        # because it names another element by its id.
        if nearby:
            menu_items.append((nearby, lambda: speak(neighbour_note(game, jukebox_id))))
        menu_items.append((f"Set cinema mode (now: {mode_now})", go_cinema_mode))
        # How big this room is. It sits under the mode because it answers the
        # next question a staff member asks at a cabinet: the room is on, but
        # the speakers down the hall are silent -- are they mine?
        menu_items.append((f"Set cinema reach (now: {_cabinet_cinema_reach(gp, jukebox_id):.0f} m)",
                           go_cinema_reach))
    # A locked cabinet says who holds it and how to get in, and only the
    # accounts that may lock one get the line that manages it -- a menu line
    # that exists is a line that works (the Server answers the same rank).
    if lock_now:
        owner, coded = lock_now
        if _may_lock_cinema_mode(gp) or owner == _own_name(gp):
            menu_items.append((f"Manage cinema mode lock (owner: {owner})…",
                               go_cinema_lock))
        elif coded:
            menu_items.append((f"Cinema mode is locked by {owner} "
                               "(the code opens one change)", speak_lock))
    elif _may_lock_cinema_mode(gp):
        menu_items.append(("Lock cinema mode (set a code)…", go_cinema_lock))

    # Last, beside the cinema lock: a listening choice for the person standing
    # here, not a setting of the cabinet (it takes nothing from anyone else).
    menu_items.append((_voice_label, go_voice))

    # Staff may take the cabinet's own switch -- at the end, beside Cancel, like
    # the other lines that change the cabinet rather than listen to it. The map's
    # power is the supply; this is the appliance, and a shared cabinet is staff's
    # to close. Players only ever get the way on.
    if bool(getattr(gp, "is_staff", False) or getattr(gp, "is_builder", False)):
        def go_switch_off():
            gp.pop_last_substate()
            network = getattr(game, "network", None)
            if network is not None:
                from .. import consts
                network.send(consts.CHANNEL_MISC, "jukebox_switch",
                             {"id": jukebox_id, "on": False})
            speak("Switching the cabinet off.")
        menu_items.append(("Turn off the cabinet (Staff only)", go_switch_off))

    menu_items.append(("Cancel", go_cancel))

    # The title says which thing this is: a plain cabinet, or one playing
    # through a room. Read once here (the menu's own ``Cinema:`` line stays a
    # callable, so it is still fresh every time the menu speaks).
    m = menu_mod.Menu(game, _cabinet_title(game, gp, jukebox_id, mode_now),
                      parrent=gp)
    m.add_items(menu_items)
    menus.set_default_sounds(m)
    gp.add_substate(m)


def open_jukebox_switch_menu(game, gp, jukebox_id):
    """The small screen a dark cabinet answers with: turn it on, or leave it.

    A cabinet whose own switch is off is not a cabinet with a queue: every line
    the normal menu carries would refuse, so the interact key gets the switch
    itself and nothing else. Whether the switch may be thrown at all -- and by
    whom, for going off -- is the Server's answer, never this menu's.
    """
    from .. import menu as menu_mod, menus

    def go_cancel():
        gp.pop_last_substate()

    def go_on():
        gp.pop_last_substate()
        network = getattr(game, "network", None)
        if network is not None:
            from .. import consts
            network.send(consts.CHANNEL_MISC, "jukebox_switch",
                         {"id": jukebox_id, "on": True})
        speak("Turning the cabinet on.")

    menu_items = [
        ("Turn on the cabinet", go_on),
        ("Cancel", go_cancel),
    ]
    m = menu_mod.Menu(game, "Music Jukebox", parrent=gp)
    m.add_items(menu_items)
    menus.set_default_sounds(m)
    gp.add_substate(m)


def _open_search_input(game, gp, jukebox_id):
    gp.add_substate(game.input.run(
        "Enter a song name or livestream to search YouTube:",
        handeler=lambda query: _on_search_submit(game, gp, jukebox_id, query),
    ))


def _on_search_submit(game, gp, jukebox_id, query):
    gp.pop_last_substate()
    if not (query or "").strip():
        speak("Search cancelled.")
        return
    speak("Searching YouTube...")

    def do_search():
        from ..music_bot import YouTubeSearcher
        results = YouTubeSearcher.search(query.strip(), count=5)
        game.put(lambda: _show_results_menu(game, gp, jukebox_id, results))

    threading.Thread(target=do_search, daemon=True).start()


def _open_url_input(game, gp, jukebox_id):
    gp.add_substate(game.input.run(
        "Enter YouTube video URL (or paste a livestream link):",
        handeler=lambda url: _on_url_submit(game, gp, jukebox_id, url),
    ))


def _on_url_submit(game, gp, jukebox_id, raw_url):
    gp.pop_last_substate()
    url = (raw_url or "").strip()
    if not url:
        speak("Cancelled.")
        return
    # The jukebox streams through the server's YouTube relay, so only YouTube links are
    # supported. Other yt-dlp sites would resolve here and then be mangled into a bogus
    # youtube.com/watch?v=<foreign-id> queue entry that can never play.
    if url and not url.startswith(("http://", "https://")):
        # Be kind to links pasted without a scheme.
        url = "https://" + url
    try:
        from urllib.parse import urlparse
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        host = ""
    if not url.startswith(("http://", "https://")) or not (
            host == "youtube.com" or host.endswith(".youtube.com") or host == "youtu.be"):
        speak("Only YouTube links are supported on the jukebox.")
        return
    speak("Loading YouTube song info...")

    def do_fetch():
        try:
            import yt_dlp
            from .. import logger
            # noplaylist: a shared link with &list=... must resolve to the ONE
            # video, not the whole playlist (otherwise the playlist name, a
            # guessed duration, and the playlist URL all get queued as a song).
            ydl_opts = {
                'format': 'bestaudio/best',
                'quiet': True,
                'no_warnings': True,
                'noplaylist': True,
                'extractor_args': {'youtube': {'player_client': ['android', 'web']}},
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
            # A pure /playlist?list= link still resolves to the whole playlist
            # even with noplaylist — queue its first video instead.
            if info and 'entries' in info:
                entries = [e for e in (info.get('entries') or []) if e]
                if not entries:
                    raise ValueError("empty playlist")
                info = entries[0]
            title = info.get('title', 'YouTube Audio')
            duration = int(info.get('duration') or 300)
            webpage_url = info.get('webpage_url') or ""
            if not _is_canonical_youtube(webpage_url) and info.get('id'):
                webpage_url = f"https://www.youtube.com/watch?v={info['id']}"
            if not webpage_url:
                webpage_url = url

            def do_send():
                _queue_song(game, gp, jukebox_id, title, webpage_url, duration)

            game.put(do_send)
        except Exception as ex:
            from .. import logger
            logger.log_exception(ex, "Jukebox direct URL load")
            game.put(lambda: speak("Could not load YouTube URL. Please check the link."))

    threading.Thread(target=do_fetch, daemon=True).start()


def _skip_song(game, gp, jukebox_id):
    from .. import consts
    game.network.send(consts.CHANNEL_MISC, "jukebox_skip", {"id": jukebox_id})


def _clear_all(game, gp, jukebox_id):
    from .. import consts
    game.network.send(consts.CHANNEL_MISC, "jukebox_clear_all", {"id": jukebox_id})


def _stop_playback(game, gp, jukebox_id):
    from .. import consts
    game.network.send(consts.CHANNEL_MISC, "jukebox_stop", {"id": jukebox_id})


def _toggle_pause(game, gp, jukebox_id):
    """Ask the server to toggle shared playback without trusting client state."""
    from .. import consts
    game.network.send(
        consts.CHANNEL_MISC, "jukebox_toggle_pause", {"id": jukebox_id}
    )


def _toggle_repeat(game, gp, jukebox_id):
    from .. import consts
    order = ("off", "one", "all")
    current = _get_repeat_mode(gp, jukebox_id)
    nxt = order[(order.index(current) + 1) % len(order)]
    # The server speaks the confirmation and the queue update refreshes the
    # cached mode shown by the menu label.
    game.network.send(consts.CHANNEL_MISC, "jukebox_set_repeat", {
        "id": jukebox_id,
        "repeat": nxt,
    })


def _shuffle_queue(game, gp, jukebox_id):
    from .. import consts
    game.network.send(consts.CHANNEL_MISC, "jukebox_shuffle", {"id": jukebox_id})


def _show_results_menu(game, gp, jukebox_id, results):
    from .. import menu as menu_mod, menus

    if not results:
        speak("No results found.")
        return

    def make_callback(idx):
        return lambda: _pick_song(game, gp, jukebox_id, results[idx])

    m = menu_mod.Menu(game, "Search Results", parrent=gp)
    items = []
    for i, r in enumerate(results):
        # int(): yt-dlp flat search returns durations as float, and the
        # '02d' format only accepts ints — a float here used to crash the
        # whole results menu before it opened.
        dur = int(r.get("duration") or 0)
        is_live = r.get("is_live", False) or r.get("live_status") == "is_live"
        if is_live:
            dur_str = "LIVE"
        elif dur:
            dur_str = f"{dur // 60}:{dur % 60:02d}"
        else:
            dur_str = "?"
        title = r.get("title", "Unknown")
        items.append((f"{title} ({dur_str})", make_callback(i)))
    items.append(("Cancel", lambda: gp.pop_last_substate()))
    m.add_items(items)
    menus.set_default_sounds(m)
    gp.add_substate(m)


def _is_canonical_youtube(url):
    """True when the URL is a stable https youtube.com / youtu.be page URL."""
    try:
        from urllib.parse import urlparse
        host = urlparse(url).hostname or ""
        return (url or "").startswith("https://") and (
            host == "youtube.com" or host.endswith(".youtube.com") or host == "youtu.be"
        )
    except Exception:
        return False


def _queue_song(game, gp, jukebox_id, title, url, duration):
    from .. import consts
    game.network.send(consts.CHANNEL_MISC, "jukebox_queue_add", {
        "id": jukebox_id,
        "title": title,
        "url": url,
        "duration": duration,
    })
    speak(f"Queued {title} on the jukebox.")


def _pick_song(game, gp, jukebox_id, result):
    gp.pop_last_substate()
    title = result.get("title", "Unknown Song")
    duration = int(result.get("duration") or 0)
    if duration < 5:
        duration = 300
    # Keep the canonical webpage URL in the server-owned queue. yt-dlp search results also
    # contain a signed googlevideo URL, but that URL expires and may be bound to the
    # requesting client. Every listener (including a player returning from a match later) must
    # resolve a fresh stream URL.
    url = result.get("webpage_url") or ""
    if not url and result.get("id"):
        url = f"https://www.youtube.com/watch?v={result['id']}"
    if not url and _is_canonical_youtube(result.get("url", "")):
        url = result.get("url")
    if url:
        _queue_song(game, gp, jukebox_id, title, url, duration)
        return
    # Only a signed googlevideo URL remains.  Resolve its canonical page URL in
    # the background so the queued URL never expires mid-song (403).
    direct_url = result.get("url", "")
    if not direct_url:
        speak("Could not resolve the audio stream for that song.")
        return
    speak("Resolving song link...")

    def do_resolve():
        canonical = direct_url
        try:
            import yt_dlp
            ydl_opts = {
                'format': 'bestaudio/best',
                'quiet': True,
                'no_warnings': True,
                'extractor_args': {'youtube': {'player_client': ['android', 'web']}},
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(direct_url, download=False)
                if info:
                    vid = info.get('id') or ""
                    wp = info.get('webpage_url') or ""
                    if wp and _is_canonical_youtube(wp):
                        canonical = wp
                    elif vid:
                        canonical = f"https://www.youtube.com/watch?v={vid}"
        except Exception:
            pass
        game.put(lambda: _queue_song(game, gp, jukebox_id, title, canonical, duration))

    threading.Thread(target=do_resolve, daemon=True).start()


def _view_queue(game, gp):
    from .. import menu as menu_mod, menus

    jb = _closest_jukebox(gp)
    if jb is None:
        speak("There is no jukebox available here.")
        return
    jb_state = _current_state(gp).get("jukeboxes", {}).get(jb.get("id"), {})
    current = jb_state.get("current")
    queue = jb_state.get("queue") or []

    m = menu_mod.Menu(game, "Jukebox Queue", parrent=gp)
    items = []
    repeat_names = {"off": "Off", "one": "Repeat one", "all": "Repeat all"}
    items.append((
        f"Repeat mode: {repeat_names.get(jb_state.get('repeat') or 'off', 'Off')}",
        lambda: None,
    ))
    if current:
        items.append((
            f"Now playing: {current.get('title', 'Unknown')} "
            f"(requested by {current.get('queuedBy', '?')})",
            lambda: None,
        ))
    if not queue:
        items.append(("The queue is empty. Search YouTube to add a song.", lambda: None))
    for i, song in enumerate(queue):
        items.append((
            f"{i + 1}. {song.get('title', 'Unknown')} "
            f"(requested by {song.get('queuedBy', '?')})",
            lambda: None,
        ))
    items.append(("Back", lambda: (gp.pop_last_substate(), open_jukebox_menu(game, gp))))
    m.add_items(items)
    menus.set_default_sounds(m)
    gp.add_substate(m)


def _remove_my_song(game, gp):
    from .. import menu as menu_mod, menus

    jb = _closest_jukebox(gp)
    if jb is None:
        speak("There is no jukebox available here.")
        return
    jukebox_id = jb.get("id")
    queue = _current_state(gp).get("jukeboxes", {}).get(jukebox_id, {}).get("queue") or []
    me = getattr(getattr(gp, "player", None), "name", "")
    mine = [(i, s) for i, s in enumerate(queue) if s.get("queuedBy") == me]

    def make_callback(idx):
        return lambda: _do_remove(game, gp, jukebox_id, idx)

    m = menu_mod.Menu(game, "Remove My Song", parrent=gp)
    items = []
    if not mine:
        items.append(("You have no songs in the queue.", lambda: None))
    for i, song in mine:
        items.append((song.get("title", "Unknown"), make_callback(i)))
    items.append(("Back", lambda: (gp.pop_last_substate(), open_jukebox_menu(game, gp))))
    m.add_items(items)
    menus.set_default_sounds(m)
    gp.add_substate(m)


def _do_remove(game, gp, jukebox_id, index):
    gp.pop_last_substate()
    from .. import consts
    game.network.send(consts.CHANNEL_MISC, "jukebox_queue_remove", {
        "id": jukebox_id,
        "index": index,
    })
    speak("Your song was removed from the queue.")
