"""Local PA simultaneous-feed simulation.

Why "music + talk at the same time" used to delay/stretch/crackle:

OLD: the music bot (20ms frames @ 20ms cadence) and the local mic (10ms chunks
@ 10ms cadence) BOTH called _feed_local_megaphone_direct with the SAME local
player id, queueing PCM straight into the SAME OpenAL source queue. The playhead
consumed 20ms of audio per 20ms, but the queue received 30ms per 20ms -> the
queue grew without bound (delay kept climbing while talking, drained when
speech stopped), and the content alternated music/voice 20ms slices (both
sounded stretched/squeezed with clicks at slice boundaries).

FIX: each local producer gets its OWN per-player source set
('<player>:<producer>'), so simultaneous streams play as SEPARATE OpenAL
sources that OpenAL mixes. Each stream keeps its own cadence and its own
queue: no interleaving, no queue growth, no added latency, and bursts are
absorbed by the direct queue exactly like the original music-only path.

Run:  python tools/local_pa_interleave_sim.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import types as _types
import struct
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


FRAME_BYTES = 1920   # 20ms mono16 @ 48kHz
HALF_BYTES = 960     # 10ms


# ---------------------------------------------------------------------------
# 1. Model of the OLD shared-queue behavior (why it broke)
# ---------------------------------------------------------------------------
def shared_queue_growth(seconds, music=True, mic=True, guitar=False):
    queue_bytes = 0.0
    t = 0.0
    dt = 1.0
    music_next = 0.0
    mic_next = 0.0
    guitar_next = 0.0
    max_backlog = 0.0
    while t < seconds * 1000:
        if music and t >= music_next:
            queue_bytes += FRAME_BYTES
            music_next += 20.0
        if mic and t >= mic_next:
            queue_bytes += HALF_BYTES
            mic_next += 10.0
        if guitar and t >= guitar_next:
            queue_bytes += FRAME_BYTES
            guitar_next += 20.0
        if int(t) % 20 == 0:
            queue_bytes = max(0.0, queue_bytes - FRAME_BYTES)
        max_backlog = max(max_backlog, queue_bytes / (FRAME_BYTES / 20.0))
        t += dt
    return max_backlog, queue_bytes / (FRAME_BYTES / 20.0)


print("=" * 66)
print("OLD shared-queue behavior (the bug)")
print("=" * 66)
mb, fb = shared_queue_growth(5.0)
print(f"  music(20ms) + mic(10ms) for 5s -> backlog {fb:.0f}ms")
check("old: shared queue backlog grows past 2s in 5s of talking", fb >= 1500)
mb2, fb2 = shared_queue_growth(5.0, music=True, mic=False)
check("old: music alone stays stable", fb2 < 50)
mb3, fb3 = shared_queue_growth(5.0, music=True, mic=True, guitar=True)
print(f"  music + mic + guitar for 5s -> backlog {fb3:.0f}ms")
check("old: 3 simultaneous feeds backlog fastest", fb3 >= fb)


# ---------------------------------------------------------------------------
# 2. Real-path test: per-producer source sets via _feed_local_megaphone_direct
# ---------------------------------------------------------------------------
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


class FakeMega:
    def __init__(self):
        self.speaker_data = []
        self.player_sources = {}
        self.eq_slot = None

    def get_megaphone_player_sources(self, key):
        if key not in self.player_sources:
            srcs = [FakeSrc(), FakeSrc()]
            self.player_sources[key] = {'sources': srcs, 'filters': [],
                                        'targets_vol': [0.5, 0.5],
                                        'currents_vol': [0.5, 0.5]}
        return self.player_sources[key]['sources']

    def update_megaphone_audio(self):
        pass


class FakeGameplay:
    concert_spectator_mode = False

    def __init__(self):
        self.player = _types.SimpleNamespace(id='local', gameplay=None)
        self.megaphone = FakeMega()
        self.game = _types.SimpleNamespace(
            audio_mngr=_types.SimpleNamespace(
                context=_types.SimpleNamespace(gen_buffer=lambda: FakeBuf()),
                efx=_types.SimpleNamespace(send=lambda *a, **k: None),
            )
        )


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


print()
print("=" * 66)
print("FIX: per-producer source sets (real _feed_local_megaphone_direct)")
print("=" * 66)

music_frame = struct.pack('<%dh' % 960, *([6000] * 960))
mic_chunk = struct.pack('<%dh' % 480, *([4000] * 480))

import libs.voice_chat as vc_mod

# Song/cover sync compensation: the local 'music' monitor is intentionally held
# back by _compensation_frames() (2RTT + 40ms, default 2 frames at RTT 0) so a
# remote cover performer lines up with the song. Reset it between scenarios; the
# cadence checks below assert the STEADY STATE (1 in / 1 out) after the initial
# fill, which is the property that matters (no drops, no growth).
def _reset_comp():
    vc_mod._comp_fifos.clear()
    vc_mod._comp_last_feed.clear()
    vc_mod._measured_rtt_ms = None  # -> 40ms floor -> 2 frames
    return vc_mod._compensation_frames()

# --- 2a. Music + mic simultaneously: TWO separate source sets ---
gp = FakeGameplay()
clock = _Clock()
vc_mod.time.time = clock
comp_frames = _reset_comp()

for step in range(50):  # 500ms
    clock.t = 1000.0 + step * 0.01
    if step % 2 == 0:
        vc_mod._feed_local_megaphone_direct(gp, music_frame, producer='music')
    vc_mod._feed_local_megaphone_direct(gp, mic_chunk, producer='mic')

music_srcs = gp.megaphone.player_sources['local:music']['sources']
mic_srcs = gp.megaphone.player_sources['local:mic']['sources']
check("music and mic got SEPARATE source sets (no shared queue)",
      music_srcs is not mic_srcs)
expected_music = 25 - (comp_frames - 1)
check("music stream kept its own cadence: %d of 25 frames (first %d held by sync comp) (%d)"
      % (expected_music, comp_frames - 1, music_srcs[0].buffers_queued),
      music_srcs[0].buffers_queued == expected_music)
check("mic stream fed immediately: 50 chunks in 500ms (%d)" % mic_srcs[0].buffers_queued,
      mic_srcs[0].buffers_queued == 50)

# Every music frame is a FULL music window (no slices stolen by voice).
music_ok = True
for buf in music_srcs[0].queued_frames:
    vals = struct.unpack('<%dh' % 960, bytes(buf.queued_data))
    if not all(v == 6000 for v in vals[96:]):  # skip 96-sample restart fade
        music_ok = False
        break
check("every music frame is full music (no interleaved voice slices)", music_ok)

# Steady state after the fill: each extra feed emits exactly one frame.
before = music_srcs[0].buffers_queued
clock.t += 0.02
vc_mod._feed_local_megaphone_direct(gp, music_frame, producer='music')
check("steady state after fill: 1 feed -> 1 emitted (no backlog growth)",
      music_srcs[0].buffers_queued == before + 1)

# --- 2b. Burst: comp_frames+1 music frames inside one tick are retained in order ---
gp2 = FakeGameplay()
clock2 = _Clock()
vc_mod.time.time = clock2
comp_frames = _reset_comp()
burst = comp_frames + 1
for _ in range(burst):
    vc_mod._feed_local_megaphone_direct(gp2, music_frame, producer='music')
m2 = gp2.megaphone.player_sources['local:music']['sources']
# The FIFO holds the first comp_frames-1 feeds, then emits 1 per feed:
# after 'burst' feeds exactly burst - comp_frames + 1 frames played.
played = burst - comp_frames + 1
check("burst held by the sync buffer (no drops, no early play) (%d)"
      % m2[0].buffers_queued,
      m2[0].buffers_queued == played)
# One more feed -> the next burst frame plays (order preserved).
vc_mod._feed_local_megaphone_direct(gp2, music_frame, producer='music')
check("burst frames preserved: next feed emits the next one (no drops) (%d)"
      % m2[0].buffers_queued,
      m2[0].buffers_queued == played + 1)

# --- 2c. Zero added latency: a mic chunk is queued on the SAME call ---
gp3 = FakeGameplay()
clock3 = _Clock()
vc_mod.time.time = clock3
_reset_comp()
vc_mod._feed_local_megaphone_direct(gp3, mic_chunk, producer='mic')
m3 = gp3.megaphone.player_sources['local:mic']['sources']
check("voice is queued immediately (no window buffering delay)",
      m3[0].buffers_queued == 1)

# --- 2d. Steady music-only: 1 frame per 20ms after the fill, no growth ---
gp4 = FakeGameplay()
clock4 = _Clock()
vc_mod.time.time = clock4
comp_frames = _reset_comp()
for step in range(50):
    clock4.t = 1000.0 + step * 0.01
    if step % 2 == 0:
        vc_mod._feed_local_megaphone_direct(gp4, music_frame, producer='music')
m4 = gp4.megaphone.player_sources['local:music']['sources']
expected4 = 25 - (comp_frames - 1)
check("steady music-only: %d of 25 frames (sync fill, then no drops/growth) (%d)"
      % (expected4, m4[0].buffers_queued),
      m4[0].buffers_queued == expected4)
before4 = m4[0].buffers_queued
clock4.t += 0.02
vc_mod._feed_local_megaphone_direct(gp4, music_frame, producer='music')
check("steady music-only continues 1-in/1-out (no backlog accumulation)",
      m4[0].buffers_queued == before4 + 1)

print()
print(f"RESULT: {PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
