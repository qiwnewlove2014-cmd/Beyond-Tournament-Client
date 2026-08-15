"""Adaptive voice-channel jitter padding tests (drives the REAL recieve2).

The old normal-voice receive path padded EVERY cold-start burst with 5 x 20ms
of silence (100ms) - so a guitarist's first strum after a pause, or any new
burst after a gap, landed a full 100ms late even on a clean LAN. This test
proves the new per-channel adaptive margin (the same fast-attack / decay math
the megaphone uses):

  - clean steady stream  -> 2 frame pad (40ms) instead of 5 (100ms)
  - a jitter spike       -> the margin grows (2 + jitter/20 frames)
  - a >180ms silence gap -> the next burst resets to the 40ms minimum
    (a pause between strums must NOT look like jitter)

Run:  python tools/voice_adaptive_pad_test.py
"""
import os
import sys
import types as _types

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


FRAME_BYTES = 1920          # 20ms mono16 @ 48kHz
DATA_BYTE = 0x55            # distinguishable from silence (0x00)


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
        self.queued = []          # list of bytes, silence = all zeros

    def queue_buffers(self, buf):
        self.buffers_queued += 1
        self.queued.append(buf.queued_data)

    def unqueue_buffers(self):
        if self.buffers_processed > 0:
            self.buffers_processed -= 1
            self.buffers_queued -= 1
            return FakeBuf()
        return None

    def play(self):
        self.state = cyal.SourceState.PLAYING


class FakeContext:
    def gen_buffer(self):
        return FakeBuf()

    def batch(self):
        import contextlib
        return contextlib.nullcontext()


class FakeGameplay:
    def __init__(self, src):
        self.player = _types.SimpleNamespace(dead=False, has_radio=False)
        self.megaphone = None
        self.voice_channels = {
            20: _types.SimpleNamespace(has_radio=False, vc_source=src,
                                       radio_source=None),
        }


class FakeGame:
    def __init__(self):
        self.audio_mngr = _types.SimpleNamespace(context=FakeContext())


import libs.voice_chat as vc


def silence_count(src):
    """How many all-zero 20ms buffers were queued (the padding frames)."""
    n = 0
    for b in src.queued:
        if b and len(b) == FRAME_BYTES and all(v == 0 for v in b):
            n += 1
    return n


def data_count(src):
    n = 0
    for b in src.queued:
        if b and len(b) == FRAME_BYTES and b[0] == DATA_BYTE:
            n += 1
    return n


def make_compression():
    comp = vc.voice_chat_compression.__new__(vc.voice_chat_compression)
    comp.game = FakeGame()
    comp.decoder = _types.SimpleNamespace(
        decode=lambda d: bytes([DATA_BYTE]) * FRAME_BYTES)
    comp.channel = 20
    return comp


def reset_state():
    vc._voice_last_pkt.clear()
    vc._speaker_jitter_ms.clear()
    vc._speaker_jitter_ts.clear()


def feed(comp, src, gameplay, t):
    """Deliver one 20ms voice packet at wall-clock t (seconds)."""
    vc.time.time = lambda: t
    payload = bytearray(4)  # 1 byte channel tag + 3 opus bytes (decoder fakes)
    comp.recieve2(payload, src, None, 20, gameplay)


print("=" * 66)
print("TEST 1: clean steady stream -> 2-frame (40ms) pad, not 5 (100ms)")
print("=" * 66)
reset_state()
src = FakeSrc()
gameplay = FakeGameplay(src)
comp = make_compression()
feed(comp, src, gameplay, 0.000)
# First packet is a cold start: 2 silence + 1 data
check("cold start pads 2 silence frames (was 5 = 100ms)",
      silence_count(src) == 2, f"silence={silence_count(src)}")
check("first data frame queued after the pad", data_count(src) == 1)
# Steady 20ms stream: no further padding, frames queue 1:1
for i in range(1, 6):
    feed(comp, src, gameplay, i * 0.020)
check("5 more feeds -> 5 data frames, no extra silence",
      data_count(src) == 6 and silence_count(src) == 2,
      f"data={data_count(src)} silence={silence_count(src)}")
check("margin stays 2 frames (40ms) on a clean network",
      vc._adaptive_margin_frames("vc:20") == 2,
      f"margin={vc._adaptive_margin_frames('vc:20')}")

print()
print("=" * 66)
print("TEST 2: a jitter spike grows the margin (fast attack)")
print("=" * 66)
reset_state()
src = FakeSrc()
gameplay = FakeGameplay(src)
comp = make_compression()
feed(comp, src, gameplay, 0.000)
feed(comp, src, gameplay, 0.020)   # steady
# One packet arrives 40ms late (interval 60ms instead of 20ms):
feed(comp, src, gameplay, 0.080)
check("jitter estimate jumped to ~40ms", vc._speaker_jitter_ms["vc:20"] >= 39.5,
      f"jitter={vc._speaker_jitter_ms.get('vc:20')}")
check("margin grew to 2 + 40/20 = 4 frames",
      vc._adaptive_margin_frames("vc:20") == 4,
      f"margin={vc._adaptive_margin_frames('vc:20')}")
# A cold start during the jittery period (interval still 60ms, < 180ms so it
# is NOT a fresh transmission) re-pads with the grown margin:
src2 = FakeSrc()
gameplay2 = FakeGameplay(src2)
feed(comp, src2, gameplay2, 0.140)
check("next cold burst pads 4 frames (80ms) to absorb the jitter",
      silence_count(src2) == 4, f"silence={silence_count(src2)}")

print()
print("=" * 66)
print("TEST 3: a >180ms gap resets to the 40ms minimum (strum pause != jitter)")
print("=" * 66)
reset_state()
src = FakeSrc()
gameplay = FakeGameplay(src)
comp = make_compression()
feed(comp, src, gameplay, 0.000)
feed(comp, src, gameplay, 0.020)
feed(comp, src, gameplay, 0.100)   # 60ms excess -> margin 5
check("margin is 5 before the gap", vc._adaptive_margin_frames("vc:20") == 5)
# Silence for 300ms (the guitarist pauses between strums), then a new burst:
src2 = FakeSrc()
gameplay2 = FakeGameplay(src2)
feed(comp, src2, gameplay2, 0.400)
check("after a gap the margin resets to 2 frames (40ms)",
      vc._adaptive_margin_frames("vc:20") == 2,
      f"margin={vc._adaptive_margin_frames('vc:20')}")
check("new burst pads exactly 2 silence frames",
      silence_count(src2) == 2, f"silence={silence_count(src2)}")

print()
print("=" * 66)
print("BEFORE / AFTER (cold-start pad per burst, clean LAN)")
print("=" * 66)
print("  OLD fixed: 5 x 20ms = 100ms  (every strum burst / new sentence)")
print("  NEW clean: 2 x 20ms =  40ms  (-60ms, stable music/voice floor)")
print("  NEW under jitter: margin grows (e.g. 40ms spike -> 4 x 20ms = 80ms)")
print("  NEW after pause: back to 40ms (a pause never counts as jitter)")
print()
print(f"RESULT: {PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
