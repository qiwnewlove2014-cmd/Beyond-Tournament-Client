"""A line-in instrument has to arrive sounding like itself, and without holes.

Two complaints, one place each.

A guitar heard on the voice channel sounded thin and bright instead of like a
guitar. That is Opus's ``voip`` mode -- its own documentation says it
"enhances the input signal by high-pass filtering and emphasizing formants and
harmonics" and that "even at high bitrates the output may sound different from
the input" -- and the instrument's own encoder was created with it, while the
music bot's live relay next door already asked for ``audio``. The same guitar
therefore sounded different depending on which leg carried it.

And a guitar routed to a PA speaker cut in and out. ``queue_and_delay_frame``
reclaimed a speaker's buffers at the top of every frame by reading
``buffers_processed``; on a source that is not playing, OpenAL counts **every**
buffer it holds as processed, so the frames a hitch had left waiting were
handed back as "finished" and never heard. The cinema room's own legs already
refuse to read a stopped source for exactly this reason (``bank.reclaim``,
``speech._publish``); the PA path did not.

Measured on a hitchy main thread (``tools/instrument_pa_continuity_sim.py``):
a continuous 20 ms stream loses frames and leaves dropouts before these two
changes, and plays its full 10 s afterwards.
"""

import unittest
from types import SimpleNamespace
from unittest import mock

import cyal
import numpy as np

from libs import instrument_input, voice_chat


class _FakeBuffer:
    def __init__(self):
        self.data = None

    def set_data(self, data, **_kwargs):
        self.data = bytes(data)


class _FakeSource:
    """One OpenAL source, with the one rule that matters here.

    A STOPPED source reports every buffer it holds as processed -- played or
    not -- because OpenAL counts a buffer as done the moment its source is not
    playing it. That is the trap: reclaiming a quiet source throws away audio
    nobody heard.
    """

    def __init__(self):
        self.state = cyal.SourceState.INITIAL
        self.gain = 1.0
        self.queued = []
        self.processed = 0
        self.play_calls = 0
        self.rewind_calls = 0

    @property
    def buffers_queued(self):
        return len(self.queued)

    @property
    def buffers_processed(self):
        if self.state == cyal.SourceState.STOPPED:
            return len(self.queued)
        return self.processed

    def unqueue_buffers(self, max=None):
        limit = self.buffers_processed if max is None else int(max)
        taken = self.queued[:limit]
        del self.queued[:len(taken)]
        self.processed = 0
        return taken

    def queue_buffers(self, buf):
        self.queued.append(buf)

    def play(self):
        self.play_calls += 1
        self.state = cyal.SourceState.PLAYING

    def stop(self):
        self.state = cyal.SourceState.STOPPED

    def rewind(self):
        self.rewind_calls += 1
        self.state = cyal.SourceState.INITIAL
        self.processed = 0


def _gameplay(sources, key):
    return SimpleNamespace(
        concert_spectator_mode=False,
        camera=SimpleNamespace(
            focus_object=SimpleNamespace(x=0.0, y=0.0, z=0.0)
        ),
        map=SimpleNamespace(minz=0.0),
        player=SimpleNamespace(id="player-1", name="player-1"),
        game=SimpleNamespace(
            audio_mngr=SimpleNamespace(
                context=SimpleNamespace(gen_buffer=_FakeBuffer),
                cinema_speech=None,
            )
        ),
        megaphone=SimpleNamespace(
            fading_sources=[],
            speaker_data=[],
            get_megaphone_player_sources=lambda _key: list(sources),
            player_sources={key: {"currents_vol": [1.0], "targets_vol": [1.0]}},
        ),
    )


class _MegaphoneStates:
    """Reset the PA's module-level delay/tail bookkeeping between tests."""

    def setUp(self):
        voice_chat._speaker_last_calc_time = {}
        voice_chat._speaker_current_delays = {}
        voice_chat._speaker_initial_delays = {}
        voice_chat._speaker_delay_cache_expires = {}
        voice_chat._last_tail_sample = {}
        voice_chat._just_padded = {}
        self.pool = []
        self.pool_patch = mock.patch.object(voice_chat, "_shared_buffer_pool", self.pool)
        self.pool_patch.start()
        self.addCleanup(self.pool_patch.stop)


class TheStreamAfterAHitch(_MegaphoneStates, unittest.TestCase):
    """A speaker that stopped must not lose the audio it was still holding."""

    FRAME = b"\x10\x00" * 960
    FRAMES = [bytes([value, 0]) * 960 for value in range(1, 9)]

    def feed(self, gameplay, source, frame, real_prebuffer_frames=3):
        voice_chat.queue_and_delay_frame(
            gameplay, "player-1:guitar", [source], frame,
            margin_frames=0,
            real_prebuffer_frames=real_prebuffer_frames,
            ignore_speaker_delay=True,
        )

    def test_frames_waiting_on_a_stopped_speaker_are_not_reclaimed_as_played(self):
        source = _FakeSource()
        gameplay = _gameplay([source], "player-1:guitar")
        for frame in self.FRAMES[:3]:
            self.feed(gameplay, source, frame)
        self.assertEqual(source.state, cyal.SourceState.PLAYING)

        # The driver went quiet mid-stream with three frames still queued, then
        # the next frames arrive: the unheard ones may be dropped (they cannot be
        # played late), but the NEW ones must all reach the speaker.
        source.stop()
        for frame in self.FRAMES[3:]:
            self.feed(gameplay, source, frame)

        # The head of a re-started source is faded in (a de-click ramp), so the
        # frame is recognised by its body -- the part a fade never touches.
        queued_bodies = [buffer.data[-1000:] for buffer in source.queued]
        for frame in self.FRAMES[3:]:
            self.assertIn(
                frame[-1000:], queued_bodies,
                f"frame {frame[0]} was thrown away instead of queued",
            )

    def test_a_live_instrument_monitor_starts_on_real_frames(self):
        source = _FakeSource()
        gameplay = _gameplay([source], "player-1:guitar")
        with mock.patch.object(voice_chat, "queue_and_delay_frame") as stagger:
            voice_chat._feed_local_megaphone_main(
                gameplay, self.FRAME, producer="guitar"
            )
        # The song's monitor already starts on three real frames; a guitar is a
        # continuous stream of the same shape, and the legacy "one frame + trailing
        # silence" start was the order the music producer was moved off because it
        # was heard as a repeating stutter.
        stagger.assert_called_once_with(
            gameplay, "player-1:guitar", [source], self.FRAME,
            margin_frames=0,
            real_prebuffer_frames=3,
            ignore_speaker_delay=True,
        )

    def test_the_microphone_keeps_its_legacy_start(self):
        source = _FakeSource()
        gameplay = _gameplay([source], "player-1:mic")
        with mock.patch.object(voice_chat, "queue_and_delay_frame") as stagger:
            voice_chat._feed_local_megaphone_main(
                gameplay, self.FRAME, producer="mic"
            )
        # 10 ms chunks every 10 ms: the first syllable matters more than a frame
        # of slack, and it never has to wait for a second chunk to begin.
        self.assertIsNone(stagger.call_args.kwargs["real_prebuffer_frames"])

    def test_a_stopped_speaker_is_left_alone_by_the_reclaim(self):
        source = _FakeSource()
        source.state = cyal.SourceState.STOPPED
        source.queued = [_FakeBuffer() for _ in range(3)]
        voice_chat._reclaim_source_buffers(source)
        self.assertEqual(len(source.queued), 3,
                         "a stopped source's queue is audio nobody heard")

    def test_teardown_may_drain_a_stopped_speaker(self):
        source = _FakeSource()
        source.state = cyal.SourceState.STOPPED
        source.queued = [_FakeBuffer() for _ in range(3)]
        voice_chat._reclaim_source_buffers(source, drain_stopped=True)
        self.assertEqual(len(source.queued), 0)


class TheInstrumentEncoder(unittest.TestCase):
    """The instrument must go out as music, not as a speech call."""

    def build(self, **kwargs):
        with mock.patch.object(voice_chat, "OpusEncoder", _RecordingEncoder), \
                mock.patch.object(voice_chat, "OpusDecoder", _RecordingDecoder), \
                mock.patch.object(voice_chat.voice_chat_compression, "start",
                                  lambda _self: None):
            worker = voice_chat.voice_chat_compression(None, **kwargs)
        return worker

    def test_the_instrument_asks_opus_for_audio(self):
        worker = self.build(channel=20, max_pending_frames=2, application="audio")
        self.assertEqual(worker.encoder.application, "audio")

    def test_the_voice_path_keeps_the_speech_model(self):
        self.assertEqual(self.build().encoder.application, "voip")

    def test_the_guitar_voice_leg_is_created_as_music(self):
        captured = {}

        def fake_compression(game, channel, **kwargs):
            captured["channel"] = channel
            captured.update(kwargs)

            class _Sink:
                def put(self, _value):
                    pass

            return _Sink()

        session = instrument_input.InstrumentInput.__new__(
            instrument_input.InstrumentInput)
        session.game = SimpleNamespace()
        session.running = True
        session.recording = True
        session._voice_lock = mock.MagicMock()
        session._guitar_voice = None
        with mock.patch.object(voice_chat, "voice_chat_compression",
                               side_effect=fake_compression):
            session._feed_guitar_voice(b"\x10\x00" * 960)

        self.assertEqual(captured.get("application"), "audio",
                         "the instrument's own leg must not be a speech call")
        self.assertEqual(captured.get("max_pending_frames"), 2)


class TheMeasuredTimbre(unittest.TestCase):
    """Measured, not remembered: the codec's own low end on a guitar chord.

    Opus's ``voip`` mode is speech-tuned; the same chord through ``audio`` keeps
    the body of the instrument. The numbers are the codec's, not a mock's.
    """

    def chord(self, seconds=1.5):
        rate = 48000
        time = np.arange(int(rate * seconds)) / rate
        signal = np.zeros_like(time)
        for root in (82.41, 123.47, 164.81):          # E2, B2, E3
            for harmonic in range(1, 9):
                signal += (0.25 / harmonic) * np.sin(
                    2 * np.pi * root * harmonic * time)
        return (signal / np.max(np.abs(signal)) * 0.5 * 32767).astype(np.int16)

    @staticmethod
    def low_share(samples):
        window = np.abs(np.fft.rfft(samples * np.hanning(len(samples))))
        freqs = np.fft.rfftfreq(len(samples), 1 / 48000)
        total = max(1e-9, window.sum())
        return float(window[(freqs >= 20) & (freqs < 300)].sum() / total * 100)

    def roundtrip(self, chord, application):
        try:
            from pyogg import OpusDecoder, OpusEncoder
        except ImportError:                       # pragma: no cover - shipped dep
            self.skipTest("pyogg is not installed")
        encoder = OpusEncoder()
        encoder.set_application(application)
        encoder.set_channels(1)
        encoder.set_sampling_frequency(48000)
        decoder = OpusDecoder()
        decoder.set_channels(1)
        decoder.set_sampling_frequency(48000)
        packets = [encoder.encode(chord[i:i + 960].tobytes())
                   for i in range(0, len(chord) - 960, 960)]
        pcm = b"".join(bytes(decoder.decode(bytearray(packet)))
                       for packet in packets)
        return np.frombuffer(pcm, dtype=np.int16).astype(np.float32)

    def test_audio_keeps_the_body_that_voip_takes_away(self):
        chord = self.chord()
        source = self.low_share(chord[:len(chord) - 960].astype(np.float32))
        voice = self.low_share(self.roundtrip(chord, "voip")[960 * 10:])
        music = self.low_share(self.roundtrip(chord, "audio")[960 * 10:])

        self.assertGreater(
            music, voice + 2.0,
            f"audio mode must keep more low end than voip (audio {music:.1f}% "
            f"vs voip {voice:.1f}% of the source {source:.1f}%)")
        self.assertGreater(
            music, source * 0.90,
            f"audio mode must stay close to the instrument's own balance "
            f"({music:.1f}% of {source:.1f}%)")


class _RecordingEncoder:
    def __init__(self):
        self.application = None

    def set_application(self, application):
        self.application = application

    def set_channels(self, _channels):
        pass

    def set_sampling_frequency(self, _rate):
        pass


class _RecordingDecoder(_RecordingEncoder):
    pass


if __name__ == "__main__":
    unittest.main()
