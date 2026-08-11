"""End-to-end: guitar line-in audio mixed into the REAL music bot streamer.

The sender drives the actual AudioStreamer._send_to_network_actual (the code
that mixes music + mic + guitar) with a fake network object that forwards the
Opus packets over enet to the server. The server relays them (megaphone
path), and the listener decodes and pitch-detects to confirm the guitar
notes arrive - the full "music bot broadcast on -> others hear the guitar"
flow.

Usage (server must be running with trusted_staff.json granting
"megaphone_broadcast" to the sender account):
    python guitar_musicbot_e2e_test.py --host 127.0.0.1 --port 13000
"""

import argparse
import collections
import json
import os
import sys
import threading
import time

import enet
import numpy as np
from pyogg import OpusDecoder, OpusEncoder

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from libs import music_bot as mb
from libs.pitch import yin_pitch, hz_to_midi, midi_to_name

CHANNEL_MISC = 0
CHANNEL_MAP = 4
CHANNEL_PING = 5
CHANNEL_MEGAPHONE = 30
CLIENT_VERSION = "BT-1.7.0"
PASSWORD = "loadtest-pass"
SR = 48000


def _pkt(data: bytes, reliable=True) -> enet.Packet:
    flags = enet.PACKET_FLAG_RELIABLE if reliable else enet.PACKET_FLAG_UNRELIABLE_FRAGMENT
    return enet.Packet(data, flags=flags)


def register(host, port, username):
    net = enet.Host(None, 1, 256, 0, 0)
    peer = net.connect(enet.Address(host.encode(), port), 256)
    deadline = time.perf_counter() + 15
    done = False
    while not done and time.perf_counter() < deadline:
        ev = net.service(0)
        if ev.type == enet.EVENT_TYPE_CONNECT:
            peer.send(CHANNEL_MISC, _pkt(json.dumps({
                "event": "create",
                "data": {"username": username, "password": PASSWORD,
                         "version": CLIENT_VERSION},
            }).encode()))
        elif ev.type == enet.EVENT_TYPE_RECEIVE and ev.channelID < CHANNEL_PING:
            try:
                data = json.loads(ev.packet.data)
            except Exception:
                continue
            if data.get("event") in ("create", "create_success", "login"):
                done = True
    net.flush()
    time.sleep(0.2)


def pluck_chunks(freq, seconds=0.4, sr=SR):
    t = np.arange(int(sr * seconds)) / sr
    x = (np.sin(2 * np.pi * freq * t)
         + 0.5 * np.sin(2 * np.pi * 2 * freq * t) * np.exp(-t * 5)) * 0.5 * np.exp(-t * 2.0)
    pcm16 = (x * 32767).astype(np.int16).tobytes()
    return [pcm16[i:i + 1920] for i in range(0, len(pcm16), 1920)][:6]


class Sender(threading.Thread):
    def __init__(self, host, port, username):
        super().__init__(daemon=True)
        self.host, self.port, self.username = host, port, username
        self.connected = threading.Event()
        self.quit = threading.Event()

    def run(self):
        net = enet.Host(None, 1, 256, 0, 0)
        peer = net.connect(enet.Address(self.host.encode(), self.port), 256)
        sent = False
        streamed = False
        deadline = time.perf_counter() + 20

        class FakeNetwork:
            def send(self, channel, event, data, reliable=False):
                peer.send(channel, _pkt(bytes(data), reliable=reliable))

        class FakeBot:
            broadcast_enabled = True
            broadcast_to_megaphone = True
            guitar_pcm_queue = collections.deque(maxlen=10)
            mic_pcm_queue = None
            duck_multiplier = 1.0

        streamer = mb.AudioStreamer.__new__(mb.AudioStreamer)
        streamer.game = type("FakeGame", (), {"network": FakeNetwork()})()
        streamer.bot = FakeBot()
        streamer.encoder = OpusEncoder()
        streamer.encoder.set_application("voip")
        streamer.encoder.set_channels(1)
        streamer.encoder.set_sampling_frequency(SR)
        streamer.volume = 50

        while time.perf_counter() < deadline:
            ev = net.service(0)
            if ev.type == enet.EVENT_TYPE_CONNECT and not sent:
                peer.send(CHANNEL_MISC, _pkt(json.dumps({
                    "event": "login",
                    "data": {"username": self.username, "password": PASSWORD,
                             "version": CLIENT_VERSION},
                }).encode()))
                sent = True
                self.connected.set()
            elif ev.type == enet.EVENT_TYPE_RECEIVE and not streamed:
                try:
                    data = json.loads(ev.packet.data)
                except Exception:
                    continue
                if data.get("event") == "parse_map":
                    # guitar phrase through the REAL music bot streamer
                    for freq in (164.81, 220.0, 246.94):
                        for chunk in pluck_chunks(freq):
                            FakeBot.guitar_pcm_queue.append(chunk)
                            streamer._send_to_network_actual(bytes(3840))
                            time.sleep(0.02)
                    streamed = True
                    time.sleep(0.3)
            elif ev.type == enet.EVENT_TYPE_DISCONNECT:
                break
        net.flush()


class Listener(threading.Thread):
    def __init__(self, host, port, username):
        super().__init__(daemon=True)
        self.host, self.port, self.username = host, port, username
        self.pcm = b""
        self.connected = threading.Event()

    def run(self):
        net = enet.Host(None, 1, 256, 0, 0)
        peer = net.connect(enet.Address(self.host.encode(), self.port), 256)
        sent = False
        dec = OpusDecoder()
        dec.set_channels(1)
        dec.set_sampling_frequency(SR)
        deadline = time.perf_counter() + 25
        last_pkt = time.perf_counter()
        while time.perf_counter() < deadline:
            ev = net.service(0)
            if ev.type == enet.EVENT_TYPE_CONNECT and not sent:
                peer.send(CHANNEL_MISC, _pkt(json.dumps({
                    "event": "login",
                    "data": {"username": self.username, "password": PASSWORD,
                             "version": CLIENT_VERSION},
                }).encode()))
                sent = True
                self.connected.set()
            elif ev.type == enet.EVENT_TYPE_RECEIVE:
                if ev.channelID == CHANNEL_MEGAPHONE and len(ev.packet.data) >= 2:
                    self.pcm += dec.decode(bytearray(ev.packet.data[1:]))
                    last_pkt = time.perf_counter()
            elif ev.type == enet.EVENT_TYPE_DISCONNECT:
                break
            if self.pcm and time.perf_counter() - last_pkt > 1.5:
                break
        net.flush()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=13000)
    args = ap.parse_args()

    register(args.host, args.port, "meg_listener")
    register(args.host, args.port, "meg_gtr")

    lst = Listener(args.host, args.port, "meg_listener")
    snd = Sender(args.host, args.port, "meg_gtr")
    lst.start()
    snd.start()
    lst.connected.wait(10)
    snd.connected.wait(10)
    snd.join(12)
    lst.join(12)

    if not lst.pcm:
        print("RESULT: FAIL - listener received no audio")
        return

    raw = np.frombuffer(bytes(lst.pcm), dtype=np.int16).astype(np.float32) / 32768.0
    rms = float(np.sqrt(np.mean(raw ** 2)))
    notes = []
    for i in range(0, len(raw) - 959, 960):
        h = yin_pitch(raw[i:i + 960])
        if h is not None:
            n = midi_to_name(hz_to_midi(h))
            if not notes or notes[-1] != n:
                notes.append(n)
    print(f"received {len(raw)} samples, RMS {rms:.4f}")
    print("notes heard by listener:", notes)
    ok = rms > 0.005 and all(n in notes for n in ("E3", "A3", "B3"))
    print("RESULT:", "PASS - guitar heard through music bot broadcast"
          if ok else "FAIL")


if __name__ == "__main__":
    main()
