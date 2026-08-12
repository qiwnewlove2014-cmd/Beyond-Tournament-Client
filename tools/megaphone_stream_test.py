"""Test: can raw (guitar) audio stream through the megaphone/PA channel?

Proves the game's voice pipeline carries arbitrary PCM (not just the voice
mic): the sender Opus-encodes synthetic guitar plucks and sends them on
CHANNEL_MEGAPHONE (exactly what voice_chat_compression does); the server
relays the raw packets to everyone on the map; the listener strips the
1-byte sender tag (as process_voice_data does) and pitch-detects the decoded
audio to confirm the notes came through intact.

Prereqs:
  - server running with trusted_staff.json granting "megaphone_broadcast"
    to the sender account (PA test mode is staff-only), e.g.:
      {"meg_gtr": ["megaphone_broadcast"]}
  - sender account registered

Usage:
    python megaphone_stream_test.py --host 127.0.0.1 --port 13000
"""

import argparse
import json
import os
import sys
import threading
import time

import enet
import numpy as np
from pyogg import OpusEncoder, OpusDecoder

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from libs.pitch import yin_pitch, hz_to_midi, midi_to_name

CHANNEL_MISC = 0
CHANNEL_MAP = 4
CHANNEL_PING = 5
CHANNEL_MEGAPHONE = 30
CLIENT_VERSION = "BT-1.7.2"
PASSWORD = "loadtest-pass"
SR = 48000


def _pkt(data: bytes) -> enet.Packet:
    return enet.Packet(data, flags=enet.PACKET_FLAG_RELIABLE)


def pluck(freq, seconds=0.8, sr=SR):
    """Synthetic plucked-string note (harmonics + decay)."""
    t = np.arange(int(sr * seconds)) / sr
    x = (np.sin(2 * np.pi * freq * t)
         + 0.5 * np.sin(2 * np.pi * 2 * freq * t) * np.exp(-t * 5)
         + 0.25 * np.sin(2 * np.pi * 3 * freq * t) * np.exp(-t * 8))
    return (x * 0.5 * np.exp(-t * 2.0)).astype(np.float32)


def pcm_chunks(seconds=3.0, sr=SR):
    """Build a guitar phrase: E3 -> A3 -> B3 with short gaps."""
    phrase = np.concatenate([
        pluck(164.81, 0.8), np.zeros(int(sr * 0.2)),
        pluck(220.0, 0.8), np.zeros(int(sr * 0.2)),
        pluck(246.94, 0.8), np.zeros(int(sr * 0.4)),
    ])
    n = int(sr * seconds)
    if len(phrase) < n:
        phrase = np.concatenate([phrase, np.zeros(n - len(phrase))])
    pcm16 = (phrase[:n] * 32767).astype(np.int16).tobytes()
    return [pcm16[i:i + 1920] for i in range(0, len(pcm16), 1920)]


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
            if data.get("event") in ("create", "create_success", "create_done", "login"):
                done = True
    net.flush()
    time.sleep(0.2)


class Sender(threading.Thread):
    """Logs in as a megaphone-permitted account and streams guitar Opus."""

    def __init__(self, host, port, username):
        super().__init__(daemon=True)
        self.host, self.port, self.username = host, port, username
        self.connected = threading.Event()
        self.error = None

    def run(self):
        net = enet.Host(None, 1, 256, 0, 0)
        peer = net.connect(enet.Address(self.host.encode(), self.port), 256)
        sent = False
        streamed = False
        deadline = time.perf_counter() + 20
        enc = OpusEncoder()
        enc.set_application("voip")
        enc.set_channels(1)
        enc.set_sampling_frequency(SR)
        chunks = pcm_chunks()
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
                evt = data.get("event")
                if evt == "parse_map":
                    # placed on a map: stream the guitar phrase in real time
                    print(f"[sender] parse_map received, streaming {len(chunks)} chunks")
                    for chunk in chunks:
                        opus = bytes(enc.encode(bytearray(chunk)))
                        peer.send(CHANNEL_MEGAPHONE, enet.Packet(
                            opus, flags=enet.PACKET_FLAG_UNRELIABLE_FRAGMENT))
                        time.sleep(0.02)
                    streamed = True
                    print("[sender] streamed all chunks")
                    time.sleep(0.3)
            elif ev.type == enet.EVENT_TYPE_DISCONNECT:
                break
        net.flush()


class Listener(threading.Thread):
    """Collects megaphone packets, decodes them, and pitch-detects."""

    def __init__(self, host, port, username):
        super().__init__(daemon=True)
        self.host, self.port, self.username = host, port, username
        self.pcm = b""
        self.connected = threading.Event()
        self.error = None

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
                if ev.channelID == CHANNEL_MEGAPHONE:
                    data = ev.packet.data
                    if len(data) >= 2:
                        self.pcm += dec.decode(bytearray(data[1:]))
                    last_pkt = time.perf_counter()
            elif ev.type == enet.EVENT_TYPE_DISCONNECT:
                break
            # stop once streaming ends (quiet for 1.5s after first packet)
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
        print("RESULT: FAIL - listener received no megaphone audio")
        return

    print(f"received {len(lst.pcm)} bytes of decoded PCM "
          f"({len(lst.pcm) / SR:.2f}s)")
    raw = np.frombuffer(lst.pcm, dtype=np.int16).astype(np.float32) / 32768.0
    rms = float(np.sqrt(np.mean(raw ** 2)))
    print(f"RMS: {rms:.4f}")
    notes = []
    for i in range(0, len(raw) - 959, 960):
        h = yin_pitch(raw[i:i + 960])
        if h is not None:
            n = midi_to_name(hz_to_midi(h))
            if not notes or notes[-1] != n:
                notes.append(n)
    print("notes detected in streamed audio:", notes)
    ok = rms > 0.005 and "E3" in notes and "A3" in notes and "B3" in notes
    print("RESULT:", "PASS - raw guitar audio streams through megaphone"
          if ok else "FAIL")


if __name__ == "__main__":
    main()
