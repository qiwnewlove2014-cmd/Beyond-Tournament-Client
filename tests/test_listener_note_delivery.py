"""Every note a performer plays reaches a listener -- source build and pack.

A performer hears their own notes unconditionally: local prediction, no wait,
no queue.  A listener's note is a different journey -- a queue the network
thread fills, a bounded wait for the sample, then the instrument -- so a loss
here is silent on the performing machine and audible only to everybody else.
The report is "I play it fine and you all hear it wrong", which is exactly what
these tests refuse to let happen.

The bounded wait is the part with a real limit: ``DEFERRED_NOTE_TIMEOUT_S``
(0.6 s) for a note whose sample is still decoding, ``MAX_DEFERRED_NOTES`` (64)
held at once, ``MAX_PENDING_NOTES_PER_UPDATE`` (64) drained per frame.  A
passage faster than the machine can decode -- or a note outside the shipped
range, which never becomes ready at all -- is where notes have to be counted
rather than assumed.

The real path is driven end to end: ``PianoAudio.enqueue_remote_note`` (the
network thread's entry point) through ``_process_pending_notes`` and the shared
``InstrumentSampleCache``, with the shipped sample names, in both ways the game
is run: from source (a real ``data/piano/`` folder) and compiled (the
``sounds.dat`` pack, decoded lazily through the VFS).
"""

import importlib.util
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

CLIENT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CLIENT))

from libs import consts  # noqa: E402
from libs import vfs  # noqa: E402
from libs.audio_manager import AudioManager  # noqa: E402
from libs.instrument_samples import InstrumentSampleCache  # noqa: E402
from libs.piano import PianoAudio  # noqa: E402

spec_pack = importlib.util.spec_from_file_location(
    "fixture_pack_data", CLIENT / "tools/pack_data.py")
pack_data = importlib.util.module_from_spec(spec_pack)
spec_pack.loader.exec_module(pack_data)

# The shipped set exactly: seven octaves of C..B with flats, plus the B0/C8
# edges -- ``data/piano`` holds 85 files (no Gb7), and the Server spells a sharp
# note as its flat one before it is sent, so these are the names a listener ever
# sees.
NOTE_NAMES = ("C", "Db", "D", "Eb", "E", "F", "Gb", "G", "Ab", "A", "Bb", "B")
SHIPPED = [f"{name}{octave}" for octave in range(1, 8) for name in NOTE_NAMES
           if f"{name}{octave}" != "Gb7"]
SHIPPED += ["B0", "C8"]
# A fast passage inside the eager octaves: 8 notes a second for three seconds.
PASSAGE = [note for note in SHIPPED if note[-1] in "345"][:24]
CHORD = ["C4", "Eb4", "G4", "Bb4"]
# The rate the Server's own anti-flood bucket allows a guitar (30/s) is far
# above any hand; this is the density a fast piano passage really reaches.
PASSAGE_INTERVAL_S = 0.125


class _Buffer:
    def set_data(self, *args, **kwargs):
        return None


class _Batch:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class _Context:
    def batch(self):
        return _Batch()


class _Audio:
    """Enough AudioManager for PianoAudio, with no device anywhere."""

    def __init__(self):
        self.position = (0.0, 0.0, 0.0)
        self.volume_categories = {"jukebox": [100], "music": [100],
                                  "miscelaneous": [100], "master": [100]}
        self.direct = []
        self.filter = []
        self.sends = []
        self.played = []
        self.context = _Context()
        self.efx = SimpleNamespace(send=lambda *a, **k: None)
        self.buffers = {}
        self._preloaded_buffers = {}
        self.instrument_samples = None

    def gen_filter(self, kind, *params):
        return ("filter", kind, params)

    def gen_effect(self, *args, **kwargs):
        return ("effect", args, kwargs)

    def release_effect_slot(self, *args, **kwargs):
        return None

    def play_unbound_stereo_spatial(self, path, x, y, z, **kwargs):
        self.direct.append((path, (x, y, z), kwargs))
        return SimpleNamespace(source=SimpleNamespace(set=lambda *a, **k: None,
                                                     stop=lambda: None))


class Listener:
    """A listener's piano, over the real sample cache."""

    def __init__(self, real_play=False):
        self.audio = _Audio()
        self.audio.instrument_samples = InstrumentSampleCache(
            AudioManager._resolve_instrument_sample_path,
            AudioManager._decode_instrument_sample,
            lambda data, channels, rate: _Buffer(),
        )
        self.piano = PianoAudio(self.audio)
        self.piano.gameplay = SimpleNamespace(
            player=SimpleNamespace(x=0.0, y=0.0, z=0.0), map=None)
        self.heard = []
        self.stopped = []
        self.sent = []
        # Playback is counted rather than sounded -- these tests ask what the
        # pipeline delivered, and a device is not part of that question. A
        # ``real_play`` listener calls the instrument for real as well, so one
        # test can check the counters are not the only evidence.
        self.piano.stop_note = lambda peer_id, note: self.stopped.append(note)
        if real_play:
            real_play_note = self.piano.play_note

            def _recorded(**kwargs):
                self.heard.append(kwargs["note_name"])
                return real_play_note(**kwargs)

            self.piano.play_note = _recorded
        else:
            self.piano.play_note = lambda **kwargs: self.heard.append(
                kwargs["note_name"])
        self.max_deferred = 0

    # -- the game's own two entry points ---------------------------------
    def send(self, note, peer="kan"):
        self.sent.append(note)
        self.piano.enqueue_remote_note(
            {"peer_id": peer, "note": note, "x": 1.0, "y": 2.0, "z": 0.0})

    def release(self, note, peer="kan"):
        self.piano.enqueue_remote_stop({"peer_id": peer, "note": note})

    # -- the frame ------------------------------------------------------
    def pump(self, seconds, step=0.016):
        end = time.monotonic() + seconds
        while True:
            self.audio.instrument_samples.pump(
                max_uploads=4, budget_seconds=0.002,
                max_bytes=AudioManager.INSTRUMENT_UPLOAD_BYTES_PER_FRAME)
            self.piano.update()
            self.max_deferred = max(self.max_deferred,
                                    len(self.piano._deferred_notes))
            if time.monotonic() >= end:
                return
            time.sleep(step)

    def warm(self, notes, deadline_s=60.0):
        """Ask for every sample and pump until none of them is still loading."""
        paths = [self.sample_path(note) for note in notes]
        self.audio.instrument_samples.request(paths)
        started = time.perf_counter()
        while time.perf_counter() - started < deadline_s:
            self.audio.instrument_samples.pump(max_uploads=4,
                                               budget_seconds=0.01)
            if all(self.audio.instrument_samples.status([path]) == "ready"
                   for path in paths):
                return True
            time.sleep(0.01)
        return False

    def play_passage(self, notes, interval_s=PASSAGE_INTERVAL_S, tail_s=0.6):
        for note in notes:
            self.send(note)
            self.pump(interval_s)
        self.pump(tail_s)

    # -- what came out --------------------------------------------------
    @staticmethod
    def sample_path(note):
        return f"piano/Piano.mf.{note}.ogg"

    def missing(self):
        return [note for note in self.sent if note not in self.heard]

    def close(self):
        self.audio.instrument_samples.close()


class AListenerHearsEveryNoteTests(unittest.TestCase):
    """From source: a real ``data/piano/`` folder decides."""

    def setUp(self):
        self.listener = Listener()
        self.addCleanup(self.listener.close)

    def test_the_whole_shipped_range_arrives_on_one_burst(self):
        """85 notes at once -- faster than any two hands -- all of them out."""
        self.assertTrue(self.listener.warm(SHIPPED))
        for note in SHIPPED:
            self.listener.send(note)
        self.listener.pump(1.2)
        self.assertEqual(self.listener.missing(), [])
        self.assertEqual(len(self.listener.heard), len(SHIPPED))
        # Nothing was left waiting on a sample that never came.
        self.assertEqual(self.listener.piano._deferred_notes, [])

    def test_a_fast_passage_arrives_whole_on_a_warm_listener(self):
        self.assertTrue(self.listener.warm(PASSAGE))
        self.listener.play_passage(PASSAGE)
        self.assertEqual(self.listener.missing(), [])
        self.assertEqual(self.listener.max_deferred, 0)

    def test_a_cold_listener_still_hears_the_passage(self):
        """No warm-up at all: one decode per note, and 125 ms between notes."""
        self.listener.play_passage(PASSAGE)
        self.assertEqual(self.listener.missing(), [])

    def test_one_cold_note_arrives_within_the_held_window(self):
        started = time.perf_counter()
        self.listener.send("G6")
        self.listener.pump(0.5)
        self.assertEqual(self.listener.heard, ["G6"])
        self.assertLess(time.perf_counter() - started,
                        PianoAudio.DEFERRED_NOTE_TIMEOUT_S)

    def test_every_chord_tone_sounds_and_every_release_stops_it(self):
        self.assertTrue(self.listener.warm(CHORD))
        for note in CHORD:
            self.listener.send(note)
        self.listener.pump(0.2)
        self.assertEqual(self.listener.missing(), [])
        for note in CHORD:
            self.listener.release(note)
        self.listener.pump(0.2)
        self.assertEqual(self.listener.stopped, CHORD)

    def test_a_release_cancels_a_note_still_waiting_for_its_sample(self):
        """Held notes are retired by their own key release, so none fires late."""
        self.listener.send("B0")
        self.listener.release("B0")
        self.listener.pump(0.8)
        self.assertEqual(self.listener.heard, [])
        self.assertEqual(self.listener.piano._deferred_notes, [])

    def test_a_note_whose_sample_never_arrives_is_dropped_not_delayed(self):
        """A name with no file behind it: silence, never a late strike."""
        self.listener.send("Hz9")
        self.listener.pump(0.4)
        self.assertEqual(self.listener.heard, [])
        self.assertEqual(self.listener.piano._deferred_notes, [])

    def test_a_real_play_call_sounds_every_delivered_note(self):
        """The counters above are not the only evidence: real playback too."""
        listener = Listener(real_play=True)
        self.addCleanup(listener.close)
        self.assertTrue(listener.warm(CHORD))
        for note in CHORD:
            listener.send(note)
        listener.pump(0.3)
        self.assertEqual(sorted(listener.heard), sorted(CHORD))
        paths = [entry[0] for entry in listener.audio.direct]
        for note in CHORD:
            self.assertTrue(any(path.endswith(f"{note}.ogg") for path in paths),
                            f"{note} never reached the speaker")


class AListenerHearsEveryNotePackedTests(unittest.TestCase):
    """A compiled run: the same notes through the ``sounds.dat`` pack."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="bt-note-pack-"))
        self.old_cwd = os.getcwd()
        self.saved_prepend = consts.SOUNDPREPEND
        self.saved_sprepend = consts.SOUNDSPREPEND
        self.listener = None
        # Registered before the mount: a pack left mounted, or a moved cwd,
        # would decide what the next test file in the same process reads.
        self.addCleanup(self._unmount)
        assets = self.root / "assets"
        shutil.copytree(CLIENT / "data" / "piano", assets / "piano")
        pack_data.pack_data(assets, self.root / "sounds.dat",
                            "official.example", 13000)
        os.chdir(self.root)
        vfs._reset_for_tests()
        vfs.init_vfs()
        self.listener = Listener()

    def _unmount(self):
        if self.listener is not None:
            self.listener.close()
        vfs._reset_for_tests()
        os.chdir(self.old_cwd)
        consts.SOUNDPREPEND = self.saved_prepend
        consts.SOUNDSPREPEND = self.saved_sprepend
        shutil.rmtree(self.root, ignore_errors=True)

    def test_the_range_arrives_through_the_pack(self):
        self.assertTrue(self.listener.warm(SHIPPED))
        for note in SHIPPED:
            self.listener.send(note)
        self.listener.pump(1.2)
        self.assertEqual(self.listener.missing(), [])

    def test_a_cold_passage_arrives_through_the_pack(self):
        """The compiled build decodes slower; the held window must still fit."""
        self.listener.play_passage(PASSAGE)
        self.assertEqual(self.listener.missing(), [])


if __name__ == "__main__":
    unittest.main()
