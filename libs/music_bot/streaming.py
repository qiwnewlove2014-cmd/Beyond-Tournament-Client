"""Music Bot streaming layer - the ffmpeg decode + OpenAL playback thread.

``AudioStreamer`` owns the song path, including the paced 20 ms Opus network
sender and direct/jam alignment. The live-instrument relay lives in
``live_relay.py`` and is re-exported here so existing imports keep working.
"""

import queue
import random
import subprocess
import threading
import time
from collections import deque

import cyal

from .. import consts
from .. import logger
from .. import voice_chat
from .. import audio_trace
from ..audio_diagnostics import probe
from ..jukebox_clock import PAIR_KEY, SpotClock, mono_ms
from ..speech import speak
from .media import FFMPEG_PATH
from . import transport
from . import output
from . import spatial
from . import network
from .transpose import ChromaticTransposer

from .live_relay import (
    LiveRelayStreamer, live_relay_has_input, live_relay_needed,
    mix_live_stereo, sequence_pa_frame,
)


class AudioStreamer(threading.Thread):
    """Background thread: ffmpeg decodes audio URL -> raw PCM -> queued to OpenAL.

                YouTube URL -> ffmpeg (s16le mono 48 kHz) -> OpenAL buffer queue, and the same PCM ->
                Opus -> network broadcast rate-limited to real time (one 20 ms frame per ~20 ms) so
                receivers are not bursted. One song runs as one phase per method, in `run`'s order:
                `_resolve_input` -> `_build_cmd` -> `_prepare_output` -> `_prebuffer_flight` (the
                flight loop that retries a dead link and re-aims a late start, see the jam-note-timing
                skill) -> `_begin_playback` -> `_feed_and_drain` -> `_classify_end`.
                Rules: .agents/skills/music_bot_integration/ and
                .agents/skills/jukebox-system/.
                """

    # Hand-built instances (tests, diagnostics tools) bypass __init__, so both
    # mode flags need class defaults for the shared playback paths to work.
    cinema = None
    spatial_pair = None
    # ... and so does the player's own output device (music_bot/output_device.py):
    # a stream built by hand -- a test, a diagnostics tool -- plays where the
    # game plays, which is what an unset option means too.
    remote = None
    remote_slot = 0
    remote_category = 1.0
    # The music bot's PA sequence (channel 31). Class defaults for the same
    # reason as the flags above; the send site stamps the epoch on first use
    # (voice_chat.pa_timeline_epoch), so a hand-built instance streams too.
    _pa_epoch = None
    _pa_seq = 0
    # The song's key (libs/music_bot/transpose.py). Class defaults so a
    # hand-built stream -- a test, a diagnostics tool -- plays at the song's
    # own pitch; the bot turns the dial through set_transpose().
    transpose_semitones = 0
    transposer = None

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
                          # 100 ms delay line (was 10/200 ms, lowered together with MusicCompression.PRE_BUFFER_FRAMES so
                          # live music streams stay low-latency on both ends).
                          #
                          # Direct-transport jukebox timeline alignment: in direct mode every listener resolves and starts
                          # its own ffmpeg, so all clients derive ONE wall-clock deadline from the server's jukebox_play
                          # broadcast (arrival instant minus start_offset) and hold the prebuffered audio until then -
                          # resolve variance becomes wait time, never audible skew. Bots and livestreams never anchor.
    DIRECT_LEAD_IN_S = 3.5         # fresh song: hold the intro this long after the broadcast
                                   # (matches the server's end-of-song grace; machines slower
                                   # than this join late and trail the room for the song)
    DIRECT_FRESH_MAX_S = 2.0       # a play event this young is a fresh broadcast (start_offset
                                   # ≈ its age): hold position 0. Older offsets are resumes /
                                   # reload re-broadcasts whose intro the room already heard —
                                   # they must seek, never replay.
    DIRECT_STARTUP_EST_S = 4.5     # mid-song: aim -ss past the projected audible start;
                                   # Arriving early is correctable by an exact wait, arriving late never recovers under '-re'
                                   # pacing. This estimate IS the alignment slack (LEAD_IN + EST after resolve): a machine whose
                                   # seek+prebuffer outruns it starts late and trails the room for the whole song, so its jam notes
                                   # land off the beat. 4.5 s keeps typical seeks inside the slack at the cost of skipping ~3 s more
                                   # of the song for joiners/resumes.
    DIRECT_ALIGN_SLACK_S = DIRECT_LEAD_IN_S + DIRECT_STARTUP_EST_S
    DIRECT_MAX_ALIGN_WAIT_S = DIRECT_ALIGN_SLACK_S + 1.0  # clock safety valve
    DIRECT_LATE_TOLERANCE_S = 0.75 # best-effort: log joins later than this
    # A machine whose own resolve+startup outran the shared deadline becomes audible late and plays
    # the WHOLE song that far behind the room (``direct_late_s``), so its notes land behind the beat
    # for everybody. It is re-aimed ONCE, at the room's position, and the frames it decoded behind
    # the room are dropped rather than played. The price is silence - the second launch's startup -
    # so the gate is ``direct_catch_up_aim_s``: past the alignment slack the cure costs more than the
    # drift, and below DIRECT_CATCH_UP_MIN_S nobody hears it.
    DIRECT_CATCH_UP_MAX = 1
    DIRECT_CATCH_UP_MIN_S = DIRECT_LATE_TOLERANCE_S
    DIRECT_CATCH_UP_MARGIN_S = 0.5

    def __init__(self, game, audio_url, source, volume=50, bot=None, channels=2,
                 spatial_pair=None, start_offset=0.0, http_headers=None,
                 start_offset_received_at=None, canonical_url=None, media_cache=None,
                 timeline_anchor=None, start_paused=False, room_lead_in_s=None,
                 join_playing_room=False, cinema=None, remote=None,
                 remote_slot=0, remote_category=1.0):
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
        # Lead-in the ROOM holds before a fresh song becomes audible. Every machine anchored to the same
        # broadcast holds it, so it cancels out of note timing. A machine playing direct while the rest
        # of the room hears the server RELAY must anchor with 0.0 instead: the relay room holds no
        # lead-in, and holding one leaves it exactly DIRECT_LEAD_IN_S behind for the whole song.
        self.room_lead_in_s = (
            AudioStreamer.DIRECT_LEAD_IN_S if room_lead_in_s is None
            else float(room_lead_in_s)
        )
        # Per-listener direct fallback into a room that is ALREADY playing (server relay):
        # the song position advances from the broadcast instant, so even a zero-offset
        # event must seek PAST the projected audible start - starting at 0 would trail the
        # room by this machine's whole resolve+startup.
        self._join_playing_room = bool(join_playing_room)
        self._direct_seek_to = 0.0        # content position the decode head starts from
        self._fed_content_seconds = 0.0   # content seconds queued to OpenAL since the seek
        # Seconds the audible start ran past the shared wall-clock deadline
        # (slow yt-dlp resolve / ffmpeg startup). Jam-note scheduling reads
        # this so remote notes wait out a local stream that trails the room.
        self.direct_late_s = 0.0
        # The one re-aim a late audible start is allowed (see
        # ``direct_catch_up_aim_s``): whether it has been spent, the room
        # instant it targeted, and the launch instant the aim was measured
        # from.
        self._catch_up_used = False
        self._join_aim_ahead_s = None
        self._attempt_started_at = None
        self.direct_catch_up_s = 0.0
        self.http_headers = dict(http_headers or {})
        # Spatial stereo pair (jukeboxes): two MONO sources at the same spot minus/plus a small offset,
        # fed with the LEFT and RIGHT channels of a STEREO decode, so they give a real stereo image up
        # close that collapses toward mono at distance. `spatial_pair` is (src_l, src_r,
        # reference_distance, max_distance). A cinema room REPLACES the pair rather than joining it -
        # both cannot play or the room hears the song twice and combs it - and the same interleaved
        # decode is handed to the bank.
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
        # The player's other device (music_bot/output_device.py): while this is
        # set, every frame goes to that process instead of into this one's
        # OpenAL, and this stream's own ear source stays silent. A cinema room
        # or a spatial pair wins over it -- those are map objects in the game's
        # world, and a device in the player's other ear has no position in it.
        self.remote = (None if (cinema is not None or spatial_pair is not None)
                       else remote)
        self.remote_slot = remote_slot
        # What the music category multiplies the volume by; the ear source's
        # gain already carries it (see MapMusicBot._new_bot_source).
        self.remote_category = float(remote_category or 1.0)
        if self.remote is not None:
            try:
                self.remote.set_format(self.remote_slot, self.channels)
                self.remote.set_gain(self.remote_slot,
                                     self.remote_category * volume / 100.0)
            except Exception:
                self.remote = None
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
        # At EOF an output that has stopped taking the tail must not keep this
        # thread alive (see _track_tail_progress).
        self._tail_queued = None
        self._tail_stall_at = None
        self._tail_abandoned = False
        # The key shifter's own look-ahead, given back once at the end of the
        # song (only ever to the local output -- see _flush_transpose).
        self._tail_returned = False
        self._transpose_lock = threading.Lock()
        self.transposer = None
        self.transpose_semitones = 0
        if bot is not None:
            try:
                value = int(getattr(bot, "transpose_semitones", 0) or 0)
            except (TypeError, ValueError):
                value = 0
            self.transpose_semitones = max(
                -ChromaticTransposer.MAX_SEMITONES,
                min(ChromaticTransposer.MAX_SEMITONES, value))
        # Crossfade bookkeeping: a retired outgoing stream whose network leg
        # must go silent the instant the next track takes over (two Music Bot
        # streams must never overlap on the PA / broadcast channel).
        self.network_muted = False
        # Network-leg crossfade state: (partner_streamer, monotonic start,
        # seconds). While set, the network sender blends this stream's own
        # frames with the partner's (fade-out / fade-in) so broadcast and PA
        # listeners hear the same overlap as the local performer.
        self._network_mix = None
        # Monotonic instant the speakers last consumed an OpenAL buffer (jukebox direct only - see
        # _reclaim_processed). The jukebox watchdog rebuilds a stream whose OUTPUT stalls even though its
        # thread is alive, since direct playback has no server relay to announce a mid-song death.
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
        """All OpenAL sources this stream feeds (see output.py)."""
        return output.all_sources(self)

    def _play_all(self):
        output.play_all(self)

    def _all_playing(self):
        return output.all_playing(self)

    def _start_output_playing(self):
        """Start whichever sources are not playing yet (see output.py)."""
        output.start_output_playing(self)

    def _buffers_queued(self):
        return output.buffers_queued(self)

    @staticmethod
    def _split_stereo_16(data):
        """Split interleaved s16le stereo bytes into (left, right) mono bytes."""
        return output.split_stereo_16(data)

    @staticmethod
    def _mix_live_into_stereo(payload, live_frames, state_key):
        """Add the mono mic/line-in mix to both sides (see spatial.py)."""
        return spatial.mix_live_into_stereo(payload, live_frames, state_key)

    @staticmethod
    def _mix_external_into_stereo(payload, external_frames, state_key):
        """Add a captured program's own sides (see spatial.py)."""
        return spatial.mix_external_into_stereo(payload, external_frames, state_key)

    def set_cabinet_volume(self, volume):
        """Set the cabinet's own level and re-apply the fade (see spatial.py)."""
        spatial.set_cabinet_volume(self, volume)

    def _update_spatial_gain(self):
        """Distance fade / occlusion for the spatial pair (see spatial.py)."""
        spatial.update_spatial_gain(self)

    def set_output_gain(self, gain):
        """One absolute gain for this stream's output, wherever it plays.

        The crossfade ramps two streams against each other, and on a chosen
        device that ramp is a gain in the child process, not an OpenAL source
        here -- one call site so the two can never disagree.
        """
        gain = max(0.0, min(1.0, float(gain)))
        if self.remote is not None:
            try:
                self.remote.set_gain(self.remote_slot, gain)
            except Exception:
                pass
            return
        try:
            if self.source is not None:
                self.source.gain = gain
        except Exception:
            pass

    def set_volume(self, volume):
        """Update playback volume in real time."""
        self.volume = volume
        self.spatial_base_gain = max(0.0, min(1.0, volume / 100.0))
        if self.remote is not None:
            try:
                self.remote.set_gain(self.remote_slot,
                                     self.remote_category * volume / 100.0)
            except Exception:
                pass
            return
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
        """Pre-allocate OpenAL buffers for reuse (see output.py)."""
        output.init_buffer_pool(self)

    @staticmethod
    def _ffmpeg_header_block(headers):
        """Build an injection-safe CRLF header block for ffmpeg's input."""
        return transport.ffmpeg_header_block(headers)

    def _get_buffer(self):
        """Get a reclaimed buffer without growing the pool (see output.py)."""
        return output.get_buffer(self)

    def _reclaim_processed(self):
        """Return processed buffers to the pool (see output.py)."""
        output.reclaim_processed(self)

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
        """Queue a PCM chunk for the sender thread (see network.py)."""
        network.send_to_network(self, data, timeline_epoch, timeline_seq)

    def _route_aligned_network_frame(self, decoded_frame=None):
        """Advance the broadcast delay line (see network.py)."""
        network.route_aligned_network_frame(self, decoded_frame)

    def begin_network_crossfade(self, partner, seconds):
        """Blend two network legs (see network.py)."""
        network.begin_network_crossfade(self, partner, seconds)

    def _mix_network_frames(self, own, partner, own_gain, partner_gain):
        """Convex blend of two frames (see network.py)."""
        return network.mix_network_frames(own, partner, own_gain, partner_gain)

    def _network_sender_loop(self):
        """Paced network sender thread (see network.py)."""
        network.network_sender_loop(self)

    def _send_to_network_actual(self, data, timeline_epoch=None, timeline_seq=None):
        """Encode and send one frame (see network.py)."""
        network.send_to_network_actual(self, data, timeline_epoch, timeline_seq)

    def _note_fed_content(self, data):
        """Track the media position already handed to OpenAL (jukebox retire math)."""
        self._fed_content_seconds += len(data) / (48000.0 * self.channels * 2)

    def content_position(self):
        """Approximate media position (seconds) of the local decode head."""
        return self._direct_seek_to + self._fed_content_seconds

    # ------------------------------------------------------- the song's key
    # The transposer itself is libs/music_bot/transpose.py; this is only where
    # it sits in the song's path: every frame the decode produces passes it
    # before the local output, the broadcast and a room hear anything. Because
    # it returns the same number of frames it was given, the song's position --
    # content_position, the end-of-song hand-over, the pair clock and the
    # network timeline -- is untouched by a key change.

    def set_transpose(self, semitones):
        """Move this song's key by whole semitones (0 is the song's own key)."""
        try:
            value = int(round(float(semitones or 0)))
        except (TypeError, ValueError):
            value = 0
        value = max(-ChromaticTransposer.MAX_SEMITONES,
                    min(ChromaticTransposer.MAX_SEMITONES, value))
        with self._transpose_lock:
            self.transpose_semitones = value
            if self.transposer is None:
                if not value:
                    return value
                self.transposer = ChromaticTransposer(channels=self.channels)
            self.transposer.set_semitones(value)
        return value

    def _shape_frame(self, data):
        """The song's own pitch, before any output hears it."""
        if self.transposer is None:
            if not self.transpose_semitones:
                return data
            with self._transpose_lock:
                if self.transposer is None:
                    self.transposer = ChromaticTransposer(channels=self.channels)
                    self.transposer.set_semitones(self.transpose_semitones)
        with self._transpose_lock:
            shaped = self.transposer.process(data)
        # A trailing partial sample at the very end of a song is not a frame
        # and is dropped rather than padded (see process()).
        return shaped

    def _flush_transpose(self):
        """The look-ahead the key shifter still holds, at the end of the song.

        Local output only: the tail is not a whole 20 ms Opus frame, and the
        broadcast's last frames are not worth a malformed one. A stream that
        never engaged the shifter has nothing to give back.
        """
        if self.transposer is None:
            return None
        with self._transpose_lock:
            tail = self.transposer.flush()
        return tail or None

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
        if self.remote is not None:
            if not self.remote.alive():
                # The other device's process is gone (unplugged headset, a
                # killed child): carry on where the game plays rather than
                # freezing the song against a dead pipe. The bot notices the
                # same fact on the next song and says it once.
                self.remote = None
            else:
                # False means its queue is full, which is the same "come back
                # for this frame" a spent buffer pool is.
                if self.remote.push(data, self.remote_slot):
                    self._note_fed_content(data)
                    return True
                return False
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

                                The bank renders it into one MONO feed per speaker and queues each into that
                                speaker's own pool, so this stream never touches a buffer or a source directly in
                                cinema mode.

                                A frame the room accepted still counts as played content exactly as on a plain
                                source: ``content_position()`` measures the Music Bot's seek and the Jukebox's
                                end-of-song hand-over, and a room is the one output whose frames never pass
                                through ``_queue_local``. Crediting per ACCEPTED frame keeps that honest while the
                                pre-buffer retries a frame the room has no buffer for yet.
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
    # A cabinet's cinema mode can be changed while its song plays, and that is a change of *output*,
    # not of song: nothing here decodes anything or seeks anything. A room reshapes in place on its
    # own (the bank a stream holds is the same object), so the two methods below are only for the
    # crossings between a room and the plain stereo pair.

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
    # A note waits on the frames the pair has *played*, re-projected every frame, instead of a queue
    # depth measured once when the note arrived: the queue drains and refills, and a hold that does
    # not follow it walks away from the song, differently on each machine. The rule lives in one
    # place (``libs/jukebox_clock.py``); what is the streamer's own is the counter and the frame's
    # measured size.

    def _note_pair_fed(self, mono_pcm):
        """Record one frame handed to the plain pair (its own clock's input)."""
        self.pair_frames_fed = int(getattr(self, "pair_frames_fed", 0)) + 1
        measured = mono_ms(mono_pcm)
        if measured > 0.0:
            self._pair_frame_ms = measured

    def _pair_clock_reset(self):
        """Start the pair's clock over (new sources, or a queue that was cut).

                                The epochs are forgotten rather than the waits dropped: a note already waiting keeps
                                the instant it was going to sound at.
                                """
        self.pair_frames_fed = 0
        self._pair_frame_ms = 0.0
        clock = getattr(self, "_pair_spot_clock", None)
        if clock is not None:
            clock.forget()

    def pair_frame_ms(self):
        """How long one pair frame is here, measured from the audio (ms).

                                Latched from the frames actually handed over, because the transports do not agree
                                (this streamer decodes 20 ms, the relay is sent 40 ms).
                                """
        measured = float(getattr(self, "_pair_frame_ms", 0.0) or 0.0)
        return measured if measured > 0.0 else self.SAMPLES_PER_BUFFER / 48.0

    def pair_played_frames(self):
        """Frames the plain pair has already played: fed minus still queued.

                                ``pair_queued_frames`` is what OpenAL reports ahead of the listener, so the
                                difference is the content instant the pair is playing right now.
                                """
        fed = int(getattr(self, "pair_frames_fed", 0) or 0)
        return max(0, fed - int(self.pair_queued_frames()))

    def pair_is_playing(self):
        """True while the plain pair is this stream's output, and playing.

                                A stream that handed its output to a cinema room has no pair to measure, and a pair
                                whose sources are not playing reports everything it holds as finished - neither is a
                                clock for a live note (see ``libs/jukebox_clock.py``).
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

                    Hand-built instances (tests) bypass ``__init__``, so nothing here is assumed to exist before
                    the first call.
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

                                A room cannot take over in one step: it is handed one frame per speaker, starts only
                                once its pre-buffer is in, and the frames it must be given are the ones the pair is
                                *about to* play - priming it from the live edge makes the room start a whole queue
                                behind the song (heard as the song stepping back). So the room is remembered here and
                                committed from the frame path (``_commit_room_swap``), where the ring and the pair's
                                queue depth are both known. A stream that cannot make that window refuses the room
                                (``swap_room_failed``).
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

                                ``pair`` is ``(src_l, src_r, reference_distance, max_distance)`` and normally comes out
                                of the room that was playing (``CinemaSpeakerBank.detach_primary_pair``): its sources
                                already hold the frames the room was about to play, so the song keeps its content
                                instant and only its shape changes. Never refuses - the pair is what plays when a
                                room cannot be resolved at all.
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

                                A pair taken out of a room keeps the frames the room was about to play, so that queue is
                                not a standing debt: the fed counter starts level with it and ``pair_played_frames``
                                begins at zero *played* rather than reading the whole queue as still to come.
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

                                Returns False only while the pair's queue is still draining (the frame is held and
                                retried - nothing is dropped). Anything that makes the swap impossible refuses it
                                instead: a ring shorter than the room wants, pools that refuse the frames, a room that
                                will not start.
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
        """Resolve the page URL through the isolated helper (see transport.py)."""
        return transport.resolve_playback_info(self, canonical_url, use_cache=use_cache)

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
                # The key shifter sits here, before the frame is either queued
                # locally or remembered for the broadcast: one song, one key.
                data = self._shape_frame(leftover[:self.BUFFER_SIZE])
                leftover = leftover[self.BUFFER_SIZE:]
                if not data:
                    continue
                with self._lock:
                    if self._diagnostic_startup_call("direct.queue", self._queue_local, data):
                        pre_buffered += 1
                        if self.bot:
                            self._timeline_delay.append(bytes(data))
        return pre_buffered, leftover

    def _hold_direct_start(self, leftover):
        """Hold the prebuffered head until the shared wall-clock deadline.

                                Drains ffmpeg while waiting ('-re' keeps producing at media rate, so a blocked pipe would
                                stall the decoder and the CDN read behind it) into the normal OpenAL staging deque,
                                returning the partial chunk so run()'s streaming loop keeps its leftover contract.
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
                        frame = self._shape_frame(leftover[:self.BUFFER_SIZE])
                        if frame:
                            self._pause_buffer.append(frame)
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
        """Wall-clock instant (received_at's monotonic domain) when the head plays.

                                Every listener derives the same value from the same jukebox_play broadcast: t_zero - when
                                the room's song position was 0 - is the arrival instant minus start_offset, and the room's
                                clock runs one lead-in behind the server's audioStartedAt. Fresh songs and mid-song joins
                                must share that shift, or a joiner lands a whole lead-in ahead of the room.

                                ``lead_in`` overrides the room's shared hold: a machine that switched itself to direct
                                playback while the room still hears the relay must pass 0.0.
                                """
        if lead_in is None:
            lead_in = AudioStreamer.DIRECT_LEAD_IN_S
        t_zero = received_at - start_offset
        return t_zero + lead_in + seek_to

    @staticmethod
    def direct_seek_seconds(start_offset, received_at, now, aim_ahead_s=None, lead_in=None):
        """Input seek for an anchored mid-song join, aimed PAST the projected audible start.

                                Arriving early is corrected by an exact hold at prebuffer-complete; arriving late never
                                recovers, because under '-re' pacing skipping PCM costs the same wall time as the
                                lateness itself.

                                ``aim_ahead_s`` is the room instant this seek targets, measured from ``now``: how far
                                ahead of now this machine expects to *become audible*. Default is the alignment slack
                                (lead-in + startup estimate); a flight that has just watched itself start late replaces
                                it with its own measured startup. The content named is the same either way, because the
                                deadline adds the room's own lead-in.
                                """
        if received_at is None or start_offset <= 0.0:
            return start_offset
        if lead_in is None:
            lead_in = AudioStreamer.DIRECT_LEAD_IN_S
        if aim_ahead_s is None:
            aim_ahead_s = lead_in + AudioStreamer.DIRECT_STARTUP_EST_S
        return (start_offset + max(0.0, now - received_at)
                + (aim_ahead_s - lead_in))

    # ---- What a late start is allowed to do about it ----

    @classmethod
    def catch_up_aim_seconds(cls, late_s, startup_s):
        """The room instant a flight that started ``startup_s`` ago aims at, or None.

                                The one home of the re-aim's arithmetic, so a rig can ask for it instead of modelling it:
                                the aim is the *measured* startup of the flight that just failed, plus the margin that
                                keeps the error on the early side. ``None`` is the two ends of the rule - a lateness
                                nobody hears (``DIRECT_CATCH_UP_MIN_S``) and a restart that would cost more silence
                                than the drift it removes.
                                """
        if float(late_s or 0.0) < cls.DIRECT_CATCH_UP_MIN_S:
            return None
        startup = max(0.0, float(startup_s or 0.0))
        if startup > cls.DIRECT_ALIGN_SLACK_S:
            return None
        return startup + cls.DIRECT_CATCH_UP_MARGIN_S

    def direct_catch_up_aim_s(self):
        """The room instant this flight should be re-aimed at, or None.

                                A late audible start is not a listening problem that scheduling can compensate for: this
                                machine's ear holds an older piece of music, so a note played here reaches every other
                                machine after the beat. The one cure is to start the decode somewhere else, and the flight
                                loop spends this once.

                                The aim is this machine's own *measured* startup (``catch_up_aim_seconds``): projecting
                                that keeps the error on the early side, where the pre-buffer hold turns it into a wait,
                                rather than on the late side where nothing can. Its cost is that projection in silence,
                                so a machine slower than the whole alignment slack is refused it.
                                """
        if not self._direct_anchor or self._catch_up_used:
            return None
        if self.cinema is not None:
            # A room is fed frame by frame and cannot be emptied and refilled
            # the way a plain pair can (``CinemaSpeakerBank.stop`` is its
            # teardown), so a decode that jumped would hand it frames from two
            # places in the song at once.
            return None
        if self.start_offset_received_at is None or self._attempt_started_at is None:
            return None
        return self.catch_up_aim_seconds(
            self.direct_late_s, time.monotonic() - self._attempt_started_at)

    def _reaim_at_the_room(self, aim_ahead_s):
        """Spend the one re-aim: arm the next flight, then drop this one.

                                The broadcast this stream is anchored to does not change (t_zero, the offset and the room's
                                lead-in are the same numbers), so the next flight is simply this stream *joining* the room
                                at the position the room will have reached.
                                """
        self._catch_up_used = True
        self.direct_catch_up_s = max(0.0, float(aim_ahead_s))
        self._join_playing_room = True
        self._join_aim_ahead_s = self.direct_catch_up_s
        logger.log(
            "[AudioStreamer] direct sync: audible start "
            f"{float(self.direct_late_s or 0.0):.2f}s past the shared deadline "
            "(slow resolve/startup) — re-aiming at the room's position, "
            f"~{self.direct_catch_up_s:.2f}s ahead (one restart; the frames "
            "decoded behind the room are dropped)"
        )
        self._discard_flight()

    def _discard_flight(self):
        """Drop the flight that started late, and every frame it produced.

                                Nothing has been heard yet: what is queued is the pre-buffer, the output has not been
                                started, and no note has been placed on the frames. Buffers go back to their pool (a
                                stopped source reports everything it holds as processed, which is why the reclaim comes
                                after the stop) and the pair's clock starts over, because the queue it was counting is gone.
                                """
        process = self.process
        if process is not None:
            try:
                process.kill()
                process.wait(timeout=2)
            except Exception:
                pass
        self.process = None
        for src in self._all_sources():
            try:
                src.stop()
            except Exception:
                pass
        self._reclaim_processed()
        self._pause_buffer.clear()
        # The new flight starts somewhere else in the song, so nothing of the
        # old position may still be inside the key shifter's look-ahead.
        with self._transpose_lock:
            if self.transposer is not None:
                self.transposer.reset()
        self._tail_returned = False
        with self._ring_lock:
            self._fed_ring.clear()
        self._fed_content_seconds = 0.0
        self._pair_clock_reset()

    def run(self):
        """One song, in phases: resolve -> ffmpeg argv -> pre-buffer -> feed.

        The worker thread body: every phase below is its own method, in the order it
        runs, so the flight loop that retries a dead link and aims a late start at the
        room (`_prebuffer_flight`) reads on its own instead of inside a 400-line
        method. What each phase does is what it always did.
        """
        if not FFMPEG_PATH:
            print("[MusicBot] ffmpeg not found!")
            speak("ffmpeg not found.")
            self.failure_reason = "ffmpeg not found"
            self.running = False
            self._cleanup()
            return

        resolved = self._resolve_input()
        if resolved is None:
            return
        target_url, input_headers, canonical_url = resolved

        try:
            cmd = self._build_cmd(target_url, input_headers)
        except Exception as ex:
            logger.log_exception(ex, "AudioStreamer.run command")
            self.failure_reason = "playback command error"
            self.running = False
            self._cleanup()
            return

        self._prepare_output()

        pre_buffered, leftover, error_detail = self._prebuffer_flight(
            target_url, input_headers, canonical_url, cmd)
        if not self.running:
            # Intentional cancellation (map change, newer playback generation,
            # or stop) is not a load failure and must stay silent.
            self._cleanup()
            return
        if pre_buffered == 0:
            self._report_no_audio(error_detail)
            return
        if not self._begin_playback(pre_buffered):
            return

        self._feed_and_drain(leftover)

        if self.running:
            self._classify_end()

        # Cleanup
        self._cleanup()

    def _resolve_input(self):
        """Resolve the stream URL and headers (see transport.resolve_input)."""
        return transport.resolve_input(self)

    def _build_cmd(self, target_url, input_headers):
        """The ffmpeg argv for one flight (see transport.build_cmd)."""
        return transport.build_cmd(self, target_url, input_headers)

    def _prepare_output(self):
        """The local buffer pool and the paced network sender, before any decode."""
        # Initialize buffer pool
        self._diagnostic_startup_call("direct.buffers", self._init_buffer_pool)

        # Start background network sender thread ONLY when this stream actually
        # broadcasts (bot attached). Jukebox players (bot=None) play locally only
        # and must never re-broadcast the song as their own music bot stream.
        if self.bot:
            self.sender_thread = threading.Thread(target=self._network_sender_loop, daemon=True)
            self.sender_thread.start()

    def _prebuffer_flight(self, target_url, input_headers, canonical_url, cmd):
        """Launch ffmpeg, fill the local pre-buffer, and aim this flight at the room.

        A flight is one ffmpeg launch, its pre-buffer and its hold. A link that fails
        (403, a stall) is retried and re-resolved; a flight that became audible past
        the shared deadline is dropped for one aimed at the room. Returns
        ``(pre_buffered, leftover, error_detail)``: the frames the local buffers hold,
        the bytes read past a packet boundary, and what ffmpeg said when it produced
        nothing.
        """
        # === Pre-buffer phase: fill LOCAL buffers before starting playback ===
        # Some fresh googlevideo URLs briefly return 403 while their CDN edge authorization propagates:
        # retry, first with the same URL+headers, then with a freshly RE-RESOLVED URL (a stale signed URL
        # 403s forever). All work stays on this worker thread.
        #
        # A flight is one ffmpeg launch, its pre-buffer and its hold; the loop below re-aims a machine whose
        # own resolve+startup outran the shared deadline once, at the room's position.

        catch_up_attempts = 0
        pre_buffered = 0
        _pre_leftover = b''
        while True:
            pre_buffered = 0
            _pre_leftover = b''
            error_detail = ""
            for attempt in range(4):
                stalled = False
                if not self.running:
                    break
                try:
                    self._attempt_started_at = time.monotonic()
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
                        # Alive and silent after the whole pre-buffer window: ffmpeg is inside its own reconnect attempt
                        # (nothing is logged at `-loglevel error` until it gives up) against a host that is not answering.
                        # There is no 403 to match on, but repeating this URL is exactly what it has been doing for twelve
                        # seconds, so this counts as a failed link - a freshly resolved one points at another CDN edge.
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
                    cmd = self._build_cmd(target_url, input_headers)
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
                            cmd = self._build_cmd(target_url, input_headers)
                        except Exception:
                            pass
                time.sleep(1.0 + attempt)

            if not self.running or pre_buffered == 0:
                break
            if self._direct_anchor:
                # Shared-timeline hold: every listener becomes audible at
                # the same wall-clock instant regardless of how long its
                # own resolve and startup took.
                _pre_leftover = self._hold_direct_start(_pre_leftover)
                if not self.running:
                    break
                aim = None
                if catch_up_attempts < self.DIRECT_CATCH_UP_MAX:
                    aim = self.direct_catch_up_aim_s()
                if aim is not None:
                    # This flight is behind the room: drop it and start
                    # again aimed at where the room is by then.
                    catch_up_attempts += 1
                    self._reaim_at_the_room(aim)
                    try:
                        cmd = self._build_cmd(target_url, input_headers)
                    except Exception as ex:
                        logger.log_exception(ex, "AudioStreamer.run catch-up")
                        break
                    continue
            break
        return pre_buffered, _pre_leftover, error_detail

    def _report_no_audio(self, error_detail):
        """The pre-buffer produced nothing: say why, then shut this worker down."""
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

    def _begin_playback(self, pre_buffered):
        """Announce the pre-buffer and start local playback, frame zero included.

        Called once the pre-buffer holds audio; returns ``False`` when the worker was
        cancelled while starting, so ``run`` stops exactly where it always did.
        """
        # Start local playback after pre-buffering (spatial pairs also need
        # their distance fade computed before the first audible sample).
        if pre_buffered > 0:
            self.prebuffer_event.set()
            self._remember_prebuffered_media()
            if not self.running:
                self._cleanup()
                return False
            # The shared-timeline hold, and the one re-aim a late start may
            # take, both belong to the flight loop above: either one can end
            # the decode that is running and start another.
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
        return True

    def _feed_and_drain(self, leftover):
        """The streaming loop (decode -> OpenAL -> network), then the tail drain.

        ``leftover`` is what the pre-buffer read past one packet boundary, carried
        into the first frame of the song.
        """
        # === Streaming loop ===
        eof = False
        _leftover = leftover  # Carry over any leftover bytes from pre-buffer
        while self.running:
            if self.paused:
                # A pause is not a stalled output: the tail gets its whole
                # window again once the song is resumed.
                self._tail_stall_at = None
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

            tail = None
            if data:
                data = self._shape_frame(data)
            if eof and not _leftover and not self._tail_returned:
                # ffmpeg has ended, and the key shifter is still holding the
                # song's look-ahead: that is the last note of the song, not
                # silence. Give it back once, to the local output only -- and
                # after `data`, because a song whose length is not a whole
                # number of 20 ms frames still has that last partial frame in
                # front of the look-ahead. Queued the other way round, the tail
                # plays first and the last sliver of the song lands after it.
                self._tail_returned = True
                tail = self._flush_transpose()

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
            if tail:
                self._pause_buffer.append(tail)

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
                    elif eof and not self._all_playing():
                        # The decode ended with audio still queued and silent: a
                        # room that ran low is holding its last frames for a refill
                        # that can no longer arrive (see _release_held_room).
                        self._release_held_room()
                except Exception:
                    pass

            if eof and self._track_tail_progress():
                # The output has stopped taking the tail: finish here rather than
                # leave a song the Music Bot can never call over.
                break

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

        # Wait for remaining buffers to finish playing. A tail this stream has
        # already given up on is not waited for again here. A streamer built
        # without `__init__` (a test fixture, a restored session) has no tail
        # state yet, and "nothing abandoned" is the right answer for it.
        if self.running and not getattr(self, "_tail_abandoned", False):
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

    #: How long an output may stay unchanged at EOF before the tail is abandoned.
    #: A room drains its last frames in a couple of hundred milliseconds; an
    #: output that has not moved a buffer in this long is not going to.
    TAIL_STALL_SECONDS = 3.0

    def _track_tail_progress(self):
        """True when the output has stopped taking this stream's tail.

        A stream's thread waits for its output to drain before it finishes, and
        the Music Bot reads the end of a song from that thread: while the thread
        waits, `is_alive()` is True and the play-next queue stays shut. A room
        that ran low is released by `_release_held_room`; a room that was taken
        away under the stream, went silent, or stopped consuming its buffers has
        nothing to let go, and the loop then spun here for the rest of the
        session. The report was the workaround rather than the bug -- "I turn the
        routing off and on and then the queued song loads" -- because releasing
        the room made `_buffers_queued()` fall to zero and the loop break.
        Nothing can play those frames, so they are dropped and the thread ends.
        """
        queued = self._buffers_queued()
        # A streamer built without `__init__` (a test fixture, a restored
        # session) has none of the tail state yet; a missing count is "nothing
        # seen before", not a crash in the drain loop.
        if queued != getattr(self, "_tail_queued", None):
            # The output is still working: any change is progress.
            self._tail_queued = queued
            self._tail_stall_at = None
            return False
        if queued == 0 and not self._pause_buffer:
            return False
        now = time.monotonic()
        stall_at = getattr(self, "_tail_stall_at", None)
        if stall_at is None:
            self._tail_stall_at = now
            return False
        if now - stall_at < self.TAIL_STALL_SECONDS:
            return False
        self._pause_buffer.clear()
        self._tail_abandoned = True
        return True

    def _release_held_room(self):
        """Let a held room play out once this stream has reached its end.

                            A room that ran low holds every speaker until more frames arrive
                            (``CinemaSpeakerBank._begin_refill_hold``). A stream at EOF has no
                            more frames to give it, so that hold could only ever be released by
                            audio that will never come: the speakers stayed paused over the
                            song's tail, the room never drained, and this thread -- which waits
                            for it to drain before it finishes -- never ended. The Music Bot
                            reads the end of a song from that thread, so the queue never
                            advanced and the next queued track only started when somebody
                            skipped to it by hand.
                            """
        if self.cinema is None:
            return False
        if not getattr(self.cinema, "awaiting_refill", False):
            return False
        try:
            return bool(self.cinema.play_out())
        except Exception:
            return False

    def _classify_end(self):
        """A finished song or an early death, before this worker cleans up."""
        if self.running:
            # Distinguish a natural song end from a mid-song ffmpeg death (403 on a CDN reconnect, connection
            # reset, ...): ffmpeg exits 0 on a clean EOF, so any other exit code after audio already started
            # means the stream died EARLY. Mark it a failure so the jukebox recovery watchdog rebuilds with a
            # fresh resolve instead of treating the silence as a finished song.
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
        if self.remote is not None:
            # Hand the slot back (that flushes only this stream's queue), and
            # stop pointing at a device another song will own next.
            try:
                self.remote.release_slot(self.remote_slot)
            except Exception:
                pass
            self.remote = None
        self._buffer_pool.clear()
        self._pause_buffer.clear()
        # Drain the network queue to free references
        while not self.network_queue.empty():
            try:
                self.network_queue.get_nowait()
            except Exception:
                pass
        voice_chat.reset_audio_limiter(
            voice_chat.audio_limiter_key("musicbot-mix", self))

    def stop(self):
        self.running = False
        self._cleanup()

    def resume_output_if_buffered(self):
        """Restart a stopped OpenAL output without replacing this stream.

                                Intentionally limited to already queued frames: starting a fresh stream here would lose
                                timing and can duplicate a broadcast, and the decoder thread remains the owner of
                                buffering new audio.
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
                if self.remote is not None:
                    self.remote.set_pause(paused, self.remote_slot)
                    if not paused:
                        self.ready_event.set()
                    return
                if self.cinema is not None:
                    # A cinema room is N speakers, and pausing only the one this stream was handed leaves the rest
                    # playing their queues - and then replaying from the front of those queues on resume, which is heard
                    # as speakers that never line up again after a pause. The bank holds them all.
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
