"""Jukebox Opus decoding worker with exclusively main-owner OpenAL output.

Network reception and the daemon handle bounded bytes queues only. Sources,
buffers, gain, EFX and final disposal belong to the constructing audio thread.
"""

import array
import contextlib
import queue
import threading
import time

import cyal
from .audio_diagnostics import probe as audio_probe


class JukeboxRelayReceiver(threading.Thread):
    main_thread_audio = True
    PREBUFFER_FRAMES = 4
    RESUME_FRAMES = 3
    MAX_PENDING_FRAMES = 32
    NUM_BUFFERS = 32
    MAX_QUEUED_BUFFERS = 10
    MAX_PCM_BYTES = 48000 * 2 * 2 * 120 // 1000

    def __init__(self, game, source_l, source_r, volume, relay_id,
                 stream_epoch, reference_distance, max_distance,
                 box_pos=None, player=None, reverb_slot=None, eq_slot=None,
                 cabinet_volume=100, *, clock=None):
        super().__init__(daemon=True, name=f"jukebox-relay-{relay_id}")
        self._owner = threading.get_ident()
        self._clock = clock or time.monotonic
        self.game = game
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
        for source in (self.source_l, self.source_r):
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
        for source in (self.source_l, self.source_r):
            if source is not None:
                with contextlib.suppress(Exception):
                    source.stop()
        self._reclaim(stopped=True)
        self._play_started = False

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
        new_buffers = 0
        while (self._allocated_buffers < self.NUM_BUFFERS
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
            if len(self._pool) < 2 or not has_time():
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
                backlog = max(self.source_l.buffers_queued, self.source_r.buffers_queued)
                if self._play_started and backlog >= self.MAX_QUEUED_BUFFERS:
                    continue
                if self._queue_pair(left, right):
                    queued_pairs += 1
            except Exception:
                self.failure_reason = "relay audio state unavailable"
                break
        try:
            audio_probe.count("relay.new_buffers", new_buffers)
            audio_probe.count("relay.frames", queued_pairs)
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
        self.reverb_slot = self.eq_slot = None
        self.player = self.game = None
        cleanup, self._retire_cleanup = self._retire_cleanup, None
        if callable(cleanup):
            cleanup()
