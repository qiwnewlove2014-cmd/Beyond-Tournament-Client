"""PA spatial-image stability test.

The stereo image between two PA cabinets comes from the per-speaker
propagation-delay STAGGER (their queued silence offsets). When the stagger
flips, the image snaps between separated (1+ frame apart) and merged (same
quantized 20ms frame) - the 'แยกบ้าง รวมบ้าง' complaint.

FIX UNDER TEST: the propagation-delay baseline is frozen at the stream's
start position. Movement re-basing + recovery re-padding used to change the
stagger on every walk, so standing between two cabinets (equal distances ->
equal quantized delays -> stagger 0) suddenly sounded mono, then separated
again after a step.

This drives the REAL queue_and_delay_frame() with a fake gameplay and a
walking listener, and asserts the per-speaker frames_delay never changes
after the stream starts (stable image) while the gains keep tracking.

Run:  python tools/spatial_image_test.py
"""
import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import cyal

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


class FakeBuf:
    def __init__(self):
        self.queued_data = None

    def set_data(self, data, *a, **k):
        self.queued_data = bytes(data)


class FakeSrc:
    def __init__(self):
        self.state = cyal.SourceState.STOPPED
        self.buffers_queued = 0
        self.buffers_processed = 0
        self.gain = 0.0
        self.queued_frames = []

    def queue_buffers(self, buf):
        self.buffers_queued += 1
        self.queued_frames.append(buf)

    def unqueue_buffers(self):
        if self.buffers_processed > 0:
            self.buffers_processed -= 1
            return FakeBuf()
        return None

    def play(self):
        self.state = cyal.SourceState.PLAYING


class Focus:
    def __init__(self, x, y, z):
        self.x, self.y, self.z = x, y, z


class FakeMap:
    minz = 0.0


class FakeMega:
    def __init__(self):
        # Two cabinets 30 units apart; each gets primary + ground-reflection
        # source (idx 2*i, 2*i+1). Static map delay 0 so only propagation
        # contributes to the stagger.
        self.speaker_data = [
            {'position': (-15.0, 0.0, 1.0), 'delay': 0.0},
            {'position': (15.0, 0.0, 1.0), 'delay': 0.0},
        ]
        self.player_sources = {}


class FakeGameplay:
    concert_spectator_mode = False

    def __init__(self):
        self.camera = types.SimpleNamespace(focus_object=Focus(0.0, 0.0, 1.0))
        self.map = FakeMap()
        self.megaphone = FakeMega()
        self.voice_chat_buffer_pool = []
        self.game = types.SimpleNamespace(
            audio_mngr=types.SimpleNamespace(
                context=types.SimpleNamespace(gen_buffer=lambda: FakeBuf()),
                efx=types.SimpleNamespace(send=lambda *a, **k: None),
            )
        )


import libs.voice_chat as vc_mod

FRAME = bytes(1920)


def one_packet(gp, sender, sources):
    """Feed one packet through the real queue_and_delay_frame."""
    vc_mod.queue_and_delay_frame(gp, sender, sources, FRAME)


print("=" * 66)
print("Stereo image stability while the listener walks")
print("=" * 66)

gp = FakeGameplay()
sender = "band_member"
srcs = [FakeSrc(), FakeSrc(), FakeSrc(), FakeSrc()]  # A, A-refl, B, B-refl
# Pre-queue a couple frames so the sources are not 'starved' at start.
for s in srcs:
    s.buffers_queued = 2

# Stream starts with the listener at the origin.
one_packet(gp, sender, srcs)
baseline = list(vc_mod._speaker_current_delays[sender])
stagger0 = abs(baseline[0] - baseline[2])  # primary A vs primary B
print(f"  stream start: frames_delay={baseline}  stagger={stagger0}")

# Walk a long path across the venue: 60 steps, 1 unit each.
delays_seen = set()
stagger_seen = set()
for step in range(1, 61):
    gp.camera.focus_object = Focus(step, 0.0, 1.0)  # walks from x=0 to x=60
    # Steady streaming: each packet replaces one consumed frame.
    for s in srcs:
        s.buffers_queued = max(1, s.buffers_queued)
    one_packet(gp, sender, srcs)
    cur = list(vc_mod._speaker_current_delays[sender])
    delays_seen.add(tuple(cur))
    stagger_seen.add(abs(cur[0] - cur[2]))

print(f"  distinct delay configs seen while walking: {len(delays_seen)}")
print(f"  distinct staggers seen: {sorted(stagger_seen)}")

check("frames_delay NEVER changes after stream start (frozen baseline)",
      len(delays_seen) == 1)
check("stagger stays exactly as at stream start (image never merges/splits)",
      stagger_seen == {stagger0})

# Fresh stream at a different position still gets a (stable) baseline.
gp2 = FakeGameplay()
gp2.camera.focus_object = Focus(25.0, 0.0, 1.0)  # off-center start
srcs2 = [FakeSrc(), FakeSrc(), FakeSrc(), FakeSrc()]
for s in srcs2:
    s.buffers_queued = 2
vc_mod._speaker_current_delays.pop(sender, None)
vc_mod._speaker_initial_delays.pop(sender, None)
one_packet(gp2, sender, srcs2)
base2 = list(vc_mod._speaker_current_delays[sender])
delays2 = {tuple(base2)}
for step in range(1, 31):
    gp2.camera.focus_object = Focus(25.0 - step, 0.0, 1.0)
    for s in srcs2:
        s.buffers_queued = max(1, s.buffers_queued)
    one_packet(gp2, sender, srcs2)
    delays2.add(tuple(vc_mod._speaker_current_delays[sender]))
check("second stream (off-center) also frozen while walking",
      len(delays2) == 1)

# Underrun recovery rebuilds the SAME stagger (stable image after a gap).
gp3 = FakeGameplay()
srcs3 = [FakeSrc(), FakeSrc(), FakeSrc(), FakeSrc()]
for s in srcs3:
    s.buffers_queued = 2
vc_mod._speaker_current_delays.pop(sender, None)
vc_mod._speaker_initial_delays.pop(sender, None)
one_packet(gp3, sender, srcs3)
base3 = tuple(vc_mod._speaker_current_delays[sender])
# Simulate a gap: all sources run dry, then packets resume.
for s in srcs3:
    s.buffers_queued = 0
one_packet(gp3, sender, srcs3)  # any_starved=True -> recovery re-pad
recovered = tuple(vc_mod._speaker_current_delays[sender])
check("underrun recovery rebuilds the SAME stagger (no image jump)",
      recovered == base3)

print()
print(f"RESULT: {PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
