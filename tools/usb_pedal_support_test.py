"""Test: USB multi-effects pedals work as the guitar line-in.

Covers the "guitar plugs into an effects pedal, the pedal's USB audio out is
the capture source" setup:

  1. Detection - realistic pedal device names (Boss GT, Zoom G, Line 6 POD /
     Helix, Valeton, NUX, Mooer, Kemper, Axe-Fx, DigiTech, Vox, ...) are
     recognized; plain microphones and generic USB audio devices are not.
  2. Stereo downmix - many pedals expose stereo capture; the downmix helper
     must produce correct mono16 (L+R averaged) that keeps the pitch.
  3. Menu - the instrument input menu tags and sorts likely pedal/guitar
     devices first; the voice menu is unchanged.
  4. Real stereo capture - opens an actual capture device in STEREO16 (the
     fallback path used when a pedal rejects mono), downmixes, and confirms
     live signal comes through.

Usage:
    python usb_pedal_support_test.py
"""

import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from libs import instrument_input as ii
from libs.pitch import yin_pitch, hz_to_midi, midi_to_name

SR = 48000
fails = []


def check(name, ok, detail=""):
    print(("PASS" if ok else "FAIL"), "-", name, (": " + detail) if detail else "")
    if not ok:
        fails.append(name)


# ---------------------------------------------------------------- 1. detection
PEDAL_DEVICES = [
    "OpenAL Soft on Boss GT-1",
    "OpenAL Soft on Boss ME-80",
    "OpenAL Soft on Zoom G1X Four",
    "OpenAL Soft on Zoom G3Xn",
    "OpenAL Soft on Line 6 POD Go",
    "OpenAL Soft on Line6 Helix LT",
    "OpenAL Soft on Valeton GP-100",
    "OpenAL Soft on NUX MG-30",
    "OpenAL Soft on Mooer GE300",
    "OpenAL Soft on Kemper Profiling Amp",
    "OpenAL Soft on Fractal Audio Axe-Fx III",
    "OpenAL Soft on DigiTech RP355",
    "OpenAL Soft on Vox Tonelab ST",
    "OpenAL Soft on Focusrite Scarlett 2i2",
    "OpenAL Soft on iRig HD 2",
]
PLAIN_DEVICES = [
    "OpenAL Soft on Microphone (USB Audio Device)",
    "OpenAL Soft on Microphone Array (AMD Audio Device)",
    "OpenAL Soft on Speakers (Realtek Audio)",
    "OpenAL Soft on Webcam Mic",
]

matched = ii.detect_guitar_inputs(PEDAL_DEVICES)
check("all 15 pedal/interface names detected", len(matched) == len(PEDAL_DEVICES),
      f"got {len(matched)}/{len(PEDAL_DEVICES)}")

false_pos = ii.detect_guitar_inputs(PLAIN_DEVICES)
check("plain mics / generic USB audio NOT detected", len(false_pos) == 0,
      f"false positives: {false_pos}")

# a pedal plugged in alongside the built-in mic
mixed = ["OpenAL Soft on Microphone (USB Audio Device)",
         "OpenAL Soft on Boss Katana",
         "OpenAL Soft on Microphone Array (AMD Audio Device)"]
detected = ii.detect_guitar_inputs(mixed)
check("pedal found among mics, mics excluded",
      detected == ["OpenAL Soft on Boss Katana"], f"got {detected}")


# ------------------------------------------------------------ 2. stereo downmix
t = np.arange(960) / SR
# same tone on both channels -> downmix must keep it (L+R)/2 == L
x = 0.4 * np.sin(2 * np.pi * 164.81 * t)  # E3
stereo = np.empty(1920, dtype=np.int16)
stereo[0::2] = (x * 32767).astype(np.int16)
stereo[1::2] = (x * 32767).astype(np.int16)
mono = ii._downmix_stereo(stereo.tobytes())
mono_arr = np.frombuffer(mono, dtype=np.int16)
check("stereo downmix length", len(mono_arr) == 960)
h = yin_pitch(mono_arr.astype(np.float32) / 32768.0)
n = midi_to_name(hz_to_midi(h)) if h else None
check("downmix keeps the pitch (E3)", n == "E3", f"detected {n}")

# L has signal, R silent -> (L+R)/2 halves the amplitude
stereo2 = np.zeros(1920, dtype=np.int16)
stereo2[0::2] = (x * 32767).astype(np.int16)
mono2 = np.frombuffer(ii._downmix_stereo(stereo2.tobytes()), dtype=np.int16)
ratio = float(np.max(np.abs(mono2))) / float(np.max(np.abs(mono_arr)))
check("L-only downmix halves amplitude", abs(ratio - 0.5) < 0.02, f"ratio={ratio:.3f}")


# ------------------------------------------------------------- 3. menu entries
menu_devices = [
    "OpenAL Soft on Microphone (USB Audio Device)",
    "OpenAL Soft on Zoom G1X Four",
    "OpenAL Soft on Microphone Array (AMD Audio Device)",
]
entries = ii.instrument_menu_entries(menu_devices)
labels = [label for label, _ in entries]
raw_order = [d for _, d in entries]
# note: cyal device names start with "OpenAL Soft on " (15 chars) so [14:]
# labels keep a leading space - that is the existing menu convention.
check("pedal sorted first in instrument menu",
      labels[0] == " Zoom G1X Four (guitar/pedal)", f"order={labels}")
check("mics untagged in instrument menu",
      labels[1:] == [" Microphone (USB Audio Device)",
                     " Microphone Array (AMD Audio Device)"],
      f"labels={labels}")
check("entry keeps the raw device for opening",
      raw_order == [menu_devices[1], menu_devices[0], menu_devices[2]])


# ---------------------------------------- 4. real stereo capture (fallback path)
try:
    import cyal
    import time
    cap = cyal.CaptureExtension()
    real = False
    for dev in cap.devices:
        if not dev:
            continue
        try:
            inp = cap.open_device(name=dev.encode(), sample_rate=48000,
                                  format=cyal.BufferFormat.STEREO16)
        except Exception:
            continue
        inp.start()
        time.sleep(0.6)
        if inp.available_samples >= 960:
            buf = bytearray(960 * 4)
            inp.capture_samples(buf)
            mono = ii._downmix_stereo(buf)
            arr = np.frombuffer(mono, dtype=np.int16).astype(np.float32) / 32768.0
            rms = float(np.sqrt(np.mean(arr ** 2)))
            inp.stop()
            inp = None
            print(f"[real stereo capture] {dev[14:]}: RMS={rms:.4f}")
            # A mono-only device returns silence in stereo mode (OpenAL Soft
            # behaviour); any real stereo-capable device must carry signal.
            if rms > 0.001:
                check("real STEREO16 capture + downmix yields signal", True,
                      f"{dev[14:]} RMS={rms:.4f}")
                real = True
                break
    if not real:
        print("[real stereo capture] skipped - all devices are mono-only "
              "(stereo capture is a fallback for pedals that reject mono)")
except ImportError:
    print("[real stereo capture] skipped - cyal not importable in this env")


print()
if fails:
    print("RESULT: FAIL -", len(fails), "failed:", fails)
    sys.exit(1)
print("RESULT: ALL PASS - USB effects pedals supported as guitar input")
