"""Offline sample wiring checks: no OpenAL context, device or server is opened."""

import ctypes
import os
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from libs import consts, safe_vorbis
from libs.audio_manager import AudioManager
from libs.drums import DrumAudio
from libs.instrument_samples import InstrumentSampleCache
from libs.piano import PianoAudio


class InstrumentAudioIntegrationTests(unittest.TestCase):
    def test_native_decoder_uses_relative_path_for_unicode_installation(self):
        canonical = AudioManager._resolve_instrument_sample_path("piano/Piano.mf.C4.ogg")
        with patch.object(safe_vorbis, "load_vorbis_pcm") as decode:
            AudioManager._decode_instrument_sample(canonical)
        decode.assert_called_once_with(os.path.relpath(canonical), max_pcm_bytes=32 * 1024 * 1024)
        with patch("libs.audio_manager.os.path.relpath", side_effect=ValueError), patch.object(safe_vorbis, "load_vorbis_pcm") as decode:
            AudioManager._decode_instrument_sample(canonical)
        decode.assert_called_once_with(canonical, max_pcm_bytes=32 * 1024 * 1024)

    def test_source_and_compiled_vfs_paths_share_canonical_key(self):
        for root in ("data/", "C:/temporary instrument sounds/mounted/"):
            with self.subTest(root=root), patch.object(consts, "SOUNDPREPEND", root):
                expected = os.path.normcase(os.path.abspath(os.path.join(root, "piano/Piano.mf.C4.ogg")))
                for path in ("piano/Piano.mf.C4.ogg", expected, root + "piano/Piano.mf.C4.ogg"):
                    self.assertEqual(AudioManager._resolve_instrument_sample_path(path), expected)
                    self.assertTrue(AudioManager._is_prepared_instrument_sample(path))
                self.assertFalse(AudioManager._is_prepared_instrument_sample("ui/notify1.ogg"))
                self.assertFalse(AudioManager._is_prepared_instrument_sample("../outside/piano/test.ogg"))

    def test_instrument_miss_never_falls_back_to_synchronous_decoder(self):
        audio = AudioManager.__new__(AudioManager)
        audio.instrument_samples = SimpleNamespace(get=Mock(return_value=None))
        with patch.object(safe_vorbis, "load_vorbis_pcm", side_effect=AssertionError("sync decode")):
            self.assertIsNone(audio.load_buffer("piano/Piano.mf.C4.ogg", instrument=True))
            self.assertIsNone(audio.load_buffer("drums/Drums.kick.ogg", as_mono=True, instrument=True))
            self.assertEqual(PianoAudio(audio).load_stereo_split_buffers("piano/Piano.mf.C4.ogg"), (None, None))
            self.assertEqual(DrumAudio(audio).load_stereo_split_buffers("drums/Drums.kick.ogg"), (None, None))
        self.assertEqual([call.kwargs["kind"] for call in audio.instrument_samples.get.call_args_list],
                         ["stereo", "mono", "split", "split"])

    def test_generic_preview_and_map_sound_callers_keep_existing_buffer_contract(self):
        audio = AudioManager.__new__(AudioManager)
        audio.instrument_samples = SimpleNamespace(get=Mock(side_effect=AssertionError("unexpected async request")))
        buffer = Mock()
        audio.buffers = {}
        audio._preloaded_buffers = {}
        audio.context = SimpleNamespace(gen_buffer=lambda: buffer)
        decoded = SimpleNamespace(buffer=b"\x00\x00", channels=1, frequency=44100)
        with patch.object(safe_vorbis, "load_vorbis_pcm", return_value=decoded) as decode:
            self.assertIs(audio.load_buffer("piano/Piano.mf.C4.ogg"), buffer)
            decode.assert_called_once()
        buffer.set_data.assert_called_once()

    def test_each_drum_kit_requests_only_real_pad_paths_without_loading(self):
        requested = []
        audio = SimpleNamespace(instrument_samples=SimpleNamespace(request=lambda paths: requested.append(tuple(paths))))
        drums = DrumAudio(audio)
        for kit in drums.KITS:
            drums.preload(kit)
            self.assertEqual(requested[-1], tuple(path for _, path, _, _ in drums.pad_defs(kit) if path))

    def test_map_metadata_prewarms_listeners_without_entering_a_session(self):
        from libs.world_map import Map
        from libs.map import Map_parser
        requested = []
        audio = SimpleNamespace(instrument_samples=SimpleNamespace(request=lambda paths: requested.append(tuple(paths))))
        audio.piano, audio.drums = PianoAudio(audio), DrumAudio(audio)
        world = Map.__new__(Map)
        world.game = SimpleNamespace(audio_mngr=audio)
        world.destroy = Mock()
        data = dict(minx=0, maxx=10, miny=0, maxy=10, minz=0, maxz=0, elements=[
            {"type": "instrument", "data": {"instrument": "piano"}},
            {"type": "instrument", "data": {"instrument": "drumset", "kit": "diw"}},
            {"type": "instrument", "data": {"instrument": "unknown"}},
        ])
        Map_parser(world.game, world).load(data)
        self.assertEqual(len(requested), 2)
        self.assertIn("piano/Piano.mf.C3.ogg", requested[0])
        self.assertIn("piano/Piano.mf.B5.ogg", requested[0])
        self.assertEqual(len(requested[0]), 36)
        self.assertEqual(requested[1], tuple(path for _, path, _, _ in DrumAudio.pad_defs("diw") if path))
        self.assertEqual(audio.drums._active_kit, DrumAudio.DEFAULT_KIT)
        audio.drums._active_kit = "diw"
        world.spawn_instrument(instrument="Drumset")
        self.assertEqual(requested[-1], tuple(path for _, path, _, _ in DrumAudio.pad_defs("default") if path))
        self.assertEqual(audio.drums._active_kit, "diw")

    def test_audio_loop_pumps_before_instrument_events(self):
        from contextlib import nullcontext
        calls = []
        audio = AudioManager.__new__(AudioManager)
        audio.context = SimpleNamespace(batch=nullcontext)
        audio._drain_audio_inbox = lambda: calls.append("inbox")
        audio.instrument_samples = SimpleNamespace(pump=lambda **kwargs: calls.append(("pump", kwargs)))
        audio.piano = SimpleNamespace(update=lambda: calls.append("piano"))
        audio.drums = SimpleNamespace(update=lambda: calls.append("drums"))
        audio.unbound_sources = []
        audio.soundgroups = []
        audio.loop()
        self.assertEqual(calls, ["inbox", ("pump", {"max_uploads": 4, "budget_seconds": 0.002}), "piano", "drums"])

    def test_blocked_decoder_does_not_block_main_audio_loop(self):
        started, release = threading.Event(), threading.Event()
        owner = threading.get_ident()
        decode_threads, upload_threads = [], []

        def decode(path):
            decode_threads.append(threading.get_ident())
            started.set()
            if not release.wait(2):
                raise RuntimeError("test did not release decoder")
            return SimpleNamespace(buffer=b"\x01\x00\x02\x00", channels=2, frequency=44100)

        cache = InstrumentSampleCache(str, decode, lambda *args: upload_threads.append(threading.get_ident()) or object())
        try:
            cache.request(["one", "two"])
            self.assertTrue(started.wait(1))
            # Deterministic: decoder is still blocked while game frames continue.
            for _ in range(100):
                self.assertEqual(cache.pump(), 0)
                self.assertIsNone(cache.get("one"))
            self.assertEqual(upload_threads, [])
            self.assertNotEqual(decode_threads, [owner])
        finally:
            cache.close()
            release.set()
            cache._worker.join(2)  # Offline-test cleanup, never production code.

    def test_piano_start_only_schedules_session_not_bulk_loading(self):
        from libs.event_handeler import EventHandeler
        calls = []
        handler = EventHandeler.__new__(EventHandeler)
        start = Mock()
        handler.game = SimpleNamespace(put=calls.append)
        handler.gameplay = SimpleNamespace(_start_piano_session=start)
        handler.piano_start({})
        self.assertEqual(calls, [start])
        start.assert_not_called()


class SafeVorbisSizeLimitTests(unittest.TestCase):
    def fake_library(self):
        chunks = iter([b"1234", b"5678", b""])
        def read(vf, target, size, *args):
            chunk = next(chunks)
            ctypes.memmove(target, chunk, len(chunk))
            return len(chunk)
        return SimpleNamespace(
            ov_fopen=Mock(return_value=0),
            ov_info=Mock(return_value=SimpleNamespace(contents=SimpleNamespace(channels=1, rate=44100))),
            ov_read=Mock(side_effect=read), ov_clear=Mock(),
        )

    def test_limit_checked_during_decode_and_native_file_always_closed(self):
        lib = self.fake_library()
        with patch.object(safe_vorbis._vorbis, "libvorbisfile", lib):
            with self.assertRaisesRegex(safe_vorbis.SafeVorbisError, "size limit"):
                safe_vorbis.load_vorbis_pcm("fake.ogg", max_pcm_bytes=6)
        self.assertEqual(lib.ov_read.call_count, 2)
        lib.ov_clear.assert_called_once()

    def test_exact_limit_and_unlimited_existing_callers(self):
        for limit in (8, None):
            lib = self.fake_library()
            with patch.object(safe_vorbis._vorbis, "libvorbisfile", lib):
                decoded = safe_vorbis.load_vorbis_pcm("fake.ogg", max_pcm_bytes=limit)
            self.assertEqual(decoded.buffer, b"12345678")
            lib.ov_clear.assert_called_once()


if __name__ == "__main__":
    unittest.main()
