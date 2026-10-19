"""Does a live instrument stay a continuous stream when it goes out the PA?

A line-in instrument is not speech: it arrives as a 20 ms frame every 20 ms,
for as long as the player plays, and it has no pause to hide a hole in. This
drives the *real* ``voice_chat.queue_and_delay_frame`` and
``_queue_packet_to_source`` with the frames ``InstrumentInput`` emits, against a
source that plays them in real time (an OpenAL source consumes a buffer the
moment its 20 ms are up, and counts a buffer as processed again the instant it
has nothing left to play) -- and reports what the ear would actually hear:

* **real** frames: 20 ms of the instrument, the only frames that carry audio;
* **silence** frames: what the PA's stagger/recovery logic inserts;
* **gaps**: a run of silence between two real frames, heard as the stutter a
  player reports as "the sound drops in and out" (and, when a pad lands on
  every frame of a one-deep queue, as "stretched");
* **drift**: how far the last real frame ends after the wall clock, i.e. how
  late the stream has become.

The frames do not reach the speakers on the capture clock: ``InstrumentInput``
emits on its worker and ``_feed_local_megaphone_direct`` defers the OpenAL work
to the main thread, which drains that inbox once per rendered frame. So the
arrival pattern is quantised to 16.7 ms (60 fps) and a frame can be two thirds
of a frame late -- the condition a one-buffer-deep speaker queue has to survive.

Run from ``client/``:

    python tools/instrument_pa_continuity_sim.py

The numbers it prints are the report; ``tests/test_instrument_pa_continuity.py``
pins the rule they come from.
"""

import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cyal  # noqa: E402  (after the path fix, like every client tool)

from libs import voice_chat  # noqa: E402

SAMPLE_RATE = 48000
FRAME_SAMPLES = 960                    # 20 ms, what InstrumentInput._emit_frame sends
FRAME_MS = FRAME_SAMPLES / SAMPLE_RATE * 1000.0
FRAME_BYTES = FRAME_SAMPLES * 2


class _Buffer:
    def __init__(self, data=b""):
        self.data = bytes(data)

    def set_data(self, data, **_kwargs):
        self.data = bytes(data)


class _ClockSource:
    """One PA speaker source, playing whatever it is given in real time.

    Mirrors the parts of OpenAL the queue path asks about: ``state``,
    ``buffers_queued``, ``buffers_processed`` and ``unqueue_buffers``. A
    finished buffer stays *queued* until it is reclaimed -- and a stopped source
    reports every buffer it holds as processed, which is the trap that makes
    reclaiming a quiet source throw away audio nobody has heard.
    """

    def __init__(self):
        self.state = cyal.SourceState.INITIAL
        self.gain = 1.0
        self.queue = []          # data of the buffers still attached to the source
        self.finished = 0        # buffers played through, not yet unqueued
        self.now = 0.0
        self.play_head = 0.0     # virtual play position, ms
        self.played = []         # (start_ms, duration_ms, is_silence)
        self.play_calls = 0
        self.lost = 0            # frames reclaimed before they were ever heard

    # -- OpenAL surface ---------------------------------------------------
    @property
    def buffers_queued(self):
        return len(self.queue) + self.finished

    @property
    def buffers_processed(self):
        # A stopped source reports EVERY buffer it holds as processed: OpenAL
        # counts a buffer as done the moment its source is not playing it.
        if self.state == cyal.SourceState.STOPPED:
            return len(self.queue) + self.finished
        return self.finished

    def unqueue_buffers(self, max=None):
        """Hand back what OpenAL says is finished -- which, on a stopped source, is
        *everything it holds*, played or not. That is the trap this sim exists for.
        """
        limit = self.buffers_processed if max is None else int(max)
        taken = []
        while self.finished and len(taken) < limit:
            self.finished -= 1
            taken.append(_Buffer())
        while self.queue and len(taken) < limit:
            data = self.queue.pop(0)
            self.lost += 1
            taken.append(_Buffer(data))
        return taken

    def queue_buffers(self, buf):
        self.queue.append(buf.data)

    def play(self):
        self.play_calls += 1
        self.state = cyal.SourceState.PLAYING
        self.play_head = max(self.play_head, self.now)

    def stop(self):
        self.state = cyal.SourceState.STOPPED

    def rewind(self):
        # Only ever called on an empty queue (the price of replaying the past is
        # not paid here); the play head may not move backwards, or the next
        # buffer would be heard twice.
        self.state = cyal.SourceState.INITIAL
        self.play_head = max(self.play_head, self.now)

    # -- the driver's own clock ------------------------------------------
    def advance(self, now):
        self.now = now
        if self.state != cyal.SourceState.PLAYING:
            return
        while self.queue:
            data = self.queue[0]
            duration = len(data) / 2 / SAMPLE_RATE * 1000.0
            if self.play_head + duration > now + 1e-9:
                return
            self.queue.pop(0)
            self.finished += 1
            self.played.append((self.play_head, duration, not any(data)))
            self.play_head += duration
        # Everything queued has played: OpenAL stops on its own.
        self.state = cyal.SourceState.STOPPED


def _gameplay(speaker_x=0.0, delay=0.0, source=None, key=None):
    """The minimum a PA speaker source and its local monitor feed need."""
    megaphone = types.SimpleNamespace(
        fading_sources=[],
        speaker_data=[{"position": (speaker_x, 0.0, 0.0), "delay": delay}],
        get_megaphone_player_sources=lambda _key: [source],
        player_sources={key: {"currents_vol": [1.0], "targets_vol": [1.0]}},
    )
    return types.SimpleNamespace(
        concert_spectator_mode=False,
        camera=types.SimpleNamespace(
            focus_object=types.SimpleNamespace(x=0.0, y=0.0, z=0.0)
        ),
        map=types.SimpleNamespace(minz=0.0),
        player=types.SimpleNamespace(id="player-1", name="player-1"),
        game=types.SimpleNamespace(
            audio_mngr=types.SimpleNamespace(
                context=types.SimpleNamespace(gen_buffer=_Buffer),
                # No cinema room: this run measures the PA leg on its own.
                cinema_speech=None,
            )
        ),
        megaphone=megaphone,
    )


def run(seconds=10.0, distance=12.0, producer="guitar",
        render_hz=60.0, first_frame_late_ms=0.0, legacy_start=False,
        spike_every_ms=0.0, spike_ms=0.0, jitter_ms=0.0, legacy_reclaim=False):
    """Feed ``seconds`` of a live instrument to one PA speaker.

    The frames go through ``_feed_local_megaphone_main`` -- the one function the
    audio inbox runs -- so the prebuffer (or the legacy silence cushion) is
    whatever the producer's own code path chooses. ``render_hz`` is the main
    thread that drains that inbox; 0 feeds the frame the moment it is captured.

    ``spike_every_ms``/``spike_ms`` add the render hitch this game really has
    (a map streaming a chunk, a GC pass, a window switch): the inbox is not
    drained for that long, and the frames that were waiting arrive together
    afterwards. ``jitter_ms`` is the ordinary frame-time wobble.
    """
    voice_chat._speaker_last_calc_time = {}
    voice_chat._speaker_current_delays = {}
    voice_chat._speaker_initial_delays = {}
    voice_chat._speaker_delay_cache_expires = {}
    voice_chat._last_tail_sample = {}
    voice_chat._just_padded = {}
    voice_chat._shared_buffer_pool[:] = []

    source = _ClockSource()
    key = f"player-1:{producer}"
    gameplay = _gameplay(speaker_x=distance, source=source, key=key)

    frame = b"\x10\x00" * FRAME_SAMPLES

    tick = 1000.0 / render_hz if render_hz else 0.0
    inbox = []
    steps = int(seconds * 1000 / FRAME_MS)
    events = []
    for step in range(steps):
        events.append((step * FRAME_MS, "capture", step))
    if tick:
        now = first_frame_late_ms
        step = 0
        wobble = 0.0
        while now < seconds * 1000:
            events.append((now, "render", step))
            step += 1
            wobble = jitter_ms if step % 2 else 0.0
            now += tick + wobble
            if spike_every_ms and step and (step * tick) % spike_every_ms < tick:
                now += spike_ms
    events.sort()

    restore = None
    if legacy_reclaim:
        # The reclaim this sim was written for: it trusted `buffers_processed` on a
        # quiet source, which OpenAL fills with the whole queue.
        def _naive(src):
            try:
                while src.buffers_processed > 0:
                    voice_chat._recycle_buffers(src.unqueue_buffers())
            except Exception:
                pass
        restore = voice_chat._reclaim_source_buffers
        voice_chat._reclaim_source_buffers = _naive

    for now, kind, step in events:
        source.advance(min(now, steps * FRAME_MS))
        if kind == "capture":
            inbox.append(frame)          # defer_audio: the worker hands it over
            if tick:
                continue
            # No main thread in this run: the frame goes straight to OpenAL.
        while inbox:                     # AudioManager.loop() drains the inbox
            frame_now = inbox.pop(0)
            if legacy_start:
                # The start order the guitar used before this fix: no real
                # prebuffer, so the legacy trailing-silence cushion applies.
                voice_chat.queue_and_delay_frame(
                    gameplay, key, [source], frame_now,
                    margin_frames=0,
                    real_prebuffer_frames=None,
                    ignore_speaker_delay=True,
                )
            else:
                voice_chat._feed_local_megaphone_main(
                    gameplay, frame_now, producer=producer)
        source.advance(now)
    if restore is not None:
        voice_chat._reclaim_source_buffers = restore
    source.advance(source.play_head + 1000.0)  # let it drain

    real = [entry for entry in source.played if not entry[2]]
    silence = [entry for entry in source.played if entry[2]]
    holes = _holes(source.played)
    real_ms = sum(duration for _start, duration, _sil in real)
    silence_ms = sum(duration for _start, duration, _sil in silence)
    expected_ms = steps * FRAME_MS
    drift_ms = (real[-1][0] + real[-1][1]) - expected_ms if real else 0.0
    return {
        "producer": producer,
        "expected_ms": expected_ms,
        "real_ms": real_ms,
        "silence_ms": silence_ms,
        "real_frames": len(real),
        "silence_frames": len(silence),
        "holes": holes,
        "hole_ms": sum(holes),
        "longest_hole_ms": max(holes, default=0.0),
        "drift_ms": drift_ms,
        "lost_frames": source.lost,
        "pending_frames": len(source.queue),
        "continuity": real_ms / expected_ms if expected_ms else 0.0,
    }


def _holes(played):
    """Dead air between two real frames, in ms (a dropout, however it arises).

    A silence *buffer* is only one way to lose time: a speaker that stopped and
    starts again also leaves a hole, and the ear hears both the same way.
    """
    holes = []
    previous_end = None
    for start, duration, silence in played:
        if silence:
            continue
        if previous_end is not None:
            hole = start - previous_end
            if hole > 1.0:
                holes.append(round(hole, 1))
        previous_end = start + duration
    return holes


def describe(result):
    return (
        f"{result['producer']:7s} "
        f"real={result['real_ms']:7.1f}ms of {result['expected_ms']:.0f}ms "
        f"({result['continuity'] * 100:5.1f}%)  "
        f"silence={result['silence_ms']:6.1f}ms  "
        f"holes={len(result['holes']):3d} totalling {result['hole_ms']:7.1f}ms "
        f"(longest {result['longest_hole_ms']:5.0f}ms)  "
        f"lost={result['lost_frames']:2d}  still queued={result['pending_frames']:2d}  "
        f"drift={result['drift_ms']:7.1f}ms"
    )


def main():
    print("A live 20 ms instrument stream into one PA speaker, 10 s, 12 m away")
    print("the main thread drains the deferred inbox at 60 fps")
    print("-" * 100)
    for label, kwargs in (
        ("GUITAR before the fix: no real prebuffer, legacy trailing-silence start",
         dict(producer="guitar", legacy_start=True)),
        ("GUITAR now: three real frames, like the music monitor",
         dict(producer="guitar")),
        ("guitar, 120 fps main thread",
         dict(producer="guitar", render_hz=120.0)),
        ("MIC (10 ms chunks, legacy start kept on purpose)",
         dict(producer="mic", legacy_start=True)),
    ):
        result = run(**kwargs)
        print(f"{label}\n  {describe(result)}")
    print("-" * 100)
    print("the same, on a main thread with the hitches a real frame has "
          "(a 60 ms spike every 1.5 s, 6 ms of wobble)")
    print("-" * 100)
    for label, kwargs in (
        ("GUITAR before the fix: legacy start order AND the old reclaim",
         dict(producer="guitar", legacy_start=True, legacy_reclaim=True)),
        ("GUITAR now", dict(producer="guitar")),
        ("music monitor before the reclaim fix",
         dict(producer="music", legacy_reclaim=True)),
        ("MIC (legacy start kept)", dict(producer="mic", legacy_start=True)),
    ):
        result = run(spike_every_ms=1500.0, spike_ms=60.0, jitter_ms=6.0, **kwargs)
        print(f"{label}\n  {describe(result)}")
    print("-" * 100)
    print("real% is the share of wall time that carried the instrument (above 100% "
          "means a frame was played twice, which is a stutter too); silence frames are "
          "the padded ones, and every hole is dead air between two real frames.")


if __name__ == "__main__":
    main()
