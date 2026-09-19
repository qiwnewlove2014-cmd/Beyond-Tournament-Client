"""Music Bot streaming layer - the ffmpeg decode + OpenAL playback thread
(AudioStreamer, including the paced 20 ms Opus network sender and direct/jam
alignment) plus the live-instrument relay (LiveRelayStreamer)."""

import contextlib
import queue
import random
import struct
import subprocess
import threading
import time
from collections import deque

import cyal
import cyal.exceptions

from .. import logger
from ..audio_diagnostics import probe
from ..jukebox_clock import PAIR_KEY, SpotClock, mono_ms
from ..party_sync import stereo_upload_eligible
from ..speech import speak
from .media import FFMPEG_PATH, YouTubeSearcher


class AudioStreamer(threading.Thread):
    """Background thread: ffmpeg decodes audio URL → raw PCM mono → queued to OpenAL source.
    
    Audio pipeline:
      YouTube URL → ffmpeg (decode to s16le mono 48kHz) → PCM chunks → OpenAL buffer queue
                                                        → Opus encode → Network broadcast (rate-limited)
    
    Network streaming uses real-time rate limiting (one 20ms frame per ~20ms) to prevent
    packet bursting which causes stuttering on receivers.
    """

    # Hand-built instances (tests, diagnostics tools) bypass __init__, so both
    # mode flags need class defaults for the shared playback paths to work.
    cinema = None
    spatial_pair = None

    @property
    def spatial_active(self):
        """Either spatial mode: both own their per-frame distance ramp."""
        return bool(self.spatial_pair or self.cinema is not None)

    # 960 samples per channel (20ms at 48kHz for Opus)
    SAMPLES_PER_BUFFER = 960
    BUFFER_SIZE = SAMPLES_PER_BUFFER * 2 * 2  # stereo 16-bit (3840 bytes)
    NUM_BUFFERS = 32      # Total buffers in pool
    # Frames this stream remembers of what it fed: the window a room handed
    # this output mid-song is primed from (see ``request_room``). A room can
    # hold ``prime_frames()`` of them, and a pair's queue is bounded by the
    # pool that feeds it, so a ring deeper than the pool is always enough.
    FED_RING_FRAMES = 64
    # How many frames a requested room may hold the stream's feeding while a
    # deep pair queue drains: 64 frames is 1.28 s of song, past which the swap
    # has waited longer than any refill and the room is left for the next song.
    SWAP_DRAIN_MAX_FRAMES = 64
    PRE_BUFFER_COUNT = 5  # Buffers to fill before starting local playback
                          # (100ms delay line; was 10/200ms — lowered together
                          # with MusicCompression.PRE_BUFFER_FRAMES so live
                          # music streams stay low-latency on both ends)
    # Direct-transport jukebox timeline alignment. In direct mode every
    # listener resolves and starts its own ffmpeg, so without a shared
    # anchor each machine began the same song seconds apart. All clients
    # derive ONE wall-clock deadline from the server's jukebox_play
    # broadcast (arrival instant minus start_offset) and hold the
    # prebuffered audio until that deadline: resolve variance becomes
    # wait time, never audible skew. Personal music bots (bot set) and
    # livestreams (timeline_anchor=False, no stable content position)
    # never anchor.
    DIRECT_LEAD_IN_S = 3.5         # fresh song: hold the intro this long after the broadcast
                                   # (matches the server's end-of-song grace; machines slower
                                   # than this join late and trail the room for the song)
    DIRECT_FRESH_MAX_S = 2.0       # a play event this young is a fresh broadcast (start_offset
                                   # ≈ its age): hold position 0. Older offsets are resumes /
                                   # reload re-broadcasts whose intro the room already heard —
                                   # they must seek, never replay.
    DIRECT_STARTUP_EST_S = 4.5     # mid-song: aim -ss past the projected audible start;
                                   # arriving early is correctable by an exact wait,
                                   # arriving late never recovers under '-re' pacing.
                                   # This estimate IS the alignment slack (LEAD_IN +
                                   # EST after resolve): a machine whose seek+
                                   # prebuffer outruns it starts late and trails the
                                   # room for the whole song — its jam notes then land
                                   # off the beat for everyone. 4.5s keeps typical
                                   # seeks (network download to the keyframe) inside
                                   # the slack at the cost of skipping ~3s more of the
                                   # song for joiners/resumes.
    DIRECT_MAX_ALIGN_WAIT_S = DIRECT_LEAD_IN_S + DIRECT_STARTUP_EST_S + 1.0  # clock safety valve
    DIRECT_LATE_TOLERANCE_S = 0.75 # best-effort: log joins later than this

    def __init__(self, game, audio_url, source, volume=50, bot=None, channels=2,
                 spatial_pair=None, start_offset=0.0, http_headers=None,
                 start_offset_received_at=None, canonical_url=None, media_cache=None,
                 timeline_anchor=None, start_paused=False, room_lead_in_s=None,
                 join_playing_room=False, cinema=None):
        super().__init__(daemon=True)
        self.game = game
        self.bot = bot
        self.audio_url = audio_url
        # The stable YouTube page URL (if known). When a freshly resolved
        # googlevideo URL 403s at startup, re-resolving THIS url yields a new
        # signed URL+headers — retrying the same stale URL never helps.
        self.canonical_url = canonical_url
        # Opt-in from JukeboxPlayer only. Workers borrow its bounded metadata
        # cache; personal broadcasts, native sources and playback positions
        # are never cached. These per-attempt fields belong to this worker.
        self._media_cache = media_cache if bot is None else None
        self._media_cache_key = None
        self._media_cache_entry = None
        self._media_cache_candidate = None
        self.source = source  # cyal OpenAL source
        self.volume = volume
        self.start_offset = float(start_offset or 0.0)
        self.start_offset_received_at = start_offset_received_at
        # Shared-timeline anchoring: jukebox direct playback only. The
        # personal music bot never passes start_offset_received_at, and
        # JukeboxPlayer passes timeline_anchor=False for livestreams.
        if timeline_anchor is None:
            timeline_anchor = True
        self._direct_anchor = bool(
            bot is None
            and start_offset_received_at is not None
            and timeline_anchor
        )
        # Lead-in the ROOM holds before a fresh song becomes audible. Every
        # machine that anchors to the same broadcast holds the same lead-in,
        # so it cancels out of note timing. A machine that plays direct
        # while the rest of the room hears the server RELAY must anchor with
        # 0.0 instead: the relay room holds no lead-in, and holding one
        # leaves that machine exactly DIRECT_LEAD_IN_S behind the room for
        # the whole song (its jam notes then land ~3.5s off the beat for
        # everyone, with no lag report to fix it — its own anchor says it
        # started on time).
        self.room_lead_in_s = (
            AudioStreamer.DIRECT_LEAD_IN_S if room_lead_in_s is None
            else float(room_lead_in_s)
        )
        # Per-listener direct fallback into a room that is ALREADY playing
        # (server relay). The song position advances from the broadcast
        # instant, so even a zero-offset event must seek PAST the projected
        # audible start — starting at position 0 would trail the room by
        # this machine's whole resolve+startup (seconds off the beat for
        # everyone, with no lag report to fix it).
        self._join_playing_room = bool(join_playing_room)
        self._direct_seek_to = 0.0        # content position the decode head starts from
        self._fed_content_seconds = 0.0   # content seconds queued to OpenAL since the seek
        # Seconds the audible start ran past the shared wall-clock deadline
        # (slow yt-dlp resolve / ffmpeg startup). Jam-note scheduling reads
        # this so remote notes wait out a local stream that trails the room.
        self.direct_late_s = 0.0
        self.http_headers = dict(http_headers or {})
        # Spatial stereo pair (jukeboxes): two MONO sources placed at the same
        # spot minus/plus a small offset, fed with the LEFT and RIGHT channels
        # of a STEREO decode. Two positioned mono sources produce a real stereo
        # image when you stand close, which naturally collapses toward mono at
        # distance — exactly how piano/drum sounds are anchored in the world.
        # `spatial_pair` is (src_l, src_r, reference_distance, max_distance).
        # A cinema room (CinemaSpeakerBank) REPLACES the stereo pair rather
        # than joining it: both cannot play, or the room would hear the song
        # twice and comb filter it. The same interleaved stereo decode is
        # simply handed to the bank, which owns one positioned source per
        # speaker, so nothing else about this stream changes.
        self.cinema = cinema
        if self.cinema is not None:
            spatial_pair = None
        self.spatial_pair = spatial_pair
        # A room handed this stream mid-song (``request_room``) is primed from
        # the frames already fed, so the last few seconds of them are kept.
        # Guarded by its own lock: the decode thread appends, the game thread
        # (the mode pick) reads.
        self._fed_ring = deque(maxlen=self.FED_RING_FRAMES)
        self._ring_lock = threading.Lock()
        self._pending_room = None
        self._swap_hold_frames = 0
        self.swap_room_failed = False
        # Frames handed to the plain pair, and what one of them measured: the
        # pair's own content clock (``pair_played_frames``), which a live
        # note's wait is re-projected from instead of a queue depth measured
        # once when the note arrived (see ``libs/jukebox_clock.py``).
        self.pair_frames_fed = 0
        self._pair_frame_ms = 0.0
        self._pool_swap_armed = False
        if self.spatial_pair:
            self.spatial_src_l, self.spatial_src_r = spatial_pair[0], spatial_pair[1]
            self.spatial_ref = float(spatial_pair[2])
            self.spatial_max = float(spatial_pair[3])
            self.spatial_base_gain = max(0.0, min(1.0, volume / 100.0))
            self.channels = 2  # decode interleaved stereo, split per channel
        elif self.cinema is not None:
            self.spatial_base_gain = max(0.0, min(1.0, volume / 100.0))
            self.channels = 2
        else:
            self.channels = int(channels)
        self.BUFFER_SIZE = self.SAMPLES_PER_BUFFER * self.channels * 2
        self.running = True
        # A stream can be created already paused (a seek performed while the
        # bot was paused): its pre-buffered head stays silent until resume.
        self.paused = bool(start_paused)
        self._lock = threading.Lock()
        self._cleanup_lock = threading.Lock()
        self._cleaned_up = False
        self.ready_event = threading.Event()
        # Set as soon as the startup pre-buffer has been queued, whether or
        # not playback actually started (a stream created start_paused=True
        # never sets ready_event). Crossfade pre-rolls poll this event so the
        # next track's decoder is ready before its audible hand-over.
        self.prebuffer_event = threading.Event()
        self.failure_reason = None
        self.completed_normally = False
        # Crossfade bookkeeping: a retired outgoing stream whose network leg
        # must go silent the instant the next track takes over (two Music Bot
        # streams must never overlap on the PA / broadcast channel).
        self.network_muted = False
        # Network-leg crossfade state: (partner_streamer, monotonic start,
        # seconds). While set, the network sender blends this stream's own
        # frames with the partner's (fade-out / fade-in) so broadcast and PA
        # listeners hear the same overlap as the local performer.
        self._network_mix = None
        # Monotonic instant the speakers last consumed an OpenAL buffer
        # (jukebox direct streams only — see _reclaim_processed). The
        # jukebox watchdog rebuilds a stream whose OUTPUT stalls even
        # though its thread is alive, since direct playback has no server
        # relay to announce a mid-song death.
        self.last_output_at = None
        self.process = None
        self._buffer_pool = []       # Reusable buffer objects
        self._pause_buffer = deque() # Store data read while paused
        self.encoder = None
        self.encoder_stereo = None
        if bot is not None:
            # Listening-only jukebox streams never broadcast PCM back out.
            from pyogg import OpusEncoder
            self.encoder = OpusEncoder()
            self.encoder.set_application('audio')
            self.encoder.set_channels(1)  # public/PA network stream is MONO
            self.encoder.set_sampling_frequency(48000)
            # Party Sync private leg can carry true stereo (see
            # stereo_upload_eligible) so guests hear real left/right instead
            # of a mono downmix of the host's music.
            self.encoder_stereo = OpusEncoder()
            self.encoder_stereo.set_application('audio')
            self.encoder_stereo.set_channels(2)
            self.encoder_stereo.set_sampling_frequency(48000)
        self.last_send_time = None
        # Keep broadcast latency bounded.  A network hiccup must discard stale
        # music frames instead of building an ever-growing backlog that later
        # reaches each PA cabinet at a different time.
        self.network_queue = queue.Queue(maxsize=50)
        self.sender_thread = None
        # Versioned performance timeline. The delayed PCM deque mirrors the
        # ten OpenAL pre-buffer frames, so normal Music Broadcast packets leave
        # at the same media position the performer is hearing locally. PA uses
        # its proven legacy path and is deliberately not changed here.
        self._timeline_lock = threading.Lock()
        self._timeline_epoch = random.getrandbits(32) or 1
        self._timeline_next_seq = 0
        self._timeline_last_sent_seq = None
        self._timeline_delay = deque()

    def _all_sources(self):
        """All OpenAL sources this stream feeds (1 normal, 2 spatial, N cinema)."""
        if self.cinema is not None:
            return self.cinema.sources
        if self.spatial_pair:
            return (self.spatial_src_l, self.spatial_src_r)
        return (self.source,)

    def _play_all(self):
        for src in self._all_sources():
            try:
                src.play()
            except Exception:
                pass

    def _all_playing(self):
        for src in self._all_sources():
            try:
                if src.state != cyal.SourceState.PLAYING:
                    return False
            except Exception:
                return False
        return True

    def _start_output_playing(self):
        """Start whichever sources are not playing yet, as one output.

        In a cinema room the bank owns that decision, because starting a
        speaker on its own is what makes one of them permanently miss the
        room's beat: the bank puts a speaker that fell behind back on the
        room's content instant first (see ``CinemaSpeakerBank.realign``) and
        then starts the room together.
        """
        if self.cinema is not None:
            try:
                self.cinema.start_playback()
                return
            except Exception:
                pass
        self._play_all()

    def _buffers_queued(self):
        total = 0
        for src in self._all_sources():
            try:
                total += src.buffers_queued
            except Exception:
                pass
        return total

    @staticmethod
    def _split_stereo_16(data):
        """Split interleaved s16le stereo bytes into (left, right) mono bytes."""
        import array
        samples = array.array('h')
        samples.frombytes(data)
        return samples[0::2].tobytes(), samples[1::2].tobytes()

    def set_cabinet_volume(self, volume):
        self.cabinet_gain = max(0.0, min(1.0, float(volume) / 100.0))
        if self.cinema is not None:
            self.cinema.set_cabinet_volume(volume)
        self._update_spatial_gain()

    def _update_spatial_gain(self):
        """Linear distance fade for spatial pairs (same behavior as drums' 3D
        stereo): full volume inside the reference distance, linearly down to
        true silence at max_distance, computed from the listener's position."""
        if self.cinema is not None:
            # Per-speaker distance, occlusion and environment for the room.
            self.cinema.update_output()
            return
        try:
            audio = getattr(self.game, "audio_mngr", None)
            pos = getattr(audio, "position", None)
            if pos is None:
                return
            span = max(0.0001, self.spatial_max - self.spatial_ref)
            audible = False
            for src in (self.spatial_src_l, self.spatial_src_r):
                p = src.position
                dx = pos[0] - p[0]
                dy = pos[1] - p[1]
                dz = pos[2] - p[2]
                dist = (dx * dx + dy * dy + dz * dz) ** 0.5
                if dist <= self.spatial_ref:
                    g = 1.0
                elif dist >= self.spatial_max:
                    g = 0.0
                else:
                    g = 1.0 - (dist - self.spatial_ref) / span
                audible = audible or g > 0.0
                # Jukebox songs use their own mixer category ("jukebox"), so
                # lowering the music-bot/map-music slider does not silence them.
                music_gain = audio.volume_categories.get("jukebox", [100])[0] / 100.0
                src.gain = self.spatial_base_gain * music_gain * getattr(self, "cabinet_gain", 1.0) * g

            if not audible:
                return  # Keep timeline playing, but do not raycast silent cabinets.
            # Wall occlusion check for spatial pair (Jukebox direct mode):
            if self.spatial_src_l is not None and self.spatial_src_r is not None:
                box_pos = (
                    (self.spatial_src_l.position[0] + self.spatial_src_r.position[0]) / 2.0,
                    self.spatial_src_l.position[1],
                    self.spatial_src_l.position[2],
                )
                gp = getattr(self.game, "gameplay", None)
                cur_map = getattr(gp, "map", None) if gp is not None else None
                if cur_map is not None and hasattr(cur_map, "valid_straight_path"):
                    # Wall-thickness tiers (same 0/1/2 scale as jukebox.py):
                    # a lone pillar tile only slightly dulls the song, while
                    # thick walls keep the full standard muffle.
                    tier = 0
                    tfn = getattr(cur_map, "occlusion_tier", None)
                    jukebox_player = getattr(self, "jukebox_player", None)
                    if jukebox_player is not None:
                        tier = jukebox_player.occlusion_tier(box_pos, pos, self.spatial_max)
                    elif tfn is not None:
                        with contextlib.suppress(Exception):
                            tier = int(tfn(box_pos, pos))
                    else:
                        with contextlib.suppress(Exception):
                            tier = 2 if cur_map.valid_straight_path(box_pos, pos) is False else 0
                    if tier != getattr(self, "_last_occluded", None):
                        self._last_occluded = tier
                        filt = None
                        if tier >= 2 and hasattr(audio, "gen_filter"):
                            if not hasattr(self, "_occlusion_filter") or self._occlusion_filter is None:
                                self._occlusion_filter = audio.gen_filter("LOWPASS", ("GAINHF", 0.05), ("GAIN", 0.22))
                            filt = self._occlusion_filter
                        elif tier == 1 and hasattr(audio, "gen_filter"):
                            if not hasattr(self, "_light_occlusion_filter") or self._light_occlusion_filter is None:
                                self._light_occlusion_filter = audio.gen_filter("LOWPASS", ("GAINHF", 0.45), ("GAIN", 0.75))
                            filt = self._light_occlusion_filter
                        for s in (self.spatial_src_l, self.spatial_src_r):
                            try:
                                if filt is not None:
                                    s.direct_filter = filt
                                else:
                                    # A wall clearing must not strip an active
                                    # global filter (the underwater muffle):
                                    # restore it instead of deleting it.
                                    active = getattr(audio, "filter", None)
                                    if active and active[-1] is not None:
                                        s.direct_filter = active[-1]
                                    else:
                                        with contextlib.suppress(Exception):
                                            del s.direct_filter
                                if getattr(audio, "efx", None) is not None:
                                    if hasattr(self, "reverb_slot") and self.reverb_slot is not None:
                                        audio.efx.send(s, 0, self.reverb_slot, filter=filt)
                                    if hasattr(self, "eq_slot") and self.eq_slot is not None:
                                        audio.efx.send(s, 1, self.eq_slot, filter=filt)
                            except Exception:
                                pass
        except Exception:
            pass

    def set_volume(self, volume):
        """Update playback volume in real time."""
        self.volume = volume
        self.spatial_base_gain = max(0.0, min(1.0, volume / 100.0))
        if self.spatial_pair:
            self._update_spatial_gain()
        elif self.cinema is not None:
            self.cinema.set_volume(volume)
        elif self.source:
            try:
                self.source.gain = max(0.0, min(1.0, volume / 100.0))
            except Exception:
                pass

    def _init_buffer_pool(self):
        """Pre-allocate OpenAL buffers for reuse"""
        if self.cinema is not None:
            # A cinema room allocates its own per-speaker pools: a single
            # shared pool could never hold N frames the pre-buffer needs.
            return
        for _ in range(self.NUM_BUFFERS):
            try:
                buf = self.game.audio_mngr.context.gen_buffer()
                self._buffer_pool.append(buf)
            except cyal.exceptions.InvalidOperationError:
                # The project audio backend can surface one stale OpenAL error
                # before succeeding; other buffer loaders use the same retry.
                try:
                    buf = self.game.audio_mngr.context.gen_buffer()
                    self._buffer_pool.append(buf)
                except Exception as ex:
                    logger.log_exception(ex, "AudioStreamer._init_buffer_pool retry")
                    break
            except Exception:
                break

    @staticmethod
    def _ffmpeg_header_block(headers):
        """Build an injection-safe CRLF header block for ffmpeg's input."""
        lines = []
        token_chars = "!#$%&'*+-.^_`|~"
        for raw_name, raw_value in (headers or {}).items():
            name = str(raw_name).strip()
            value = str(raw_value).strip()
            if not name or not value:
                continue
            if any(ch in name or ch in value for ch in ('\r', '\n')):
                continue
            if not all(ch.isalnum() or ch in token_chars for ch in name):
                continue
            lines.append(f"{name}: {value}\r\n")
        return "".join(lines)

    def _get_buffer(self):
        """Get a reclaimed buffer without growing the OpenAL pool.

        Streaming must apply backpressure when every pre-allocated buffer is in
        flight.  Allocating another hardware buffer here lets a fast decoder
        queue an entire song and can exhaust the shared audio device.
        """
        self._reclaim_processed()

        if not self._buffer_pool and self._pool_swap_armed:
            # A stream handed from a room to the plain pair never built a
            # pool of its own (a room allocates per speaker), and it is being
            # fed again before the pair's own queued frames have recycled a
            # buffer. Build the pool here, on the thread that already owns
            # this stream's buffers, instead of queueing nothing forever.
            self._pool_swap_armed = False
            self._init_buffer_pool()
        if self._buffer_pool:
            return self._buffer_pool.pop(0)
        return None

    def _reclaim_processed(self):
        """Return processed buffers to pool for reuse.
        
        CRITICAL: cyal's unqueue_buffers() returns a SINGLE Buffer object by default,
        not a list. Handle both cases robustly. Spatial pairs drain both sources.
        """
        if self.cinema is not None:
            # The speaker bank owns its per-speaker pools, so it owns the
            # reclamation too -- but the watchdog's audible-progress stamp is
            # this stream's contract, so it is still stamped here.
            reclaimed = False
            try:
                reclaimed = self.cinema.reclaim()
            except Exception:
                reclaimed = False
            if reclaimed and getattr(self, "jukebox_player", None) is not None:
                self.last_output_at = time.monotonic()
            return
        reclaimed = False
        try:
            for src in self._all_sources():
                while src.buffers_processed > 0:
                    result = src.unqueue_buffers()
                    if result is not None:
                        reclaimed = True
                        try:
                            for buf in result:
                                self._buffer_pool.append(buf)
                        except TypeError:
                            # Not iterable — single buffer object (cyal default)
                            self._buffer_pool.append(result)
        except Exception:
            pass
        if reclaimed and getattr(self, "jukebox_player", None) is not None:
            # A buffer finished on the speaker: audible progress. Only
            # jukebox direct streams feed the jukebox watchdog (jukebox.py
            # reads this stamp); personal Music Bot streams never set
            # jukebox_player, so their output is not watched here.
            self.last_output_at = time.monotonic()

    def _claim_timeline_marker(self):
        with self._timeline_lock:
            seq = self._timeline_next_seq
            self._timeline_next_seq = (seq + 1) & 0xFFFFFFFF
            return self._timeline_epoch, seq

    def performance_timeline_marker(self):
        """Return the last normal-broadcast frame aligned to local playback."""
        if not self.ready_event.is_set() or not self.running:
            return None
        with self._timeline_lock:
            seq = self._timeline_last_sent_seq
            if seq is None:
                return None
            return {
                "version": 1,
                "epoch": self._timeline_epoch,
                "frame_seq": seq,
            }

    def _send_to_network(self, data, timeline_epoch=None, timeline_seq=None):
        """Queue raw PCM chunk to be sent to the network by the sender thread."""
        item = (data, timeline_epoch, timeline_seq)
        try:
            self.network_queue.put_nowait(item)
        except queue.Full:
            try:
                self.network_queue.get_nowait()
                self.network_queue.put_nowait(item)
            except queue.Empty:
                pass

    def _route_aligned_network_frame(self, decoded_frame=None):
        """Advance the normal-broadcast delay line by one media frame.

        Normal Music Broadcast sends the oldest pre-buffered frame, matching
        the performer's OpenAL playhead. Megaphone keeps sending the current
        decoded frame so the just-verified PA transport remains untouched.
        """
        if not self.bot:
            return
        if decoded_frame is not None:
            self._timeline_delay.append(bytes(decoded_frame))
        aligned = self._timeline_delay.popleft() if self._timeline_delay else None
        if getattr(self.bot, 'broadcast_to_megaphone', False):
            if decoded_frame is not None:
                self._send_to_network(decoded_frame)
            return
        if aligned is not None:
            epoch, seq = self._claim_timeline_marker()
            self._send_to_network(aligned, epoch, seq)

    def begin_network_crossfade(self, partner, seconds):
        """Blend this stream's network leg with `partner`'s for `seconds`.

        Each outgoing frame is mixed with the partner's frame (this stream
        fading out, the partner fading in) so broadcast / PA listeners hear
        the same crossfade the local performer hears. When the window
        elapses this stream retires its network leg (network_muted) and the
        partner's own leg takes over.
        """
        self._network_mix = (partner, time.monotonic(), max(0.1, float(seconds)))

    def _mix_network_frames(self, own, partner, own_gain, partner_gain):
        """Convex blend of two stereo int16 PCM frames (gains sum to ~1).

        A convex combination can never exceed the loudest input sample, so
        the blend cannot clip; audioop clips to int16 anyway as a backstop.
        """
        try:
            import audioop
            own_gain = max(0.0, min(1.0, own_gain))
            partner_gain = max(0.0, min(1.0, partner_gain))
            if own is None:
                return audioop.mul(partner, 2, partner_gain)
            if partner is None:
                return audioop.mul(own, 2, own_gain)
            n = min(len(own), len(partner))
            out = audioop.add(
                audioop.mul(own[:n], 2, own_gain),
                audioop.mul(partner[:n], 2, partner_gain),
                2,
            )
            if len(partner) > n:
                out += audioop.mul(partner[n:], 2, partner_gain)
            return out
        except Exception:
            return own if own is not None else partner

    def _network_sender_loop(self):
        """Paced network sending loop running in a separate thread.
        Decouples network scheduling sleeps from local OpenAL playback.
        """
        while self.running:
            if getattr(self, "network_muted", False):
                # Retired during a crossfade: drop everything (music, live
                # guitar/mic mix, PA monitor) so listeners never hear the old
                # song under the new one. Poll slowly to keep the thread alive
                # in case it is ever unmuted before being stopped.
                time.sleep(0.05)
                continue

            data = None
            timeline_epoch = timeline_seq = None
            mix = getattr(self, "_network_mix", None)
            if mix is not None:
                partner, mix_started, mix_seconds = mix
                progress = (time.monotonic() - mix_started) / mix_seconds
                if progress >= 1.0:
                    # The overlap is over: retire this network leg; the
                    # incoming stream takes over the broadcast on its own now.
                    self._network_mix = None
                    self.network_muted = True
                    continue
                try:
                    item = self.network_queue.get(timeout=0.1)
                    if isinstance(item, tuple) and len(item) == 3:
                        data = item[0]
                    else:
                        data = item
                except queue.Empty:
                    data = None
                partner_data = None
                partner_queue = getattr(partner, "network_queue", None)
                if partner_queue is not None:
                    try:
                        partner_item = partner_queue.get_nowait()
                        if isinstance(partner_item, tuple) and len(partner_item) == 3:
                            partner_data = partner_item[0]
                        else:
                            partner_data = partner_item
                    except Exception:
                        partner_data = None
                if data is not None or partner_data is not None:
                    data = self._mix_network_frames(
                        data, partner_data, 1.0 - progress, progress)
                    # The outgoing track still owns the room timeline until
                    # the hand-over; the blend keeps its markers.
                    timeline_epoch, timeline_seq = self._claim_timeline_marker()
            else:
                try:
                    # Wait for a packet, with timeout so we check self.running regularly
                    item = self.network_queue.get(timeout=0.1)
                    if isinstance(item, tuple) and len(item) == 3:
                        data, timeline_epoch, timeline_seq = item
                    else:
                        data, timeline_epoch, timeline_seq = item, None, None
                except queue.Empty:
                    data = None
                    timeline_epoch = timeline_seq = None

            # Do not leak pre-pause PCM into the next broadcast segment unless live input is present.
            if self.paused:
                data = None

            # Single music slot: when "Broadcast to Megaphone" is ON but another
            # performer holds the music-bot PA slot, keep the MP3 private - only
            # this performer's live instruments (guitar/mic) continue into the
            # PA mix, so two people's music never overlaps on the speakers.
            if (data is not None and self.bot
                    and getattr(self.bot, 'broadcast_to_megaphone', False)
                    and not self.bot._is_music_owner()):
                data = None

            if data is None:
                # Keep the live mix flowing while the music bot broadcast is on
                # OR the performer enabled "Broadcast to Megaphone" (an
                # independent toggle, like piano/drums).
                if self.bot and (
                    getattr(self.bot, 'broadcast_enabled', False)
                    or getattr(self.bot, 'broadcast_to_megaphone', False)
                ):
                    has_guitar = bool(getattr(self.bot, 'guitar_pcm_queue', None) and len(self.bot.guitar_pcm_queue) > 0)
                    has_mic = bool(getattr(self.bot, 'mic_pcm_queue', None) and len(self.bot.mic_pcm_queue) > 0)
                    if has_guitar or has_mic:
                        data = b'\x00' * 3840
                        if not getattr(self.bot, 'broadcast_to_megaphone', False):
                            timeline_epoch, timeline_seq = self._claim_timeline_marker()

            if data is None:
                continue

            # High-resolution, deadline-based pacing. Advancing the ideal
            # deadline by exactly 20 ms prevents normal scheduler overshoot
            # (typically 0.2-0.8 ms/frame on Windows) from accumulating into a
            # steadily growing network queue. A large stall starts a fresh
            # cadence rather than bursting old audio to catch up.
            now = time.perf_counter()
            target_interval = 0.020  # 20ms per buffer
            if self.last_send_time is None or now - self.last_send_time > 0.100:
                deadline = now
            else:
                deadline = self.last_send_time + target_interval
                if now < deadline:
                    # Sleep most of the way (subtracting 1ms margin for Windows scheduler inaccuracy)
                    sleep_time = deadline - now
                    if sleep_time > 0.001:
                        time.sleep(sleep_time - 0.001)
                    # Spin lock for the remaining fraction of a millisecond
                    while time.perf_counter() < deadline:
                        pass
                elif now - deadline > 0.040:
                    # Do not emit a catch-up burst after a suspended/stalled worker.
                    deadline = now
            # Keep the IDEAL deadline, not the overshot wall-clock time, so small
            # scheduling errors are corrected by the next interval instead of
            # becoming permanent drift.
            self.last_send_time = deadline

            self._send_to_network_actual(data, timeline_epoch, timeline_seq)

    def _send_to_network_actual(self, data, timeline_epoch=None, timeline_seq=None):
        """Downmix Stereo to Mono, scale volume, encode as Opus, and send to network."""
        try:
            if not self.game or not self.game.network:
                return
                
            # Check if the stream is being broadcast: the music bot broadcast
            # is on, OR the performer enabled "Broadcast to Megaphone" (the
            # PA/megaphone routing is an independent toggle - exactly like
            # piano/drums, so guitar and music reach the PA on their own), OR
            # the player hosts an active Party Sync session (private upload;
            # the server relays only to session guests). A stream WITHOUT a
            # bot (jukebox playback, bot=None) NEVER sends: otherwise the
            # jukebox audio would be re-broadcast to the whole map as the
            # player's own music bot stream (double audio everywhere).
            if not self.bot or not (
                self.bot.broadcast_enabled
                or self.bot.broadcast_to_megaphone
                or getattr(self.bot, "party_sync_force_upload", False)
                # A song routed into a cabinet's room is uploaded whatever the
                # Broadcast switch says: the room is a place other people
                # stand in, and the frames are the only way it reaches them
                # (see libs/audio/cinema/peer.py). The switch itself is
                # untouched and comes back the moment the routing goes off.
                or getattr(self.bot, "cinema_force_upload", False)
            ):
                return

            # Downmix 16-bit stereo → 16-bit mono
            import audioop
            mono_data = audioop.tomono(data, 2, 0.5, 0.5)

            # Scale the PCM stream volume according to self.volume and the dynamic ducking multiplier before network broadcast
            current_volume_scale = (self.volume / 100.0)
            if self.bot:
                current_volume_scale *= getattr(self.bot, 'duck_multiplier', 1.0)
            
            if current_volume_scale != 1.0:
                try:
                    mono_data = audioop.mul(mono_data, 2, current_volume_scale)
                except Exception:
                    pass

            from .. import consts
            target_channel = consts.CHANNEL_MUSICBOT
            # A Party Sync session shares the bot privately with its guests:
            # always use the private MUSICBOT leg (never the PA megaphone),
            # regardless of the user's megaphone toggle, while the session is
            # active. The server relays that leg only to session guests.
            if (self.bot and self.bot.broadcast_to_megaphone
                    and not getattr(self.bot, "party_sync_force_upload", False)):
                target_channel = consts.CHANNEL_MEGAPHONE

            # Mix queued live input (voice mic and/or line-in guitar) into the
            # outgoing stream. This happens on BOTH broadcast paths (the 3D
            # music bot channel and the megaphone), so a guitar strum is heard
            # either way - always gated by broadcast_enabled above.
            mic_data = None
            guitar_data = None
            if self.bot:
                try:
                    if getattr(self.bot, 'mic_pcm_queue', None) and self.bot.mic_pcm_queue:
                        mic_data = self.bot.mic_pcm_queue.popleft()
                except Exception:
                    pass
                try:
                    if getattr(self.bot, 'guitar_pcm_queue', None) and self.bot.guitar_pcm_queue:
                        guitar_data = self.bot.guitar_pcm_queue.popleft()
                except Exception:
                    pass
            if mic_data is not None or guitar_data is not None:
                try:
                    def _align(b):
                        if len(b) > len(mono_data):
                            return b[:len(mono_data)]
                        if len(b) < len(mono_data):
                            return b + b'\x00' * (len(mono_data) - len(b))
                        return b
                    # Scale down before mixing to prevent 16-bit overflow clipping
                    mono_data = audioop.mul(mono_data, 2, 0.75)
                    if mic_data is not None:
                        mono_data = audioop.add(
                            mono_data, audioop.mul(_align(mic_data), 2, 0.85), 2)
                    if guitar_data is not None:
                        mono_data = audioop.add(
                            mono_data, audioop.mul(_align(guitar_data), 2, 0.85), 2)
                except Exception:
                    pass

            # When routing through the megaphone, feed the mixed stream into the
            # local PA sidechain (main thread) so the broadcaster hears their own
            # music/instruments through the speakers with zero latency - the server
            # no longer echoes the broadcast back to the sender. Skipped while a
            # Party Sync session is active (that leg is private-to-guests).
            if (self.bot and self.bot.broadcast_to_megaphone
                    and not getattr(self.bot, "party_sync_force_upload", False)):
                try:
                    gp = self.bot._find_gameplay()
                    if gp is not None:
                        from .. import voice_chat
                        if hasattr(voice_chat, '_feed_local_megaphone_direct'):
                            local_pcm = bytes(mono_data)
                            voice_chat._feed_local_megaphone_direct(
                                gp, local_pcm, producer='music'
                            )
                except Exception:
                    pass

            # Party Sync private leg carries TRUE STEREO so guests hear real
            # left/right separation instead of the mono downmix; public
            # broadcasts and the PA megaphone keep the proven mono path.
            # Live-input (mic/guitar) mixes stay mono (built as one channel).
            use_stereo = stereo_upload_eligible(
                self.bot,
                getattr(self, "channels", 2),
                live_input_pending=bool(
                    mic_data is not None or guitar_data is not None
                ),
            )
            if use_stereo:
                enc = self.encoder_stereo or self.encoder
                payload = bytes(data) if data is not None else b""
                if current_volume_scale != 1.0 and payload:
                    try:
                        payload = audioop.mul(payload, 2, current_volume_scale)
                    except Exception:
                        pass
            else:
                enc = self.encoder
                payload = mono_data
            if enc is None or not payload:
                return
            encoded = enc.encode(bytearray(payload))
            if (target_channel == consts.CHANNEL_MUSICBOT
                    and getattr(self.game, 'music_timeline_supported', False)
                    and timeline_epoch is not None and timeline_seq is not None):
                target_channel = consts.CHANNEL_MUSICBOT_TIMELINE
                encoded = struct.pack(
                    ">BII", 1, int(timeline_epoch) & 0xFFFFFFFF,
                    int(timeline_seq) & 0xFFFFFFFF,
                ) + bytes(encoded)
                with self._timeline_lock:
                    self._timeline_last_sent_seq = int(timeline_seq) & 0xFFFFFFFF

            self.game.network.send(target_channel, "n/a", encoded, reliable=False)
        except Exception:
            pass



    def _note_fed_content(self, data):
        """Track the media position already handed to OpenAL (jukebox retire math)."""
        self._fed_content_seconds += len(data) / (48000.0 * self.channels * 2)

    def content_position(self):
        """Approximate media position (seconds) of the local decode head."""
        return self._direct_seek_to + self._fed_content_seconds

    def _queue_local(self, data):
        """Queue a chunk of PCM data to the LOCAL OpenAL source(s)."""
        if self._pending_room is not None and not self._commit_room_swap():
            # The room is waiting for the pair's queue to come down to what
            # it can hold: hold this frame (it is retried, never dropped) and
            # let the song play out of the queue it already has.
            return False
        if self.cinema is not None:
            return self._queue_local_cinema(data)
        if self.spatial_pair:
            return self._queue_local_spatial(data)
        self._reclaim_processed()
        buf = self._get_buffer()
        if buf is None:
            return False
        try:
            # Local playback is STEREO for the personal music bot (highest
            # quality); MONO for plain jukebox streams.
            if self.channels == 1:
                buf.set_data(data, sample_rate=48000, format=cyal.BufferFormat.MONO16)
            else:
                buf.set_data(data, sample_rate=48000, format=cyal.BufferFormat.STEREO16)
            self.source.queue_buffers(buf)
            self._note_fed_content(data)
            return True
        except Exception:
            return False

    def _queue_local_cinema(self, data):
        """Hand one interleaved stereo frame to the room's speaker bank.

        The bank renders it into one MONO feed per speaker and queues each
        into that speaker's own pool, so this stream never touches a buffer
        or a source directly in cinema mode.

        A frame the room accepted still counts as played content, exactly as
        it does on a plain source: ``content_position()`` is what the Music
        Bot's seek and the Jukebox's end-of-song hand-over are measured from,
        and a room is the one output whose frames never pass through
        ``_queue_local``'s own sources. Crediting it per ACCEPTED frame (not
        per decoded one) keeps that position honest while the pre-buffer
        retries a frame the room has no buffer for yet.
        """
        try:
            left, right = self._split_stereo_16(data)
        except Exception:
            return False
        try:
            queued = bool(self.cinema.queue_frame(left, right))
        except Exception:
            self.failure_reason = "cinema speaker queue failed"
            return False
        if queued:
            self._remember_frame(left, right)
            self._note_fed_content(data)
        return queued

    def _queue_local_spatial(self, data):
        """Queue a STEREO chunk split into L/R MONO buffers on the two positioned
        sources (jukebox stereo-spatial playback, same as drums)."""
        self._reclaim_processed()
        buf_l = self._get_buffer()
        if buf_l is None:
            return False
        buf_r = self._get_buffer()
        if buf_r is None:
            # The left buffer has not been queued yet, so return it to the
            # shared pool and retry this PCM frame on the next iteration.
            self._buffer_pool.append(buf_l)
            return False
        try:
            left, right = self._split_stereo_16(data)
            buf_l.set_data(left, sample_rate=48000, format=cyal.BufferFormat.MONO16)
            buf_r.set_data(right, sample_rate=48000, format=cyal.BufferFormat.MONO16)
            self.spatial_src_l.queue_buffers(buf_l)
            self.spatial_src_r.queue_buffers(buf_r)
            self._remember_frame(left, right)
            self._note_pair_fed(left)
            self._note_fed_content(data)
            return True
        except Exception:
            return False

    # -------------------------------------------------- output handed over
    #
    # A cabinet's cinema mode can be changed while its song plays, and the
    # change is a change of *output*, not of song: nothing here decodes
    # anything or seeks anything. A room reshapes in place on its own (the
    # bank a stream holds is the same object, see ``CinemaSpeakerBank``), so
    # the two methods below are only for the crossings between a room and the
    # plain stereo pair.

    def _remember_frame(self, left, right):
        with self._ring_lock:
            self._fed_ring.append((left, right))

    def pending_room(self):
        """The room this stream is waiting to hand its output to, or None."""
        return self._pending_room

    def pair_queued_frames(self):
        """Frames the plain pair is holding ahead of what is audible."""
        if not self.spatial_pair:
            return 0
        try:
            return min(int(self.spatial_src_l.buffers_queued),
                       int(self.spatial_src_r.buffers_queued))
        except Exception:
            return 0

    # ------------------------------------------- a live note's own clock
    #
    # A note played along to this cabinet waits on the frames the pair has
    # *played*, re-projected every frame, instead of on a queue depth measured
    # once when the note arrived: the queue drains and refills, and a hold that
    # does not follow it walks away from the song -- differently on each
    # machine. The rule lives in one place (``libs/jukebox_clock.py``), shared
    # with a cinema room's bank; what is the streamer's own is the counter and
    # the frame's measured size.

    def _note_pair_fed(self, mono_pcm):
        """Record one frame handed to the plain pair (its own clock's input)."""
        self.pair_frames_fed = int(getattr(self, "pair_frames_fed", 0)) + 1
        measured = mono_ms(mono_pcm)
        if measured > 0.0:
            self._pair_frame_ms = measured

    def _pair_clock_reset(self):
        """Start the pair's clock over (new sources, or a queue that was cut).

        The epochs are forgotten rather than the waits dropped: a note already
        waiting here keeps the instant it was going to sound at, which is what
        a note lost to a hiccup would not have.
        """
        self.pair_frames_fed = 0
        self._pair_frame_ms = 0.0
        clock = getattr(self, "_pair_spot_clock", None)
        if clock is not None:
            clock.forget()

    def pair_frame_ms(self):
        """How long one pair frame is here, measured from the audio (ms).

        Latched from the frames actually handed over, because the transports do
        not agree (this streamer decodes 20 ms; the relay is sent 40 ms), and a
        note's wait is counted in those frames.
        """
        measured = float(getattr(self, "_pair_frame_ms", 0.0) or 0.0)
        return measured if measured > 0.0 else self.SAMPLES_PER_BUFFER / 48.0

    def pair_played_frames(self):
        """Frames the plain pair has already played: fed minus still queued.

        ``pair_queued_frames`` is what OpenAL reports as still ahead of the
        listener and the fed counter is this stream's own, so the difference is
        the content instant the pair is playing right now -- what a live note's
        wait is measured against, every frame.
        """
        fed = int(getattr(self, "pair_frames_fed", 0) or 0)
        return max(0, fed - int(self.pair_queued_frames()))

    def pair_is_playing(self):
        """True while the plain pair is this stream's output, and playing.

        A stream that handed its output to a cinema room has no pair to
        measure, and a pair whose sources are not playing reports everything it
        holds as finished -- neither is a clock for a live note (see
        ``libs/jukebox_clock.py``).
        """
        if self.cinema is not None or not self.spatial_pair:
            return False
        if not self.running or self.paused:
            return False
        # The pair only starts once its own pre-buffer is in (the direct path
        # anchors its audible start), and a queue that has not started says
        # nothing about where the song is.
        return self._all_playing()

    @property
    def pair_clock(self):
        """The plain pair's own clock, made on first use.

        Hand-built instances (tests) bypass ``__init__``, so nothing here is
        assumed to exist before the first call.
        """
        clock = getattr(self, "_pair_spot_clock", None)
        if clock is None:
            clock = SpotClock(
                progress_of=lambda key: self.pair_played_frames(),
                frame_ms_of=self.pair_frame_ms,
                playing_of=lambda key: self.pair_is_playing(),
                keys_of=lambda: (PAIR_KEY,),
                lead_of=lambda key: self.pair_queued_frames(),
            )
            self._pair_spot_clock = clock
        return clock

    def request_room(self, bank):
        """Ask this stream to play through ``bank`` instead of the plain pair.

        A room cannot take over in one step: it is handed one frame per
        speaker, starts only once its pre-buffer is in, and the frames it
        must be given are the ones the pair is *about to* play -- prime it
        from the live edge instead and the room starts a whole queue behind
        the song (heard as the song stepping back the moment a mode is
        picked). So the room is remembered here and committed from the frame
        path (``_commit_room_swap``), where the ring and the pair's own queue
        depth are both known, and the song keeps playing out of the pair
        until then. A stream that cannot make that window refuses the room
        (``swap_room_failed``) and the caller leaves it for the next song.
        """
        self._pending_room = bank
        self._swap_hold_frames = 0
        self.swap_room_failed = False
        return True

    def cancel_room(self, bank=None):
        """Give up a room this stream was asked to take over."""
        if bank is None or bank is self._pending_room:
            self._pending_room = None
        return True

    def switch_to_pair(self, pair):
        """Play this stream through the plain stereo pair from now on.

        ``pair`` is ``(src_l, src_r, reference_distance, max_distance)`` and
        normally comes out of the room that was playing
        (``CinemaSpeakerBank.detach_primary_pair``): its sources already hold
        the frames the room was about to play, so the song keeps its content
        instant and only its shape changes -- the room's other speakers stop
        where they stand, and the two that remain carry the cabinet's own
        stereo. Never refuses: the pair is what plays when a room cannot be
        resolved at all.
        """
        src_l, src_r, ref, maxd = pair
        self.spatial_src_l, self.spatial_src_r = src_l, src_r
        self.spatial_ref, self.spatial_max = float(ref), float(maxd)
        self.spatial_base_gain = max(0.0, min(1.0, self.volume / 100.0))
        self.spatial_pair = (src_l, src_r, float(ref), float(maxd))
        self.cinema = None
        self._pending_room = None
        self.channels = 2
        self.BUFFER_SIZE = self.SAMPLES_PER_BUFFER * self.channels * 2
        self._pool_swap_armed = True
        self._pair_credit_from_room()
        self._update_spatial_gain()
        return True

    def _pair_credit_from_room(self):
        """Start the pair's clock level with the queue it arrived holding.

        A pair taken out of a room keeps the frames the room was about to
        play, so that queue is not a standing debt: the fed counter starts
        level with it and ``pair_played_frames`` begins at zero *played*
        rather than reading the whole queue as still to come (which would
        hold every note at its wall-clock instant for the queue's length).
        """
        self.pair_frames_fed = int(self.pair_queued_frames())
        self._pair_frame_ms = 0.0
        clock = getattr(self, "_pair_spot_clock", None)
        if clock is not None:
            clock.forget()

    def _configure_cinema(self, bank):
        """Give a room taking this stream over the cabinet's own numbers."""
        try:
            bank.set_volume(self.volume)
            bank.set_cabinet_volume(getattr(self, "cabinet_gain", 1.0) * 100.0)
            if getattr(self, "reverb_slot", None) is not None:
                bank.set_reverb(self.reverb_slot)
            if getattr(self, "eq_slot", None) is not None:
                bank.set_eq_slot(self.eq_slot)
            bank.update_output()
        except Exception:
            pass

    def _stop_pair_sources(self):
        self._pair_clock_reset()
        for source in (getattr(self, "spatial_src_l", None),
                       getattr(self, "spatial_src_r", None)):
            if source is None:
                continue
            try:
                source.stop()
            except Exception:
                pass

    def _abandon_room_swap(self):
        """Refuse a room this stream could not prime; the caller hands it back."""
        self._pending_room = None
        self.swap_room_failed = True

    def _commit_room_swap(self):
        """Hand this stream's output to a requested room, if it can be primed.

        Returns False only while the pair's queue is still draining (the
        frame is held and retried -- nothing is dropped). Anything that makes
        the swap impossible refuses it instead: a ring shorter than the room
        wants, a room whose pools refuse the frames, a room that will not
        start.
        """
        bank = self._pending_room
        if bank is None:
            return True
        capacity = int(bank.prime_frames() or 0)
        depth = self.pair_queued_frames()
        if capacity > 0 and depth > capacity:
            if self._swap_hold_frames < self.SWAP_DRAIN_MAX_FRAMES:
                self._swap_hold_frames += 1
                return False
            self._abandon_room_swap()
            return True
        take = max(depth, int(bank.wanted_for_start() or 0))
        with self._ring_lock:
            ring = list(self._fed_ring)
        if take <= 0 or len(ring) < take or (capacity > 0 and take > capacity):
            self._abandon_room_swap()
            return True
        for left, right in ring[-take:]:
            try:
                if not bank.queue_frame(left, right):
                    self._abandon_room_swap()
                    return True
            except Exception:
                self._abandon_room_swap()
                return True
        self._configure_cinema(bank)
        try:
            started = bool(bank.start_playback())
        except Exception:
            started = False
        if not started:
            self._abandon_room_swap()
            return True
        self.cinema = bank
        self.spatial_pair = None
        self._pending_room = None
        self._stop_pair_sources()
        return True

    def _diagnostic_startup_call(self, label, function, *args, **kwargs):
        # This class also handles personal music broadcasts. Only the direct
        # jukebox worker participates in these temporary startup measurements.
        if self.bot is None:
            return probe.worker_call(label, function, *args, **kwargs)
        return function(*args, **kwargs)

    def _resolve_playback_info(self, canonical_url, *, use_cache=False):
        self._media_cache_key = canonical_url
        self._media_cache_entry = None
        self._media_cache_candidate = None
        if not self.running:
            return None
        if use_cache and self._media_cache is not None:
            entry = self._media_cache.get(canonical_url)
            if entry is not None:
                self._media_cache_entry = entry
                return entry.info()
        # Retries bypass the cache, retaining the original isolated resolver
        # and cancellation contract. Only successful prebuffering promotes it.
        fresh = self._diagnostic_startup_call("direct.resolve", YouTubeSearcher.get_stream_info,
                                             canonical_url, cancelled=lambda: not self.running)
        if self.running and self._media_cache is not None:
            self._media_cache_candidate = fresh
        return fresh

    def _invalidate_cached_media(self):
        if self._media_cache is not None and self._media_cache_entry is not None:
            self._media_cache.invalidate(self._media_cache_key, self._media_cache_entry)
        self._media_cache_entry = None

    def _remember_prebuffered_media(self):
        if (self.running and self._media_cache is not None
                and self._media_cache_candidate is not None):
            self._media_cache_entry = self._media_cache.put(
                self._media_cache_key, self._media_cache_candidate)
            if not self.running:
                # Cancellation can arrive during validation/publication. Only
                # remove our own entry, never another worker's newer result.
                self._invalidate_cached_media()
        self._media_cache_candidate = None

    def _read_prebuffer(self):
        """Read and queue the startup buffer for the current ffmpeg process."""
        pre_buffered = 0
        leftover = b''
        deadline = time.time() + 12.0
        while (self.running and pre_buffered < self.PRE_BUFFER_COUNT
               and time.time() < deadline):
            if self.process.poll() is not None and not leftover:
                break
            try:
                needed = self.BUFFER_SIZE - len(leftover)
                chunk = self._diagnostic_startup_call("direct.read", self.process.stdout.read, needed)
                if chunk:
                    leftover += chunk
                elif self.process.poll() is not None:
                    break
                else:
                    time.sleep(0.02)
            except Exception:
                time.sleep(0.02)

            while (len(leftover) >= self.BUFFER_SIZE
                   and pre_buffered < self.PRE_BUFFER_COUNT):
                data = leftover[:self.BUFFER_SIZE]
                leftover = leftover[self.BUFFER_SIZE:]
                with self._lock:
                    if self._diagnostic_startup_call("direct.queue", self._queue_local, data):
                        pre_buffered += 1
                        if self.bot:
                            self._timeline_delay.append(bytes(data))
        return pre_buffered, leftover

    def _hold_direct_start(self, leftover):
        """Hold the prebuffered head until the shared wall-clock deadline.

        Drains ffmpeg while waiting ('-re' keeps producing at media rate, so
        a blocked pipe would stall the decoder and the CDN read behind it)
        into the normal OpenAL staging deque. Returns the partial chunk so
        run()'s streaming loop keeps its leftover contract.
        """
        deadline = self.direct_start_deadline(
            self.start_offset, self.start_offset_received_at,
            self._direct_seek_to, self.room_lead_in_s)
        now = time.monotonic()
        hold = min(self.DIRECT_MAX_ALIGN_WAIT_S, max(0.0, deadline - now))
        if hold > 0.0:
            wake_at = now + hold
            while self.running and self.process is not None:
                if time.monotonic() >= wake_at:
                    break
                try:
                    chunk = self.process.stdout.read1(
                        max(1, self.BUFFER_SIZE - len(leftover)))
                except Exception:
                    break
                if chunk:
                    leftover += chunk
                    while len(leftover) >= self.BUFFER_SIZE:
                        self._pause_buffer.append(leftover[:self.BUFFER_SIZE])
                        leftover = leftover[self.BUFFER_SIZE:]
                elif self.process.poll() is not None:
                    break
                else:
                    time.sleep(0.02)
        if self.running:
            late = time.monotonic() - deadline
            # Expose how far the audible start ran past the shared deadline:
            # jam-note scheduling adds this so notes stay on the beat of the
            # (trailing) music a slow-starting machine actually hears.
            self.direct_late_s = max(0.0, late)
            if late > self.DIRECT_LATE_TOLERANCE_S:
                logger.log(
                    "[AudioStreamer] direct sync: audible start "
                    f"{late:.2f}s past the shared deadline (slow resolve/startup)"
                )
        return leftover

    # ---- Direct-transport shared-timeline math (pure, unit-testable) ----

    @staticmethod
    def direct_start_deadline(start_offset, received_at, seek_to, lead_in=None):
        """Wall-clock instant (received_at's monotonic domain) when the
        prebuffered head should become audible.

        Every listener derives the same value from the same jukebox_play
        broadcast: t_zero — when the room's song position was 0 — is the
        arrival instant minus start_offset, and the room's clock runs one
        lead-in behind the server's audioStartedAt (the price of hearing
        the full intro). Fresh songs and mid-song joins must share that
        shift, otherwise a joiner lands a whole lead-in ahead of the room.

        ``lead_in`` overrides the room's shared lead-in hold. A machine
        that switched itself to direct playback while the room still hears
        the server relay must pass 0.0: the relay room holds no lead-in, so
        holding one would leave it a full lead-in behind the room.
        """
        if lead_in is None:
            lead_in = AudioStreamer.DIRECT_LEAD_IN_S
        t_zero = received_at - start_offset
        return t_zero + lead_in + seek_to

    @staticmethod
    def direct_seek_seconds(start_offset, received_at, now):
        """Input seek for an anchored mid-song join, aimed PAST the position
        projected at audible start.

        Arriving early is corrected by an exact hold at prebuffer-complete;
        arriving late never recovers, because under '-re' pacing skipping
        PCM costs the same wall time as the lateness itself.
        """
        if received_at is None or start_offset <= 0.0:
            return start_offset
        return start_offset + max(0.0, now - received_at) + AudioStreamer.DIRECT_STARTUP_EST_S

    def run(self):
        if not FFMPEG_PATH:
            print("[MusicBot] ffmpeg not found!")
            speak("ffmpeg not found.")
            self.failure_reason = "ffmpeg not found"
            self.running = False
            self._cleanup()
            return

        # Resolve the stream URL if target_url is not already a direct stream.
        canonical_url = self.canonical_url
        target_url = self.audio_url
        input_headers = dict(self.http_headers)
        if "youtube.com" in target_url or "youtu.be" in target_url:
            canonical_url = target_url
            target_url = ""
        if canonical_url and not target_url.startswith(("http://", "https://")):
            fresh = self._resolve_playback_info(canonical_url, use_cache=True)
            if not self.running:
                self._cleanup()
                return
            if fresh:
                target_url = fresh['url']
                input_headers = fresh.get('http_headers') or {}
            else:
                # A failed/timed-out helper must not fall back to in-process
                # extraction or launch ffmpeg with an empty input URL.
                self.failure_reason = "audio link resolution failed"
                self._cleanup()
                return

        def _build_cmd():
            cmd = [FFMPEG_PATH]
            if target_url.startswith(("http://", "https://")):
                # Reconnect on mid-stream drops for EVERY network source,
                # including googlevideo. A CDN connection close near the end
                # of a song used to kill ffmpeg outright (no reconnect flags)
                # and the song ended early — verified locally: ffmpeg exits
                # with an I/O error on a dropped connection, but with
                # reconnect flags it reconnects at the last byte offset
                # (range requests) and the full song plays out. YouTube's
                # signed URLs remain valid for this machine, so reconnecting
                # with the same URL + yt-dlp headers works.
                if "googlevideo.com" in target_url.lower():
                    # Shorter budget: the startup 403 path (stale signed URL)
                    # must re-resolve a fresh URL quickly instead of burning
                    # the full backoff window before giving up.
                    cmd.extend([
                        '-reconnect', '1',
                        '-reconnect_streamed', '1',
                        '-reconnect_on_http_error', '403,429,5xx',
                        '-reconnect_delay_max', '2',
                        '-reconnect_delay_total_max', '6',
                    ])
                else:
                    cmd.extend([
                        '-reconnect', '1',
                        '-reconnect_streamed', '1',
                        '-reconnect_on_http_error', '403,429,5xx',
                        '-reconnect_delay_max', '5',
                        '-reconnect_delay_total_max', '12',
                    ])
            user_agent = next(
                (str(value).strip() for name, value in input_headers.items()
                 if str(name).lower() == 'user-agent'),
                '',
            )
            if user_agent and '\r' not in user_agent and '\n' not in user_agent:
                # ffmpeg has a dedicated option for User-Agent. Using it avoids
                # a built-in Lavf agent overriding the yt-dlp authorization UA.
                cmd.extend(['-user_agent', user_agent])
            header_block = self._ffmpeg_header_block({
                name: value for name, value in input_headers.items()
                if str(name).lower() != 'user-agent'
            })
            if header_block:
                cmd.extend(['-headers', header_block])
            effective_offset = getattr(self, "start_offset", 0.0)
            # Timeline anchoring (jukebox direct only): a fresh song keeps
            # position 0 and waits out the shared lead-in below (adding
            # resolve time here is what used to skip the intro), while a
            # mid-song join seeks PAST the projected audible start and
            # waits the exact residual at prebuffer-complete. Non-anchored
            # workers (personal music bot, livestreams) keep legacy behavior.
            if self.start_offset_received_at is not None:
                if self._join_playing_room:
                    # Joining a room that is already playing (per-listener
                    # direct fallback into a relay room): always seek — the
                    # seek formula cancels the offset entirely and lands this
                    # machine on the room's clock (position = room position +
                    # startup estimate at the audible start).
                    effective_offset = max(0.001, effective_offset)
                    effective_offset = self.direct_seek_seconds(
                        effective_offset, self.start_offset_received_at, time.monotonic())
                elif effective_offset > 0.0:
                    if self._direct_anchor and effective_offset <= self.DIRECT_FRESH_MAX_S:
                        effective_offset = 0.0
                    elif self._direct_anchor:
                        effective_offset = self.direct_seek_seconds(
                            effective_offset, self.start_offset_received_at, time.monotonic())
                    else:
                        # Only compensate the client's resolve delay when RESUMING
                        # mid-song (start_offset > 0). A fresh song must start at 0:
                        # adding the yt-dlp resolve time here made the jukebox skip
                        # past the intro every time the direct fallback was used.
                        effective_offset += max(0.0, time.monotonic() - self.start_offset_received_at)
            self._direct_seek_to = effective_offset if effective_offset > 0.5 else 0.0
            # Input authorization must precede seek. YouTube's signed range
            # request can return 403 when -ss is placed before these headers.
            if effective_offset > 0.5:
                cmd.extend(['-ss', f"{effective_offset:.2f}"])
            cmd.extend([
                '-re',  # Read/decode at media rate; network and OpenAL consume 20 ms frames.
                '-i', target_url,
                '-vn',  # Video containers (mp4/mkv/...) must decode audio only.
                '-f', 's16le',
                '-ar', '48000',
                '-ac', str(self.channels),
                '-loglevel', 'error',
                'pipe:1'
            ])
            return cmd

        try:
            cmd = _build_cmd()
        except Exception as ex:
            logger.log_exception(ex, "AudioStreamer.run command")
            self.failure_reason = "playback command error"
            self.running = False
            self._cleanup()
            return

        # Initialize buffer pool
        self._diagnostic_startup_call("direct.buffers", self._init_buffer_pool)

        # Start background network sender thread ONLY when this stream actually
        # broadcasts (bot attached). Jukebox players (bot=None) play locally only
        # and must never re-broadcast the song as their own music bot stream.
        if self.bot:
            self.sender_thread = threading.Thread(target=self._network_sender_loop, daemon=True)
            self.sender_thread.start()

        # === Pre-buffer phase: fill LOCAL buffers before starting playback ===
        # Some fresh googlevideo URLs briefly return 403 while their CDN edge
        # authorization propagates. Retry — first with the same URL+headers, then
        # with a freshly RE-RESOLVED URL (a stale signed URL 403s forever no
        # matter how many times the identical command is retried). All work stays
        # on this worker thread.
        pre_buffered = 0
        _pre_leftover = b''
        error_detail = ""
        for attempt in range(4):
            stalled = False
            if not self.running:
                break
            try:
                self.process = self._diagnostic_startup_call("direct.launch", subprocess.Popen,
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
                )
            except Exception as ex:
                logger.log_exception(ex, "AudioStreamer.run Popen")
                self.failure_reason = "playback launch error"
                break

            pre_buffered, _pre_leftover = self._diagnostic_startup_call("direct.prebuffer", self._read_prebuffer)
            if pre_buffered > 0 or not self.running:
                break

            try:
                if self.process.poll() is not None:
                    error_detail = self.process.stderr.read(4096).decode(
                        'utf-8', 'replace'
                    ).strip()
                else:
                    # Alive and silent after the whole pre-buffer window: ffmpeg
                    # is inside its own reconnect attempt (nothing is logged at
                    # `-loglevel error` until it gives up) against a host that
                    # is not answering. There is no 403 to match on, but
                    # repeating this URL is exactly what it has been doing for
                    # twelve seconds, so this counts as a failed link -- a
                    # freshly resolved one points at another CDN edge.
                    stalled = True
            except Exception:
                error_detail = ""

            try:
                self.process.kill()
                self.process.wait(timeout=2)
            except Exception:
                pass
            self.process = None

            if not self.running:
                break
            cached_attempt = self._media_cache_entry is not None
            self._invalidate_cached_media()
            if cached_attempt and attempt < 3:
                # A cached URL may have expired or become IP-bound. Never
                # repeat it or sleep before requesting a fresh local URL.
                # This consumes the existing retry budget, not an extra loop.
                fresh = self._resolve_playback_info(canonical_url)
                if not self.running:
                    break
                if not fresh:
                    self.failure_reason = "audio link resolution failed"
                    break
                target_url = fresh['url']
                input_headers = fresh.get('http_headers') or {}
                cmd = _build_cmd()
                continue

            retryable = stalled or any(tok in error_detail for tok in ("403", "429", "503", "connection", "timeout", "reset"))
            if not retryable or attempt >= 3:
                break
            # A stall is refreshed on the FIRST attempt too (the retry budget
            # otherwise spends itself re-running the command the stalled host
            # already ignored for twelve seconds).
            if (stalled or attempt >= 1) and canonical_url \
                    and ("youtube.com" in canonical_url or "youtu.be" in canonical_url):
                # The exact URL+headers already failed once — grab a fresh
                # signed stream URL instead of re-running the same command.
                fresh = self._resolve_playback_info(canonical_url)
                if not self.running:
                    break
                if fresh and fresh.get('url'):
                    target_url = fresh['url']
                    input_headers = fresh.get('http_headers') or {}
                    try:
                        cmd = _build_cmd()
                    except Exception:
                        pass
            time.sleep(1.0 + attempt)

        if not self.running:
            # Intentional cancellation (map change, newer playback generation,
            # or stop) is not a load failure and must stay silent.
            self._cleanup()
            return

        if pre_buffered == 0:
            if self.failure_reason is None:
                self.failure_reason = "stream produced no audio"
            error_summary = ""
            if error_detail:
                error_summary = next(
                    (line.strip() for line in reversed(error_detail.splitlines())
                     if line.strip()),
                    "",
                )
            logger.log(
                "[AudioStreamer] stream produced no audio (channels="
                f"{self.channels}, url={self.audio_url[:80]!r}, "
                f"ffmpeg={error_summary[-240:]!r})"
            )
            if self.bot is None:
                speak("The jukebox song could not be loaded. Try another song.")
            self.running = False
            self._cleanup()
            return

        # Start local playback after pre-buffering (spatial pairs also need
        # their distance fade computed before the first audible sample).
        if pre_buffered > 0:
            self.prebuffer_event.set()
            self._remember_prebuffered_media()
            if not self.running:
                self._cleanup()
                return
            if self._direct_anchor:
                # Shared-timeline hold: every listener becomes audible at the
                # same wall-clock instant regardless of resolve/startup time.
                _pre_leftover = self._hold_direct_start(_pre_leftover)
                if not self.running:
                    self._cleanup()
                    return
            if not self.paused:
                if self.spatial_active:
                    self._diagnostic_startup_call("direct.spatial", self._update_spatial_gain)
                self._diagnostic_startup_call("direct.first_play", self._play_all)
                self.ready_event.set()
                # Frame zero leaves at the same instant local OpenAL begins
                # frame zero. Subsequent decoded frames advance this fixed
                # delay line.
                self._route_aligned_network_frame()
            # A stream created paused (seek while paused) holds its prebuffer
            # silently; set_pause(False) starts it and announces it later.

        # === Streaming loop ===
        eof = False
        _leftover = _pre_leftover  # Carry over any leftover bytes from pre-buffer
        while self.running:
            if self.paused:
                time.sleep(0.05)
                continue

            data = None
            if not eof:
                # Accumulate partial reads until we have a full frame.
                # Only set eof when read() returns empty (ffmpeg closed pipe).
                while len(_leftover) < self.BUFFER_SIZE:
                    proc = self.process
                    if proc is None:
                        eof = True
                        break
                    chunk = proc.stdout.read(self.BUFFER_SIZE - len(_leftover))
                    if not chunk:  # Real EOF: ffmpeg closed the pipe
                        eof = True
                        break
                    _leftover += chunk
                if len(_leftover) >= self.BUFFER_SIZE:
                    data = _leftover[:self.BUFFER_SIZE]
                    _leftover = _leftover[self.BUFFER_SIZE:]
                elif _leftover and eof:
                    # Flush remaining partial data at the very end
                    data = _leftover
                    _leftover = b''

            if not self.running:
                break

            # === NETWORK: Send at the performer's audible media position ===
            if data:
                self._route_aligned_network_frame(data)
            elif eof and self._timeline_delay:
                # ffmpeg has ended, but OpenAL still owns the pre-buffer tail.
                # Flush one aligned frame per loop so listeners hear the same
                # ending instead of losing the final ~180ms.
                self._route_aligned_network_frame()

            # === LOCAL: Buffer for OpenAL playback ===
            if data:
                self._pause_buffer.append(data)

            with self._lock:
                if not self.running:
                    break
                try:
                    # [FIX]: Always reclaim processed buffers so buffers_queued drops to 0 at EOF
                    self._reclaim_processed()

                    # Keep the spatial pair's distance fade up to date as the
                    # listener walks (cheap: one distance check per speaker).
                    if self.spatial_active:
                        self._update_spatial_gain()

                    # Drain pause buffer into OpenAL
                    while self._pause_buffer:
                        chunk = self._pause_buffer[0]
                        if self._queue_local(chunk):
                            self._pause_buffer.popleft()
                        else:
                            break  # No available OpenAL buffers, wait

                    # Restart if source stopped and we have buffers queued (only if new buffers are queued and not EOF)
                    if not eof and not self._all_playing() and self._buffers_queued() > 0:
                        self._start_output_playing()
                except Exception:
                    pass

            if (eof and not self._pause_buffer
                    and not self._timeline_delay
                    and self._buffers_queued() == 0):
                break
                
            # Sleep a bit to prevent busy-waiting ONLY if we didn't read any data
            # (e.g. EOF reached, but OpenAL is still playing the last few buffers).
            # Do NOT sleep during normal streaming — ffmpeg's '-re' flag already
            # paces stdout.read() perfectly. Sleeping here adds Windows scheduler jitter.
            if not data:
                time.sleep(0.02)

        # Wait for remaining buffers to finish playing
        if self.running:
            try:
                # Keep checking and unqueuing until all queued buffers are processed
                while self._buffers_queued() > 0 and self.running:
                    with self._lock:
                        self._reclaim_processed()
                        if self.spatial_active:
                            self._update_spatial_gain()
                    # If we are paused at the very end, wait here until resumed
                    if not self.paused and not self._all_playing() and self._buffers_queued() > 0:
                        self._start_output_playing()
                    time.sleep(0.05)
            except Exception:
                pass

        if self.running:
            # Distinguish a natural song end from a mid-song ffmpeg death (403
            # on a CDN reconnect, connection reset, ...). ffmpeg exits 0 on a
            # clean EOF; any other exit code after audio already started means
            # the stream died EARLY. Mark it a failure so the jukebox recovery
            # watchdog rebuilds with a fresh resolve instead of treating the
            # silence as a finished song ("music disappears before the end").
            exit_code = None
            try:
                if self.process is not None:
                    exit_code = self.process.poll()
            except Exception:
                exit_code = None
            if exit_code not in (None, 0):
                self.failure_reason = f"ffmpeg exited early (code {exit_code})"
                self._invalidate_cached_media()
            else:
                self.completed_normally = True

        # Cleanup
        self._cleanup()

    def _cleanup(self):
        """Clean up ffmpeg process and buffers"""
        with self._cleanup_lock:
            if self._cleaned_up:
                return
            self._cleaned_up = True
        self.running = False
        if getattr(self, "ytdlp_process", None):
            try:
                self.ytdlp_process.kill()
                self.ytdlp_process.wait(timeout=2)
            except Exception:
                pass
            self.ytdlp_process = None
        if self.process:
            try:
                self.process.kill()
                self.process.wait(timeout=2)
            except Exception:
                pass
            self.process = None
        # Drain remaining buffers (both sources for spatial pairs)
        for src in self._all_sources():
            try:
                src.stop()
                drain_limit = 64
                while src.buffers_processed > 0 and drain_limit > 0:
                    src.unqueue_buffers()
                    drain_limit -= 1
            except Exception:
                pass
        self._buffer_pool.clear()
        self._pause_buffer.clear()
        # Drain the network queue to free references
        while not self.network_queue.empty():
            try:
                self.network_queue.get_nowait()
            except Exception:
                pass

    def stop(self):
        self.running = False
        self._cleanup()

    def resume_output_if_buffered(self):
        """Restart a stopped OpenAL output without replacing this stream.

        This is intentionally limited to already queued frames.  Starting a
        fresh stream here would lose timing and can duplicate a broadcast;
        the decoder thread remains the owner of buffering new audio.
        """
        with self._lock:
            if not self.running or self.paused or self._buffers_queued() <= 0:
                return False
            if not self._all_playing():
                self._start_output_playing()
                return True
        return False

    def set_pause(self, paused):
        self.paused = paused
        if paused:
            # The local source can pause in-place, but queued network PCM has
            # no timestamp.  It must be discarded or listeners will hear it
            # after resume as a second, delayed copy of the song.
            while True:
                try:
                    self.network_queue.get_nowait()
                except queue.Empty:
                    break
        with self._lock:
            try:
                if self.cinema is not None:
                    # A cinema room is N speakers, and pausing only the one this
                    # stream was handed leaves the rest of them playing their
                    # queues -- and then replaying from the front of those
                    # queues on resume, which is heard as speakers that never
                    # line up again after a pause. The bank holds them all,
                    # and puts a speaker that ran dry during the hold back on
                    # the room's content instant before resuming.
                    self.cinema.set_paused(paused)
                    if not paused:
                        self.ready_event.set()
                    return
                if paused:
                    self.source.pause()
                else:
                    self.source.play()
                    # A stream created paused never set ready at prebuffer
                    # (nothing is audible yet) — announce on first resume.
                    self.ready_event.set()
            except Exception:
                pass


class LiveRelayStreamer(threading.Thread):
    """Stand-alone streaming thread for live instrument (guitar/mic) PCM when no MP3 audio is playing."""
    def __init__(self, game, bot):
        super().__init__(daemon=True)
        self.game = game
        self.bot = bot
        self.running = True
        from pyogg import OpusEncoder
        self.encoder = OpusEncoder()
        self.encoder.set_application('audio')
        self.encoder.set_channels(1)  # Opus network stream is MONO
        self.encoder.set_sampling_frequency(48000)
        self.last_send_time = None

    def stop(self):
        self.running = False

    def run(self):
        import audioop
        from .. import consts
        while self.running and self.bot and (
            getattr(self.bot, 'broadcast_enabled', False)
            or getattr(self.bot, 'broadcast_to_megaphone', False)
            or getattr(self.bot, 'party_sync_force_upload', False)
        ):
            # If main AudioStreamer MP3 thread is active, let it handle the network stream
            if self.bot.streamer and self.bot.streamer.is_alive():
                break

            guitar_data = None
            mic_data = None
            try:
                if getattr(self.bot, 'guitar_pcm_queue', None) and self.bot.guitar_pcm_queue:
                    guitar_data = self.bot.guitar_pcm_queue.popleft()
            except Exception:
                pass
            try:
                if getattr(self.bot, 'mic_pcm_queue', None) and self.bot.mic_pcm_queue:
                    mic_data = self.bot.mic_pcm_queue.popleft()
            except Exception:
                pass

            if guitar_data is None and mic_data is None:
                time.sleep(0.010)
                continue

            # Base silent mono PCM buffer (20ms at 48kHz = 960 samples * 2 bytes = 1920 bytes)
            mono_data = b'\x00' * 1920
            try:
                def _align(b, length=1920):
                    if len(b) > length:
                        return b[:length]
                    if len(b) < length:
                        return b + b'\x00' * (length - len(b))
                    return b

                if guitar_data is not None:
                    mono_data = audioop.add(mono_data, audioop.mul(_align(guitar_data), 2, 0.85), 2)
                if mic_data is not None:
                    mono_data = audioop.add(mono_data, audioop.mul(_align(mic_data), 2, 0.85), 2)

                current_volume_scale = (self.bot.volume / 100.0) * getattr(self.bot, 'duck_multiplier', 1.0)
                if current_volume_scale != 1.0:
                    mono_data = audioop.mul(mono_data, 2, current_volume_scale)

                party_private = bool(getattr(self.bot, 'party_sync_force_upload', False))
                target_channel = (
                    consts.CHANNEL_MUSICBOT
                    if (not self.bot.broadcast_to_megaphone or party_private)
                    else consts.CHANNEL_MEGAPHONE
                )

                # Local zero-latency PA monitoring (the server no longer echoes
                # the megaphone broadcast back to the sender). Skipped while a
                # Party Sync session is active (private-to-guests leg).
                if self.bot.broadcast_to_megaphone and not party_private:
                    try:
                        gp = self.bot._find_gameplay()
                        if gp is not None:
                            from .. import voice_chat
                            if hasattr(voice_chat, '_feed_local_megaphone_direct'):
                                local_pcm = bytes(mono_data)
                                voice_chat._feed_local_megaphone_direct(gp, local_pcm, producer='music')
                    except Exception:
                        pass

                # Rate-limit to 20ms pacing
                now = time.perf_counter()
                if self.last_send_time is not None:
                    elapsed = now - self.last_send_time
                    if elapsed < 0.020:
                        sleep_time = 0.020 - elapsed
                        if sleep_time > 0.001:
                            time.sleep(sleep_time - 0.001)
                        while time.perf_counter() - self.last_send_time < 0.020:
                            pass
                self.last_send_time = time.perf_counter()

                encoded = self.encoder.encode(bytearray(mono_data))
                if self.game and self.game.network:
                    self.game.network.send(target_channel, "n/a", encoded, reliable=False)
            except Exception:
                time.sleep(0.010)


