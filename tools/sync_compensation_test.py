"""Song/cover sync compensation tests (drives the REAL _feed_local_megaphone_direct).

The owner's local 'music' PA monitor must play 2RTT + 40ms late (piano/drums
are MIDI note events: song heard one leg = RTT+40ms, then the note travels one
round trip = RTT, so they arrive at the owner 2RTT+40ms behind the zero-latency
local song). Instruments ('guitar', 'mic', ...) stay instant - their players
anchor to the delayed song themselves.

Run:  python tools/sync_compensation_test.py
"""
import os
import sys
import time
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
        self.gain = 1.0
        self.queued_data = []

    def queue_buffers(self, buf):
        self.buffers_queued += 1
        self.queued_data.append(buf.queued_data)

    def unqueue_buffers(self):
        if self.buffers_processed > 0:
            self.buffers_processed -= 1
            return FakeBuf()
        return None

    def play(self):
        self.state = cyal.SourceState.PLAYING


class FakeMega:
    def __init__(self):
        self.speaker_data = []
        self.player_sources = {}

    def get_megaphone_player_sources(self, key):
        if key not in self.player_sources:
            srcs = [FakeSrc(), FakeSrc()]
            self.player_sources[key] = {
                "sources": srcs,
                "filters": [],
                "targets_vol": [0.5, 0.5],
                "currents_vol": [0.5, 0.5],
            }
        return self.player_sources[key]["sources"]

    def update_megaphone_audio(self):
        pass


class FakeGameplay:
    concert_spectator_mode = False

    def __init__(self):
        self.player = _types.SimpleNamespace(id="local", name="local")
        self.megaphone = FakeMega()
        self.game = _types.SimpleNamespace(
            audio_mngr=_types.SimpleNamespace(
                context=_types.SimpleNamespace(gen_buffer=lambda: FakeBuf()),
                efx=_types.SimpleNamespace(send=lambda *a, **k: None),
            )
        )


import libs.voice_chat as vc


def feed(gp, producer, frame_tag):
    """Feed one 20ms frame tagged with a distinct low-amplitude pattern."""
    frame = bytes([frame_tag]) * 1920
    vc._feed_local_megaphone_direct(gp, frame, producer=producer)


def queued_markers(gp, key):
    """The frame tags each source actually played (tail bytes survive fade/limit)."""
    entry = gp.megaphone.player_sources[key]
    src = entry["sources"][0]
    markers = []
    for data in src.queued_data:
        if data and len(data) >= 64:
            markers.append(data[-32])
    return markers


def reset_state():
    vc._comp_fifos.clear()
    vc._comp_last_feed.clear()
    vc._measured_rtt_ms = None


print("=" * 66)
print("TEST 1: _compensation_frames math")
print("=" * 66)
reset_state()
vc._measured_rtt_ms = None
check("no RTT measured -> 40ms floor -> 2 frames", vc._compensation_frames() == 2)
vc._measured_rtt_ms = 40.0
check("RTT 40ms -> 2*40+40=120ms -> 6 frames", vc._compensation_frames() == 6)
vc._measured_rtt_ms = 500.0
check("huge RTT capped at 12 frames (240ms)", vc._compensation_frames() == 12)
vc._measured_rtt_ms = 0.0
check("RTT 0 -> 40ms floor -> 2 frames", vc._compensation_frames() == 2)

print()
print("=" * 66)
print("TEST 2: local 'music' monitor is delayed by the compensation FIFO")
print("=" * 66)
reset_state()
vc._measured_rtt_ms = 40.0   # -> 6 frames of delay
gp = FakeGameplay()
for i in range(1, 6):
    feed(gp, "music", i)
key = "local:music"
markers = queued_markers(gp, key)
check(
    "first 5 feeds queued NOTHING (buffer still filling)",
    len(markers) == 0,
    f"queued {len(markers)}",
)
for i in range(6, 13):
    feed(gp, "music", i)
markers = queued_markers(gp, key)  # re-read after the full 12 feeds
# After 12 feeds with a 6-frame buffer, exactly 7 frames emitted (feeds 6..12)
check("7 frames emitted after 12 feeds (steady 1-in/1-out)", len(markers) == 7, f"{len(markers)}")
check(
    "emitted order == feed order, delayed by 6 (first emitted = feed #1)",
    markers == [1, 2, 3, 4, 5, 6, 7],
    str(markers),
)
# Steady state: 2 more feeds -> 2 more emissions, same delay preserved
for i in range(13, 15):
    feed(gp, "music", i)
markers = queued_markers(gp, key)
check(
    "steady state keeps 6-frame delay (emitted 8,9 after feeding 13,14)",
    markers[-2:] == [8, 9],
    str(markers[-4:]),
)

print()
print("=" * 66)
print("TEST 3: instruments/mic stay INSTANT (no compensation)")
print("=" * 66)
reset_state()
vc._measured_rtt_ms = 40.0
gp = FakeGameplay()
feed(gp, "guitar", 21)
feed(gp, "mic", 22)
feed(gp, "drums", 23)
check(
    "guitar queued immediately (feed #1)", queued_markers(gp, "local:guitar") == [21]
)
check("mic queued immediately", queued_markers(gp, "local:mic") == [22])
check(
    "any future instrument (drums) also instant",
    queued_markers(gp, "local:drums") == [23],
)

print()
print("=" * 66)
print("TEST 4: FIFO resets after a feed gap (pause/stop)")
print("=" * 66)
reset_state()
vc._measured_rtt_ms = 40.0
gp = FakeGameplay()
feed(gp, "music", 31)
feed(gp, "music", 32)
check("buffer filling (2 feeds, nothing emitted)", queued_markers(gp, "local:music") == [])
time.sleep(0.6)  # simulate a pause longer than the gap threshold
feed(gp, "music", 33)
check(
    "after a gap the buffer refills (feed 33 not emitted yet)",
    queued_markers(gp, "local:music") == [],
)
# 6-frame buffer needs 6 fills + 7 emits = 13 post-gap feeds to emit 7 frames
for i in range(34, 46):
    feed(gp, "music", i)
check(
    "post-gap steady state resumes (emits 33..39 in order)",
    queued_markers(gp, "local:music")[:7] == list(range(33, 40)),
    str(queued_markers(gp, "local:music")),
)

print()
print(f"RESULT: {PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
