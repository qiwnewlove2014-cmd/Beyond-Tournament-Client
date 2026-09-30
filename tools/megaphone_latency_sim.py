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
  v1.5     pre=3, fixed margin=6
  v1.6     pre=6, fixed margin=6   (two cushions, but only one reserve)
  NOW      the shipped pair from libs/voice_chat (gate + source margin)
  MIN      pre=1, fixed margin=1 (the earlier crackly experiment)

The last table answers a different question: the client->server PA leg is
UNRELIABLE, so a lost packet is a permanent hole rather than a pause, and the
source margin is what pays for it. It models the NOT-YET-BUILT sequence + Opus
PLC concealment (plc=True) so the cut it would buy can be measured before it is
written -- nothing in that table is shipped behaviour.

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
    def __init__(self, pre_frames, margin_fn, frames_delay=0, plc=False):
        self.pre_frames = pre_frames
        self.margin_fn = margin_fn          # callable() -> margin frames
        self.frames_delay = frames_delay
        # plc=True models the candidate fix: a sequence gap is concealed with a
        # decode-missing-packet frame, so the hole never reaches the source and
        # the reserve stops draining. plc=False is every shipped build today.
        self.plc = plc
        self.last_seq = None
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

    def on_packet(self, t, seq=None):
        missing = 0
        if seq is not None:
            if self.last_seq is not None and seq > self.last_seq + 1:
                missing = seq - self.last_seq - 1
            self.last_seq = seq
        self._consume(t)
        if self.plc and missing:
            # The gap is decoded as concealment frames and queued like any other
            # frame; the first one inherits a resync if the source had already
            # run dry, exactly as a real arrival would.
            for _ in range(missing):
                self._fill(t, resync=self.starved)
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
print(f"  v1.5: pre=3x20ms + fixed 6x20ms = {9 * FRAME_MS:.0f}ms minimum")
print(f"  v1.6: pre=6x20ms + fixed 6x20ms = {12 * FRAME_MS:.0f}ms minimum "
      "(the same reserve counted twice)")
steady_margin = vc._megaphone_margin_frames("steady_floor")
print(f"  NOW:  pre={vc.MegaphoneJitterBuffer.PRE_BUFFER_FRAMES}x20ms + fixed "
      f"({steady_margin}x20ms) = "
      f"{(vc.MegaphoneJitterBuffer.PRE_BUFFER_FRAMES + steady_margin) * FRAME_MS:.0f}ms minimum")
print(f"  MIN:  pre=1x20ms + fixed 1x20ms = {2 * FRAME_MS:.0f}ms minimum"
      " (crackly: see the candidate table below)")

print()
print("Propagation delay (distance / 343 m/s, per speaker, kept):")
for dist in (5, 20, 50, 100):
    print(f"  {dist:>4} m -> {dist / 343.0 * 1000.0:5.1f} ms")

for name, pat in patterns.items():
    print()
    print(f"--- {name} ---")
    old_lat, old_starv = run(pat, 6, "fixed:6")
    new_lat, new_starv = run(
        pat,
        vc.MegaphoneJitterBuffer.PRE_BUFFER_FRAMES,
        f"fixed:{vc._megaphone_margin_frames('production')}",
    )
    min_lat, min_starv = run(pat, 1, "fixed:1")
    table_row("v1.6 (pre6 + m6)", old_lat, old_starv)
    table_row("NOW (shipped pair)", new_lat, new_starv)
    table_row("MIN (pre1 + m1)", min_lat, min_starv)
    print(f"  (measured peak jitter: {last_est():.1f} ms)")

# --------------------------------------------------------------------------
# CANDIDATE RESERVES: the PA pays TWO cushions before the first word is heard
# - the jitter pre-buffer (a start gate over the arrival queue) and the fixed
# source margin (silence queued ahead of the audio). Their SUM is the start
# latency, but only the source margin is what a stall has to eat through
# before the speakers go quiet, so the pair is not interchangeable. This table
# is the evidence for whichever pair ships: latency AND starvations on every
# pattern, so a cheaper cushion cannot be bought with an audible gap.
print()
print("=" * 74)
print("CANDIDATE RESERVES (latency / starvations per pattern)")
print("=" * 74)

CANDIDATES = [
    ("pre6+m6 (shipped)", 6, "fixed:6"),
    ("pre2+m6", 2, "fixed:6"),
    ("pre6+m4", 6, "fixed:4"),
    ("pre2+m4", 2, "fixed:4"),
    ("pre3+m3", 3, "fixed:3"),
    ("pre2+m3", 2, "fixed:3"),
    ("pre1+m1 (crackly)", 1, "fixed:1"),
]

print()
print(f"  {'reserve':<20} " + " ".join(f"{n.split(' ')[0]:>16}" for n in patterns))
for label, pre, margin_kind in CANDIDATES:
    cells = []
    for pat in patterns.values():
        lat, starv = run(pat, pre, margin_kind)
        cells.append(f"{lat:6.0f}ms/{starv:<2d}" if lat is not None else "   n/a  ")
    print(f"  {label:<20} " + " ".join(f"{c:>16}" for c in cells))

# --------------------------------------------------------------------------
# HOLES, NOT LAG. The client->server PA leg is UNRELIABLE (`send2` forces it for
# channels >= 20), so a lost packet is a PERMANENT hole: the listener never gets
# that 20 ms and the source's reserve pays for it, one frame per hole, until it
# hits zero and re-pads (the audible spike). That - not the reliable leg's
# retransmission pause - is what the six-frame margin is really buying, and it
# is why sequence + Opus PLC is the only way to buy it back: a concealed frame
# keeps the reserve full instead of draining it. The `plc=True` row models that
# fix, which does not exist yet; the first row is today's build.
def arrivals_lossy(seconds, loss_pct, seed):
    """20 ms cadence where `loss_pct` of the frames never arrive.

    Returns (t_ms, frame_seq) pairs, so a gap in frame_seq is a visible hole.
    """
    import random
    r = random.Random(seed)
    t = 0.0
    seq = 0
    out = []
    while t < seconds * 1000.0:
        if r.random() * 100.0 < loss_pct:
            t += FRAME_MS          # lost on the wire: no arrival, no advance
            seq += 1
            continue
        out.append((t, seq))
        t += FRAME_MS
        seq += 1
    return out


def run_seq(pattern, pre_frames, margin_kind, plc):
    """As run(), but the arrivals carry a frame sequence so a gap is visible."""
    if margin_kind.startswith("fixed:"):
        n = int(margin_kind.split(":")[1])
        margin_fn = lambda: n
    else:
        margin_fn = lambda: vc._adaptive_margin_frames(SENDER)
    pipe = Pipeline(pre_frames, margin_fn, plc=plc)
    t0 = pattern[0][0]
    for t, seq in pattern:
        pipe.on_packet(t, seq)
    if pipe.first_heard is None:
        return None, pipe.starvations
    return pipe.first_heard - t0, pipe.starvations


print()
print("=" * 74)
print("HOLES, NOT LAG (unreliable leg: a lost PA packet is a permanent hole)")
print("=" * 74)

LOSSES = (("loss 1%", 1.0, 11), ("loss 3%", 3.0, 22), ("loss 5%", 5.0, 33))
# The sequenced rows use the SHIPPING reserve, not a copy of its number: the
# whole point of the table is that this reserve only survives because a
# listener conceals the hole, so the two must not be able to drift apart.
_SEQ_RESERVE = vc.MEGAPHONE_MARGIN_SEQUENCED
LOSS_CONFIGS = [
    ("v1.6 pre6+m6 (240ms)", 6, "fixed:6", False),
    ("now pre2+m6 (160ms)", 2, "fixed:6", False),
    (f"pre2+m{_SEQ_RESERVE}, no PLC (80ms)", 2, f"fixed:{_SEQ_RESERVE}", False),
    (f"pre2+m{_SEQ_RESERVE} + PLC (80ms)", 2, f"fixed:{_SEQ_RESERVE}", True),
]
print()
print("  latency / starvations over 20 s, with holes")
print(f"  {'config':<28} " + " ".join(f"{n:>15}" for n, _, _ in LOSSES))
loss_cells = {}
for label, pre, mk, plc in LOSS_CONFIGS:
    cells = []
    starvs = []
    for _n, pct, seed in LOSSES:
        lat, starv = run_seq(arrivals_lossy(20.0, pct, seed), pre, mk, plc)
        starvs.append(starv)
        cells.append(f"{lat:6.0f}ms/{starv:<2d}" if lat is not None else "   n/a  ")
    loss_cells[label] = starvs
    print(f"  {label:<28} " + " ".join(f"{c:>15}" for c in cells))

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


# Production's PA floor is the sum of the shipped pair: the start gate from
# libs/voice_chat plus the fixed six-frame source margin. tests/
# test_voice_chat_jitter.py pins both numbers.
steady_shipped, _ = run(
    arrivals_steady(4.0),
    vc.MegaphoneJitterBuffer.PRE_BUFFER_FRAMES,
    f"fixed:{vc._megaphone_margin_frames('production')}",
)
shipped_floor_ms = ((vc.MegaphoneJitterBuffer.PRE_BUFFER_FRAMES
                     + vc._megaphone_margin_frames("production")) * FRAME_MS)
check("steady: shipped PA floor is gate + source margin",
      steady_shipped is not None and steady_shipped == shipped_floor_ms,
      f"{steady_shipped:.0f}ms vs floor {shipped_floor_ms:.0f}ms")
check("the PA no longer pays the reserve twice (floor under 240ms)",
      shipped_floor_ms < 240.0, f"{shipped_floor_ms:.0f}ms")

# Keeping the six-frame source margin is what buys back the cut: the same
# patterns, the same starvation counts as the old double reserve, 80ms less
# start latency. Trimming the margin instead is what costs dropouts.
for _pn, _pat in (("spikey 60ms", arrivals_spikey(10.0, 60.0, 2000, 1)),
                  ("spikey 100ms", arrivals_spikey(12.0, 100.0, 3000, 7))):
    _n_lat, _n_starv = run(
        _pat,
        vc.MegaphoneJitterBuffer.PRE_BUFFER_FRAMES,
        f"fixed:{vc._megaphone_margin_frames('production')}",
    )
    _o_lat, _o_starv = run(_pat, 6, "fixed:6")
    _m_lat, _m_starv = run(
        _pat,
        vc.MegaphoneJitterBuffer.PRE_BUFFER_FRAMES,
        "fixed:3",
    )
    check(f"{_pn}: shipped pair starves no more than the old double reserve",
          _n_starv <= _o_starv,
          f"shipped {_n_starv} vs old {_o_starv}")
    check(f"{_pn}: the smaller gate really is cheaper",
          _n_lat is not None and _n_lat < _o_lat,
          f"{_n_lat:.0f}ms vs {_o_lat:.0f}ms")
    check(f"{_pn}: trimming the source margin instead costs dropouts",
          _m_starv > _n_starv,
          f"margin3 {_m_starv} vs shipped {_n_starv}")

# unit checks on the real margin math
# Normal voice chat's cushion: one frame on a clean link (the latency the
# ear pays on every frame of the burst), grown only by measured jitter.
vc._speaker_jitter_ms["u"] = 0.0
check("adaptive margin = 1 frame at 0ms jitter (normal voice)",
      vc._adaptive_margin_frames("u") == 1)
vc._speaker_jitter_ms["u"] = 45.0
check("adaptive margin = 3 frames at 45ms jitter",
      vc._adaptive_margin_frames("u") == 3)
vc._speaker_jitter_ms["u"] = 500.0
check("adaptive margin capped at 6 frames",
      vc._adaptive_margin_frames("u") == 6)
check("PA margin holds the six-frame reserve for a LEGACY sender",
      vc._megaphone_margin_frames("u") == vc.MEGAPHONE_MARGIN_LEGACY == 6)
# ... and the small one for a sender whose frames carry their position, which
# is the case the loss table accepts below (a concealed hole, not a bigger
# reserve, is what pays for the 80 ms pair).
vc._megaphone_sequenced_senders.add("u")
check("PA margin pays the two-frame reserve for a SEQUENCED sender",
      vc._megaphone_margin_frames("u") == vc.MEGAPHONE_MARGIN_SEQUENCED == 2)
vc._megaphone_sequenced_senders.discard("u")
check("the loss table's sequenced rows use the shipping reserve",
      _SEQ_RESERVE == vc.MEGAPHONE_MARGIN_SEQUENCED,
      f"table {_SEQ_RESERVE} vs shipped {vc.MEGAPHONE_MARGIN_SEQUENCED}")

# The shipped pre-buffer must stay small enough that the two cushions are not
# one reserve counted twice, yet large enough to keep an arrival-jitter
# reserve in the queue. 0 starvations on the steady and jittery patterns is
# the condition for accepting it.
vc._jitter_buffers.pop("__cand__", None)
pre = vc.MegaphoneJitterBuffer.PRE_BUFFER_FRAMES
check("PA pre-buffer is one 20ms frame or two, not a second six-frame reserve",
      pre in (1, 2), f"{pre} frames")

# The loss table is the acceptance test for the sequence + PLC work: it must
# show that the reserve is what an unconcealed hole costs, and that concealment
# - not a smaller number - is what makes the smaller pair survivable.
_shipped_holes = sum(loss_cells["v1.6 pre6+m6 (240ms)"])
_now_holes = sum(loss_cells["now pre2+m6 (160ms)"])
_cheap_holes = sum(loss_cells["pre2+m2, no PLC (80ms)"])
_plc_holes = sum(loss_cells["pre2+m2 + PLC (80ms)"])
check("holes: cutting the reserve without concealment costs dropouts",
      _cheap_holes > _shipped_holes,
      f"pre2+m2 {_cheap_holes} vs v1.6 {_shipped_holes}")
check("holes: concealment keeps the smaller reserve as fed as the shipped pair",
      _plc_holes <= _shipped_holes,
      f"pre2+m2+PLC {_plc_holes} vs v1.6 {_shipped_holes}")
check("holes: the gate cut shipped on 2026-09-30 does not cost dropouts",
      _now_holes <= _shipped_holes,
      f"pre2+m6 {_now_holes} vs v1.6 {_shipped_holes}")
for _pn, _pat in (("steady", patterns["steady (exact 20ms)"]),
                  ("jittery", patterns["jittery (+/-12ms)"])):
    _lat, _starv = run(_pat, pre, f"fixed:{vc._megaphone_margin_frames('production')}")
    check(f"{_pn}: shipped pair does not starve with a small pre-buffer",
          _starv == 0, f"{_starv} starvation(s)")

print()
print(f"RESULT: {passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
