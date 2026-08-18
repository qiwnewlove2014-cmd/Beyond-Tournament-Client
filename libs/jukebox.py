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
        self.created_at = time.monotonic()
        self.last_packet_at = None
        # When audio last made REAL progress (a buffer successfully queued
        # into OpenAL). The auto-recovery watchdog compares this against
        # last_packet_at to catch "frames keep arriving but no sound" states
        # (dead sources after an audio device switch) that the packet-based
        # stall check can never see.
        self.last_audio_activity = None
        # When OpenAL last CONSUMED a buffer (a source reported
        # buffers_processed > 0). This is the ground truth of audible
        # playback: a slowly starving stream (frames arriving, buffers
        # queueing, but the speaker underrunning) stays invisible to every
        # other check — only consumption proves sound is coming out.
        self.last_output_at = None
        # Total frames accepted through the sequence gate. The starvation
        # watchdog compares this over a rolling window: a trickling channel
        # (a few frames every few seconds) keeps every liveness check green
        # while the listener hears mostly silence — only the arrival RATE
        # exposes it (a healthy stream delivers 25 fps).
        self.received_frames = 0
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
        self.last_packet_at = time.monotonic()
        self.received_frames += 1
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
                if source.buffers_processed > 0:
                    # OpenAL only marks buffers processed while a source is
                    # audibly playing them out — this is the proof of life
                    # the underrun watchdog depends on.
                    self.last_output_at = time.monotonic()
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
            self.last_audio_activity = time.monotonic()
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
        # Frames buffered for relay routes whose jukebox_play event is still
        # sitting on the deferred game queue (see pend_relay_route) — this is
        # what keeps the first fraction of a song (its intro) from being
        # dropped between the network thread and the main loop.
        self._relay_pending = {}
        self._control_serial = 0
        # Jukebox ids awaiting a post-reload play confirmation (see
        # mark_pending_map_change). Confirmed players are kept playing
        # seamlessly; unconfirmed ones are stopped after a short grace period.
        self._pending_map_change = set()
        self._pending_map_change_serial = None
        self._last_recovery_request_at = 0.0
        # Consecutive relay recovery failures per jukebox + sticky
        # direct-playback deadlines (see update() — the cure for "sound dies
        # until a full relogin": map changes keep the same ENet peer, only a
        # fresh connection or local direct playback restores audio).
        self._relay_fail_counts = {}
        self._direct_fallback_until = {}
        # Consecutive warm-up un-stick attempts per stalled jukebox (see
        # update(): failed un-sticks escalate to a full rebuild early).
        self._stall_unsticks = {}

    RELAY_STARTUP_TIMEOUT = 7.0
    RELAY_STALL_TIMEOUT = 5.0
    # A stall that survived a warm-up un-stick attempt escalates to a full
    # rebuild (chronic loss of the unreliable relay channel).
    RELAY_HARD_STALL_TIMEOUT = 12.0
    # Frames still arriving but OpenAL making no audible progress for this
    # long -> full rebuild (dead sources / lost context after an audio device
    # switch — the packet-based stall check can never see this state).
    RELAY_AUDIO_STALL_TIMEOUT = 10.0
    # Playback started but the speakers stopped CONSUMING buffers for this
    # long -> the stream is slowly starving (underrunning): frames arrive
    # just often enough to dodge the packet stall check and buffers still
    # queue, yet the listener hears silence or a stutter loop. Only real
    # output consumption proves otherwise.
    RELAY_OUTPUT_STALL_TIMEOUT = 8.0
    # Frame-rate starvation: a trickling channel (a few frames every few
    # seconds) passes every liveness check above — packets arrive, buffers
    # queue, speakers consume the tiny bursts — while the listener hears a
    # sped-up stutter that fades to silence. A healthy relay delivers 25
    # frames/second; below half of that over a 10s window the channel is
    # starving and the rebuild ladder (ending in direct TCP playback) must
    # kick in MID-SONG instead of waiting for the next song.
    RELAY_STARVE_WINDOW = 10.0
    RELAY_STARVE_MIN_FPS = 12.5
    # A relay_pending placeholder whose follow-up play event never landed
    # (server worker died mid-retry) asks for a resync after this long.
    RELAY_PENDING_TIMEOUT = 15.0
    RECOVERY_COOLDOWN = 5.0
    # A stall normally recovers via resync + warm-up replay. If this many
    # consecutive un-stick attempts fail to hold, rebuild immediately instead
    # of waiting for the hard-stall timeout (observed after map reloads: the
    # warm-up blip plays but live frames never follow — only a fresh
    # receiver fixes that, so get there in ~10s instead of 15).
    RELAY_UNSTICK_TRIES = 2
    # When relay recovery fails this many times in a row for one jukebox, the
    # client plays that song directly over HTTP/ffmpeg (TCP) instead. Chronic
    # loss of the unreliable relay channel on one player's ENet peer can never
    # be fixed by resyncing — the warm-up frames use the same lost channel —
    # and used to leave that player silent until a full relogin.
    RELAY_DIRECT_FALLBACK_AFTER = 3
    # Direct stays sticky for this long (or until a map change) so later songs
    # don't re-pay the failing-relay window; relay is then tried again.
    DIRECT_FALLBACK_TTL = 10 * 60.0
    # Full parse_map reloads can cross the ordered map channel and the misc
    # response channel.  The observed state reply can arrive just over two
    # seconds later, so do not destroy the healthy receiver prematurely.
    MAP_RELOAD_CONFIRM_TIMEOUT = 4.0
    # A pending relay route that never turns into a real receiver (play was
    # rejected, or the server switched relay identity) is swept after this.
    RELAY_PENDING_TTL = 10.0

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

        # Emergency direct fallback: when this connection chronically loses
        # the unreliable relay channel, play locally over HTTP instead of
        # waiting for relay frames that never arrive. Sticky until the
        # deadline (or a map change) so later songs stay audible too.
        if transport in ("relay", "relay_pending"):
            deadline = self._direct_fallback_until.get(jukebox_id)
            if deadline is not None and time.monotonic() < deadline:
                if relay_id is not None and stream_epoch is not None:
                    try:
                        with self._lock:
                            self._relay_pending.pop((int(relay_id), int(stream_epoch)), None)
                    except (TypeError, ValueError):
                        pass
                transport = "direct"

        effective_volume = self.volume if volume is None else volume
        playback_key = ("id", int(playback_id)) if playback_id is not None else ("url", url)
        # Kept so the emergency direct fallback can re-start this exact song
        # from its current wall-clock position without another server event.
        play_params = {
            "x": float(x), "y": float(y), "z": float(z),
            "title": title, "url": url, "duration": int(duration or 0),
            "start_offset": float(start_offset or 0.0),
            "http_headers": http_headers,
            "received_at": time.monotonic(),
        }

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

        # Make-before-break: a relay_pending event must not kill a working
        # stream for the same song. Map reloads re-offer the relay
        # ("wait for it") while the listener is happily playing direct —
        # stopping that stream made ~10s of silence until the relay actually
        # became ready. The same applies to a LIVE relay receiver when the
        # server's retry notice races in: the worker may still be streaming
        # (frames keep arriving), and tearing the receiver down would cut the
        # song for no reason. Keep the live stream running; the ready relay
        # event (or the stall watchdogs / direct fallback) switches streams on
        # its own.
        if transport == "relay_pending":
            with self._lock:
                existing_entry = self.players.get(jukebox_id)
                keep_live = False
                if (
                    existing_entry is not None
                    and existing_entry.get("playback_key") == playback_key
                ):
                    existing_streamer = existing_entry.get("streamer")
                    if existing_entry.get("transport") == "direct":
                        keep_live = bool(
                            existing_streamer is not None
                            and hasattr(existing_streamer, "is_alive")
                            and existing_streamer.is_alive()
                        )
                    elif existing_entry.get("transport") == "relay":
                        last_packet = getattr(existing_streamer, "last_packet_at", None)
                        keep_live = bool(
                            existing_streamer is not None
                            and hasattr(existing_streamer, "is_alive")
                            and existing_streamer.is_alive()
                            and last_packet is not None
                            and time.monotonic() - float(last_packet) < 2.0
                        )
            if keep_live:
                log_line(
                    f"[Jukebox] play({jukebox_id}) keeping live "
                    f"{existing_entry.get('transport')} stream for {title!r} "
                    f"while the relay readies"
                )
                return

        self.stop(jukebox_id)
        if transport == "relay_pending":
            with self._lock:
                self.players[jukebox_id] = {
                    "source": None, "secondary_source": None, "streamer": None,
                    "title": title, "url": url, "transport": transport,
                    "playback_key": playback_key,
                    "play_params": play_params,
                    # Without a timestamp the update() watchdog computed this
                    # placeholder's age as 0 on every frame, so a placeholder
                    # whose follow-up relay event never landed waited silently
                    # forever (the "must relog after a map reload" bug).
                    "created_at": time.monotonic(),
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
                "created_at": time.monotonic(),
                "play_params": play_params,
            }
            if transport == "relay":
                self.relay_routes[(int(relay_id), int(stream_epoch))] = streamer
                # Take over any frames buffered while this receiver was being
                # created and flush them UNDER THE SAME LOCK: a live frame on
                # the network thread must not slip in first and make the
                # sequence gate reject the buffered intro frames.
                pending = self._relay_pending.pop((int(relay_id), int(stream_epoch)), None)
                if pending is not None:
                    for seq, payload, flags in pending["frames"]:
                        streamer.receive(seq, payload, flags)
        log_line(
            f"[Jukebox] playing {title!r} transport={transport} "
            f"playback={playback_id!r} relay={relay_id!r}/{stream_epoch!r} "
            f"at ({x}, {y}, {z}) offset={start_offset:.1f}s url={url[:60]!r}"
        )

    def stop(self, jukebox_id, playback_id=None):
        """Stop the song for one jukebox and free its audio source."""
        with self._lock:
            existing = self.players.get(jukebox_id)
            if existing is not None and playback_id is not None:
                try:
                    expected_key = ("id", int(playback_id))
                except (TypeError, ValueError):
                    return False
                if existing.get("playback_key") != expected_key:
                    return False
            player = self.players.pop(jukebox_id, None)
        if not player:
            return False
        log_line(f"[Jukebox] stop({jukebox_id}) title={player.get('title')!r}")
        streamer = player.get("streamer")
        relay_key = player.get("relay_key")
        if relay_key is not None:
            with self._lock:
                self.relay_routes.pop(relay_key, None)
                self._relay_pending.pop(relay_key, None)
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
        return True

    def stop_all(self):
        """Stop every jukebox (map change / disconnect).

        Clears the pending map-change marks too, so any in-flight sweep from an
        earlier reload can never act on a jukebox created after this teardown.
        """
        with self._lock:
            self._pending_map_change = set()
            self._pending_map_change_serial = None
            ids = list(self.players.keys())
        for jukebox_id in ids:
            self.stop(jukebox_id)

    def update(self):
        """Recover a jukebox stream that stopped without a stop packet.

        Relay frames are intentionally unreliable.  A temporary loss should
        normally be hidden by the receiver's buffer, but a dead receiver or a
        route that no longer receives frames used to leave an existing client
        permanently silent until reconnect.  The server's ``jukebox_resync``
        reply includes both the authoritative play state and relay warm-up
        frames, so it is the safe recovery authority.

        Recovery is layered so every failure mode ends in a full rebuild
        instead of an infinite silent wait:
        * frames stopped arriving -> resync + warm-up un-stick (5s), escalating
          to a full rebuild if the stall survives to 12s;
        * frames keep arriving but OpenAL makes no audible progress -> full
          rebuild after 10s;
        * a relay_pending placeholder whose follow-up play event never lands
          -> resync after 15s.
        """
        now = time.monotonic()
        rebuilds = []  # [(jukebox_id, reason)]
        stalled_ids = []  # relays relying on a warm-up un-stick this cycle
        needs_resync = False
        with self._lock:
            # Drop pending relay routes that never became receivers (play
            # rejected, or the server switched relay identity mid-flight).
            stale_pending = [
                key for key, entry in self._relay_pending.items()
                if now - entry["at"] > self.RELAY_PENDING_TTL
            ]
            for key in stale_pending:
                del self._relay_pending[key]
            for jukebox_id, entry in list(self.players.items()):
                transport = entry.get("transport")
                streamer = entry.get("streamer")
                created_at = float(entry.get("created_at") or now)
                age = now - created_at
                if transport == "relay" and streamer is not None:
                    alive = streamer.is_alive() if hasattr(streamer, "is_alive") else True
                    last_packet = getattr(streamer, "last_packet_at", None)
                    stall_age = (now - float(last_packet)) if last_packet is not None else None
                    no_packets = (
                        age >= self.RELAY_STARTUP_TIMEOUT and last_packet is None
                    ) or (
                        stall_age is not None and stall_age >= self.RELAY_STALL_TIMEOUT
                    )
                    last_audio = getattr(streamer, "last_audio_activity", None)
                    stuck_audio = bool(
                        alive
                        and last_packet is not None
                        and stall_age is not None and stall_age < 1.0
                        and getattr(streamer, "_play_started", False)
                        and last_audio is not None
                        and now - float(last_audio) >= self.RELAY_AUDIO_STALL_TIMEOUT
                    )
                    # Underrun watchdog: frames may arrive and buffers may
                    # queue, but if the speakers have not consumed anything
                    # for a while the listener is hearing silence/stutter
                    # that every packet-based check calls "healthy".
                    last_output = getattr(streamer, "last_output_at", None)
                    starved_output = bool(
                        alive
                        and getattr(streamer, "_play_started", False)
                        and last_output is not None
                        and now - float(last_output) >= self.RELAY_OUTPUT_STALL_TIMEOUT
                    )
                    hard_stall = bool(
                        no_packets and stall_age is not None
                        and stall_age >= self.RELAY_HARD_STALL_TIMEOUT
                    )
                    # Frame-rate starvation: everything above can look green
                    # while the listener hears a sped-up stutter fading to
                    # silence. Only the arrival RATE over a window exposes
                    # the trickling channel (healthy = 25 fps).
                    starved_rate = False
                    if (alive and not no_packets and not stuck_audio
                            and not starved_output
                            and getattr(streamer, "_play_started", False)):
                        frames_now = int(getattr(streamer, "received_frames", 0) or 0)
                        check = entry.get("frame_rate_check")
                        if not isinstance(check, list) or len(check) != 2:
                            entry["frame_rate_check"] = [now, frames_now]
                        elif now - float(check[0]) >= self.RELAY_STARVE_WINDOW:
                            window = now - float(check[0])
                            fps = (frames_now - int(check[1])) / window
                            entry["frame_rate_check"] = [now, frames_now]
                            if fps < self.RELAY_STARVE_MIN_FPS:
                                starved_rate = True
                                rebuilds.append(
                                    (jukebox_id, f"frame starvation ({fps:.1f} fps)")
                                )
                    if (alive and not no_packets and not stuck_audio
                            and not starved_output and not starved_rate
                            and last_audio is not None):
                        # Relay demonstrably working — clear the strike and
                        # un-stick counters.
                        self._relay_fail_counts[jukebox_id] = 0
                        self._stall_unsticks[jukebox_id] = 0
                    if not alive:
                        rebuilds.append((jukebox_id, "receiver thread died"))
                    elif stuck_audio:
                        rebuilds.append((jukebox_id, "frames arriving but no audio progress"))
                    elif starved_output:
                        rebuilds.append((jukebox_id, "speaker stopped consuming (underrun)"))
                    elif no_packets:
                        if hard_stall:
                            # The stall survived a warm-up un-stick attempt —
                            # stop waiting and rebuild from scratch.
                            rebuilds.append((jukebox_id, f"no frames for {stall_age:.0f}s"))
                        else:
                            stalled_ids.append(jukebox_id)
                        needs_resync = True
                elif transport == "relay_pending":
                    # The server said a relay is coming but the follow-up play
                    # event never landed (worker died mid-retry): ask for the
                    # authoritative state instead of waiting for a relog.
                    if age >= self.RELAY_PENDING_TIMEOUT:
                        needs_resync = True
                elif transport == "direct" and streamer is not None:
                    # Direct fallback has no server relay to signal a mid-song
                    # decoder failure.  Restart only an explicit failure; a
                    # normally completed stream is allowed to await the server's
                    # next-song timer.
                    if (hasattr(streamer, "is_alive") and not streamer.is_alive()
                            and getattr(streamer, "failure_reason", None)):
                        rebuilds.append((jukebox_id, "direct streamer failed"))
        if rebuilds:
            needs_resync = True
        if needs_resync:
            # Do not remove a dead route unless this call actually sent the
            # recovery request.  Otherwise a cooldown could leave no route
            # behind to trigger the next retry.
            if not self.request_resync("relay recovery"):
                rebuilds = []
            else:
                # The resync just went out: every still-stalled relay that is
                # NOT being rebuilt now owes its recovery to the warm-up
                # replay. Count these un-stick attempts — when they repeat
                # without holding (observed after map reloads: the warm-up
                # blip plays but live frames never follow), rebuild right
                # away instead of waiting out the hard-stall timeout.
                for jukebox_id in stalled_ids:
                    if any(jid == jukebox_id for jid, _ in rebuilds):
                        continue
                    unsticks = self._stall_unsticks.get(jukebox_id, 0) + 1
                    self._stall_unsticks[jukebox_id] = unsticks
                    if unsticks >= self.RELAY_UNSTICK_TRIES:
                        rebuilds.append((jukebox_id, "warm-up un-stick did not hold"))
                # A dead receiver must be removed before the matching
                # ``jukebox_play`` arrives; otherwise idempotent same-identity
                # handling would retain it.  The resync reply rebuilds it with
                # fresh sources, and the buffered pending-route frames plus
                # the server's warm-up replay bridge the gap seamlessly.
                direct_switches = []
                for jukebox_id, reason in rebuilds:
                    log_line(f"[Jukebox] auto-recovery: rebuilding {jukebox_id} ({reason})")
                    params = None
                    with self._lock:
                        entry = self.players.get(jukebox_id)
                        # A strike per EXECUTED recovery attempt (not per scan
                        # frame): three failed rebuild cycles in a row means
                        # resyncing cannot fix this connection.
                        strikes = self._relay_fail_counts.get(jukebox_id, 0) + 1
                        self._relay_fail_counts[jukebox_id] = strikes
                        if entry is not None and strikes >= self.RELAY_DIRECT_FALLBACK_AFTER:
                            # Snapshot before stop() removes the entry.
                            params = dict(entry.get("play_params") or {})
                    self.stop(jukebox_id)
                    if params is not None:
                        direct_switches.append((jukebox_id, params))
                for jukebox_id, params in direct_switches:
                    # Relay keeps failing on this connection: switch this one
                    # jukebox to local direct playback (TCP-based) instead of
                    # looping rebuilds forever on a lost unreliable channel.
                    self._switch_to_direct(jukebox_id, params)

    def _switch_to_direct(self, jukebox_id, params):
        """Emergency: play the current song locally over HTTP when the relay
        channel is chronically unusable on this connection.

        Direct playback does not depend on the unreliable relay channel at
        all, so it survives exactly the cases resyncing cannot fix — chronic
        per-peer loss of the relay channel, where even the warm-up frames
        die and only a full relogin used to restore audio. Other listeners
        keep hearing the server relay unaffected."""
        params = dict(params or {})
        self._direct_fallback_until[jukebox_id] = time.monotonic() + self.DIRECT_FALLBACK_TTL
        if not params.get("url"):
            return
        # Continue from the song's current wall-clock position: the original
        # offset plus everything elapsed since the play event arrived.
        now = time.monotonic()
        offset = float(params.get("start_offset") or 0.0) + max(
            0.0, now - float(params.get("received_at") or now))
        log_line(f"[Jukebox] auto-recovery: {jukebox_id} relay unusable — direct playback")
        speak("Jukebox playing directly.")
        self.play(
            jukebox_id, params["x"], params["y"], params["z"],
            params.get("title", ""), params["url"], params.get("duration") or 0,
            transport="direct", start_offset=offset,
            http_headers=params.get("http_headers"),
        )

    def request_resync(self, reason="manual recovery"):
        """Ask the server for current jukebox routes and relay warm-up frames.

        This is safe after a UI transition: the server remains the playback
        authority, and the cooldown prevents repeated requests from creating
        needless traffic.
        """
        now = time.monotonic()
        with self._lock:
            # A UI close needs no server round-trip when this map has no
            # active jukebox playback known to the client.
            if not self.players:
                return False
            if now - self._last_recovery_request_at < self.RECOVERY_COOLDOWN:
                return False
            self._last_recovery_request_at = now
        try:
            from . import consts
            self.game.network.send(consts.CHANNEL_MISC, "jukebox_resync")
            log_line(f"[Jukebox] requested resync: {reason}")
            return True
        except Exception:
            return False

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
            # This mark is queued from CHANNEL_MAP. A newer jukebox_play may
            # already have reached the game queue through another ENet channel;
            # never let the stale reload mark claim that newly confirmed stream.
            if self._control_serial != serial:
                return False
            pending = set(self.players.keys())
            self._pending_map_change = pending
            self._pending_map_change_serial = serial
            # A fresh map is a fresh chance for the relay channel: give the
            # server relay another try after the emergency direct fallback.
            self._direct_fallback_until.clear()
            self._relay_fail_counts.clear()
            self._stall_unsticks.clear()
            if pending:
                log_line(
                    f"[Jukebox] map reload: {len(pending)} jukebox(es) awaiting "
                    f"play confirmation"
                )
        if not pending:
            return True

        def sweep():
            time.sleep(self.MAP_RELOAD_CONFIRM_TIMEOUT)
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
        return True

    def pend_relay_route(self, relay_id, stream_epoch):
        """Reserve a relay route BEFORE play() registers the real receiver.

        Relay frames are processed synchronously on the network thread, while
        jukebox_play playback setup is deferred to the main game loop. Without
        a pending buffer every frame in that gap — the song's first few 40 ms
        slices — was dropped, so songs started a fraction of a second in.
        Called from the jukebox_play network handler, so it only touches a dict.
        """
        if relay_id is None or stream_epoch is None:
            return
        try:
            key = (int(relay_id), int(stream_epoch))
        except (TypeError, ValueError):
            return
        with self._lock:
            if key in self.relay_routes:
                return
            if key not in self._relay_pending:
                self._relay_pending[key] = {"frames": [], "at": time.monotonic()}

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
                pending = None
                if receiver is None:
                    # No live route yet: keep the frame for a pending route so
                    # the song intro survives until play() registers the
                    # receiver. Bounded; the oldest frames drop first.
                    pending = self._relay_pending.get((relay_id, stream_epoch))
                    if pending is not None:
                        pending["at"] = time.monotonic()
                        frames = pending["frames"]
                        if len(frames) >= JukeboxRelayReceiver.MAX_PENDING_FRAMES:
                            frames.pop(0)
                        frames.append((sequence, payload, flags))
            if receiver is None:
                return pending is not None
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

    def detach_reverb(self):
        """Detach active streams before map reverb slots return to the pool."""
        audio = getattr(self.game, "audio_mngr", None)
        if audio is None or not hasattr(audio, "efx"):
            return
        with self._lock:
            sources = [
                source
                for player in self.players.values()
                for source in (player.get("source"), player.get("secondary_source"))
                if source is not None
            ]
        for source in sources:
            try:
                audio.efx.send(source, 0, None)
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

    def go_stop():
        gp.pop_last_substate()
        _stop_playback(game, gp, jukebox_id)

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

    repeat_names = {"off": "off", "one": "repeat one", "all": "repeat all"}

    def _repeat_label():
        # Callable label: evaluated when the menu speaks, so it always shows
        # the latest repeat mode from the cached server state.
        mode = _get_repeat_mode(gp, jukebox_id)
        return f"Toggle repeat mode (now: {repeat_names.get(mode, 'off')})"

    menu_items = [
        ("Search YouTube and queue a song", go_search),
        ("Queue by YouTube URL", go_direct_url),
        ("Skip current song", go_skip),
        ("Stop playback", go_stop),
        (_repeat_label, go_repeat),
        ("Shuffle queue", go_shuffle),
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
    # The jukebox streams through the server's YouTube relay, so only YouTube
    # links are supported. Other yt-dlp sites would resolve here and then be
    # mangled into a bogus youtube.com/watch?v=<foreign-id> queue entry that
    # can never play.
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
            from . import logger
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


def _stop_playback(game, gp, jukebox_id):
    from . import consts
    game.network.send(consts.CHANNEL_MISC, "jukebox_stop", {"id": jukebox_id})


def _get_repeat_mode(gp, jukebox_id):
    """The cached server repeat mode for one jukebox (off / one / all)."""
    box = _current_state(gp).get("jukeboxes", {}).get(jukebox_id) or {}
    mode = box.get("repeat") or "off"
    return mode if mode in ("off", "one", "all") else "off"


def _toggle_repeat(game, gp, jukebox_id):
    from . import consts
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
    from . import consts
    game.network.send(consts.CHANNEL_MISC, "jukebox_shuffle", {"id": jukebox_id})


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
        # int(): yt-dlp flat search returns durations as float, and the
        # '02d' format only accepts ints — a float here used to crash the
        # whole results menu before it opened.
        dur = int(r.get("duration") or 0)
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
