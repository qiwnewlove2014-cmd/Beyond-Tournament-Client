"""How late does a performer hear their own string? Measured, not guessed.

The chain a line-in guitar goes through before the player hears it:

    string -> capture device -> capture read -> main loop -> monitor queue
           -> driver mix -> ear

Only two links are not the machine's: how big a read is, and how deep the
monitor's queue is allowed to get. Both are read from the shipping code
(``libs.instrument_input``), and the queue link is driven by the REAL
``GuitarLocalMonitor`` with a fake OpenAL source that plays in real time -- so
what this prints is the shipped behaviour, not a model of it.

The machine's two links are measurable and are probed live when a device can be
opened (``--offline`` skips it). Both are the WASAPI shared-mode device period,
and on this project's machine both measured 480 samples every 10 ms -- the
capture endpoint hands audio over in those periods, and the mixer consumes it
in those periods. Neither is tunable downwards: asking OpenAL for
``period_size = 240`` in its config still advanced in 480-sample steps, because
the device period is the floor. So a capture read can never beat one delivery
period, and no output can beat one mixer period.

What the table is for: the *second* strum, not the first. The capture arrives at
exactly the rate the source plays, so audio left waiting through a stalled main
thread is still being heard that much later for the rest of the session --
nothing later in the song lets the ear catch up. That is the difference between
"the guitar is a little behind" and "the guitar keeps drifting away from the
hand that plays it".

Run:  python tools/instrument_monitor_latency_sim.py
      python tools/instrument_monitor_latency_sim.py --offline
"""
import collections
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")

import cyal

from libs import instrument_input as ii

RATE = 48000
SAMPLES_PER_MS = RATE / 1000.0

# The shipping code's own numbers: what a read is and what the monitor is given.
NEW_READ_TRIGGER = ii.InstrumentInput.MIN_READ_SAMPLES
NEW_READ_MAX = ii.InstrumentInput.MONITOR_CHUNK_SAMPLES
NEW_MONITOR_CHUNK = ii.InstrumentInput.MONITOR_CHUNK_SAMPLES
OLD_READ_TRIGGER = ii.InstrumentInput.FRAME_SAMPLES
OLD_READ_MAX = ii.InstrumentInput.FRAME_SAMPLES
OLD_MONITOR_CHUNK = ii.InstrumentInput.FRAME_SAMPLES
QUEUE_LIMIT = ii.GuitarLocalMonitor.MONITOR_QUEUE_LIMIT_MS

# Driver sizes used when nothing can be probed (what was measured on this
# project's machine: 480 samples every 10 ms, in and out).
DEFAULT_CAPTURE_PERIOD_MS = 10.0
DEFAULT_MIX_PERIOD_MS = 10.0
HITCH_MS = 150.0
HITCH_AT_MS = 2000.0


# ---------------------------------------------------------------------------
# The machine's own two numbers
# ---------------------------------------------------------------------------

def probe_capture_period(seconds=0.6):
    """How many samples the capture endpoint hands over at a time.

    A capture device carries silence as readily as a guitar, so this needs no
    instrument: open the default device, drain it immediately, and look at the
    sizes it arrives in. Returns ms, or None if it could not be measured.
    """
    import cyal
    ext = cyal.CaptureExtension()
    device = ext.open_device(name=ext.default_device, sample_rate=RATE,
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
    """How many samples the mixer consumes at a time.

    Play silence and watch the play position: it advances in whole mixer
    periods, which is the granularity audio reaches the card in. Returns ms.
    """
    import cyal
    device = cyal.Device(name=cyal.util.get_default_all_device_specifier())
    context = cyal.Context(device, make_current=True)
    source = context.gen_source()
    buffer = context.gen_buffer()
    buffer.set_data(bytes(RATE * 2 * 30), sample_rate=RATE,
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
# A fake OpenAL source that plays in real time, for the real monitor class
# ---------------------------------------------------------------------------
class SimBuffer:
    def __init__(self):
        self.samples = 0

    def set_data(self, pcm, sample_rate=None, format=None):
        self.samples = len(pcm) // 2


class SimSource:
    """The parts of ``cyal.Source`` that :class:`GuitarLocalMonitor` touches.

    Playback is modelled from the model's own clock: a chunk queued while the
    source is idle starts one mixer period later (that is when the card next
    takes audio), otherwise it starts where the chunk before it ended. A
    stopped source has played everything it held -- measured through the
    shipped OpenAL, and the reason a monitor can drop its backlog at all.
    """

    # The very states the monitor checks for: the real Spellings, or its
    # "play it if it is not playing" branch would never run in this model.
    INITIAL = cyal.SourceState.INITIAL
    PLAYING = cyal.SourceState.PLAYING
    STOPPED = cyal.SourceState.STOPPED

    def __init__(self, clock, mix_period_ms=DEFAULT_MIX_PERIOD_MS):
        self.clock = clock
        self.mix_period_ms = mix_period_ms
        self.looping = False
        self.position = (0.0, 0.0, 0.0)
        self.queued = []          # live [samples, start_ms, index, dropped]
        self.log = []             # every chunk ever queued, in order
        self.next_index = 0
        self.stopping = False
        self._state = self.INITIAL

    def queue_buffers(self, *buffers):
        for buffer in buffers:
            now = self.clock.now_ms
            if self.queued:
                start = self.queued[-1][1] + self.queued[-1][0] / SAMPLES_PER_MS
            else:
                start = _ceil_to(now, self.mix_period_ms)
            entry = [buffer.samples, start, self.next_index, False]
            self.queued.append(entry)
            self.log.append(entry)
            self.next_index += buffer.samples

    def unqueue_buffers(self):
        processed = self.buffers_processed
        for entry in self.queued[:processed]:
            if self.stopping:
                entry[3] = True
        del self.queued[:processed]
        if not self.queued and self._state == self.PLAYING:
            self._state = self.STOPPED   # it ran out of buffers

    def play(self):
        self.stopping = False
        self._state = self.PLAYING

    def stop(self):
        # Every chunk a stopped source held is audio the ear never got.
        self.stopping = True
        self._state = self.STOPPED
        for entry in self.queued:
            entry[1] = min(entry[1], self.clock.now_ms)

    @property
    def buffers_processed(self):
        now = self.clock.now_ms
        count = 0
        for samples, started, _index, _dropped in self.queued:
            if started + samples / SAMPLES_PER_MS <= now:
                count += 1
            else:
                break
        return count

    @property
    def buffers_queued(self):
        return len(self.queued)

    @property
    def state(self):
        # Queueing on a stopped source leaves it stopped: that is what makes
        # the monitor's "play it if it is not playing" branch run again after
        # a backlog was let go (measured behaviour of the shipped OpenAL).
        if self._state == self.PLAYING and not self.queued:
            return self.STOPPED
        return self._state


class SimContext:
    """Hands the monitor the one fake source the run is measured through."""

    def __init__(self, source):
        self.source = source

    def gen_source(self, **kwargs):
        return self.source

    def gen_buffer(self):
        return SimBuffer()


class _Clock:
    def __init__(self):
        self.now_ms = 0.0



class SimpleAudioManager:
    """Only what the monitor reaches for: ``audio_mngr.context``."""

    def __init__(self, context):
        self.context = context


def _ceil_to(value, step):
    if step <= 0 or value % step == 0:
        return value
    return step * -(-value // step)


# ---------------------------------------------------------------------------
# The run itself
# ---------------------------------------------------------------------------
def simulate(*, read_trigger, read_max, monitor_chunk, fps, queue_limit_ms=None,
             hitch_ms=0.0, hitch_at_ms=HITCH_AT_MS, seconds=6.0,
             capture_period_ms=DEFAULT_CAPTURE_PERIOD_MS,
             mix_period_ms=DEFAULT_MIX_PERIOD_MS):
    """Run the capture-and-monitor chain once and report what the ear heard.

    Returns ``(chunks, skips)`` where each chunk is
    ``(first_sample_index, heard_at_ms, dropped)``: the sample at
    ``first_sample_index`` was struck at ``first_sample_index / 48`` ms, so its
    delay is ``heard_at_ms - first_sample_index / 48``.
    """
    clock = _Clock()
    source = SimSource(clock, mix_period_ms)
    monitor = ii.GuitarLocalMonitor(SimpleAudioManager(SimContext(source)))
    if queue_limit_ms is None:
        monitor.queue_limit_samples = 1 << 30
    else:
        monitor.queue_limit_samples = int(queue_limit_ms * SAMPLES_PER_MS)

    period_samples = int(capture_period_ms * SAMPLES_PER_MS)
    tick_ms = 0.5
    frame_ms = 1000.0 / fps
    available = 0
    monitor_pending = 0
    frames = collections.deque()

    next_delivery = 0.0
    next_poll = 0.0
    next_frame = 0.0
    end_ms = seconds * 1000.0

    while clock.now_ms < end_ms:
        clock.now_ms += tick_ms
        now = clock.now_ms

        # The capture device hands one period over at a time.
        while now >= next_delivery:
            available += period_samples
            next_delivery += capture_period_ms

        # The capture worker: one read at most, as soon as its trigger is met.
        if now >= next_poll:
            next_poll = now + tick_ms
            if available >= read_trigger:
                take = min(available, read_max)
                available -= take
                monitor_pending += take
                while monitor_pending >= monitor_chunk:
                    monitor_pending -= monitor_chunk
                    frames.append(monitor_chunk)

        # The main loop drains the monitor once per frame, unless it is stalled.
        stalled = hitch_ms and hitch_at_ms <= now < hitch_at_ms + hitch_ms
        if now >= next_frame and not stalled:
            next_frame = now + frame_ms
            while frames:
                monitor.feed(bytes(frames.popleft() * 2))

    chunks = [(entry[2], entry[1], entry[3]) for entry in source.log]
    return chunks, monitor.skips


def delay_of_sample_at(chunks, at_ms):
    """Delay a string struck at ``at_ms`` was heard with (None = never heard)."""
    index = int(at_ms * SAMPLES_PER_MS)
    chosen = None
    for first_index, heard_at, dropped in chunks:
        if first_index <= index:
            chosen = (heard_at, dropped)
        else:
            break
    if chosen is None:
        return None
    heard_at, dropped = chosen
    if dropped:
        return None
    return heard_at - index / SAMPLES_PER_MS


def _cell(delay):
    return "dropped" if delay is None else f"{delay:.0f} ms"


def _mean_cell(chunks):
    """The average delay an ear actually got, dropped chunks aside."""
    delays = [heard_at - first_index / SAMPLES_PER_MS
              for first_index, heard_at, dropped in chunks if not dropped]
    if not delays:
        return "-"
    return f"{sum(delays) / len(delays):.0f} ms"


def main():
    offline = "--offline" in sys.argv
    capture_period_ms = DEFAULT_CAPTURE_PERIOD_MS
    mix_period_ms = DEFAULT_MIX_PERIOD_MS
    print(__doc__.strip().split("\n")[0])
    print()
    if offline:
        print("  driver periods: not probed (--offline), using the measured "
              f"defaults ({capture_period_ms:.0f} ms in, {mix_period_ms:.0f} ms out)")
    else:
        for label, probe in (("capture period", probe_capture_period),
                             ("mixer period", probe_mix_period)):
            try:
                probed = probe()
            except Exception as exc:                      # pragma: no cover
                print(f"  {label}: unmeasured ({exc.__class__.__name__})")
                continue
            if probed is None:                            # pragma: no cover
                print(f"  {label}: unmeasured")
                continue
            count = probed * SAMPLES_PER_MS
            print(f"  this machine's {label}: {probed:.1f} ms ({count:.0f} samples)")
            if label.startswith("capture"):
                capture_period_ms = probed
            else:
                mix_period_ms = probed
    print()

    configs = [
        ("before  60fps", OLD_READ_TRIGGER, OLD_READ_MAX, OLD_MONITOR_CHUNK,
         None, 60),
        ("before 120fps", OLD_READ_TRIGGER, OLD_READ_MAX, OLD_MONITOR_CHUNK,
         None, 120),
        (f"after   60fps", NEW_READ_TRIGGER, NEW_READ_MAX, NEW_MONITOR_CHUNK,
         QUEUE_LIMIT, 60),
        (f"after  120fps", NEW_READ_TRIGGER, NEW_READ_MAX, NEW_MONITOR_CHUNK,
         QUEUE_LIMIT, 120),
    ]

    print(f"  a {HITCH_MS:.0f} ms stalled main thread at t={HITCH_AT_MS / 1000:.1f} s "
          "(a map reload, a slow frame):")
    print(f"  {'':15} {'mean':>8} {'0.5 s':>9} {'2.1 s':>9} {'5.5 s':>9} "
          f"{'drops':>6}")
    for label, trigger, read_max, chunk, limit, fps in configs:
        chunks, skips = simulate(
            read_trigger=trigger, read_max=read_max, monitor_chunk=chunk,
            queue_limit_ms=limit, fps=fps, hitch_ms=HITCH_MS,
            capture_period_ms=capture_period_ms, mix_period_ms=mix_period_ms)
        cells = [_cell(delay_of_sample_at(chunks, at))
                 for at in (500.0, 2100.0, 5500.0)]
        print(f"  {label:15} {_mean_cell(chunks):>8} {cells[0]:>9} "
              f"{cells[1]:>9} {cells[2]:>9} {skips:>6}")

    print()
    print("  the same run without a hitch:")
    print(f"  {'':15} {'mean':>8} {'0.5 s':>9} {'2.1 s':>9} {'5.5 s':>9}")
    for label, trigger, read_max, chunk, limit, fps in configs:
        chunks, _skips = simulate(
            read_trigger=trigger, read_max=read_max, monitor_chunk=chunk,
            queue_limit_ms=limit, fps=fps,
            capture_period_ms=capture_period_ms, mix_period_ms=mix_period_ms)
        cells = [_cell(delay_of_sample_at(chunks, at))
                 for at in (500.0, 2100.0, 5500.0)]
        print(f"  {label:15} {_mean_cell(chunks):>8} {cells[0]:>9} "
              f"{cells[1]:>9} {cells[2]:>9}")
    print()
    print("  Those delays are from the string to the driver. The ear's own two")
    print("  links are the machine's and add on top: up to one capture period")
    print(f"  for the string to be handed over ({capture_period_ms:.0f} ms) and up")
    print(f"  to one mixer period for the card to take it ({mix_period_ms:.0f} ms).")


if __name__ == "__main__":
    main()
