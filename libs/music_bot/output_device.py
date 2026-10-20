"""The Music Bot's own stream, played on an output device of its own.

**Why this is a child process.** OpenAL binds exactly one device to a context,
and every source in that context comes out of that one device. There is no
per-source device routing in the API, so "music on the speakers, the game in
the headset" cannot be done by asking a source nicely -- it needs a second
context on a second device, and a context's objects may only be touched while
that context is current *on the calling thread*. The Music Bot's sources are
created, gained, filtered and fed from three threads (the bot, the streamer and
the crossfade handler), and the equalizer, the map reverb and the underwater
muffle are EFX sends on the game's context, which a source in another context
cannot use at all. A second context in-process would therefore mean rewriting
every one of those call sites into context switches, for a feature that must
never be able to take the game's audio down with it.

A child process owns that risk instead, exactly as the folder dialog and the
YouTube resolver do (`client/libs/folder_dialog.py`,
`client/libs/youtube_resolver.py`): it opens its own device, plays the PCM it
is handed, and if it dies the game keeps playing on the game's device.

**What is played here.** Only the bot's own playback -- the interlaced stereo
the streamer already decodes (its plain source path). A cinema room's speakers
are map objects on the game's device and stay there, and so does the jukebox's
spatial pair: a cabinet heard across a room has a position, and a device in a
player's other ear does not.

**What is not.** The equalizer, the room reverb and the underwater muffle are
EFX on the game's context. On a separate device the song is played flat; the
menu says so on the line the player picks it from.

The protocol is length-prefixed binary over the child's stdin: small integers
and PCM, never text, never a shell string. One frame is what the streamer
already feeds OpenAL (`AudioStreamer.BUFFER_SIZE`, 20 ms of 16-bit audio).
"""

import os
import queue
import struct
import subprocess
import sys
import threading

# The sentinel a player who wants the bot where the game plays gets. It is the
# stored value of the option, so it must stay false-y: an empty option is also
# a client that has never opened this menu.
GAME_DEVICE = ""

# The argv flag the client re-enters itself with to become the worker. It sits
# above every game import in beyond_tournament.py, beside the resolver's and
# the folder dialog's, so a child never runs the game's startup.
DEVICE_FLAG = "--bt-music-device"

# Every OpenAL Soft specifier carries its own prefix; a menu line reads better
# without it, and the full string is what the device is opened by.
DEVICE_PREFIX = "OpenAL Soft on "

# Frame kinds. One header for all of them: kind, slot, payload length.
AUDIO = 1        # PCM for one slot, exactly one streamer frame
GAIN = 2         # float32 linear gain for one slot
PAUSE = 3        # one byte, 0/1, for one slot (or every slot)
FLUSH = 4        # drop what that slot has queued (a seek or a stop)
FORMAT = 5       # channels, sample rate: the slot's next AUDIO frames say this
QUIT = 6         # finish what is queued, then exit 0

# "Every slot" for PAUSE and FLUSH, and the number of slots the worker builds.
ALL_SLOTS = 255
MAX_SLOTS = 4

HEADER = struct.Struct(">BBI")

# How much PCM the parent may have unwritten before it stops accepting more.
# The pipe itself holds another ~64 KB, so this is the burst a hitch can
# absorb; past it ``push`` answers False and the streamer retries the same
# frame instead of dropping it (the contract ``_queue_local`` already has).
MAX_PENDING_BYTES = 6 * 3840

# A 20 ms frame of the bot's stereo stream, for reporting a queue in frames.
FRAME_BYTES = 3840


def short_name(specifier):
    """A device's name as a menu line reads it."""
    text = str(specifier or "")
    return text[len(DEVICE_PREFIX):] if text.startswith(DEVICE_PREFIX) else text


def available_devices():
    """Every output device this machine offers, as OpenAL names them.

    Read through the game's own OpenAL, so the list is exactly what the game
    could be asked to play on -- a menu that offered anything else would be
    offering a device this machine cannot open. A machine with no usable
    OpenAL gets an empty list, and the menu then holds only "Same as the game".
    """
    try:
        import cyal
        devices = list(cyal.util.get_all_device_specifiers() or ())
    except Exception:
        return ()
    seen, ordered = set(), []
    for device in devices:
        if not device or device in seen:
            continue
        seen.add(device)
        ordered.append(device)
    return tuple(ordered)


def _command(parts):
    """The argv that starts this same client as the device worker.

    A source checkout runs ``beyond_tournament.py``; a compiled build is its
    own executable, which re-enters through the same flag. Absolute paths and
    an argv list, never a shell string (a path with a space or a non-ASCII
    character is the normal case here).
    """
    args = [sys.executable]
    if not (getattr(sys, "frozen", False) or "__compiled__" in globals()):
        args.append(os.path.abspath(os.path.join(
            os.path.dirname(__file__), os.pardir, os.pardir,
            "beyond_tournament.py")))
    return args + [DEVICE_FLAG] + list(parts)


def _working_directory():
    if getattr(sys, "frozen", False) or "__compiled__" in globals():
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.abspath(os.path.join(
        os.path.dirname(__file__), os.pardir, os.pardir))


def _child_environment():
    """The known interpreter and its packages, not an inherited import override."""
    excluded = {"PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "PYTHONINSPECT",
                "PYTHONBREAKPOINT"}
    environment = {key: value for key, value in os.environ.items()
                   if key.upper() not in excluded}
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return environment


class MusicOutputProcess():
    """One child process playing the bot's frames on one device.

    The bot owns one of these while a device other than the game's is chosen:
    it is started when the stream needs it, kept across songs (a new song is
    just more frames in the same queue) and closed when the player picks
    another device or the bot stops. Frames go through a bounded queue and one
    writer thread, so the audio thread that feeds them never blocks on a pipe.
    """

    def __init__(self, device, popen=None, command=None):
        self.device = str(device or "")
        self._popen = popen or subprocess.Popen
        self._command = command or _command
        self.process = None
        self._queue = queue.Queue()
        self._lock = threading.Lock()
        self._pending = 0
        self._used_slots = set()
        self._dead = False
        self._writer = None
        self.write_failures = 0
        self.status = None

    # ---------------------------------------------------------- lifecycle
    def start(self):
        """Open the child, or raise what stopped it (the caller speaks that)."""
        if self.process is not None:
            return self
        self.process = self._popen(
            self._command([self.device]), shell=False,
            stdin=subprocess.PIPE,
            # A status line on exit is how a rig (and the player's log) can
            # tell "played" from "could not open the device".
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            close_fds=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            cwd=_working_directory(), env=_child_environment(),
        )
        self._writer = threading.Thread(target=self._write_loop,
                                        name="music-output", daemon=True)
        self._writer.start()
        return self

    def alive(self):
        return self.process is not None and self.process.poll() is None

    def stop(self, timeout=1.0):
        """Finish what is queued, close the child, and read what it reported."""
        process = self.process
        if process is None:
            return
        try:
            # Queued before the process handle is dropped, so the writer sees
            # it and the child finishes what it already holds.
            self._enqueue(QUIT, ALL_SLOTS, b"")
            self._queue.put(None)
            self.process = None
            if self._writer is not None:
                self._writer.join(timeout=timeout)
            try:
                process.stdin.close()
            except Exception:
                pass
            try:
                process.wait(timeout=timeout)
            except Exception:
                process.kill()
                try:
                    process.wait(timeout=timeout)
                except Exception:
                    pass
            self.status = self._read_status(process)
        except Exception:
            pass
        finally:
            self._writer = None
            with self._lock:
                self._pending = 0
                self._used_slots.clear()

    @staticmethod
    def _read_status(process):
        """The child's last stdout line: what it opened and how much it played."""
        try:
            raw = process.stdout.read() or b""
        except Exception:
            return None
        for line in reversed(raw.decode("utf-8", "replace").splitlines()):
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                import json
                data = json.loads(line)
            except Exception:
                continue
            if isinstance(data, dict):
                return data
        return None

    # ------------------------------------------------------------- slots
    def lease_slot(self):
        """Give one streamer a slot (a source of its own inside the child).

        Two streams meet on one device exactly as they do on the game's: the
        crossfade's outgoing song keeps its own source and its own gain while
        the incoming one starts, so the overlap is a mix and not a queue.
        """
        with self._lock:
            for slot in range(MAX_SLOTS):
                if slot not in self._used_slots:
                    self._used_slots.add(slot)
                    self._dead = False
                    return slot
        return None

    def release_slot(self, slot):
        """Hand a stream's slot back -- without cutting what it already plays.

        A song that ends has its last frames queued in the child, and they are
        the end of that song: flushing here would clip ~120 ms off every track.
        Dropping the queue is the bot's decision (`MapMusicBot.stop`, which is
        what a seek and a new song go through), not a streamer's teardown.
        """
        if slot is None:
            return
        with self._lock:
            self._used_slots.discard(slot)

    # ------------------------------------------------------------ control
    def set_format(self, slot, channels, rate=48000):
        payload = struct.pack(">HI", int(channels), int(rate))
        self._enqueue(FORMAT, slot, payload)

    def set_gain(self, slot, gain):
        payload = struct.pack(">f", float(gain))
        self._enqueue(GAIN, slot, payload)

    def set_pause(self, paused, slot=ALL_SLOTS):
        self._enqueue(PAUSE, slot, b"\x01" if paused else b"\x00")

    def flush(self, slot=ALL_SLOTS):
        self._enqueue(FLUSH, slot, b"")

    # -------------------------------------------------------------- audio
    def push(self, data, slot=0):
        """Queue one frame for `slot`. False means "not now" -- retry it.

        A full queue is not a dropped frame: the streamer holds the frame and
        comes back for it, which is the same contract it has with a spent
        OpenAL buffer pool.
        """
        data = bytes(data or b"")
        if not data:
            return True
        process = self.process
        if process is None or not self.alive():
            # A child that has exited takes nothing more; the caller falls back
            # to the game's own device rather than queueing into a dead pipe.
            self._dead = True
            return False
        with self._lock:
            if self._dead or self._pending + len(data) > MAX_PENDING_BYTES:
                return False
            self._pending += len(data)
        if not self._enqueue(AUDIO, slot, data):
            with self._lock:
                self._pending = max(0, self._pending - len(data))
            return False
        return True

    def pending_bytes(self):
        with self._lock:
            return self._pending

    def pending_frames(self):
        return self.pending_bytes() // FRAME_BYTES

    # ------------------------------------------------------------ writing
    def _enqueue(self, kind, slot, payload):
        if self.process is None:
            return False
        frame = HEADER.pack(int(kind), int(slot), len(payload)) + payload
        self._queue.put((frame, len(payload)))
        return True

    def _write_loop(self):
        """One thread owns the pipe, so no audio thread ever waits on it."""
        process = self.process
        if process is None:
            return
        stdin = process.stdin
        while True:
            item = self._queue.get()
            if item is None:
                return
            frame, payload_bytes = item
            try:
                stdin.write(frame)
                stdin.flush()
            except (BrokenPipeError, OSError, ValueError):
                self.write_failures += 1
                self._dead = True
                return
            finally:
                if payload_bytes:
                    with self._lock:
                        self._pending = max(0, self._pending - payload_bytes)
