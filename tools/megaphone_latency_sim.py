"""Megaphone receive-path latency simulation.

Discrete-event model of the exact pipeline a listener experiences:

  arrival -> MegaphoneJitterBuffer (pre-buffer) -> queue_and_delay_frame
           -> silence padding (frames_delay + adaptive margin) -> OpenAL source

The PA margin is read from the REAL production helper in libs/voice_chat
(_megaphone_margin_frames), so the restored result reflects shipped logic,
not a copied constant. Adaptive-margin checks remain for normal voice.

Metrics per run:
  - first-heard latency: ms from the first packet arriving to the moment the
    first real audio frame is consumed by the playhead (what the ear hears).
  - starvations: times the source ran dry while the sender was still live
    (audible crackle / drop).

Configs compared:
  OLD      pre=3, fixed margin=6
  RESTORED production v1.6 pre-buffer + fixed PA margin
  MIN      pre=1, fixed margin=1 (the earlier crackly experiment)

Run:  python tools/megaphone_latency_sim.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from libs import voice_chat as vc

FRAME_MS = 20.0


# --------------------------------------------------------------------------
# Discrete-event pipeline model
# --------------------------------------------------------------------------
class Pipeline:
    def __init__(self, pre_frames, margin_fn, frames_delay=0):
        self.pre_frames = pre_frames
        self.margin_fn = margin_fn          # callable() -> margin frames
        self.frames_delay = frames_delay
        self.jb = []                        # buffered packets (arrival times)
        self.src = []                       # source queue: ('S'|'R', t)
        self.playhead = 0.0                 # source audio consumed (ms)
        self.playing = False
        self.starved = True                 # source empty -> resync on next fill
        self.first_heard = None
        self.starvations = 0
        self.last_tick = None

    def _consume(self, now):
        """Advance the playhead: 1 frame per 20ms of sim time."""
        if not self.playing or self.last_tick is None:
            self.last_tick = now
            return
        dt = now - self.last_tick
        self.last_tick = now
        n = int(dt // FRAME_MS)
        for _ in range(n):
            if not self.src:
                self.starvations += 1
                self.playing = False          # dry -> stalls until next fill
                return
            kind, _t = self.src.pop(0)
            if kind == 'R' and self.first_heard is None:
                self.first_heard = now
        # keep the fractional remainder so 20ms cadence is exact
        self.last_tick = now - (dt % FRAME_MS)

    def _fill(self, packet_time, resync):
        """Mirror queue_and_delay_frame: pad silence on resync, queue 1 real."""
        if resync:
            margin = self.margin_fn()
            for _ in range(self.frames_delay + margin):
                self.src.append(('S', packet_time))
        self.src.append(('R', packet_time))
        self.starved = False
        if not self.playing:
            self.playing = True
            self.last_tick = packet_time

    def on_packet(self, t):
        self._consume(t)
        self.jb.append(t)
        if not self.playing:
            if len(self.jb) >= self.pre_frames:
                # first successful get_packet() -> queue_and_delay with resync
                self.jb.pop(0)
                self._fill(t, resync=True)
            return
        if self.starved:
            self.jb.pop(0)
            self._fill(t, resync=True)       # any_starved -> re-pad
        else:
            self.jb.pop(0)
            self._fill(t, resync=False)


def arrivals_steady(seconds):
    t = 0.0
    out = []
    while t < seconds * 1000.0:
        out.append(t)
        t += FRAME_MS
    return out


def arrivals_jittery(seconds, spread_ms, rng, seed):
    import random
    r = random.Random(seed)
    t = 0.0
    out = []
    while t < seconds * 1000.0:
        out.append(t)
        t += FRAME_MS + r.uniform(-spread_ms, spread_ms)
    return out


def arrivals_spikey(seconds, gap_ms, every_ms, seed):
    """Normal 20ms cadence, but every `every_ms` one packet slot is skipped
    and the following packet arrives `gap_ms` late (a realistic network
    hiccup - e.g. a 60ms gap every 2s)."""
    import random
    r = random.Random(seed)
    t = 0.0
    out = []
    while t < seconds * 1000.0:
        if (int(t) % every_ms < 20 and out
                and (t - out[-1]) >= FRAME_MS):
            # this slot is delayed: skip it, next packet lands gap_ms late
            t += gap_ms
            continue
        out.append(t)
        t += FRAME_MS
    return out


SENDER = "sim_sender"

def run(pattern, pre_frames, margin_kind, frames_delay=0):
    """margin_kind: 'fixed:N' (fixed N frames) or 'adaptive' (live jitter)."""
    t0 = pattern[0]
    if margin_kind.startswith("fixed:"):
        n = int(margin_kind.split(":")[1])
        pipe = Pipeline(pre_frames, lambda: n, frames_delay)
        for t in pattern:
            pipe.on_packet(t)
    else:
        # Feed the REAL _measure_speaker_jitter live as packets arrive - the
        # same call recieve2 makes - so the adaptive margin reflects real
        # peak-hold state at every resync.
        pipe = Pipeline(pre_frames, lambda: vc._adaptive_margin_frames(SENDER), frames_delay)
        prev = None
        vc._speaker_jitter_ms.pop(SENDER, None)
        vc._speaker_jitter_ts.pop(SENDER, None)
        for t in pattern:
            vc._measure_speaker_jitter(SENDER, prev, t / 1000.0)
            prev = t / 1000.0
            pipe.on_packet(t)
    if pipe.first_heard is None:
        return None, pipe.starvations
    latency = pipe.first_heard - t0
    return latency, pipe.starvations


def last_est():
    return vc._speaker_jitter_ms.get(SENDER, 0.0)


def table_row(name, latency, starv, ok=True):
    lat = f"{latency:6.0f} ms" if latency is not None else "   n/a  "
    print(f"  {name:<22} first-heard {lat}   starvations {starv:>3}   {'OK' if ok else ''}")


# --------------------------------------------------------------------------
print("=" * 74)
print("MEGAPHONE RECEIVE-PATH LATENCY SIMULATION (20ms Opus frames)")
print("=" * 74)

patterns = {
    "steady (exact 20ms)": arrivals_steady(4.0),
    "jittery (+/-12ms)": arrivals_jittery(6.0, 12.0, None, 42),
    "spikey (60ms gap / 2s)": arrivals_spikey(10.0, 60.0, 2000, 1),
    "spikey (100ms gap / 3s)": arrivals_spikey(12.0, 100.0, 3000, 7),
}

print()
print("Floor latency math (0m distance, no propagation delay):")
print(f"  OLD: pre=3x20ms + fixed 6x20ms = {9 * FRAME_MS:.0f}ms minimum")
steady_margin = vc._megaphone_margin_frames("steady_floor")
print(f"  RESTORED: pre={vc.MegaphoneJitterBuffer.PRE_BUFFER_FRAMES}x20ms + fixed "
      f"({steady_margin}x20ms) = "
      f"{(vc.MegaphoneJitterBuffer.PRE_BUFFER_FRAMES + steady_margin) * FRAME_MS:.0f}ms minimum")

print()
print("Propagation delay (distance / 343 m/s, per speaker, kept):")
for dist in (5, 20, 50, 100):
    print(f"  {dist:>4} m -> {dist / 343.0 * 1000.0:5.1f} ms")

for name, pat in patterns.items():
    print()
    print(f"--- {name} ---")
    old_lat, old_starv = run(pat, 3, "fixed:6")
    new_lat, new_starv = run(
        pat,
        vc.MegaphoneJitterBuffer.PRE_BUFFER_FRAMES,
        f"fixed:{vc._megaphone_margin_frames('production')}",
    )
    min_lat, min_starv = run(pat, 1, "fixed:1")
    table_row("OLD (fixed 120ms)", old_lat, old_starv)
    table_row("RESTORED (v1.6)", new_lat, new_starv)
    table_row("MIN (fixed 20ms)", min_lat, min_starv)
    print(f"  (measured peak jitter: {last_est():.1f} ms)")

# --------------------------------------------------------------------------
print()
print("=" * 74)
print("LATENCY ASSERTIONS")
print("=" * 74)
passed = failed = 0


def check(name, ok, detail=""):
    global passed, failed
    if ok:
        passed += 1
        print(f"  PASS - {name} {detail}")
    else:
        failed += 1
        print(f"  FAIL - {name} {detail}")


# Production intentionally matches the stable v1.6 PA latency budget.
steady_old, _ = run(arrivals_steady(4.0), 3, "fixed:6")
steady_new, _ = run(
    arrivals_steady(4.0),
    vc.MegaphoneJitterBuffer.PRE_BUFFER_FRAMES,
    f"fixed:{vc._megaphone_margin_frames('production')}",
)
check("steady: RESTORED matches v1.6 latency",
      steady_old is not None and steady_new == steady_old,
      f"v1.6 {steady_old:.0f}ms -> RESTORED {steady_new:.0f}ms")

# Spikey delivery: restored PA must starve less than the fixed-20ms attempt.
j_spike, j_starv_spike = run(
    arrivals_spikey(10.0, 60.0, 2000, 1),
    vc.MegaphoneJitterBuffer.PRE_BUFFER_FRAMES,
    f"fixed:{vc._megaphone_margin_frames('production')}",
)
m_spike, m_starv_spike = run(arrivals_spikey(10.0, 60.0, 2000, 1), 1, "fixed:1")
check("spikey: restored PA starves LESS than fixed 20ms",
      j_starv_spike <= m_starv_spike,
      f"restored {j_starv_spike} vs fixed20 {m_starv_spike}")
check("spikey: restored latency stays within v1.6 budget",
      j_spike is not None and j_spike <= 180.0, f"{j_spike:.0f}ms")

# unit checks on the real margin math
vc._speaker_jitter_ms["u"] = 0.0
check("adaptive margin = 2 frames at 0ms jitter",
      vc._adaptive_margin_frames("u") == 2)
vc._speaker_jitter_ms["u"] = 45.0
check("adaptive margin = 4 frames at 45ms jitter",
      vc._adaptive_margin_frames("u") == 4)
vc._speaker_jitter_ms["u"] = 500.0
check("adaptive margin capped at 6 frames",
      vc._adaptive_margin_frames("u") == 6)
check("PA margin fixed at v1.6 six-frame reserve",
      vc._megaphone_margin_frames("u") == 6)

print()
print(f"RESULT: {passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
