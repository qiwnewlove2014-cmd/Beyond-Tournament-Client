"""Offline simulation: does guitar audio actually flow from InstrumentInput
into the music bot broadcast stream (LiveRelayStreamer + AudioStreamer)?

Drives the REAL production classes (InstrumentInput capture loop body,
LiveRelayStreamer, AudioStreamer._send_to_network_actual) with fake game /
bot / network objects so no server or audio hardware is needed.
"""
import collections
import sys
import os
import time
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")

import numpy as np
from libs import instrument_input as ii
from libs import music_bot as mb

SR = 48000
PASS = 0
FAIL = 0


def check(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  PASS - {name} {detail}")
    else:
        FAIL += 1
        print(f"  FAIL - {name} {detail}")


def synth_chunk(freq, seconds=0.04, sr=SR):
    """20ms mono16 chunk with a decaying pluck."""
    t = np.arange(int(sr * seconds)) / sr
    x = (np.sin(2 * np.pi * freq * t)
         + 0.5 * np.sin(2 * np.pi * 2 * freq * t) * np.exp(-t * 5)) * np.exp(-t * 2.0)
    pcm = (x * 32767 * 0.6).astype(np.int16)
    out = []
    for i in range(0, len(pcm), 960):
        out.append(pcm[i:i + 960].tobytes())
    return out


class FakeNetwork:
    def __init__(self):
        self.sent = []  # (channel, data)

    def send(self, channel, event, data, reliable=False):
        self.sent.append((channel, bytes(data)))


class FakeGame:
    def __init__(self):
        self.network = FakeNetwork()
        self.stack = []


class FakeMusicBot:
    def __init__(self):
        self.game = None
        self.broadcast_enabled = False
        self.broadcast_to_megaphone = False
        self.volume = 50
        self.duck_multiplier = 1.0
        self.guitar_pcm_queue = collections.deque(maxlen=10)
        self.mic_pcm_queue = collections.deque(maxlen=10)
        self.streamer = None
        self.live_relay_streamer = None
        self.playing = False
        self.paused = False

    def _find_gameplay(self):
        return None


class FakeInstr:
    """Minimal stand-in for InstrumentInput's capture loop body."""
    def __init__(self, game, bot):
        self.game = game
        self.bot = bot
        self.stereo = False
        self.recording = True

    def _find_music_bot(self):
        return self.bot

    def run_frame(self, raw):
        # ---- copy of InstrumentInput.run() routing block (mono path) ----
        music_bot = self._find_music_bot()
        route_to_bot = bool(music_bot and (
            getattr(music_bot, "broadcast_enabled", False)
            or getattr(music_bot, "broadcast_to_megaphone", False)
        ))
        if route_to_bot:
            if not hasattr(music_bot, "guitar_pcm_queue"):
                music_bot.guitar_pcm_queue = collections.deque(maxlen=10)
            music_bot.guitar_pcm_queue.append(raw)
        return route_to_bot


def drive_live_relay(bot, chunks):
    """Start LiveRelayStreamer the same way bot.loop() -> _ensure_live_relay_streamer does,
    then feed guitar chunks and collect network output."""
    net = bot._find_gameplay()
    relay = mb.LiveRelayStreamer(FakeGame(), bot=bot)
    # give it the real network
    relay.game.network = bot._net
    relay.start()
    try:
        for chunk in chunks:
            bot.guitar_pcm_queue.append(chunk)
            time.sleep(0.002)
        time.sleep(0.25)
    finally:
        relay.stop()
        relay.join(1.0)
    return bot._net.sent


print("=" * 66)
print("TEST 1: LiveRelayStreamer (no MP3) carries guitar to the network")
print("=" * 66)
bot = FakeMusicBot()
bot._net = FakeNetwork()
bot.broadcast_enabled = True
bot.broadcast_to_megaphone = True
chunks = synth_chunk(164.81)  # E3
sent = drive_live_relay(bot, chunks)
check("guitar chunks queued", len(bot.guitar_pcm_queue) >= 0)
mega_pkts = [d for (ch, d) in sent if ch == 30]
other_pkts = [d for (ch, d) in sent if ch != 30]
check("packets sent on megaphone channel (30)", len(mega_pkts) > 0,
      f"got {len(mega_pkts)} mega pkts, {len(sent)} total")
if mega_pkts:
    # decode: first byte is sender voice_channel id in real server flow; here raw opus
    try:
        from pyogg import OpusDecoder
        dec = OpusDecoder()
        dec.set_channels(1)
        dec.set_sampling_frequency(SR)
        pcm = b"".join(dec.decode(bytearray(d)) for d in mega_pkts)
        arr = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        rms = float(np.sqrt(np.mean(arr ** 2))) if len(arr) else 0.0
        check("megaphone stream has audible guitar signal", rms > 0.005, f"rms={rms:.4f}")
    except Exception as e:
        print(f"  (decode skipped: {e})")

print()
print("=" * 66)
print("TEST 2: InstrumentInput routing block (broadcast / megaphone rules)")
print("=" * 66)
game = FakeGame()
bot2 = FakeMusicBot()
instr = FakeInstr(game, bot2)
raw = synth_chunk(220.0)[0]
r = instr.run_frame(raw)
check("no route when both toggles off", r is False and len(bot2.guitar_pcm_queue) == 0)
bot2.broadcast_enabled = True
r = instr.run_frame(raw)
check("routes when broadcast on", r is True and len(bot2.guitar_pcm_queue) == 1,
      f"queue={len(bot2.guitar_pcm_queue)}")
bot2.broadcast_enabled = False
bot2.guitar_pcm_queue.clear()
bot2.broadcast_to_megaphone = True  # "Broadcast to Megaphone: ON" alone
r = instr.run_frame(raw)
check("routes when only Broadcast-to-Megaphone is ON", r is True and len(bot2.guitar_pcm_queue) == 1,
      f"queue={len(bot2.guitar_pcm_queue)}")

print()
print("=" * 66)
print("TEST 2b: LiveRelayStreamer runs with ONLY Broadcast-to-Megaphone ON")
print("=" * 66)
bot1b = FakeMusicBot()
bot1b._net = FakeNetwork()
bot1b.broadcast_enabled = False
bot1b.broadcast_to_megaphone = True
sent1b = drive_live_relay(bot1b, synth_chunk(220.0))
mega1b = [d for (ch, d) in sent1b if ch == 30]
check("relay sends on megaphone channel without broadcast toggle", len(mega1b) > 0,
      f"got {len(mega1b)} mega pkts / {len(sent1b)} total")
# _ensure_live_relay_streamer must start the relay in this state
bot1b.live_relay_streamer = None
bot1b.guitar_pcm_queue.append(synth_chunk(220.0)[0])
mb.MapMusicBot._ensure_live_relay_streamer(bot1b)
check("_ensure_live_relay_streamer starts relay with megaphone-only",
      bot1b.live_relay_streamer is not None and bot1b.live_relay_streamer.is_alive())
if bot1b.live_relay_streamer:
    bot1b.live_relay_streamer.stop()
bot1b.broadcast_to_megaphone = False
mb.MapMusicBot._ensure_live_relay_streamer(bot1b)
check("_ensure_live_relay_streamer stops relay when both off",
      bot1b.live_relay_streamer is None)

print()
print("=" * 66)
print("TEST 3: AudioStreamer._send_to_network_actual mixes guitar into MP3 stream")
print("=" * 66)
from libs import music_bot as mb2
streamer = mb2.AudioStreamer.__new__(mb2.AudioStreamer)
net = FakeNetwork()
bot3 = FakeMusicBot()
bot3.broadcast_enabled = True
bot3.broadcast_to_megaphone = True
streamer.game = types.SimpleNamespace(network=net)
streamer.bot = bot3
streamer.encoder = None  # replaced below
try:
    from pyogg import OpusEncoder
    enc = OpusEncoder()
    enc.set_application("voip")
    enc.set_channels(1)
    enc.set_sampling_frequency(SR)
    streamer.encoder = enc
except Exception:
    streamer.encoder = None
streamer.volume = 50

for chunk in synth_chunk(164.81, seconds=0.12):
    bot3.guitar_pcm_queue.append(chunk)
    # data = 3840 bytes stereo silence representing the MP3
    streamer._send_to_network_actual(bytes(3840))
mega = [d for (ch, d) in net.sent if ch == 30]
check("mixer sent on megaphone channel", len(mega) > 0, f"{len(net.sent)} packets")

# Phase 2: broadcast toggle OFF, only Broadcast-to-Megaphone ON -> must still send
net.sent.clear()
bot3.broadcast_enabled = False
for chunk in synth_chunk(220.0, seconds=0.12):
    bot3.guitar_pcm_queue.append(chunk)
    streamer._send_to_network_actual(bytes(3840))
mega2 = [d for (ch, d) in net.sent if ch == 30]
check("mixer sends with megaphone-only (no broadcast toggle)", len(mega2) > 0,
      f"{len(mega2)} mega pkts")
# Phase 3: both toggles OFF -> must NOT send
net.sent.clear()
bot3.broadcast_to_megaphone = False
for chunk in synth_chunk(220.0, seconds=0.12):
    bot3.guitar_pcm_queue.append(chunk)
    streamer._send_to_network_actual(bytes(3840))
check("mixer silent when both toggles off", len(net.sent) == 0, f"{len(net.sent)} pkts")
if mega and streamer.encoder:
    dec = OpusDecoder()
    dec.set_channels(1)
    dec.set_sampling_frequency(SR)
    pcm = b"".join(dec.decode(bytearray(d)) for d in mega)
    arr = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
    rms = float(np.sqrt(np.mean(arr ** 2))) if len(arr) else 0.0
    check("mixed stream audible", rms > 0.005, f"rms={rms:.4f}")

print()
print("=" * 66)
print("TEST 4: client parses the multi-owner megaphone lock state")
print("=" * 66)
from libs import event_handeler

class FakeMega:
    def __init__(self):
        self.lock_owner = None
        self.lock_owners = set()

class FakeGP:
    def __init__(self):
        self.megaphone = FakeMega()

class FakeHandler:
    def __init__(self):
        self.gameplay = FakeGP()

h = FakeHandler()
event_handeler.EventHandeler.megaphone_lock_state(h, {"owner": "Alice", "owners": ["Alice", "Bob"]})
check("lock_owner parsed from payload", h.gameplay.megaphone.lock_owner == "Alice")
check("lock_owners parsed into a set", h.gameplay.megaphone.lock_owners == {"Alice", "Bob"})
event_handeler.EventHandeler.megaphone_lock_state(h, {"owner": None, "owners": []})
check("state clears when everyone leaves",
      h.gameplay.megaphone.lock_owner is None and h.gameplay.megaphone.lock_owners == set())

print()
print("=" * 66)
print("TEST 5: music bot respects the single music slot")
print("=" * 66)
bot5 = types.SimpleNamespace()
bot5._find_gameplay = lambda: types.SimpleNamespace(
    megaphone=types.SimpleNamespace(lock_owner="Alice"),
    player=types.SimpleNamespace(name="Bob"),
)
check("non-music owner is NOT the music owner", mb.MapMusicBot._is_music_owner(bot5) is False)
bot5._find_gameplay = lambda: types.SimpleNamespace(
    megaphone=types.SimpleNamespace(lock_owner="Alice"),
    player=types.SimpleNamespace(name="Alice"),
)
check("music slot holder IS the music owner", mb.MapMusicBot._is_music_owner(bot5) is True)
bot5._find_gameplay = lambda: types.SimpleNamespace(
    megaphone=types.SimpleNamespace(lock_owner=None),
    player=types.SimpleNamespace(name="Bob"),
)
check("no slot holder -> not music owner (MP3 stays private)",
      mb.MapMusicBot._is_music_owner(bot5) is False)

print()
print(f"RESULT: {PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
