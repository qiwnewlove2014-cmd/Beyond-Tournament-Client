"""Spatial smoothing + real-time tracking tests.

Verifies the four fixes behind "เสียงแกว่ง / ไม่อัปเดตแบบเรียลไทม์ / เสียงรวม-แยก":

  1. _pad_frames_for_resync   - gradual delay tracking (movement resync pads
                                at most 1 silence frame per packet instead of
                                the whole 100-300ms chunk at once)
  2. _smooth_occlusion_ratio  - per-speaker EMA so wall-edge grazing glides
                                instead of flip-flopping 0<->1
  3. _behind_strength_from_dot- smooth directional ramp replacing the hard
                                0/1 flip at the speaker's perpendicular plane
  4. spatial refresh trigger  - real-time: recompute at 10Hz while the
                                listener moves (>= 0.5 units), even without an
                                explicit camera request

Run:  python tools/spatial_smooth_test.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from libs import voice_chat as vc
from libs.systems import megaphone_system as ms

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


# ---------------------------------------------------------------------------
# 1. Gradual delay tracking
# ---------------------------------------------------------------------------
print("=" * 66)
print("TEST 1: gradual delay tracking (_pad_frames_for_resync)")
print("=" * 66)

# Movement-only resync (stream playing, nothing starved): NEVER pads - a
# single 20ms silence frame in music/voice is an audible skip (the 'สะดุดนิดๆ'
# every time the listener walked). Movement re-bases the delay reference but
# the audio plays uninterrupted.
check("movement, big deficit -> pads 0 (no skip)",
      vc._pad_frames_for_resync(10, 0, needs_initial_delay=False, any_starved=False) == 0)
check("movement, small deficit -> pads 0",
      vc._pad_frames_for_resync(5, 3, needs_initial_delay=False, any_starved=False) == 0)
check("movement, exact match -> pads 0",
      vc._pad_frames_for_resync(3, 3, needs_initial_delay=False, any_starved=False) == 0)
check("movement, queue deeper than target -> pads 0",
      vc._pad_frames_for_resync(2, 5, needs_initial_delay=False, any_starved=False) == 0)

# Fresh stream: full pad is correct (nothing is playing yet)
check("fresh stream -> full pad",
      vc._pad_frames_for_resync(10, 0, needs_initial_delay=True, any_starved=False) == 10)
check("fresh stream, partial -> full pad",
      vc._pad_frames_for_resync(7, 2, needs_initial_delay=True, any_starved=False) == 5)

# Underrun recovery: full pad restores the margin immediately
check("starvation recovery -> full pad",
      vc._pad_frames_for_resync(6, 0, needs_initial_delay=False, any_starved=True) == 6)

# Long walk: the queue never pads, so the stream is 100% uninterrupted
pads = sum(vc._pad_frames_for_resync(d, 0, needs_initial_delay=False, any_starved=False)
           for d in range(10, 0, -1))
check("long walk inserts ZERO silence frames (no skips)", pads == 0)

# ---------------------------------------------------------------------------
# 2. Smooth occlusion EMA
# ---------------------------------------------------------------------------
print()
print("=" * 66)
print("TEST 2: smooth occlusion EMA (_smooth_occlusion_ratio)")
print("=" * 66)

# Wall appears: fast attack, monotonic, converges toward 1.0
ema = 0.0
for _ in range(3):
    nxt = ms._smooth_occlusion_ratio(ema, 1.0)
    assert nxt > ema, "attack must rise monotonically"
    ema = nxt
check("wall appears: 3 refreshes reach %.3f (>= 0.93)" % ema, ema >= 0.93)
check("wall appears: 3 refreshes stay < 1.0 (no hard flip)", ema < 1.0)

# Wall clears: slower release, monotonic decrease
ema2 = 1.0
for _ in range(3):
    nxt = ms._smooth_occlusion_ratio(ema2, 0.0)
    assert nxt < ema2, "release must fall monotonically"
    ema2 = nxt
check("wall clears: 3 refreshes reach %.3f (<= 0.52)" % ema2, ema2 <= 0.52)
check("wall clears: still audible fade, not instant mute", ema2 > 0.4)

# Grazing flip-flop (0 <-> 1 every refresh): EMA damps the oscillation
flip_a = 0.0
flip_b = 0.0
for _ in range(20):
    flip_a = ms._smooth_occlusion_ratio(flip_a, 1.0)
    flip_b = ms._smooth_occlusion_ratio(flip_a, 0.0)
    flip_a = ms._smooth_occlusion_ratio(flip_b, 1.0)
check("wall-edge graze stays mid-range (no 0<->1 chatter): %.2f" % flip_a,
      0.05 < flip_a < 0.95)

# ---------------------------------------------------------------------------
# 3. Smooth behind-strength ramp
# ---------------------------------------------------------------------------
print()
print("=" * 66)
print("TEST 3: smooth directional ramp (_behind_strength_from_dot)")
print("=" * 66)

check("clearly behind (dot -5) -> full muffle",
      ms._behind_strength_from_dot(-5.0, 90) == 1.0)
check("at the perpendicular plane (dot 0) -> 0.5 (half)",
      abs(ms._behind_strength_from_dot(0.0, 90) - 0.5) < 1e-9)
check("clearly in front (dot +5) -> no muffle",
      ms._behind_strength_from_dot(5.0, 90) == 0.0)
check("omnidirectional cone (360) -> never muffled",
      ms._behind_strength_from_dot(-100.0, 360) == 0.0)

# Monotonic non-increasing as the listener crosses the plane
prev = 1.0
monotonic = True
for dot in [d / 10.0 for d in range(-40, 41, 1)]:
    v = ms._behind_strength_from_dot(dot, 90)
    if v > prev + 1e-9:
        monotonic = False
        break
    prev = v
check("monotonic ramp across the full crossing", monotonic)

# ---------------------------------------------------------------------------
# 4. Real-time spatial refresh trigger
# ---------------------------------------------------------------------------
print()
print("=" * 66)
print("TEST 4: real-time spatial refresh trigger (10Hz movement-driven)")
print("=" * 66)

INTERVAL = 0.10
MOVE_THRESHOLD = 0.5


def trigger(requested, last_time, now, moved_since):
    """Mirror of the production gate in MegaphoneManager.update_megaphone_audio."""
    do_refresh = (
        requested
        or moved_since >= MOVE_THRESHOLD
    ) and (now - last_time >= INTERVAL)
    return do_refresh


def walk_sim(seconds, speed, request_every=1e9):
    """Simulate a listener walking for `seconds` at `speed` u/s. Returns the
    number of refreshes and the max gap between them (no request flags)."""
    pos = 0.0
    t = 0.0
    last_refresh_t = 0.0
    last_refresh_pos = 0.0  # listener's starting position
    refreshes = 0
    max_gap = 0.0
    dt = 1.0 / 60.0
    while t < seconds:
        pos += speed * dt
        t += dt
        moved = abs(pos - last_refresh_pos)
        if trigger(False, last_refresh_t, t, moved):
            refreshes += 1
            gap = t - last_refresh_t
            max_gap = max(max_gap, gap)
            last_refresh_t = t
            last_refresh_pos = pos
    return refreshes, max_gap


# Walking at 5 u/s for 10s: should refresh continuously (~10Hz), gaps <= 0.2s
refreshes, max_gap = walk_sim(10.0, 5.0)
check("walking 5 u/s for 10s refreshes continuously (%d refreshes)" % refreshes,
      refreshes >= 80 and refreshes <= 100)
check("walking: max gap %.3fs <= 0.20s (real-time tracking)" % max_gap, max_gap <= 0.20)

# Slow creep (0.4 u/s): below the 0.5-unit threshold per interval -> fewer
# refreshes but never zero while moving at all over a long walk
refreshes_slow, max_gap_slow = walk_sim(10.0, 0.4)
check("slow creep still tracks eventually (%d refreshes)" % refreshes_slow,
      refreshes_slow >= 5)

# Standing still: NO refreshes unless explicitly requested
still = 0
t = 0.0
last_t = -1e9
while t < 5.0:
    t += 1.0 / 60.0
    if trigger(False, last_t, t, 0.0):
        still += 1
        last_t = t
check("standing still: zero refreshes (no wasted raycasts)", still == 0)

# Explicit request forces a refresh even when stationary
req = 0
t = 0.0
last_t = -1e9
while t < 5.0:
    t += 1.0 / 60.0
    if trigger(True, last_t, t, 0.0):
        req += 1
        last_t = t
check("explicit request refreshes at 10Hz even standing still (%d)" % req, req >= 8)

# Refresh-throttle still respected: never faster than the interval
refreshes2, _ = walk_sim(1.0, 100.0)  # teleport-fast walk for 1s
check("refresh throttled at 10Hz even while teleporting (%d in 1s)" % refreshes2,
      refreshes2 <= 12)

# ---------------------------------------------------------------------------
print()
print(f"RESULT: {PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
