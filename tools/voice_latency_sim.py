"""How many milliseconds does live voice carry? (one-way, channel 20)

The path a sung word travels before another player's ear hears it:

    mouth -> capture device period (this machine's floor) -> 20 ms Opus frame
          -> send worker poll -> Opus encode -> one network leg (ping / 2)
          -> receive worker poll -> Opus decode -> main-thread audio inbox
          -> jitter cushion -> mixer period -> ear

Only one of those links is a choice this code makes: how deep the receive
cushion is (`_adaptive_margin_frames`, 20 ms per frame). Everything else is
the machine (device periods, probed here and in
``tools/instrument_monitor_latency_sim.py``) or the network (one leg, asked
for as an RTT).

The receive side is MEASURED, not modelled from scratch: the shipped
``voice_chat_compression.recieve2`` and ``_play_voice_frame`` are driven
against a time-modelled OpenAL source (queue + playhead + mixer period), and
the number printed is the delay between a packet's arrival and the moment the
ear gets its first sample. The main-thread audio inbox is modelled too -- it
is drained once per game frame, which is why 60 fps and 120 fps differ.

Reported per scenario:
  - first-heard: the cushion, ms from the first packet's arrival to its first
    sample (the cold-start cost every burst pays).
  - steady: the same for the frames that follow it.
  - re-pads: times a running stream was re-padded (each one is an audible
    20 ms hole plus a fresh cushion): 0 is the pass condition.

Run:  python tools/voice_latency_sim.py [--offline] [--rtt 40]
"""

import collections
import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import cyal

from libs import voice_chat as vc

RATE = 48000.0
SAMPLES_PER_MS = RATE / 1000.0

FRAME_MS = vc.VOICE_FRAME_MS
FRAME_BYTES = vc.VOICE_FRAME_BYTES
FRAME_SAMPLES = FRAME_BYTES // 2

# The machine's own two numbers: how much audio a capture endpoint hands over
# at a time, and how much the mixer consumes at a time. Both measured 480
# samples (10 ms) on this project's machines; neither is tunable downwards.
DEFAULT_DEVICE_PERIOD_MS = 10.0

# Costs that are not measured here: one Opus encode and one decode step, and
# the receive worker's own poll.
ENCODE_MS = 1.0
DECODE_MS = 1.0
RECEIVE_WORKER_POLL_MS = 2.0     # voice_chat_compression.run's own poll

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS - {name} {detail}")
    else:
        FAIL += 1
        print(f"  FAIL - {name} {detail}")


# ---------------------------------------------------------------------------
# The machine's device periods
# ---------------------------------------------------------------------------
def probe_capture_period(seconds=0.6):
    import time
    ext = cyal.CaptureExtension()
    device = ext.open_device(name=ext.default_device, sample_rate=48000,
                             format=cyal.BufferFormat.MONO16)
    device.start()
    try:
        sizes = collections.Counter()
        deadline = time.perf_counter() + seconds
        while time.perf_counter() < deadline:
            ready = device.available_samples
            if ready > 0:
                device.capture_samples(bytearray(ready * 2))
                sizes[ready] += 1
            time.sleep(0.0005)
    finally:
        device.stop()
    if not sizes:
        return None
    samples, _ = sizes.most_common(1)[0]
    return samples / SAMPLES_PER_MS


def probe_mix_period(seconds=0.8):
    import time
    device = cyal.Device(name=cyal.util.get_default_all_device_specifier())
    context = cyal.Context(device, make_current=True)
    source = context.gen_source()
    buffer = context.gen_buffer()
    buffer.set_data(bytes(48000 * 2 * 30), sample_rate=48000,
                    format=cyal.BufferFormat.MONO16)
    source.queue_buffers(buffer)
    source.play()
    try:
        time.sleep(0.3)
        previous = source.get_int("SAMPLE_OFFSET")
        steps = collections.Counter()
        deadline = time.perf_counter() + seconds
        while time.perf_counter() < deadline:
            current = source.get_int("SAMPLE_OFFSET")
            if current != previous:
                steps[current - previous] += 1
                previous = current
            time.sleep(0.0005)
    finally:
        source.stop()
    if not steps:
        return None
    samples, _ = steps.most_common(1)[0]
    return samples / SAMPLES_PER_MS


# ---------------------------------------------------------------------------
# A time-modelled OpenAL source: queue, playhead, mixer period
# ---------------------------------------------------------------------------
class _Clock:
    def __init__(self):
        self.now_ms = 0.0


def _ceil_to(value, step):
    if step <= 0 or value % step == 0:
        return value
    return step * -(-value // step)


class _SimBuffer:
    def __init__(self):
        self.data = b""

    def set_data(self, data, sample_rate=None, format=None):
        self.data = bytes(data)


class _SimSource:
    """``cyal.Source`` as the voice receive path uses it, with a real playhead.

    A buffer queued while the source is idle starts one mixer period after the
    ``play()`` that follows it; a buffer queued behind others starts where the
    one before it ends. ``buffers_processed`` counts the buffers the playhead
    has passed, which is what the receive path unqueues.

    The audio each queued buffer holds is snapshotted here: the shared buffer
    pool re-uses buffer objects, so a reference is not a record of what was in
    it when it was queued.
    """

    def __init__(self, clock, mix_period_ms):
        self.clock = clock
        self.mix_period_ms = mix_period_ms
        self.entries = []           # records still queued (see queue_buffers)
        self.history = []           # every record ever queued, in order
        self._playing = False
        self.play_calls = 0

    # -- OpenAL surface ----------------------------------------------------
    def queue_buffers(self, *buffers):
        for buffer in buffers:
            samples = len(buffer.data) // 2
            if self.entries and self.entries[-1]["start"] is not None:
                start = (self.entries[-1]["start"]
                         + self.entries[-1]["samples"] / SAMPLES_PER_MS)
            else:
                start = None
            record = {"buffer": buffer, "samples": samples,
                      "marker": buffer.data[:1], "start": start}
            self.entries.append(record)
            self.history.append(record)

    def unqueue_buffers(self):
        processed = self.buffers_processed
        released = [entry["buffer"] for entry in self.entries[:processed]]
        del self.entries[:processed]
        return released

    def play(self):
        self._playing = True
        self.play_calls += 1
        if self.entries and self.entries[0]["start"] is None:
            self.entries[0]["start"] = _ceil_to(self.clock.now_ms, self.mix_period_ms)
            previous = self.entries[0]
            for entry in self.entries[1:]:
                if entry["start"] is not None:
                    break
                entry["start"] = previous["start"] + previous["samples"] / SAMPLES_PER_MS
                previous = entry

    @property
    def buffers_processed(self):
        now = self.clock.now_ms
        count = 0
        for entry in self.entries:
            if (entry["start"] is not None
                    and entry["start"] + entry["samples"] / SAMPLES_PER_MS <= now):
                count += 1
            else:
                break
        return count

    @property
    def buffers_queued(self):
        return len(self.entries)

    @property
    def state(self):
        # OpenAL stops a source that ran out of buffers: that is what lets the
        # receive path play again (and re-pad) when the next packet lands.
        if not self.entries:
            return cyal.SourceState.STOPPED
        return cyal.SourceState.PLAYING if self._playing else cyal.SourceState.INITIAL


class _SimContext:
    def gen_buffer(self):
        return _SimBuffer()


class _SimAudioManager:
    """Only what the receive path touches: context + the main-thread inbox."""

    def __init__(self, context):
        self.context = context
        self.pending = []

    def defer_audio(self, fn):
        self.pending.append(fn)


class _FakeGame:
    def __init__(self, audio_mngr):
        self.audio_mngr = audio_mngr


def make_compression():
    comp = vc.voice_chat_compression.__new__(vc.voice_chat_compression)
    comp.channel = 20
    comp.decoder = types.SimpleNamespace(
        decode=lambda payload: bytes([payload[0]]) * FRAME_BYTES)
    return comp


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------
def run_stream(arrivals_ms, fps, mix_period_ms=DEFAULT_DEVICE_PERIOD_MS,
               floor_frames=None):
    """Feed one 20 ms voice packet per arrival and measure what the ear got.

    ``arrivals_ms``: packet arrival times (ms). Packet i carries byte marker
    ``i + 1`` so its audio can be followed through the source queue.
    ``floor_frames``: optional override of the shipped cushion floor, so the
    previous behaviour can be measured on the same code (None = shipped).
    """
    clock = _Clock()
    source = _SimSource(clock, mix_period_ms)
    mngr = _SimAudioManager(_SimContext())
    comp = make_compression()
    comp.game = _FakeGame(mngr)

    gameplay = types.SimpleNamespace(
        player=types.SimpleNamespace(dead=False, has_radio=False),
        voice_channels={},
        megaphone=None,
    )

    real_time = vc.time.time
    real_monotonic = vc.time.monotonic
    if floor_frames is not None:
        original_margin = vc._adaptive_margin_frames

        def floored(sender_id):
            return max(floor_frames, original_margin(sender_id))
        vc._adaptive_margin_frames = floored

    vc.time.time = lambda: clock.now_ms / 1000.0
    vc.time.monotonic = lambda: clock.now_ms / 1000.0
    vc._voice_last_pkt.clear()
    vc._speaker_jitter_ms.clear()
    vc._speaker_jitter_ts.clear()

    frame_ms = 1000.0 / fps
    next_packet = 0
    next_frame = 0.0
    repads = 0
    burst_started = False

    try:
        while clock.now_ms < arrivals_ms[-1] + 400.0:
            clock.now_ms += 0.5
            now = clock.now_ms

            while next_packet < len(arrivals_ms) and arrivals_ms[next_packet] <= now:
                marker = (next_packet + 1) % 256 or 1
                comp.recieve2(bytearray([marker]), source, None, 20, gameplay)
                next_packet += 1

            if now >= next_frame:
                next_frame = now + frame_ms
                pending, mngr.pending = mngr.pending, []
                for fn in pending:
                    if source.buffers_queued == 0:
                        # The first fill of a burst is its cushion; a later
                        # one that finds an empty source is an audible hole.
                        if burst_started:
                            repads += 1
                        burst_started = True
                    fn()
    finally:
        vc.time.time = real_time
        vc.time.monotonic = real_monotonic
        if floor_frames is not None:
            vc._adaptive_margin_frames = original_margin

    # Which packet each played buffer held (snapshotted at queue time), and
    # how late it was heard.
    first_heard = None
    steady = []
    for index, record in enumerate(source.history):
        marker, samples, start = record["marker"], record["samples"], record["start"]
        if not marker or samples != FRAME_SAMPLES or start is None:
            continue            # a silence pad frame, or one never played
        packet = marker[0] - 1
        if packet < 0 or packet >= len(arrivals_ms):
            continue
        delay = start - arrivals_ms[packet]
        if first_heard is None:
            first_heard = delay
        elif index >= 4:
            steady.append(delay)
    mean_steady = sum(steady) / len(steady) if steady else None
    return first_heard, mean_steady, repads


def arrivals_clean(seconds=3.0):
    out = []
    t = 0.0
    while t < seconds * 1000.0:
        out.append(t)
        t += FRAME_MS
    return out


def arrivals_jittery(spread_ms, seconds=3.0, seed=5):
    import random
    rng = random.Random(seed)
    out = []
    t = 0.0
    while t < seconds * 1000.0:
        out.append(t)
        t += FRAME_MS + rng.uniform(-spread_ms, spread_ms)
    return out


def arrivals_spike(gap_ms, every_ms=2000, seconds=6.0):
    """A clean 20 ms cadence, but one packet every ``every_ms`` lands
    ``gap_ms`` late (the realistic case the adaptive margin exists for)."""
    out = []
    t = 0.0
    while t < seconds * 1000.0:
        if int(t) % every_ms < FRAME_MS and out and (t - out[-1]) >= FRAME_MS:
            t += gap_ms
            continue
        out.append(t)
        t += FRAME_MS
    return out


def fifo_ms(fps):
    """The main-thread audio inbox: drained once per game frame, so a frame's
    wait averages half a frame."""
    return 1000.0 / fps / 2


def budget(fps, floor_frames=None, rtt_ms=40.0, capture_period_ms=DEFAULT_DEVICE_PERIOD_MS,
           mix_period_ms=DEFAULT_DEVICE_PERIOD_MS):
    first, steady, repads = run_stream(arrivals_clean(), fps, mix_period_ms,
                                       floor_frames=floor_frames)
    # ``steady`` is measured from a packet's arrival to the first sample the
    # ear gets: the cushion PLUS the wait for the card's next mixer period.
    cushion = steady if steady is not None else first
    sender = (capture_period_ms / 2 + vc.VOICE_FRAME_MS / 2
              + vc.VOICE_SEND_POLL_S * 1000 / 2 + ENCODE_MS)
    receive = RECEIVE_WORKER_POLL_MS / 2 + DECODE_MS + fifo_ms(fps) + cushion
    return sender, rtt_ms / 2, receive, sender + rtt_ms / 2 + receive


# ---------------------------------------------------------------------------
def main():
    print("=" * 78)
    print("LIVE VOICE (channel 20) ONE-WAY LATENCY - measured through the shipped code")
    print("=" * 78)

    offline = "--offline" in sys.argv
    rtt = 40.0
    if "--rtt" in sys.argv:
        rtt = float(sys.argv[sys.argv.index("--rtt") + 1])

    capture_period_ms = DEFAULT_DEVICE_PERIOD_MS
    mix_period_ms = DEFAULT_DEVICE_PERIOD_MS
    if offline:
        print(f"\n  device periods: not probed (--offline), using the measured "
              f"defaults ({DEFAULT_DEVICE_PERIOD_MS:.0f} ms in, "
              f"{DEFAULT_DEVICE_PERIOD_MS:.0f} ms out)")
    else:
        for label, probe in (("capture", probe_capture_period),
                             ("mixer", probe_mix_period)):
            try:
                probed = probe()
            except Exception as exc:
                print(f"  this machine's {label} period: unmeasured "
                      f"({exc.__class__.__name__})")
                continue
            if probed is None:
                print(f"  this machine's {label} period: unmeasured")
                continue
            print(f"  this machine's {label} period: {probed:.1f} ms")
            if label == "capture":
                capture_period_ms = probed
            else:
                mix_period_ms = probed

    print()
    print("SENDER SIDE (read from the shipped code, not chosen here)")
    print("-" * 78)
    print(f"  capture device period .................. 0-{capture_period_ms:.0f} ms "
          f"(this machine; the endpoint hands over no sooner)")
    print(f"  fill to the {vc.VOICE_FRAME_MS:.0f} ms Opus frame ......... 0-{vc.VOICE_FRAME_MS:.0f} ms "
          f"(mean {vc.VOICE_FRAME_MS / 2:.0f}; VOICE_FRAME_BYTES={FRAME_BYTES})")
    print(f"  send worker poll ....................... 0-{vc.VOICE_SEND_POLL_S * 1000:.0f} ms "
          f"(VOICE_SEND_POLL_S)")
    print(f"  Opus encode ............................ ~{ENCODE_MS:.0f} ms")
    print()
    print("NETWORK")
    print("-" * 78)
    print(f"  one leg (client -> server -> client) ... ping / 2 = {rtt / 2:.0f} ms "
          f"at RTT {rtt:.0f}")
    print()
    print("RECEIVE SIDE (measured here, through recieve2 + _play_voice_frame)")
    print("-" * 78)

    print(f"\n  shipped cushion ({vc._adaptive_margin_frames('probe')} frame at 0 ms "
          f"jitter) against the previous floor (2 frames), same code:\n")
    print(f"  {'scenario':<28} {'fps':>4} {'floor':>9} {'first-heard':>12} "
          f"{'steady':>9} {'re-pads':>8}")
    for label, arrivals in (
            ("clean 20 ms", arrivals_clean()),
            ("jittery +/-8 ms", arrivals_jittery(8.0)),
            ("spike 60 ms / 2 s", arrivals_spike(60.0)),
            ("spike 100 ms / 3 s", arrivals_spike(100.0, 3000))):
        for fps in (60, 120):
            for floor in (None, 2):
                first, steady, repads = run_stream(arrivals, fps, mix_period_ms,
                                                   floor_frames=floor)
                name = label if floor is None else label + " (old floor)"
                first_cell = f"{first:6.0f} ms" if first is not None else "   n/a"
                steady_cell = f"{steady:6.0f} ms" if steady is not None else "   n/a"
                print(f"  {name:<28} {fps:>4} {floor or '1(ship)':>9} {first_cell:>12} "
                      f"{steady_cell:>9} {repads:>8}")

    print()
    print("BUDGET (mean values, one-way)")
    print("-" * 78)
    print(f"  {'config':<30} {'sender':>9} {'network':>9} {'receive':>9} {'total':>9}")
    for fps in (60, 120):
        for floor in (None, 2):
            sender, network, receive, total = budget(
                fps, floor_frames=floor, rtt_ms=rtt,
                capture_period_ms=capture_period_ms, mix_period_ms=mix_period_ms)
            name = f"{fps} fps" + ("" if floor is None else " (old floor)")
            print(f"  {name:<30} {sender:6.0f} ms {network:7.0f} ms "
                  f"{receive:7.0f} ms {total:7.0f} ms")

    print()
    print("=" * 78)
    print("ASSERTIONS")
    print("=" * 78)

    # The cushion is the one lever this code chooses: one frame on a clean
    # link, grown by measured jitter, capped at six.
    vc._speaker_jitter_ms["u"] = 0.0
    check("clean link: cushion is one 20 ms frame",
          vc._adaptive_margin_frames("u") == 1,
          f"{vc._adaptive_margin_frames('u')} frames")
    vc._speaker_jitter_ms["u"] = 40.0
    check("40 ms jitter: cushion grows to three frames",
          vc._adaptive_margin_frames("u") == 3)
    vc._speaker_jitter_ms["u"] = 500.0
    check("high jitter: cushion capped at six frames",
          vc._adaptive_margin_frames("u") == 6)
    check("the megaphone/PA reserve is untouched (six frames)",
          vc._megaphone_margin_frames("u") == 6)

    for fps in (60, 120):
        first, steady, repads = run_stream(arrivals_clean(), fps, mix_period_ms)
        check(f"clean stream at {fps} fps: no re-pad (no audible hole)",
              repads == 0, f"re-pads={repads}")
        check(f"clean stream at {fps} fps: cushion is one frame",
              first is not None and first <= FRAME_MS + mix_period_ms,
              f"first-heard={first:.0f} ms" if first is not None else "n/a")

    _, _, repads_spike = run_stream(arrivals_spike(60.0), 60, mix_period_ms)
    _, _, repads_spike_old = run_stream(arrivals_spike(60.0), 60, mix_period_ms,
                                        floor_frames=2)
    check("a 60 ms spike / 2 s is absorbed by the grown cushion",
          repads_spike <= repads_spike_old,
          f"shipped {repads_spike} vs old floor {repads_spike_old}")

    old = run_stream(arrivals_clean(), 60, mix_period_ms, floor_frames=2)[1]
    new = run_stream(arrivals_clean(), 60, mix_period_ms)[1]
    check("the shipped cushion is a full frame (20 ms) under the old floor",
          old is not None and new is not None and abs((old - new) - FRAME_MS) < 1.0,
          f"{old:.0f} ms -> {new:.0f} ms")

    print()
    print(f"RESULT: {PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
