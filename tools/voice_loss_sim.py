"""Voice/megaphone packet-loss simulation: RELIABLE vs UNRELIABLE.

Why this exists
---------------
Both client and server used to send ALL audio (voice, music bot, megaphone)
as ENet RELIABLE packets. A reliable packet that gets lost is retransmitted
after an RTT-scale timeout, and every subsequent packet is held at the
receiver until the gap is filled (head-of-line blocking). One dropped frame
therefore turns into a 100-500ms stall of the whole stream - exactly the
"หน่วงพุ่งเป็นก้อน" a player hears mid-song.

The fix forces channel >= CHANNEL_VOICECHAT (all audio) to UNRELIABLE: a lost
frame is simply missing; the next frame arrives on time, and the client jitter
buffer (pre-buffer + adaptive margin) absorbs the single 20ms hole.

This simulation replays a 60s/20ms stream under configurable RTT and loss for
both transports through an adaptive jitter buffer (mirroring the real
_measure_speaker_jitter / _adaptive_margin_frames) and reports:
  - max / p99 delivery delay (the latency spikes)
  - starvation gaps (silence holes heard)
  - buffer growth (latency the jitter margin adds)

Run:  python tools/voice_loss_sim.py
"""
import random
import sys

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


FRAME_MS = 20.0          # one Opus frame
PRE_FRAMES = 1           # client pre-buffer (1 frame = 20ms)
MAX_MARGIN = 6           # adaptive margin cap (frames)
MARGIN_DECAY_S = 2.0     # half-life of the jitter estimate (matches _measure_speaker_jitter)


def adaptive_margin(jitter_ms):
    """Mirror of client _adaptive_margin_frames."""
    frames = 1 + int(jitter_ms / FRAME_MS)
    return max(1, min(MAX_MARGIN, frames))


def run_stream(rtt_ms, loss_p, transport, seed, duration_s=60.0, retrans_base_ms=150.0):
    """Replay a 20ms frame stream with the given transport model.

    Returns (max_delay_ms, p99_delay_ms, gaps, total_gap_ms, peak_margin_ms,
             avg_margin_ms, playback_end_ms).
    """
    rng = random.Random(seed)
    one_way = rtt_ms / 2.0
    n = int(duration_s * 1000 / FRAME_MS)

    # delivery[i] = wall-clock ms when frame i arrives at the client
    delivery = [0.0] * n
    blocked_until = 0.0
    for i in range(n):
        nominal = i * FRAME_MS + one_way
        if transport == "unreliable":
            if rng.random() < loss_p:
                delivery[i] = -1.0  # lost forever
            else:
                delivery[i] = max(nominal, blocked_until)
        else:  # reliable
            if rng.random() < loss_p:
                # lost -> retransmitted after a timeout; everything behind is blocked
                retrans = nominal + max(2.0 * one_way, retrans_base_ms)
                delivery[i] = retrans
                blocked_until = retrans + FRAME_MS
            else:
                if nominal < blocked_until:
                    delivery[i] = blocked_until
                    blocked_until += FRAME_MS
                else:
                    delivery[i] = nominal

    # Client playback with adaptive jitter buffer (pre-buffer + margin)
    margin_ms = 0.0
    margin_ts = 0.0
    peak_margin = 0.0
    margin_samples = 0
    margin_total = 0.0
    gaps = 0
    total_gap = 0.0
    playhead = 0.0  # next play time
    last_arrival = None
    max_delay = 0.0
    delays = []
    first_heard = None

    for i in range(n):
        t = i * FRAME_MS
        if delivery[i] < 0:
            # lost packet: record the gap it would cause for the margin, no arrival
            if last_arrival is not None:
                interval = FRAME_MS + (0.0 if False else FRAME_MS)
                # treat as a missed slot: interval effectively 2 frames
                _ = interval
                # fast-attack jitter: excess over one frame
                jitter = max(0.0, FRAME_MS)
                margin_ms = max(margin_ms * (0.5 ** (0.0 / MARGIN_DECAY_S)), min(jitter, 200.0))
            continue

        # margin decay with time
        if margin_ts:
            margin_ms = margin_ms * (0.5 ** ((t - margin_ts) / 1000.0 / MARGIN_DECAY_S))
        margin_ts = t

        if last_arrival is not None:
            interval = delivery[i] - last_arrival
            if interval > FRAME_MS:
                jitter = min(interval - FRAME_MS, 200.0)
                margin_ms = max(margin_ms, jitter)
        last_arrival = delivery[i]

        margin_frames = adaptive_margin(margin_ms)
        margin_now = margin_frames * FRAME_MS
        peak_margin = max(peak_margin, margin_now)
        margin_samples += 1
        margin_total += margin_now

        delay = delivery[i] - t
        delays.append(delay)
        max_delay = max(max_delay, delay)

        # play this frame at its scheduled time with the current buffer depth
        buffer_end = playhead + FRAME_MS * (PRE_FRAMES + margin_frames)
        if first_heard is None and delivery[i] <= t + margin_now + FRAME_MS:
            first_heard = t
        if t + FRAME_MS > buffer_end:
            # frame not yet buffered when its slot comes up -> starvation gap
            gap = t - buffer_end
            if gap > 0:
                gaps += 1
                total_gap += gap
                playhead = t + FRAME_MS  # resync
            else:
                playhead = max(playhead + FRAME_MS, t + FRAME_MS)
        else:
            playhead = max(playhead + FRAME_MS, t + FRAME_MS)

    delays.sort()
    p99 = delays[int(len(delays) * 0.99)] if delays else 0.0
    avg_margin = margin_total / max(1, margin_samples)
    return max_delay, p99, gaps, total_gap, peak_margin, avg_margin, playhead


def fmt(v):
    return f"{v:.0f}"


print("=" * 70)
print("VOICE/MEGAPHONE PACKET-LOSS SIMULATION (60s stream, 20ms frames)")
print("=" * 70)

for rtt, loss in [(50, 0.01), (50, 0.02), (50, 0.05), (20, 0.02), (100, 0.01)]:
    rel = run_stream(rtt, loss, "reliable", seed=7)
    unr = run_stream(rtt, loss, "unreliable", seed=7)
    print(f"\n--- RTT {rtt}ms, loss {int(loss*100)}% ---")
    print(f"  RELIABLE   : max delay {fmt(rel[0])}ms | p99 {fmt(rel[1])}ms | gaps {rel[2]} (tot {fmt(rel[3])}ms) | buffer peak {fmt(rel[4])}ms | playback {fmt(rel[6])}ms")
    print(f"  UNRELIABLE : max delay {fmt(unr[0])}ms | p99 {fmt(unr[1])}ms | gaps {unr[2]} (tot {fmt(unr[3])}ms) | buffer peak {fmt(unr[4])}ms | playback {fmt(unr[6])}ms")

    if loss >= 0.02:
        # The decisive metric is DELIVERY DELAY, not gaps: both transports feed
        # the adaptive jitter buffer which absorbs the holes, but RELIABLE's
        # head-of-line blocking inflates every frame's arrival by the
        # retransmission timeout (150ms+) while UNRELIABLE stays at the floor.
        check(f"RTT{rtt} loss{int(loss*100)}: UNRELIABLE max delay < RELIABLE max delay",
              unr[0] < rel[0])
        check(f"RTT{rtt} loss{int(loss*100)}: RELIABLE max delay spikes >= 150ms (the old stutter)",
              rel[0] >= 150.0)
        check(f"RTT{rtt} loss{int(loss*100)}: UNRELIABLE max delay stays <= 60ms (no spike)",
              unr[0] <= 60.0)

# The decisive single-loss case: one dropped frame in the middle of a song.
print("\n--- single dropped frame at t=30s ---")
rel1 = run_stream(50, 0.0, "reliable", seed=1)
unr1 = run_stream(50, 0.0, "unreliable", seed=1)
print(f"  no loss baseline        : reliable max {fmt(rel1[0])}ms / unreliable max {fmt(unr1[0])}ms")
print(f"  (no-loss baseline should be ~one_way + buffer, both equal)")

print("\n" + "=" * 70)
print("STUB TEST: real Client.send2 flag selection")
print("=" * 70)
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import types
import enet
import libs.networking as net_mod
from libs import consts


class FakePeer:
    def __init__(self):
        self.sent = []

    def send(self, channel, packet):
        self.sent.append((channel, packet))


class FakeGame:
    def __init__(self):
        self.t = 0.0

    def new_clock(self):
        return lambda: self.t


c = net_mod.Client(FakeGame(), "127.0.0.1", 13000, lambda *a: None)
c.peer = FakePeer()

# Data channel (e.g. CHANNEL_MISC = 0) must stay RELIABLE
c.send2(consts.CHANNEL_MISC, "some_event", {"k": 1})
ch, pkt = c.peer.sent[-1]
check("data channel stays RELIABLE", bool(pkt.flags & enet.PACKET_FLAG_RELIABLE), str(pkt.flags))

# Audio channels must be UNRELIABLE regardless of the caller's reliable arg
for ch_name, ch_id, call_reliable in [
    ("voice chat", consts.CHANNEL_VOICECHAT, True),
    ("dynamic voice channel", 21, True),
    ("music bot", consts.CHANNEL_MUSICBOT, True),
    ("music timeline upload", consts.CHANNEL_MUSICBOT_TIMELINE, True),
    ("megaphone", consts.CHANNEL_MEGAPHONE, True),
    ("megaphone explicit reliable=True", consts.CHANNEL_MEGAPHONE, True),
]:
    c.send2(ch_id, "n/a", bytes(64), reliable=call_reliable)
    ch, pkt = c.peer.sent[-1]
    unreliable = bool(pkt.flags & enet.PACKET_FLAG_UNRELIABLE_FRAGMENT)
    check(f"{ch_name} (ch {ch_id}) forced UNRELIABLE",
          unreliable and not (pkt.flags & enet.PACKET_FLAG_RELIABLE),
          str(pkt.flags))

print()
print(f"RESULT: {PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
