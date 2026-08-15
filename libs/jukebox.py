"""Music Jukebox — a map element that plays a shared song queue.

Architecture
------------
The queue lives on the SERVER (per map), so playback keeps going no matter
which player queued the songs or leaves the map. The server broadcasts
``jukebox_play`` / ``jukebox_stop`` / ``jukebox_queue_update`` / ``jukebox_state``
events. New clients receive one server-owned live Opus stream per active
cabinet; decoding, distance attenuation, reverb, and local volume stay on the
client. If the optional server worker is unavailable, playback falls back to
the direct ffmpeg path used by the personal Music Bot.

The jukebox menu deliberately has NO broadcast/megaphone buttons — it supports
searching YouTube, direct YouTube URL input, viewing the queue, removing own
queued songs, skipping songs, adjusting volume, and staff queue clearance.
"""

import queue
import struct
import threading
import time

import cyal

from .speech import speak
from .logger import log as log_line


class JukeboxRelayReceiver(threading.Thread):
    """Decode one server-owned Opus stream without blocking the network thread."""

    PREBUFFER_FRAMES = 6
    RESUME_FRAMES = 3
    MAX_PENDING_FRAMES = 32
    NUM_BUFFERS = 32

    def __init__(self, game, source_l, source_r, volume, relay_id, stream_epoch,
                 reference_distance=8.0, max_distance=40.0):
        super().__init__(daemon=True, name=f"jukebox-relay-{relay_id}")
        from pyogg import OpusDecoder
        self.game = game
        self.source_l = source_l
        self.source_r = source_r
        self.volume = int(volume)
        self.relay_id = int(relay_id)
        self.stream_epoch = int(stream_epoch)
        self.reference_distance = float(reference_distance)
        self.max_distance = float(max_distance)
        self.running = True
        self.frames = queue.Queue(maxsize=self.MAX_PENDING_FRAMES)
        self.decoder = OpusDecoder()
        self.decoder.set_channels(2)
        self.decoder.set_sampling_frequency(48000)
        self._pool = []
        # NOTE: must NOT be named `_started` — that would shadow the private
        # `threading.Thread._started` Event (used by Thread.start()/join()) and
        # make start()/stop() raise AttributeError, killing relay playback.
        self._play_started = False
        self._last_sequence = None
        for _ in range(self.NUM_BUFFERS):
            try:
                self._pool.append(game.audio_mngr.context.gen_buffer())
            except Exception:
                break

    def set_volume(self, volume):
        self.volume = max(0, min(100, int(volume)))
        self._update_gain()

    def receive(self, sequence, payload, flags=0):
        if not self.running or not payload or len(payload) > 1275 or flags & ~0x03:
            return
        sequence = int(sequence) & 0xffff
        if self._last_sequence is not None:
            delta = (sequence - self._last_sequence) & 0xffff
            if delta == 0 or delta > 0x8000:
                return
        self._last_sequence = sequence
        if flags & 0x01:
            self._reset_queue()
        item = bytes(payload)
        try:
            self.frames.put_nowait(item)
        except queue.Full:
            try:
                self.frames.get_nowait()
            except queue.Empty:
                pass
            try:
                self.frames.put_nowait(item)
            except queue.Full:
                pass

    def _reset_queue(self):
        while True:
            try:
                self.frames.get_nowait()
            except queue.Empty:
                break

    def _reclaim(self):
        for source in (self.source_l, self.source_r):
            try:
                while source.buffers_processed > 0:
                    result = source.unqueue_buffers()
                    if result is None:
                        continue
                    try:
                        self._pool.extend(result)
                    except TypeError:
                        self._pool.append(result)
            except Exception:
                pass

    def _update_gain(self):
        try:
            audio = self.game.audio_mngr
            listener = getattr(audio, "position", None)
            # Jukebox relay audio has its own mixer slider ("jukebox"),
            # independent from the music bot / map music.
            category = audio.volume_categories.get("jukebox", [100])[0] / 100.0
            local = self.volume / 100.0
            span = max(0.0001, self.max_distance - self.reference_distance)
            for source in (self.source_l, self.source_r):
                distance_gain = 1.0
                if listener is not None:
                    pos = source.position
                    distance = sum((listener[i] - pos[i]) ** 2 for i in range(3)) ** 0.5
                    if distance >= self.max_distance:
                        distance_gain = 0.0
                    elif distance > self.reference_distance:
                        distance_gain = 1.0 - (distance - self.reference_distance) / span
                source.gain = local * category * distance_gain
        except Exception:
            pass

    def _queue_pcm(self, pcm):
        self._reclaim()
        if len(self._pool) < 2:
            return False
        from .music_bot import AudioStreamer
        left, right = AudioStreamer._split_stereo_16(pcm)
        buf_l = self._pool.pop()
        buf_r = self._pool.pop()
        try:
            buf_l.set_data(left, sample_rate=48000, format=cyal.BufferFormat.MONO16)
            buf_r.set_data(right, sample_rate=48000, format=cyal.BufferFormat.MONO16)
            self.source_l.queue_buffers(buf_l)
            self.source_r.queue_buffers(buf_r)
            return True
        except Exception:
            self._pool.extend((buf_l, buf_r))
            return False

    def run(self):
        while self.running:
            try:
                payload = self.frames.get(timeout=0.05)
            except queue.Empty:
                self._reclaim()
                self._update_gain()
                continue
            if payload is None:
                break
            try:
                pcm = bytearray(self.decoder.decode(bytearray(payload)))
                with self.game.audio_mngr.context.batch():
                    if not self._queue_pcm(pcm):
                        continue
                    self._update_gain()
                    queued = min(self.source_l.buffers_queued, self.source_r.buffers_queued)
                    stopped = (self.source_l.state != cyal.SourceState.PLAYING
                               or self.source_r.state != cyal.SourceState.PLAYING)
                    if (not self._play_started and queued >= self.PREBUFFER_FRAMES) or (
                            self._play_started and stopped and queued >= self.RESUME_FRAMES):
                        self.source_l.play()
                        self.source_r.play()
                        self._play_started = True
            except Exception as ex:
                log_line(f"[JukeboxRelay] decode/queue error: {ex}")

    def stop(self):
        self.running = False
        try:
            self.frames.put_nowait(None)
        except queue.Full:
            self._reset_queue()
            try:
                self.frames.put_nowait(None)
            except queue.Full:
                pass
        if threading.current_thread() is not self:
            self.join(timeout=1.0)
        self._reset_queue()
        for source in (self.source_l, self.source_r):
            try:
                source.stop()
                while source.buffers_queued > 0:
                    result = source.unqueue_buffers()
                    if result is not None:
                        try:
                            self._pool.extend(result)
                        except TypeError:
                            self._pool.append(result)
            except Exception:
                pass


class JukeboxPlayer:
    """Plays one song per jukebox, anchored at the jukebox's 3D position.

    Each jukebox id gets its own OpenAL source + ffmpeg streamer thread.
    """

    def __init__(self, game):
        self.game = game
        self.players = {}  # jukebox_id -> {"source", "secondary_source", "streamer", "title", "url"}
        self.volume = 65
        self._lock = threading.Lock()
        self.relay_routes = {}
        self._control_serial = 0
        # Jukebox ids awaiting a post-reload play confirmation (see
        # mark_pending_map_change). Confirmed players are kept playing
        # seamlessly; unconfirmed ones are stopped after a short grace period.
        self._pending_map_change = set()
        self._pending_map_change_serial = None

    @property
    def control_serial(self):
        with self._lock:
            return self._control_serial

    def set_volume(self, volume):
        """Set local volume level for all jukeboxes (0-100)."""
        self.volume = max(0, min(100, int(volume)))
        with self._lock:
            for p in self.players.values():
                streamer = p.get("streamer")
                if streamer is not None and hasattr(streamer, "set_volume"):
                    streamer.set_volume(self.volume)

    def play(self, jukebox_id, x, y, z, title, url, duration, volume=None, start_offset=0.0,
             playback_id=None, transport="direct", relay_id=None, stream_epoch=None,
             http_headers=None, **_kwargs):
        """Start (or seamlessly continue) the song for one jukebox at (x, y, z).

        Stereo-spatial like piano/drums: the STEREO stream is split into two
        MONO sources placed just left/right of the jukebox, so you hear a real
        stereo image up close that collapses to mono and fades with distance.
        """
        if not url and transport == "direct":
            log_line(f"[Jukebox] play({jukebox_id}) skipped: no url")
            return

        effective_volume = self.volume if volume is None else volume
        playback_key = ("id", int(playback_id)) if playback_id is not None else ("url", url)

        with self._lock:
            self._control_serial += 1
            # A play event for this jukebox confirms it is still alive after a
            # map reload — drop the pending-stop mark so playback continues
            # seamlessly instead of being torn down and rebuilt every reload.
            self._pending_map_change.discard(jukebox_id)
            existing = self.players.get(jukebox_id)
            same_relay_identity = True
            if existing is not None and transport == "relay":
                incoming_key = (
                    (int(relay_id), int(stream_epoch))
                    if relay_id is not None and stream_epoch is not None
                    else None
                )
                # If the server restarted the relay worker (new identity, same
                # song/playback), the old receiver must be torn down and the
                # new route registered — otherwise every frame from the new
                # worker is dropped and the song goes silent for clients that
                # were already connected. Fresh clients register the new route
                # on join, which is why only "old" clients lose the audio.
                same_relay_identity = existing.get("relay_key") == incoming_key
            if (
                existing is not None
                and existing.get("playback_key") == playback_key
                and existing.get("transport") == transport
                and same_relay_identity
            ):
                # Idempotent even while yt-dlp/ffmpeg is still resolving. Two map
                # sync routes must never cancel the same in-flight playback.
                src_l = existing.get("source")
                src_r = existing.get("secondary_source")
                offset = 2.5
                if src_l is not None and src_r is not None:
                    try:
                        for src, sx in ((src_l, float(x) - offset), (src_r, float(x) + offset)):
                            src.position = (sx, float(y), float(z))
                    except Exception:
                        pass
                log_line(f"[Jukebox] play({jukebox_id}) seamless continuity for {title!r}")
                return

        self.stop(jukebox_id)
        if transport == "relay_pending":
            with self._lock:
                self.players[jukebox_id] = {
                    "source": None, "secondary_source": None, "streamer": None,
                    "title": title, "url": url, "transport": transport,
                    "playback_key": playback_key,
                }
            log_line(
                f"[Jukebox] waiting for server relay {title!r} "
                f"playback={playback_id!r}"
            )
            return
        audio = getattr(self.game, "audio_mngr", None)
        if audio is None:
            log_line(f"[Jukebox] play({jukebox_id}) skipped: no audio_mngr")
            return
        try:
            src_l = audio.context.gen_source()
            src_r = audio.context.gen_source()
        except Exception:
            log_line(f"[Jukebox] play({jukebox_id}) failed: gen_source error")
            return
        offset = 2.5   # same stereo offset as piano/drums 3D-stereo sounds
        ref = 8.0      # full volume inside this distance
        maxd = 40.0    # silent at/beyond this distance
        base_gain = max(0.0, min(1.0, effective_volume / 100.0))
        try:
            for src, sx in ((src_l, float(x) - offset), (src_r, float(x) + offset)):
                src.position = (sx, float(y), float(z))
                src.rolloff_factor = 0.0   # linear fade handled per-frame
                src.reference_distance = maxd
                src.max_distance = maxd
                src.spatialize = True
                src.direct_channels = False
                src.gain = base_gain
        except Exception:
            pass
        # Room reverb: like piano/drums, the song picks up the reverb zone of
        # the place the jukebox stands in (none if the spot has no reverb).
        try:
            gameplay = getattr(self.game, "gameplay", None)
            if gameplay is not None and getattr(gameplay, "map", None) is not None:
                reverb_zone = gameplay.map.get_reverb_at(float(x), float(y), float(z))
                reverb = reverb_zone.reverb if reverb_zone and hasattr(reverb_zone, "reverb") else None
                if reverb is not None:
                    audio.efx.send(src_l, 0, reverb)
                    audio.efx.send(src_r, 0, reverb)
        except Exception:
            pass
        try:
            if transport == "relay":
                if relay_id is None or stream_epoch is None:
                    raise ValueError("missing relay identity")
                streamer = JukeboxRelayReceiver(
                    self.game, src_l, src_r, effective_volume,
                    relay_id, stream_epoch, ref, maxd,
                )
            else:
                from . import music_bot as mb
                streamer = mb.AudioStreamer(
                    self.game, url, src_l, volume=effective_volume, bot=None,
                    channels=2, spatial_pair=(src_l, src_r, ref, maxd),
                    start_offset=start_offset,
                    start_offset_received_at=time.monotonic(),
                    http_headers=http_headers,
                )
            streamer.start()
        except Exception as ex:
            for src in (src_l, src_r):
                try:
                    src.delete()
                except Exception:
                    pass
            from . import logger
            logger.log_exception(ex, f"JukeboxPlayer.play({jukebox_id}) start streamer")
            log_line(f"[Jukebox] play({jukebox_id}) failed to start streamer: {ex}")
            return
        with self._lock:
            self.players[jukebox_id] = {
                "source": src_l,
                "secondary_source": src_r,
                "streamer": streamer,
                "title": title,
                "url": url,
                "transport": transport,
                "playback_key": playback_key,
                "relay_key": (int(relay_id), int(stream_epoch)) if transport == "relay" else None,
            }
            if transport == "relay":
                self.relay_routes[(int(relay_id), int(stream_epoch))] = streamer
        log_line(
            f"[Jukebox] playing {title!r} transport={transport} "
            f"playback={playback_id!r} relay={relay_id!r}/{stream_epoch!r} "
            f"at ({x}, {y}, {z}) offset={start_offset:.1f}s url={url[:60]!r}"
        )

    def stop(self, jukebox_id):
        """Stop the song for one jukebox and free its audio source."""
        with self._lock:
            player = self.players.pop(jukebox_id, None)
        if not player:
            return
        log_line(f"[Jukebox] stop({jukebox_id}) title={player.get('title')!r}")
        streamer = player.get("streamer")
        relay_key = player.get("relay_key")
        if relay_key is not None:
            with self._lock:
                self.relay_routes.pop(relay_key, None)
        try:
            if streamer is not None:
                if isinstance(streamer, JukeboxRelayReceiver):
                    streamer.stop()
                else:
                    if hasattr(streamer, "stop"):
                        streamer.stop()
                    else:
                        streamer.running = False
                    if hasattr(streamer, "join") and threading.current_thread() is not streamer:
                        streamer.join(timeout=1.0)
        except Exception:
            pass
        for key in ("source", "secondary_source"):
            source = player.get(key)
            if source is not None:
                try:
                    audio = getattr(self.game, "audio_mngr", None)
                    if audio is not None and getattr(audio, "efx", None) is not None:
                        try:
                            audio.efx.send(source, 0, None)
                        except Exception:
                            pass
                    source.stop()
                    try:
                        drain_limit = 64
                        while source.buffers_processed > 0 and drain_limit > 0:
                            source.unqueue_buffers()
                            drain_limit -= 1
                    except Exception:
                        pass
                    source.delete()
                except Exception:
                    pass

    def stop_all(self):
        """Stop every jukebox (map change / disconnect)."""
        for jukebox_id in list(self.players.keys()):
            self.stop(jukebox_id)

    def stop_all_if_serial(self, serial):
        """Ignore a delayed map-cleanup after a newer play control has arrived."""
        with self._lock:
            unchanged = self._control_serial == serial
        if unchanged:
            self.stop_all()
        return unchanged

    def mark_pending_map_change(self, serial):
        """Mark every active player as awaiting a post-reload play event.

        After a map reload the server re-broadcasts jukebox_play for songs
        that are still playing.  Those events clear the mark (via play()) so
        the receiver keeps streaming with zero interruption.  Any player still
        unconfirmed after a short grace period is a song that truly ended or a
        jukebox that no longer exists on this map — stop it so no ghost audio
        lingers.
        """
        import threading as _threading
        with self._lock:
            pending = set(self.players.keys())
            self._pending_map_change = pending
            self._pending_map_change_serial = serial
        if not pending:
            return

        def sweep():
            time.sleep(2.0)
            with self._lock:
                if self._pending_map_change_serial != serial:
                    return  # a newer reload's mark superseded this one
                doomed = list(self._pending_map_change)
            if not doomed:
                return

            def do_stop():
                for jid in doomed:
                    with self._lock:
                        # A late play() re-confirmed it — keep playing.
                        if jid not in self._pending_map_change:
                            continue
                        self._pending_map_change.discard(jid)
                    self.stop(jid)

            if self.game is not None:
                self.game.put(do_stop)

        _threading.Thread(target=sweep, daemon=True).start()

    def receive_relay_packet(self, data):
        """Parse and enqueue a compact relay frame; safe under the network lock."""
        try:
            packet = bytes(data)
            if len(packet) < 13 or packet[0] != 1:
                return False
            relay_id, stream_epoch, sequence, flags = struct.unpack(">IIHB", packet[1:12])
            payload = packet[12:]
            if len(payload) > 1275 or flags & ~0x03:
                return False
            with self._lock:
                receiver = self.relay_routes.get((relay_id, stream_epoch))
            if receiver is None:
                return False
            receiver.receive(sequence, payload, flags)
            return True
        except Exception:
            return False

    def sync_reverb(self):
        """Re-syncs environment Reverb EFX for all active jukebox streams after map reloads."""
        gameplay = getattr(self.game, "gameplay", None)
        if gameplay is None or getattr(gameplay, "map", None) is None:
            return
        audio = getattr(self.game, "audio_mngr", None)
        if audio is None:
            return
        with self._lock:
            for jid, p in self.players.items():
                src_l = p.get("source")
                src_r = p.get("secondary_source")
                if src_l is None or src_r is None:
                    continue
                try:
                    pos = src_l.position
                    reverb_zone = gameplay.map.get_reverb_at(pos[0] + 2.5, pos[1], pos[2])
                    reverb = reverb_zone.reverb if reverb_zone and hasattr(reverb_zone, "reverb") else None
                    audio.efx.send(src_l, 0, reverb)
                    audio.efx.send(src_r, 0, reverb)
                except Exception:
                    pass


def _current_state(gp):
    """The last jukebox state payload received from the server."""
    state = getattr(gp, "jukebox_state", None)
    return state if isinstance(state, dict) else {"jukeboxes": {}}


def _closest_jukebox(gp):
    """Pick the jukebox nearest to the player from the cached server state."""
    state = _current_state(gp)
    boxes = state.get("jukeboxes", {})
    if not boxes:
        return None
    px = getattr(getattr(gp, "player", None), "x", 0) or 0
    py = getattr(getattr(gp, "player", None), "y", 0) or 0
    pz = getattr(getattr(gp, "player", None), "z", 0) or 0
    best = None
    best_dist = None
    for jid, box in boxes.items():
        bx = box.get("x", 0)
        by = box.get("y", 0)
        bz = box.get("z", 0)
        d = (px - bx) ** 2 + (py - by) ** 2 + (pz - bz) ** 2
        if best_dist is None or d < best_dist:
            best_dist = d
            best = box
    return best


def open_jukebox_menu(game, gp):
    """Open the jukebox song-queue menu."""
    from . import menu as menu_mod, menus

    jb = _closest_jukebox(gp)
    if jb is None:
        speak("There is no jukebox available here.")
        return
    jukebox_id = jb.get("id")

    def go_search():
        gp.pop_last_substate()
        _open_search_input(game, gp, jukebox_id)

    def go_direct_url():
        gp.pop_last_substate()
        _open_url_input(game, gp, jukebox_id)

    def go_skip():
        gp.pop_last_substate()
        _skip_song(game, gp, jukebox_id)

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

    menu_items = [
        ("Search YouTube and queue a song", go_search),
        ("Queue by YouTube URL", go_direct_url),
        ("Skip current song", go_skip),
        ("Adjust jukebox volume", go_volume),
        ("View queue", go_queue),
        ("Remove my queued song", go_remove),
    ]

    is_staff = bool(
        getattr(gp.player, "moderator", False)
        or (hasattr(gp.player, "hasPermission") and gp.player.hasPermission("builder"))
    )
    if is_staff:
        menu_items.append(("Clear queue and stop (Staff only)", go_clear_all))

    menu_items.append(("Cancel", lambda: gp.pop_last_substate()))

    m = menu_mod.Menu(game, "Music Jukebox", parrent=gp)
    m.add_items(menu_items)
    menus.set_default_sounds(m)
    gp.add_substate(m)


def _open_search_input(game, gp, jukebox_id):
    gp.add_substate(game.input.run(
        "Enter a song name to search YouTube:",
        handeler=lambda query: _on_search_submit(game, gp, jukebox_id, query),
    ))


def _on_search_submit(game, gp, jukebox_id, query):
    gp.pop_last_substate()
    if not (query or "").strip():
        speak("Search cancelled.")
        return
    speak("Searching YouTube...")

    def do_search():
        from .music_bot import YouTubeSearcher
        results = YouTubeSearcher.search(query.strip(), count=5)
        game.put(lambda: _show_results_menu(game, gp, jukebox_id, results))

    threading.Thread(target=do_search, daemon=True).start()


def _open_url_input(game, gp, jukebox_id):
    gp.add_substate(game.input.run(
        "Enter YouTube video URL:",
        handeler=lambda url: _on_url_submit(game, gp, jukebox_id, url),
    ))


def _on_url_submit(game, gp, jukebox_id, raw_url):
    gp.pop_last_substate()
    url = (raw_url or "").strip()
    if not url:
        speak("Cancelled.")
        return
    speak("Loading YouTube song info...")

    def do_fetch():
        try:
            import yt_dlp
            from . import logger
            ydl_opts = {'format': 'bestaudio/best', 'quiet': True, 'no_warnings': True}
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
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
            from . import logger
            logger.log_exception(ex, "Jukebox direct URL load")
            game.put(lambda: speak("Could not load YouTube URL. Please check the link."))

    threading.Thread(target=do_fetch, daemon=True).start()


def _skip_song(game, gp, jukebox_id):
    from . import consts
    game.network.send(consts.CHANNEL_MISC, "jukebox_skip", {"id": jukebox_id})


def _clear_all(game, gp, jukebox_id):
    from . import consts
    game.network.send(consts.CHANNEL_MISC, "jukebox_clear_all", {"id": jukebox_id})


def _get_or_create_jukebox_player(game, gp):
    player = getattr(gp, "jukebox_player", None)
    if player is None:
        player = gp.jukebox_player = JukeboxPlayer(game)
    return player


def _open_volume_menu(game, gp, jukebox_id):
    from . import menu as menu_mod, menus
    player = _get_or_create_jukebox_player(game, gp)
    current_vol = player.volume

    def set_vol(val):
        player.set_volume(val)
        speak(f"Jukebox volume set to {val} percent.")
        gp.pop_last_substate()
        open_jukebox_menu(game, gp)

    m = menu_mod.Menu(game, f"Jukebox Volume (Current: {current_vol}%)", parrent=gp)
    m.add_items([
        (f"100%{' (Active)' if current_vol == 100 else ''}", lambda: set_vol(100)),
        (f"80%{' (Active)' if current_vol == 80 else ''}", lambda: set_vol(80)),
        (f"65%{' (Default)' if current_vol == 65 else ''}", lambda: set_vol(65)),
        (f"50%{' (Active)' if current_vol == 50 else ''}", lambda: set_vol(50)),
        (f"30%{' (Active)' if current_vol == 30 else ''}", lambda: set_vol(30)),
        (f"10%{' (Active)' if current_vol == 10 else ''}", lambda: set_vol(10)),
        (f"Mute (0%){' (Active)' if current_vol == 0 else ''}", lambda: set_vol(0)),
        ("Back", lambda: (gp.pop_last_substate(), open_jukebox_menu(game, gp))),
    ])
    menus.set_default_sounds(m)
    gp.add_substate(m)


def _show_results_menu(game, gp, jukebox_id, results):
    from . import menu as menu_mod, menus

    if not results:
        speak("No results found.")
        return

    def make_callback(idx):
        return lambda: _pick_song(game, gp, jukebox_id, results[idx])

    m = menu_mod.Menu(game, "Search Results", parrent=gp)
    items = []
    for i, r in enumerate(results):
        dur = r.get("duration") or 0
        dur_str = f"{dur // 60}:{dur % 60:02d}" if dur else "?"
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
    from . import consts
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
    # Keep the canonical webpage URL in the server-owned queue.  yt-dlp search
    # results also contain a signed googlevideo URL, but that URL expires and
    # may be bound to the requesting client.  Every listener (including a
    # player returning from a match later) must resolve a fresh stream URL.
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
            ydl_opts = {'format': 'bestaudio/best', 'quiet': True, 'no_warnings': True}
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
    from . import menu as menu_mod, menus

    jb = _closest_jukebox(gp)
    if jb is None:
        speak("There is no jukebox available here.")
        return
    jb_state = _current_state(gp).get("jukeboxes", {}).get(jb.get("id"), {})
    current = jb_state.get("current")
    queue = jb_state.get("queue") or []

    m = menu_mod.Menu(game, "Jukebox Queue", parrent=gp)
    items = []
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
    from . import menu as menu_mod, menus

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
    from . import consts
    game.network.send(consts.CHANNEL_MISC, "jukebox_queue_remove", {
        "id": jukebox_id,
        "index": index,
    })
    speak("Your song was removed from the queue.")
