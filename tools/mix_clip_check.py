"""Verify whether the music-bot mix path clips (causes the 'slightly broken'
megaphone sound) by simulating the exact gain math the code applies.

Path being simulated (client/libs/music_bot/streaming.py, AudioStreamer + LiveRelayStreamer):
    mono_data = music  (already scaled by volume/100 * duck)
    mono_data *= 0.75                       # music headroom
    mono_data += mic  * 0.85                # audioop.add
    mono_data += guitar * 0.85              # audioop.add
    -> encode & send, no limiter after mix
"""
import audioop
import math
import struct

def make_sine(amp, n=1920, freq=440.0, sr=48000):
    return struct.pack("<%dh" % n, *[int(amp * 32767 * math.sin(2 * math.pi * freq * i / sr)) for i in range(n)])

def peak(blob):
    samples = struct.unpack("<%dh" % (len(blob) // 2), blob)
    return max(abs(s) for s in samples)

def clip_count(blob):
    return sum(1 for s in struct.unpack("<%dh" % (len(blob) // 2), blob) if abs(s) >= 32767)

n = 1920  # one 20ms Opus frame

# Realistic levels
# music: loud song ~ -12 dBFS (amp 0.25 of full scale)
music = make_sine(0.25, n)
# voice: normal speech ~ -18 dBFS
mic = make_sine(0.125, n)
# guitar strum: transient peaks can hit -6 dBFS briefly
guitar = make_sine(0.5, n)

# --- AudioStreamer path (MP3 playing + live mix) ---
mix = audioop.mul(music, 2, 0.75)
mix = audioop.add(mix, audioop.mul(mic, 2, 0.85), 2)
mix = audioop.add(mix, audioop.mul(guitar, 2, 0.85), 2)
print("AudioStreamer: peak=%d clip=%d/1920 -> %s"
      % (peak(mix), clip_count(mix), "CLIPPED!" if clip_count(mix) else "ok"))

# --- LiveRelayStreamer path (no MP3, guitar+mic only) ---
base = b"\x00" * n * 2
relay = audioop.add(base, audioop.mul(guitar, 2, 0.85), 2)
relay = audioop.add(relay, audioop.mul(mic, 2, 0.85), 2)
print("LiveRelay:  peak=%d clip=%d/1920 -> %s"
      % (peak(relay), clip_count(relay), "CLIPPED!" if clip_count(relay) else "ok"))

# --- Compare: what would the same mix look like with a limiter after mixing? ---
from libs.voice_chat import soft_limit_audio
limited = soft_limit_audio(mix)
print("After soft_limit_audio (threshold=0.35, ratio=12): peak=%d clip=%d -> %s"
      % (peak(limited), clip_count(limited), "CLIPPED!" if clip_count(limited) else "ok"))
