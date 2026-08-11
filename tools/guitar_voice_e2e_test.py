"""Test: raw (guitar) audio streams on the normal 3D voice channel.

The performer's InstrumentInput feeds raw line-in PCM into the game's own
voice compression (CHANNEL_VOICECHAT = 20) - the same Opus pipeline the mic
uses. The server assigns each player a dynamic voice channel and relays this
player's voice packets on it (3D, near the player), so nearby players hear
the strums/chords with no music bot broadcast and no special permission.

This test mirrors the real client: the sender Opus-encodes synthetic guitar
plucks and sends them on CHANNEL_VOICECHAT (20); the listener learns the
sender's dynamic voice channel from the spawn_entity packet (as the real
client does), collects the relayed packets there, and pitch-detects the
decoded audio. The normal voice path carries the raw Opus with no extra tag
byte (unlike megaphone/music bot).

Usage:
    python guitar_voice_e2e_test.py --host 127.0.0.1 --port 13000
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
CHANNEL_VOICECHAT = 20
CLIENT_VERSION = "BT-1.7.0"
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
            if data.get("event") in ("create", "create_success", "login"):
                done = True
    net.flush()
    time.sleep(0.2)


class Sender(threading.Thread):
    """Logs in as a normal player and streams guitar Opus on the voice channel."""

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
                if data.get("event") == "parse_map":
                    # placed on a map: stream the guitar phrase in real time
                    # on the plain voice channel (no megaphone permission)
                    print(f"[sender] parse_map received, streaming {len(chunks)} chunks")
                    for chunk in chunks:
                        opus = bytes(enc.encode(bytearray(chunk)))
                        peer.send(CHANNEL_VOICECHAT, enet.Packet(
                            opus, flags=enet.PACKET_FLAG_UNRELIABLE_FRAGMENT))
                        time.sleep(0.02)
                    streamed = True
                    print("[sender] streamed all chunks")
                    time.sleep(0.3)
            elif ev.type == enet.EVENT_TYPE_DISCONNECT:
                break
        net.flush()


class Listener(threading.Thread):
    """Learns the sender's voice channel from spawn_entity, then collects and
    decodes the relayed guitar packets."""

    def __init__(self, host, port, username, sender_name):
        super().__init__(daemon=True)
        self.host, self.port, self.username = host, port, username
        self.sender_name = sender_name
        self.sender_channel = None
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
                if ev.channelID == CHANNEL_MAP and not self.sender_channel:
                    try:
                        data = json.loads(ev.packet.data)
                    except Exception:
                        continue
                    d = data.get("data", {})
                    if (data.get("event") == "spawn_entity"
                            and d.get("name") == self.sender_name
                            and d.get("voice_channel")):
                        self.sender_channel = d["voice_channel"]
                        print(f"[listener] sender voice_channel = {self.sender_channel}")
                elif (self.sender_channel is not None
                        and ev.channelID == self.sender_channel):
                    # normal voice path: raw Opus, no tag byte
                    self.pcm += dec.decode(bytearray(ev.packet.data))
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

    register(args.host, args.port, "vc_listener")
    register(args.host, args.port, "vc_gtr")

    lst = Listener(args.host, args.port, "vc_listener", "vc_gtr")
    snd = Sender(args.host, args.port, "vc_gtr")
    lst.start()
    snd.start()
    lst.connected.wait(10)
    snd.connected.wait(10)
    snd.join(12)
    lst.join(12)

    if not lst.sender_channel:
        print("RESULT: FAIL - listener never learned the sender's voice channel")
        return
    if not lst.pcm:
        print("RESULT: FAIL - listener received no audio on the sender's "
              f"voice channel {lst.sender_channel}")
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
    print("RESULT:", "PASS - raw guitar audio streams on the 3D voice channel"
          if ok else "FAIL")


if __name__ == "__main__":
    main()
