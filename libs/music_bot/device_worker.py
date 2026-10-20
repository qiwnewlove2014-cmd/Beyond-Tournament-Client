"""The child side of the Music Bot's own output device.

Started by the client as `--bt-music-device <specifier>` and owned by
`libs/music_bot/output_device.py`, which documents why a process of its own is
the honest way to play one stream on a second device: an OpenAL context is
bound to a device, and every object in it may only be touched while that
context is current on the calling thread -- which the game's three audio
threads and its EFX sends are not built around.

It imports OpenAL and nothing of the game: no pygame, no options, no speech, no
VFS. Its whole job is to read frames off stdin, keep a short queue per slot and
say what it did on stdout when it exits (the rig in
`client/tools/music_output_device_live.py` and a player's log read that line).
"""

import json
import struct
import sys
import time

import cyal

from .output_device import (
    ALL_SLOTS,
    AUDIO,
    DEVICE_FLAG,
    FLUSH,
    FORMAT,
    GAIN,
    HEADER,
    MAX_SLOTS,
    PAUSE,
    QUIT,
)

# How many 20 ms frames one slot may hold before this process stops reading.
# The pipe then fills and the parent's queue answers "not now", which the
# streamer turns into "try that frame again" -- no audio is ever dropped, the
# decoder waits instead.
MAX_QUEUED = 6

# A frame this size is not one of ours: the stream is desynchronised, and
# guessing at the rest of it would be worse than stopping.
_MAX_PAYLOAD = 1 << 20

EXIT_NO_DEVICE = 3
EXIT_BROKEN_STREAM = 4


def main(argv=None):
    """Entry point for the flag: `argv` is what follows it. Returns the status."""
    argv = list(sys.argv[2:] if argv is None else argv)
    device = argv[0] if argv else ""
    return play(device, sys.stdin.buffer, sys.stdout)


class _Slot():
    """One stream's source and buffer pool inside this device."""

    def __init__(self, context, channels=2, rate=48000):
        self.context = context
        self.channels = int(channels) or 2
        self.rate = int(rate) or 48000
        self.source = context.gen_source()
        # The Music Bot's own stream is stereo straight to the ear: no
        # spatialisation, no mono downmix, the two sides it was sent with.
        self.source.direct_channels = True
        self.source.spatialize = False
        self.pool = []
        self.paused = False
        self.played = 0
        self._format = (cyal.BufferFormat.MONO16 if self.channels == 1
                        else cyal.BufferFormat.STEREO16)

    def reclaim(self):
        """Take finished buffers back, so the pool is what bounds memory."""
        try:
            while self.source.buffers_processed > 0:
                result = self.source.unqueue_buffers()
                if result is None:
                    break
                try:
                    self.pool.extend(result)
                except TypeError:
                    self.pool.append(result)
        except Exception:
            pass

    def queued(self):
        try:
            return int(self.source.buffers_queued)
        except Exception:
            return 0

    def queue(self, data):
        self.reclaim()
        buffer = self.pool.pop() if self.pool else self.context.gen_buffer()
        buffer.set_data(data, sample_rate=self.rate, format=self._format)
        self.source.queue_buffers(buffer)
        self.played += 1
        if not self.paused:
            try:
                if self.source.state != cyal.SourceState.PLAYING:
                    self.source.play()
            except Exception:
                pass

    def set_format(self, channels, rate):
        # What is queued was decoded with the old answer, so it goes.
        self.flush()
        self.channels = int(channels) or 2
        self.rate = int(rate) or 48000
        self._format = (cyal.BufferFormat.MONO16 if self.channels == 1
                        else cyal.BufferFormat.STEREO16)

    def set_pause(self, paused):
        self.paused = bool(paused)
        try:
            if self.paused:
                self.source.pause()
            elif self.queued() > 0:
                self.source.play()
        except Exception:
            pass

    def flush(self):
        try:
            self.source.stop()
            while self.source.buffers_queued > 0:
                result = self.source.unqueue_buffers()
                if result is None:
                    break
                try:
                    self.pool.extend(result)
                except TypeError:
                    self.pool.append(result)
        except Exception:
            pass
        self.reclaim()

    def close(self):
        self.flush()
        try:
            self.source.delete()
        except Exception:
            pass


def _read_exact(stream, size):
    """Read exactly `size` bytes, or None at a clean end of stream."""
    chunks = []
    remaining = size
    while remaining > 0:
        try:
            chunk = stream.read(remaining)
        except (OSError, ValueError):
            return None
        if not chunk:
            return None if not chunks else b"".join(chunks)
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _wait_for_room(slots):
    """Let the device catch up instead of reading the pipe into a growing queue."""
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        total = 0
        for slot in slots.values():
            slot.reclaim()
            total += slot.queued()
        if total < MAX_QUEUED:
            return
        time.sleep(0.005)


def play(device, stdin, stdout=None, context_factory=None):
    """Read frames until stdin closes, and play them on `device`.

    `context_factory` is the test seam: the suite drives this whole parser with
    an OpenAL-shaped double, so what is pinned is the frame handling and not a
    sound card. In the game it is left None and the device is really opened.
    """
    slots = {}
    played = 0
    error = None
    context = None
    try:
        if context_factory is not None:
            context = context_factory(device)
        else:
            cyal_device = cyal.Device(name=device)
            context = cyal.Context(
                cyal_device, make_current=True,
                mono_sources=1, stereo_sources=64,
            )
    except Exception as open_error:
        error = f"{open_error.__class__.__name__}: {open_error}"
        _report(stdout, device, 0, error)
        return EXIT_NO_DEVICE
    try:
        while True:
            if slots:
                _wait_for_room(slots)
            header = _read_exact(stdin, HEADER.size)
            if header is None or len(header) < HEADER.size:
                break
            kind, slot_id, length = HEADER.unpack(header)
            if length > _MAX_PAYLOAD:
                error = "frame too large"
                break
            payload = _read_exact(stdin, length) if length else b""
            if length and (payload is None or len(payload) < length):
                break
            if kind == QUIT:
                break
            slot = slots.get(slot_id)
            if kind == FORMAT:
                if length == 6:
                    channels, rate = struct.unpack(">HI", payload)
                    slot_id = max(0, min(MAX_SLOTS - 1, slot_id))
                    slot = slots.get(slot_id)
                    if slot is None:
                        slots[slot_id] = _Slot(context, channels, rate)
                    else:
                        slot.set_format(channels, rate)
                continue
            targets = ([s for index, s in slots.items() if index == slot_id]
                       if slot_id != ALL_SLOTS else list(slots.values()))
            if kind == PAUSE:
                paused = bool(payload[:1]) and bool(payload[0])
                for target in targets:
                    target.set_pause(paused)
                continue
            if kind == FLUSH:
                for target in targets:
                    target.flush()
                continue
            if kind == GAIN:
                if length == 4:
                    gain = struct.unpack(">f", payload)[0]
                    for target in targets:
                        try:
                            target.source.gain = max(0.0, min(1.0, gain))
                        except Exception:
                            pass
                continue
            if kind == AUDIO:
                if slot is None:
                    slot = slots[slot_id] = _Slot(context)
                try:
                    slot.queue(payload)
                except Exception:
                    # One frame the device would not take is not a reason to
                    # leave the player without music: the next one may land.
                    pass
                continue
    finally:
        for slot in slots.values():
            played += slot.played
            slot.close()
        _report(stdout, device, played, error)
    return 0 if error is None else EXIT_BROKEN_STREAM


def _report(stdout, device, played, error):
    """One JSON line for whoever started this process: what it opened and played."""
    if stdout is None:
        return
    try:
        line = json.dumps({
            "device": str(device or ""),
            "played_frames": int(played),
            "error": error,
        }, ensure_ascii=True)
        stdout.write(line + "\n")
        stdout.flush()
    except Exception:
        pass


if __name__ == "__main__":
    raise SystemExit(main())
