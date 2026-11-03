"""Party Sync -> cinema room audio.

The host's machine is the only place every jammer's audio exists at once, so it
does the summing: each source is pushed as decoded 48 kHz PCM, the mixer emits
ONE 20 ms stereo Opus stream on ``CHANNEL_MUSICBOT_PARTY_ROOM``, and the Server
relays that stream to non-members on the cabinet's map only. A party member
never receives it, which is what stops the room copy being heard twice.

The room itself is resolved by ``libs/audio/cinema/peer.py`` on each listener,
from that listener's own map and the cabinet id announced with the stream. No
speaker layout is hard-coded here: ``custom``, ``auto``, profiles and ``off``
all end at the same ``RoomPlan`` the cabinet uses for its own songs.

The room bank is tuned around 20 ms frames, so the party stream uses the same
frame size even though a live jam source arrives in 10 ms pieces. The mixer
keeps the partial pieces and only sends a frame once every active source has a
little cushion. On the receiving side a sequence/epoch header plus a jitter
buffer order and re-time the frames; a lost frame is concealed with the last
one instead of being left as a hole that drains the speakers.

Pins: client/tests/test_party_room.py.
"""

import audioop
import random
import struct
import threading
import time

from .. import consts
from .. import audio_trace


FRAME_MS = 20
FRAME_SECONDS = FRAME_MS / 1000.0
FRAME_BYTES = 3840                 # 20 ms, 48 kHz, signed 16-bit stereo
MAX_BUFFERED_FRAMES = 12           # 240 ms per source; a slow producer must not grow forever
SOURCE_IDLE_SECONDS = 0.35
MIXER_PREBUFFER_FRAMES = 6         # 120 ms: Chrome loopback can gap for ~63 ms

# Wire format: room key (2), version (1), epoch (2), frame sequence (2), Opus.
# The Server only reads the key; the listener uses epoch to notice a mixer
# restart and sequence to restore order after an unreliable packet is late.
ROOM_STREAM_VERSION = 1
ROOM_STREAM_HEADER_BYTES = 7

RECEIVER_PREBUFFER_FRAMES = 8      # 160 ms on top of the room bank's own 80 ms
RECEIVER_MAX_RELEASE_FRAMES = 16   # one burst can prime a room, never flood it
RECEIVER_REORDER_WAIT_FRAMES = 4   # wait this many later frames before concealing a hole
RECEIVER_MAX_CONCEAL_FRAMES = 5    # at most 100 ms of repeat for one gap


def pack_room_packet(room_key, epoch, seq, opus):
    """Build one room-stream packet for the Server to relay unchanged."""
    return (struct.pack(">HBHH", int(room_key) & 0xFFFF,
                        ROOM_STREAM_VERSION,
                        int(epoch) & 0xFFFF,
                        int(seq) & 0xFFFF)
            + bytes(opus or b""))


def parse_room_packet(data):
    """Return ``(key, epoch, seq, opus)``, or None when unusable.

    ``seq`` is None for the previous headerless format, so an updated listener
    can still play an older host while the two machines are being restarted.
    """
    if not isinstance(data, (bytes, bytearray, memoryview)):
        return None
    raw = bytes(data)
    if len(raw) < 3:
        return None
    key = int.from_bytes(raw[:2], "big")
    if key <= 0:
        return None
    if len(raw) >= ROOM_STREAM_HEADER_BYTES and raw[2] == ROOM_STREAM_VERSION:
        epoch, seq = struct.unpack(">HH", raw[3:ROOM_STREAM_HEADER_BYTES])
        return key, epoch, seq, raw[ROOM_STREAM_HEADER_BYTES:]
    return key, 0, None, raw[2:]


class PartyRoomMixer(threading.Thread):
    """Sum every jammer's PCM and send one 20 ms stereo Opus stream for a room."""

    def __init__(self, game, room_key, cabinet=""):
        super().__init__(daemon=True)
        self.game = game
        self.room_key = int(room_key) & 0xFFFF
        self.cabinet = str(cabinet or "")
        self.running = True
        self._lock = threading.Lock()
        self._buffers = {}
        self._pushed_at = {}
        self._primed = False
        self.epoch = random.SystemRandom().randrange(1, 0x10000)
        self.sequence = 0
        self.sent_frames = 0
        self.encode_errors = 0
        self._trace_last_send = None
        self._trace_sent = 0
        self._trace_last_idle = 0.0
        self._trace_started_at = time.monotonic()
        from pyogg import OpusEncoder
        self.encoder = OpusEncoder()
        self.encoder.set_application("audio")
        self.encoder.set_channels(2)
        self.encoder.set_sampling_frequency(48000)

    # ---------------------------------------------------------------- input
    def push(self, source, pcm, stereo=True):
        """Queue one decoded frame from ``source`` (host or a guest channel)."""
        if not self.running or not pcm:
            return
        try:
            data = bytes(pcm)
        except Exception:
            return
        if len(data) < 2:
            return
        if len(data) & 1:
            data = data[:-1]
        if not stereo:
            try:
                data = audioop.tostereo(data, 2, 1.0, 1.0)
            except Exception:
                return
        limit = FRAME_BYTES * MAX_BUFFERED_FRAMES
        with self._lock:
            buf = self._buffers.setdefault(source, bytearray())
            buf.extend(data)
            if len(buf) > limit:
                del buf[:len(buf) - limit]
            self._pushed_at[source] = time.monotonic()
            if audio_trace.enabled():
                audio_trace.event(
                    "room_push", source=str(source), bytes=len(data),
                    stereo=bool(stereo), buffered=len(buf),
                    buffered_ms=round(len(buf) / float(FRAME_BYTES) * FRAME_MS, 1),
                    context=audio_trace.party_context(self.game)
                )

    def push_host(self, pcm, stereo=True):
        """The host's own captured program/instrument."""
        self.push("host", pcm, stereo=stereo)

    def stop(self):
        self.running = False

    # ---------------------------------------------------------------- mix
    def _take_frame(self, source, now):
        """One 20 ms stereo frame for a source, padded with silence if short."""
        with self._lock:
            buf = self._buffers.get(source)
            if buf is None:
                return None
            if len(buf) >= FRAME_BYTES:
                frame = bytes(buf[:FRAME_BYTES])
                del buf[:FRAME_BYTES]
            else:
                # A 10 ms source has not filled this tick's frame yet. Emit
                # silence but KEEP the partial frame: deleting it here would
                # drop every other slice (half the jam).
                if now - self._pushed_at.get(source, 0.0) > SOURCE_IDLE_SECONDS:
                    del buf[:]
                frame = b""
            pushed = self._pushed_at.get(source, 0.0)
        if len(frame) < FRAME_BYTES:
            frame += b"\x00" * (FRAME_BYTES - len(frame))
        if not frame.strip(b"\x00"):
            # No audio from this source this tick. It stays active briefly so a
            # producer still gets an even frame every tick.
            if now - pushed > SOURCE_IDLE_SECONDS:
                return None
            return b"\x00" * FRAME_BYTES
        return frame

    @staticmethod
    def mix_frames(frames):
        """Sum equally so several jammers do not clip each other."""
        # A source that just stopped still reports a silence frame for a short
        # while. Counting that frame in the divisor would quietly lower every
        # remaining jammer for 350 ms, so only frames with audio set the gain.
        frames = [f for f in frames if f and f.strip(b"\x00")]
        if not frames:
            return b""
        gain = 1.0 / len(frames)
        out = None
        for frame in frames:
            try:
                part = audioop.mul(frame, 2, gain)
            except Exception:
                continue
            out = part if out is None else audioop.add(out, part, 2)
        if out is None:
            return b""
        return out[:FRAME_BYTES].ljust(FRAME_BYTES, b"\x00")

    def _ready_to_start(self, now):
        """Whether every recent source has the mixer's starting cushion.

        A 10 ms source needs twelve pieces (120 ms) and a 20 ms source six. The
        cushion is what makes the first frame whole instead of a silent tick
        followed by a burst, and it is what covers a loopback source's own
        short capture gaps; after the stream has started the mixer tolerates a
        single short source and keeps its partial audio for the next tick.
        """
        with self._lock:
            candidates = [(source, len(buf))
                          for source, buf in self._buffers.items() if buf]
            if not candidates:
                return False
            for source, size in candidates:
                recent = now - self._pushed_at.get(source, 0.0) <= SOURCE_IDLE_SECONDS
                if recent and size < FRAME_BYTES * MIXER_PREBUFFER_FRAMES:
                    return False
            return True

    def _active_sources(self, now):
        with self._lock:
            sources = list(self._buffers)
            pushed = dict(self._pushed_at)
            live = {source for source, buf in self._buffers.items() if buf}
        return [s for s in sources
                if s in live
                or now - pushed.get(s, 0.0) <= SOURCE_IDLE_SECONDS]

    def _trace_depths(self):
        with self._lock:
            return ";".join(
                "%s:%d" % (source, len(buf) // FRAME_BYTES)
                for source, buf in self._buffers.items()
            )

    @staticmethod
    def _wait_for_deadline(deadline):
        """Sleep only to 1 ms before a frame deadline, then spin the rest.

        ``time.sleep`` can return a few milliseconds late for the short
        remainder left after Opus encoding. That was enough to turn a 20 ms
        room frame into a 31 ms one on Windows, so the receiver's 20 ms playback
        clock drank its cushion and concealed constantly. Sleeping to an ideal
        deadline, as the live relay does, keeps the cadence instead of adding
        the overshoot to every frame.
        """
        remaining = deadline - time.monotonic()
        if remaining > 0.001:
            time.sleep(remaining - 0.001)
        while time.monotonic() < deadline:
            pass

    def run(self):
        network = getattr(self.game, "network", None)
        trace_prev_send = None
        trace_last_idle = 0.0
        trace_started_at = time.monotonic()
        next_deadline = time.monotonic()
        while self.running:
            trace_on = audio_trace.enabled()
            started = time.monotonic()
            now = time.monotonic()
            sources = self._active_sources(now)
            if not sources:
                if trace_on and now - trace_last_idle >= 1.0:
                    trace_last_idle = now
                    audio_trace.event(
                        "room_mix_idle", key=self.room_key,
                        cabinet=self.cabinet, primed=bool(self._primed),
                        context=audio_trace.party_context(self.game)
                    )
                self._primed = False
                next_deadline = time.monotonic()
                time.sleep(0.01)
                continue
            if not self._primed:
                if not self._ready_to_start(now):
                    if trace_on and now - trace_last_idle >= 1.0:
                        trace_last_idle = now
                        audio_trace.event(
                            "room_mix_wait", key=self.room_key,
                            cabinet=self.cabinet, sources=len(sources),
                            depths=self._trace_depths(),
                            context=audio_trace.party_context(self.game)
                        )
                    next_deadline = time.monotonic()
                    time.sleep(0.005)
                    continue
                self._primed = True
                if trace_on:
                    audio_trace.event(
                        "room_mix_start", key=self.room_key,
                        cabinet=self.cabinet, sources=len(sources),
                        depths=self._trace_depths(),
                        context=audio_trace.party_context(self.game)
                    )
            frames = []
            for source in sources:
                frame = self._take_frame(source, now)
                if frame:
                    frames.append(frame)
            trace_mix_start = time.monotonic() if trace_on else 0.0
            mixed = self.mix_frames(frames)
            trace_mix_ms = ((time.monotonic() - trace_mix_start) * 1000.0
                            if trace_on else 0.0)
            sent_frame = False
            if mixed and network is not None:
                try:
                    trace_encode_start = time.monotonic() if trace_on else 0.0
                    encoded = self.encoder.encode(bytearray(mixed))
                    if encoded:
                        payload = pack_room_packet(
                            self.room_key, self.epoch, self.sequence, encoded)
                        trace_send_start = time.monotonic() if trace_on else 0.0
                        network.send(
                            consts.CHANNEL_MUSICBOT_PARTY_ROOM,
                            "n/a", payload, False,
                        )
                        sent_frame = True
                        if trace_on:
                            send_now = time.monotonic()
                            audio_trace.event(
                                "room_mix", key=self.room_key,
                                cabinet=self.cabinet, seq=self.sequence,
                                epoch=self.epoch, sources=len(sources),
                                frames=len(frames), depths=self._trace_depths(),
                                gap_ms=audio_trace.gap_ms(
                                    trace_send_start, trace_prev_send),
                                mix_ms=round(trace_mix_ms, 2),
                                encode_ms=round(
                                    (trace_send_start - trace_encode_start) * 1000.0, 2),
                                send_ms=round((send_now - trace_send_start) * 1000.0, 2),
                                late_ms=round(
                                    (send_now - next_deadline) * 1000.0, 2),
                                sent=self.sent_frames + 1,
                                context=audio_trace.party_context(self.game)
                            )
                            trace_prev_send = trace_send_start
                            trace_last_idle = send_now
                            self._trace_sent += 1
                        self.sequence = (self.sequence + 1) & 0xFFFF
                        self.sent_frames += 1
                except Exception as exc:
                    self.encode_errors += 1
                    audio_trace.event(
                        "room_mix_error", key=self.room_key,
                        error=type(exc).__name__,
                        context=audio_trace.party_context(self.game)
                    )
            elif mixed and trace_on and network is None:
                audio_trace.event(
                    "room_mix_no_network", key=self.room_key,
                    context=audio_trace.party_context(self.game)
                )
            if sent_frame:
                next_deadline += FRAME_SECONDS
                now_after = time.monotonic()
                if now_after - next_deadline > 0.040:
                    # A real stall starts a fresh cadence; do not burst a
                    # backlog to catch up with a deadline that is already gone.
                    next_deadline = now_after
                self._wait_for_deadline(next_deadline)
            else:
                next_deadline = time.monotonic()
                elapsed = time.monotonic() - started
                time.sleep(max(0.0, FRAME_SECONDS - elapsed))
        audio_trace.event(
            "room_mix_stop", key=self.room_key, sent=self.sent_frames,
            errors=self.encode_errors,
            elapsed_s=round(time.monotonic() - trace_started_at, 2),
            context=audio_trace.party_context(self.game)
        )


class PartyRoomJitter:
    """Order one room's Opus frames and conceal a hole with the last frame.

    Network frames are unreliable and may arrive late, so decoding them as they
    arrive puts the Opus state out of order. This buffer keeps the packets until
    the sequence reaches them, then hands them to ``decode`` in order. A gap is
    held for a few later frames (a late packet can still fill it), then repeated
    from the last decoded frame so the room queue never simply runs dry.
    """

    def __init__(self, prebuffer=RECEIVER_PREBUFFER_FRAMES):
        self.prebuffer = max(1, int(prebuffer))
        self.epoch = None
        self.expected = None
        self._pending = {}
        self._last = None
        self._silence = b"\x00" * FRAME_BYTES
        self.last_concealed = 0
        self.last_seq_delta = None

    @staticmethod
    def _ahead(seq, expected):
        return (int(seq) - int(expected)) & 0xFFFF

    def reset(self):
        self.epoch = None
        self.expected = None
        self._pending.clear()
        self._last = None
        self.last_concealed = 0
        self.last_seq_delta = None

    def buffered(self):
        return len(self._pending)

    def _conceal(self):
        return self._last or self._silence

    def _drain(self, decode):
        out = []
        concealed = 0
        guard = 0
        while len(out) < RECEIVER_MAX_RELEASE_FRAMES and guard < RECEIVER_MAX_RELEASE_FRAMES * 2:
            guard += 1
            if self.expected in self._pending:
                payload = self._pending.pop(self.expected)
                pcm = decode(payload)
                if not pcm:
                    pcm = self._conceal()
                out.append(pcm)
                self._last = pcm
                self.expected = (self.expected + 1) & 0xFFFF
                continue
            future = [seq for seq in self._pending
                      if 0 < self._ahead(seq, self.expected) < 0x8000]
            if not future:
                break
            gap = min(self._ahead(seq, self.expected) for seq in future)
            # Wait for a few later frames: one late packet is reordering, not a
            # hole. Once the evidence says it is a hole, fill at most 100 ms
            # and stop: a longer stall must not turn into a burst of repeats
            # that floods the room in one callback.
            if gap > RECEIVER_MAX_CONCEAL_FRAMES:
                # A long gap is not a hole to repeat through: doing that once
                # per arriving packet would turn a 400 ms dropout into seconds
                # of held audio. Repeat once to soften the seam, then jump to
                # the next real frame and let the sequence carry on.
                if concealed < RECEIVER_MAX_CONCEAL_FRAMES:
                    out.append(self._conceal())
                    concealed += 1
                self.expected = min(future)
                continue
            if len(self._pending) < RECEIVER_REORDER_WAIT_FRAMES:
                break
            count = min(gap, RECEIVER_MAX_CONCEAL_FRAMES - concealed,
                        RECEIVER_MAX_RELEASE_FRAMES - len(out))
            if count <= 0:
                break
            for _ in range(count):
                out.append(self._conceal())
                self.expected = (self.expected + 1) & 0xFFFF
            concealed += count
        self.last_concealed = concealed
        return out

    def push(self, epoch, seq, opus, decode):
        """Return the ordered PCM frames that are ready, including any concealment."""
        epoch = int(epoch or 0) & 0xFFFF
        seq = int(seq) & 0xFFFF
        self.last_concealed = 0
        if self.epoch != epoch:
            self.reset()
            self.epoch = epoch
        if self.expected is None:
            self._pending[seq] = bytes(opus or b"")
            if len(self._pending) < self.prebuffer:
                return []
            self.expected = min(self._pending)
        else:
            delta = self._ahead(seq, self.expected)
            self.last_seq_delta = delta
            if delta > 0x8000:
                # A duplicate or a packet from before the stream's current
                # position. Decoding it now would rewind the Opus state.
                return []
            self._pending.setdefault(seq, bytes(opus or b""))
        return self._drain(decode)


class PartyRoomReceiver:
    """Decode one party-room stream in sequence and hand it to that room's feed."""

    def __init__(self, game):
        self.game = game
        self._decoders = {}
        self._jitter = {}
        self._epochs = {}
        self._trace_last = {}
        self.dropped = 0
        self.concealed = 0

    def _decoder(self, room_key):
        key = int(room_key)
        decoder = self._decoders.get(key)
        if decoder is None:
            try:
                from pyogg import OpusDecoder
                decoder = OpusDecoder()
                decoder.set_channels(2)
                decoder.set_sampling_frequency(48000)
            except Exception:
                return None
            self._decoders[key] = decoder
        return decoder

    def _forget_stream(self, key):
        self._jitter.pop(key, None)
        self._epochs.pop(key, None)
        self._decoders.pop(key, None)

    def decode(self, room_key, opus):
        """Decode one ordered frame; None when it is unusable."""
        if not opus or len(opus) > 1275:
            return None
        decoder = self._decoder(room_key)
        if decoder is None:
            return None
        try:
            return bytes(decoder.decode(bytearray(opus)))
        except Exception as exc:
            self.dropped += 1
            audio_trace.event(
                "room_decode_error", key=int(room_key), opus=len(opus or b""),
                error=type(exc).__name__,
                context=audio_trace.party_context(self.game)
            )
            return None

    def play(self, room_key, epoch, seq, opus):
        """MAIN THREAD: resolve the listener's room and queue ordered frames.

        Decoding happens here, not on the network thread, because the jitter
        buffer may hold a frame back for a later sequence. A decoder fed out of
        order produces broken audio even when every packet eventually arrives.
        """
        from ..audio.cinema import peer as cinema_peer
        key = int(room_key)
        trace_on = audio_trace.enabled()
        now = time.monotonic() if trace_on else 0.0
        gap = audio_trace.gap_ms(now, self._trace_last.get(key)) if trace_on else None
        if trace_on:
            self._trace_last[key] = now
            audio_trace.event(
                "room_rx", key=key, epoch=int(epoch or 0) & 0xFFFF,
                seq=None if seq is None else int(seq) & 0xFFFF,
                opus=len(opus or b""), gap_ms=gap,
                context=audio_trace.party_context(self.game)
            )
        feed = cinema_peer.route(self.game, key)
        if feed is None:
            if trace_on:
                audio_trace.event(
                    "room_drop", reason="no_room", key=key,
                    context=audio_trace.party_context(self.game)
                )
            self._forget_stream(key)
            return False
        epoch = int(epoch or 0) & 0xFFFF
        if self._epochs.get(key) != epoch:
            # A new mixer (or a new session) has its own sequence and Opus
            # history; keeping the old decoder would put a click at the seam.
            self._epochs[key] = epoch
            self._decoders.pop(key, None)
            self._jitter[key] = PartyRoomJitter()
            if trace_on:
                audio_trace.event(
                    "room_epoch", key=key, epoch=epoch,
                    context=audio_trace.party_context(self.game)
                )
        if seq is None:
            # An older host has no sequence. Play it directly rather than
            # pretending a stale header byte is one.
            pcm = self.decode(key, opus)
            if not pcm:
                return False
            frames = [pcm]
            jitter = None
        else:
            jitter = self._jitter[key]
            frames = jitter.push(
                epoch, seq, opus,
                lambda payload: self.decode(key, payload),
            )
        if not frames:
            if trace_on:
                audio_trace.event(
                    "room_buffered", key=key, seq=None if seq is None else int(seq) & 0xFFFF,
                    buffered=0 if jitter is None else jitter.buffered(),
                    expected=None if jitter is None else jitter.expected,
                    context=audio_trace.party_context(self.game)
                )
            return False
        accepted = False
        try:
            for frame in frames:
                accepted = bool(feed.push(frame, stereo=True)) or accepted
        except Exception as exc:
            audio_trace.event(
                "room_feed_error", key=key, cabinet=getattr(feed, "cabinet_id", ""),
                error=type(exc).__name__,
                context=audio_trace.party_context(self.game)
            )
            return False
        if trace_on:
            audio_trace.event(
                "room_play", key=key, seq=None if seq is None else int(seq) & 0xFFFF,
                frames=len(frames), accepted=bool(accepted),
                cabinet=getattr(feed, "cabinet_id", ""),
                concealed=0 if jitter is None else jitter.last_concealed,
                buffered=0 if jitter is None else jitter.buffered(),
                expected=None if jitter is None else jitter.expected,
                seq_delta=None if jitter is None else jitter.last_seq_delta,
                context=audio_trace.party_context(self.game)
            )
        return accepted
