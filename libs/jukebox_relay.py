"""Jukebox Opus decoding worker with exclusively main-owner OpenAL output.

Network reception and the daemon handle bounded bytes queues only. Sources,
buffers, gain, EFX and final disposal belong to the constructing audio thread.
"""

import array
import contextlib
from collections import deque
import queue
import threading
import time

import cyal
from .audio_diagnostics import probe as audio_probe
from .deferred_log import log_deferred as log_line
from .jukebox_clock import PAIR_KEY, SpotClock, mono_ms


class JukeboxRelayReceiver(threading.Thread):
    main_thread_audio = True
    # Hand-built instances (tests) bypass __init__; the room flag and the shed
    # counters need defaults so the shared output paths still resolve.
    cinema = None
    shed_frames = 0
    last_shed_at = None
    _shed_reported_at = None
    # A room this receiver has been asked to hand its output to mid-song
    # (``request_room``); ``None`` on every receiver that never was.
    _pending_room = None
    _swap_hold_frames = 0
    swap_room_failed = False
    PREBUFFER_FRAMES = 4
    RESUME_FRAMES = 3
    # What the server hands this receiver, and therefore the size of one pair
    # frame -- the unit a live note's wait is counted in (``pair_frame_ms``
    # measures it from the audio itself and only falls back to this).
    RELAY_FRAME_MS = 40.0
    MAX_PENDING_FRAMES = 32
    NUM_BUFFERS = 32
    MAX_QUEUED_BUFFERS = 10
    # Frames this receiver remembers of what it fed, and how long a room swap
    # may hold its feeding while a deep pair queue drains (see
    # ``music_bot.streaming.AudioStreamer.request_room`` for both).
    FED_RING_FRAMES = 64
    SWAP_DRAIN_MAX_FRAMES = 64
    MAX_PCM_BYTES = 48000 * 2 * 2 * 120 // 1000
    # Shedding a frame is the client catching up to the live edge, and it is
    # only ever done because the queue grew past ``MAX_QUEUED_BUFFERS`` (a
    # burst after a stall). It is worth saying out loud when it happens at all:
    # every dropped frame is audio the listener never hears, and a burst of
    # them is heard as the song jumping forward ("it speeds up for a moment").
    # At most one line per interval; the count is always kept.
    SHED_REPORT_INTERVAL = 30.0

    def __init__(self, game, source_l, source_r, volume, relay_id,
                 stream_epoch, reference_distance, max_distance,
                 box_pos=None, player=None, reverb_slot=None, eq_slot=None,
                 cabinet_volume=100, *, cinema=None, clock=None):
        super().__init__(daemon=True, name=f"jukebox-relay-{relay_id}")
        self._owner = threading.get_ident()
        self._clock = clock or time.monotonic
        self.game = game
        # A cinema room (CinemaSpeakerBank) replaces the stereo pair: it owns
        # one positioned source per speaker, so this receiver never touches a
        # source or a buffer directly while it is set.
        self.cinema = cinema
        if cinema is not None:
            source_l = source_r = None
        self.source_l, self.source_r = source_l, source_r
        self.volume = max(0, min(100, int(volume)))
        self.cabinet_volume = max(0.0, min(1.0, float(cabinet_volume) / 100.0))
        self.relay_id, self.stream_epoch = int(relay_id), int(stream_epoch)
        self.reference_distance = float(reference_distance)
        self.max_distance = float(max_distance)
        self.box_pos, self.player = box_pos, player
        self.reverb_slot, self.eq_slot = reverb_slot, eq_slot
        self._last_occluded = None
        self.running = True
        self._stopped = False
        self.frames = queue.Queue(maxsize=self.MAX_PENDING_FRAMES)
        self._pcm_frames = queue.Queue(maxsize=self.MAX_PENDING_FRAMES)
        self._receive_lock = threading.Lock()
        self._generation = 0
        self._audio_generation = 0
        self._pool = []
        self._all_buffers = []
        self._allocated_buffers = 0
        self._buffer_generation_failures = 0
        # Thread._started is a private Event; never shadow it with a flag.
        self._play_started = False
        self._last_sequence = None
        self.created_at = self._clock()
        self.last_packet_at = None
        self.last_audio_activity = None
        self.last_output_at = None
        self.received_frames = 0
        # Frames handed to the plain pair, and what one of them measured: the
        # pair's own content clock (``pair_played_frames``), which a live
        # note's wait is re-projected from.
        self.pair_frames_fed = 0
        self._pair_frame_ms = 0.0
        # Frames dropped to keep the queue at the live edge, and when the last
        # one was (see ``_report_shed``).
        self.shed_frames = 0
        self.last_shed_at = None
        self._shed_reported_at = None
        self._fed_ring = deque(maxlen=self.FED_RING_FRAMES)
        self._ring_lock = threading.Lock()
        self._pending_room = None
        self._swap_hold_frames = 0
        self.swap_room_failed = False
        self.failure_reason = None
        self._retire_started = None
        self._retire_duration = 0.0
        self._retire_cleanup = None
        register = getattr(getattr(game, "audio_mngr", None),
                           "register_jukebox_receiver", None)
        if callable(register):
            register(self)

    def _check_owner(self):
        if threading.get_ident() != self._owner:
            raise RuntimeError("jukebox audio must be pumped/stopped by its owner")

    def _shed_if_deep(self, backlog):
        """True when this frame is dropped to keep the queue at the live edge.

        Frames are only ever dropped once playback has started and the queue is
        already ``MAX_QUEUED_BUFFERS`` deep: that is the client catching up to
        the live edge after a burst, and every dropped frame is audio the
        listener never hears. A burst of them is heard as the song jumping
        forward, which is why they are counted (see ``_shed_frame``).
        """
        if not self._play_started or backlog < self.MAX_QUEUED_BUFFERS:
            return False
        self._shed_frame()
        return True

    # -------------------------------------------------- output handed over
    #
    # A cabinet's cinema mode can change while its song plays, and only its
    # *output* changes with it: a room reshapes itself in place (the bank is
    # the same object), so what is here is only the crossings between a room
    # and the plain stereo pair (see the direct streamer for the long form).

    def _ring(self):
        ring = getattr(self, "_fed_ring", None)
        if ring is None:
            # Hand-built instances (tests) bypass __init__.
            ring = self._fed_ring = deque(maxlen=self.FED_RING_FRAMES)
            self._ring_lock = threading.Lock()
        return ring

    def _remember_frame(self, left, right):
        self._ring()
        with self._ring_lock:
            self._fed_ring.append((left, right))

    def pending_room(self):
        """The room this receiver is waiting to hand its output to, or None."""
        return self._pending_room

    def pair_queued_frames(self):
        """Frames the plain pair is holding ahead of what is audible."""
        if self.cinema is not None:
            return 0
        if self.source_l is None or self.source_r is None:
            return 0
        try:
            return min(int(self.source_l.buffers_queued),
                       int(self.source_r.buffers_queued))
        except Exception:
            return 0

    # ------------------------------------------- a live note's own clock
    #
    # A note played along to this cabinet waits on the frames the pair has
    # *played*, re-projected every frame, instead of on a queue depth measured
    # once when the note arrived: the queue drains, sheds and realigns, and a
    # hold that does not follow it walks away from the song -- differently on
    # each machine. The rule lives in one place (``libs/jukebox_clock.py``),
    # shared with a cinema room's bank.

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

        Latched from the frames actually handed over, because the transports
        do not agree (the relay receives 40 ms, the direct streamer decodes
        20 ms) and a note's wait is counted in those frames. Falls back to the
        relay's own payload size until a frame has been queued.
        """
        measured = float(getattr(self, "_pair_frame_ms", 0.0) or 0.0)
        return measured if measured > 0.0 else self.RELAY_FRAME_MS

    def pair_played_frames(self):
        """Frames the plain pair has already played: fed minus still queued.

        ``pair_queued_frames`` is what OpenAL reports as still ahead of the
        listener and the fed counter is this receiver's own, so the difference
        is the content instant the pair is playing right now -- what a live
        note's wait is measured against, every frame.
        """
        fed = int(getattr(self, "pair_frames_fed", 0) or 0)
        return max(0, fed - int(self.pair_queued_frames()))

    def pair_is_playing(self):
        """True while the plain pair is this receiver's output, and playing.

        A receiver that handed its output to a cinema room has no pair to
        measure, and a pair whose sources are not playing reports everything
        it holds as finished -- neither is a clock for a live note (see
        ``libs/jukebox_clock.py``).
        """
        if self.cinema is not None or not self._play_started:
            return False
        if not self.running or self._stopped:
            return False
        for source in (self.source_l, self.source_r):
            if source is None:
                return False
            try:
                if source.state != cyal.SourceState.PLAYING:
                    return False
            except Exception:
                return False
        return True

    @property
    def pair_clock(self):
        """The plain pair's own clock, made on first use (see the note above).

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
        """Ask this receiver to play through ``bank`` instead of the pair.

        Committed from the pump (``_commit_room_swap``) once the pair's queue
        is shallow enough for the room to hold, and refused by a receiver
        that cannot make that window -- the caller then leaves the room for
        the next song rather than rebuilding anything.
        """
        self._check_owner()
        self._pending_room = bank
        self._swap_hold_frames = 0
        self.swap_room_failed = False
        return True

    def cancel_room(self, bank=None):
        """Give up a room this receiver was asked to take over."""
        self._check_owner()
        if bank is None or bank is self._pending_room:
            self._pending_room = None
        return True

    def switch_to_pair(self, pair):
        """Play this receiver through the plain stereo pair from now on.

        ``pair`` normally comes out of the room that was playing
        (``CinemaSpeakerBank.detach_primary_pair``), so the two sources keep
        the frames the room was about to play and the song keeps its content
        instant while the room's other speakers stop.
        """
        self._check_owner()
        src_l, src_r, ref, maxd = pair
        self.source_l, self.source_r = src_l, src_r
        self.reference_distance = float(ref)
        self.max_distance = float(maxd)
        self.cinema = None
        self._pending_room = None
        self._pool.clear()
        self._allocated_buffers = 0
        self._all_buffers.clear()
        self._play_started = False
        self._last_occluded = None
        self._pair_credit_from_room()
        self._update_gain()
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
        """Give a room taking this receiver over the cabinet's own numbers."""
        try:
            bank.set_volume(self.volume)
            bank.set_cabinet_volume(self.cabinet_volume * 100.0)
            if self.reverb_slot is not None:
                bank.set_reverb(self.reverb_slot)
            if self.eq_slot is not None:
                bank.set_eq_slot(self.eq_slot)
            bank.update_output()
        except Exception:
            pass

    def _stop_pair_sources(self):
        for source in (self.source_l, self.source_r):
            if source is None:
                continue
            try:
                source.stop()
            except Exception:
                pass

    def _abandon_room_swap(self):
        """Refuse a room this receiver could not prime; the caller hands it back."""
        self._pending_room = None
        self.swap_room_failed = True

    def _commit_room_swap(self):
        """Hand this receiver's output to a requested room, if it can be primed.

        False only while the pair's queue is still draining.
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
        ring = list(self._ring())
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
        self._pending_room = None
        self._play_started = True
        self._stop_pair_sources()
        return True

    def _shed_frame(self):
        """Count a frame dropped for the live edge, and say so once in a while.

        The count is what makes "the song sped up for a second" measurable:
        without it the only evidence a listener ever has is their own ears.
        """
        self.shed_frames += 1
        self.last_shed_at = self._clock()
        now = self.last_shed_at
        if (self._shed_reported_at is not None
                and now - self._shed_reported_at < self.SHED_REPORT_INTERVAL):
            return
        self._shed_reported_at = now
        log_line(f"[Jukebox] relay queue past {self.MAX_QUEUED_BUFFERS} frames: "
                 f"shedding to the live edge ({self.shed_frames} frame(s) so far)")

    def _output_sources(self):
        """Every source this receiver feeds (2 plain, N in a cinema room)."""
        if self.cinema is not None:
            return self.cinema.sources
        return (self.source_l, self.source_r)

    @staticmethod
    def _drain(q):
        while True:
            try:
                q.get_nowait()
            except queue.Empty:
                return

    @staticmethod
    def _put_latest(q, item):
        try:
            q.put_nowait(item)
        except queue.Full:
            try:
                q.get_nowait()
            except queue.Empty:
                pass
            try:
                q.put_nowait(item)
            except queue.Full:
                pass

    def receive(self, sequence, payload, flags=0):
        """Network-safe bounded enqueue; never access a source or decoder."""
        if not payload or len(payload) > 1275 or flags & ~0x03:
            return False
        sequence = int(sequence) & 0xffff
        with self._receive_lock:
            if not self.running:
                return False
            if self._last_sequence is not None:
                delta = (sequence - self._last_sequence) & 0xffff
                if delta == 0 or delta > 0x8000:
                    return False
            self._last_sequence = sequence
            self.last_packet_at = self._clock()
            self.received_frames += 1
            if flags & 0x01:
                self._generation += 1
                self._drain(self.frames)
                self._drain(self._pcm_frames)
            self._put_latest(self.frames, (self._generation, bytes(payload)))
        return True

    def _reset_queue(self):
        """Discard pending bytes, invalidating any in-progress decoder result."""
        with self._receive_lock:
            self._generation += 1
            self._drain(self.frames)
            self._drain(self._pcm_frames)

    def run(self):
        if not self.running:
            return
        try:
            from pyogg import OpusDecoder
            decoder = OpusDecoder()
            decoder.set_channels(2)
            decoder.set_sampling_frequency(48000)
        except Exception:
            self.failure_reason = "relay decoder initialization failed"
            self.running = False
            return
        while self.running:
            try:
                item = self.frames.get(timeout=0.05)
            except queue.Empty:
                continue
            if item is None:
                break
            generation, payload = item
            with self._receive_lock:
                valid = self.running and generation == self._generation
            if not valid:
                continue
            try:
                decoded = decoder.decode(bytearray(payload))
                pcm = memoryview(decoded)
                if not 0 < pcm.nbytes <= self.MAX_PCM_BYTES or pcm.nbytes % 4:
                    self.failure_reason = "invalid relay PCM frame"
                    continue
                samples = array.array("h")
                samples.frombytes(pcm.tobytes())
                pair = (generation, samples[0::2].tobytes(), samples[1::2].tobytes())
                # Stop/reset cannot race a late PCM publication into the queue.
                with self._receive_lock:
                    if self.running and generation == self._generation:
                        self._put_latest(self._pcm_frames, pair)
            except Exception:
                # A malformed frame must not perform synchronous file logging
                # or kill the stream. The next good packet can recover.
                self.failure_reason = "relay frame decode failed"

    def _return_buffers(self, result):
        if result is None:
            return False
        if isinstance(result, (list, tuple)):
            self._pool.extend(result)
        else:
            self._pool.append(result)
        return True

    @audio_probe.measured("relay.reclaim")
    def _reclaim(self, *, stopped=False):
        self._check_owner()
        if self.cinema is not None:
            # The room owns one buffer pool per speaker, so it owns the
            # reclamation too. Unqueueing the room's finished buffers into
            # *this* receiver's pool -- which cinema mode never allocates from
            # -- permanently drained the room's pools: it played the handful
            # of frames it started with and then refused every later frame, so
            # the speakers stopped consuming and the watchdog rebuilt the room
            # every ~8s forever (heard as a song that cuts in and out).
            try:
                reclaimed = self.cinema.reclaim()
            except Exception:
                reclaimed = False
            if reclaimed and not stopped:
                self.last_output_at = self._clock()
            return
        for source in self._output_sources():
            if source is None:
                continue
            try:
                remaining = self.NUM_BUFFERS
                while source.buffers_processed > 0 and remaining > 0:
                    remaining -= 1
                    if not self._return_buffers(source.unqueue_buffers()):
                        break
                    if not stopped:
                        self.last_output_at = self._clock()
            except Exception:
                pass

    def _reset_output(self):
        for source in self._output_sources():
            if source is not None:
                with contextlib.suppress(Exception):
                    source.stop()
        self._reclaim(stopped=True)
        self._play_started = False
        self._pair_clock_reset()

    def set_volume(self, volume):
        self._check_owner()
        self.volume = max(0, min(100, int(volume)))
        self._update_gain()

    def set_cabinet_volume(self, volume):
        self._check_owner()
        self.cabinet_volume = max(0.0, min(1.0, float(volume) / 100.0))
        self._update_gain()

    def _fade_gain(self):
        if self._retire_started is None:
            return 1.0
        if self._retire_duration <= 0:
            return 0.0
        return max(0.0, 1.0 - (self._clock() - self._retire_started) / self._retire_duration)

    @audio_probe.measured("relay.spatial")
    def _update_gain(self):
        self._check_owner()
        if self._stopped:
            return
        if self.cinema is not None:
            # Volume, cabinet trim and the retire ramp are the room's in
            # cinema mode, and so are per-speaker distance and occlusion.
            try:
                self.cinema.configure(volume=self.volume,
                                      cabinet_volume=self.cabinet_volume * 100.0,
                                      fade=self._fade_gain())
                self.cinema.update_output()
            except Exception:
                pass
            return
        try:
            audio = self.game.audio_mngr
            listener = getattr(audio, "position", None)
            category = audio.volume_categories.get("jukebox", [100])[0] / 100.0
            local = self.volume / 100.0 * self.cabinet_volume * self._fade_gain()
            span = max(0.0001, self.max_distance - self.reference_distance)
            audible = False
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
                audible = audible or distance_gain > 0
            if listener is None or self.box_pos is None or not audible:
                return
            provider = getattr(self.player, "occlusion_tier", None)
            if callable(provider):
                tier = provider(self.box_pos, listener, self.max_distance)
            else:
                from .jukebox import wall_occlusion_tier
                gameplay = getattr(self.game, "gameplay", None)
                current_map = getattr(gameplay, "map", None)
                tier = wall_occlusion_tier(current_map, self.box_pos, listener)
            if tier == self._last_occluded:
                return
            self._last_occluded = tier
            filt = None
            if tier >= 2 and callable(getattr(self.player, "get_occlusion_filter", None)):
                filt = self.player.get_occlusion_filter()
            elif tier == 1 and callable(getattr(self.player, "get_light_occlusion_filter", None)):
                filt = self.player.get_light_occlusion_filter()
            for source in (self.source_l, self.source_r):
                if filt is not None:
                    source.direct_filter = filt
                else:
                    # A wall clearing must not strip an active global filter
                    # (the underwater muffle): restore it instead of deleting.
                    active = getattr(audio, "filter", None)
                    if active and active[-1] is not None:
                        source.direct_filter = active[-1]
                    else:
                        with contextlib.suppress(Exception):
                            del source.direct_filter
                if getattr(audio, "efx", None) is not None:
                    if self.reverb_slot is not None:
                        audio.efx.send(source, 0, self.reverb_slot, filter=filt)
                    if self.eq_slot is not None:
                        audio.efx.send(source, 1, self.eq_slot, filter=filt)
        except Exception:
            pass

    def _queue_pair(self, left, right):
        if self.cinema is not None:
            try:
                queued = bool(self.cinema.queue_frame(left, right))
            except Exception:
                queued = False
            if queued:
                self._remember_frame(left, right)
                self.last_audio_activity = self._clock()
            else:
                self.failure_reason = "relay cinema queue failed"
            return queued
        if len(self._pool) < 2:
            return False
        buf_l, buf_r = self._pool.pop(), self._pool.pop()
        left_queued = right_queued = False
        try:
            audio_probe.call("relay.upload", buf_l.set_data, left, sample_rate=48000, format=cyal.BufferFormat.MONO16)
            audio_probe.call("relay.upload", buf_r.set_data, right, sample_rate=48000, format=cyal.BufferFormat.MONO16)
            audio_probe.call("relay.queue", self.source_l.queue_buffers, buf_l)
            left_queued = True
            audio_probe.call("relay.queue", self.source_r.queue_buffers, buf_r)
            right_queued = True
            self._remember_frame(left, right)
            self._note_pair_fed(left)
            self.last_audio_activity = self._clock()
            return True
        except Exception:
            if not left_queued:
                self._pool.append(buf_l)
            if not right_queued:
                self._pool.append(buf_r)
            # Never leave just one channel queued after a partial failure.
            self._reset_output()
            self.failure_reason = "relay audio queue failed"
            return False

    def pump_audio(self, deadline=None, max_new_buffers=4, max_frames=4):
        """Incremental owner-side audio work; return successfully queued pairs."""
        self._check_owner()
        if self._stopped:
            return 0
        if (not self.running or (self._retire_started is not None and self._fade_gain() <= 0)):
            self.stop()
            return 0
        has_time = lambda: deadline is None or self._clock() < deadline
        if not has_time():
            return 0
        with self._receive_lock:
            generation = self._generation
        if generation != self._audio_generation:
            # A relay reset discards pending network/PCM data, not healthy
            # audio already queued in OpenAL (map resync must remain seamless).
            self._audio_generation = generation
        self._reclaim()
        self._update_gain()
        if self._pending_room is not None and not self._commit_room_swap():
            # The room is waiting for the pair's queue to come down to what it
            # can hold: take no PCM this pump and let the queue already in
            # OpenAL play out. Nothing is dropped -- the bytes stay queued.
            return 0
        new_buffers = 0
        # A cinema room allocates its own per-speaker pools, so this receiver
        # never owns buffers while it is set.
        while (self.cinema is None and self._allocated_buffers < self.NUM_BUFFERS
               and new_buffers < max(0, max_new_buffers) and has_time()):
            try:
                buffer = audio_probe.call("relay.gen_buffer", self.game.audio_mngr.context.gen_buffer)
            except Exception:
                self._buffer_generation_failures += 1
                if (self._allocated_buffers < self.PREBUFFER_FRAMES * 2
                        and self._buffer_generation_failures >= 3):
                    self.failure_reason = "relay initial audio buffer allocation failed"
                break
            self._buffer_generation_failures = 0
            self._all_buffers.append(buffer)
            self._pool.append(buffer)
            self._allocated_buffers += 1
            new_buffers += 1
        queued_pairs = 0
        for _ in range(max(0, max_frames)):
            if not has_time():
                break
            if self.cinema is None and len(self._pool) < 2:
                break
            try:
                generation, left, right = self._pcm_frames.get_nowait()
            except queue.Empty:
                break
            with self._receive_lock:
                current_generation = self._generation
            if generation != current_generation:
                continue
            if generation != self._audio_generation:
                self._audio_generation = generation
            try:
                if self.cinema is not None:
                    backlog = self.cinema.queued_frames()
                else:
                    backlog = max(self.source_l.buffers_queued, self.source_r.buffers_queued)
                if self._shed_if_deep(backlog):
                    continue
                if self._queue_pair(left, right):
                    queued_pairs += 1
            except Exception:
                self.failure_reason = "relay audio state unavailable"
                break
        try:
            audio_probe.count("relay.new_buffers", new_buffers)
            audio_probe.count("relay.frames", queued_pairs)
            cinema = self.cinema
            if cinema is not None:
                queued = cinema.queued_frames()
                stopped = not cinema.playing()
                required = cinema.wanted_for_start()
                if stopped and queued >= required:
                    if not self._play_started:
                        audio_probe.event("relay.first_play")
                    audio_probe.call("relay.play", cinema.start_playback)
                    self._play_started = True
            else:
                queued = min(self.source_l.buffers_queued, self.source_r.buffers_queued)
                stopped = (self.source_l.state != cyal.SourceState.PLAYING
                           or self.source_r.state != cyal.SourceState.PLAYING)
                required = self.RESUME_FRAMES if self._play_started else self.PREBUFFER_FRAMES
                if stopped and queued >= required:
                    if not self._play_started:
                        audio_probe.event("relay.first_play")
                    audio_probe.call("relay.play", self.source_l.play)
                    audio_probe.call("relay.play", self.source_r.play)
                    self._play_started = True
        except Exception:
            self.failure_reason = "relay audio playback failed"
        return queued_pairs

    def retire(self, duration=0.5, cleanup_callback=None):
        """Fade on future main-thread pumps, then stop and clean up exactly once."""
        self._check_owner()
        if self._stopped:
            if callable(cleanup_callback):
                cleanup_callback()
            return
        if self._retire_started is not None:
            return
        self._retire_started = self._clock()
        self._retire_duration = max(0.0, float(duration))
        self._retire_cleanup = cleanup_callback
        if self._retire_duration == 0:
            self.stop()

    def stop(self):
        """Owner-only, nonblocking; daemon never owns or disposes native audio."""
        self._check_owner()
        if self._stopped:
            return
        self._stopped = True
        with self._receive_lock:
            self.running = False
            self._generation += 1
            self._drain(self.frames)
            self._drain(self._pcm_frames)
            self._put_latest(self.frames, None)
        self._reset_output()
        self._pool.clear()
        self._all_buffers.clear()
        self._allocated_buffers = 0
        # The owning JukeboxPlayer entry retains its sources until its cleanup
        # callback. The eventual daemon self-release must retain no AL objects.
        self.source_l = self.source_r = None
        self.cinema = None
        self.reverb_slot = self.eq_slot = None
        self.player = self.game = None
        cleanup, self._retire_cleanup = self._retire_cleanup, None
        if callable(cleanup):
            cleanup()
