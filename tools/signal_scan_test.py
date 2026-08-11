"""Test: signal-based pedal scan finds the guitar input when the name is generic.

Scenario: a player plugs in a USB effects pedal that Windows names plainly
("USB Audio Device") - name-based detection can't tell it from a USB mic. The
signal scan briefly opens every capture device while the player strums, and
keeps the device that actually carries signal.

Tests the scan ranking/filtering with simulated probe results, the pick
helper, the gameplay fallback wiring, and runs a real probe on this machine's
capture devices.

Usage:
    python signal_scan_test.py
"""

import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from libs import instrument_input as ii

fails = []


def check(name, ok, detail=""):
    print(("PASS" if ok else "FAIL"), "-", name, (": " + detail) if detail else "")
    if not ok:
        fails.append(name)


# ------------------------------------------------- 1. ranking / filtering (sim)
DEVICES = [
    "OpenAL Soft on Microphone (USB Audio Device)",
    "OpenAL Soft on USB Audio Device",              # generic-named pedal
    "OpenAL Soft on Boss GT-1",
]

# probe results: mic is quiet (below threshold), generic pedal is loud,
# Boss GT-1 is loud but slightly quieter than the generic one.
probe_map = {
    DEVICES[0]: (0.008, False),   # ambient only -> excluded
    DEVICES[1]: (0.22, True),     # real signal, stereo (typical pedal)
    DEVICES[2]: (0.19, False),    # real signal, mono
}
real_probe = ii._probe_device_signal


def fake_probe(device, seconds=None):
    return probe_map.get(device, None)


ii._probe_device_signal = fake_probe
try:
    found = ii.scan_for_signal_devices(DEVICES)
    names = [d["name"] for d in found]
    check("quiet mic excluded, 2 signal devices found", len(found) == 2,
          f"names={names} rms={[round(d['rms'], 2) for d in found]}")
    check("named guitar/pedal ranked above louder generic device",
          names[0] == " Boss GT-1", f"order={names}")
    check("generic-named pedal still found", any(
        d["name"] == " USB Audio Device" for d in found), f"names={names}")

    best = ii.pick_best_signal_device(found)
    check("pick prefers named pedal", best == DEVICES[2], f"picked={best}")

    # only a generic-named device carries signal -> it must be picked
    probe_map = {DEVICES[0]: (0.006, False), DEVICES[1]: (0.30, True),
                 DEVICES[2]: (0.004, False)}
    found2 = ii.scan_for_signal_devices(DEVICES)
    best2 = ii.pick_best_signal_device(found2)
    check("generic-named pedal picked when it is the only signal",
          best2 == DEVICES[1], f"picked={best2}")

    # no signal anywhere -> empty
    probe_map = {d: (0.002, False) for d in DEVICES}
    check("no signal -> empty scan", ii.scan_for_signal_devices(DEVICES) == [])
finally:
    ii._probe_device_signal = real_probe


# ----------------------------------------- 2. gameplay fallback wiring (sim)
class FakeInstr:
    def __init__(self):
        self.audio_input = object()
        self.chosen = None

    def reopen(self, device):
        self.chosen = device


class FakeOptions:
    def __init__(self):
        self.value = "system default"

    def get(self, key, default=None):
        return self.value

    def set(self, key, value):
        self.value = value


class FakeGameplay:
    def __init__(self):
        self.instrument_input = FakeInstr()
        self.audio_mngr = object()

    def _select_instrument(self):
        # the exact fallback block from toggle_guitar_mode, driven by the
        # monkeypatched detection + scan below
        if self.options.get("audio_instrument_input_device", "system default") == "system default":
            guitar_devices = _detect()
            if guitar_devices:
                device = guitar_devices[0]
                self.options.set("audio_instrument_input_device", device)
                self.instrument_input.reopen(device)
                return "name"
            else:
                if self.instrument_input.audio_input is not None:
                    self.instrument_input.audio_input = None
                found = _scan()
                if found:
                    device = ii.pick_best_signal_device(found)
                    self.options.set("audio_instrument_input_device", device)
                    self.instrument_input.reopen(device)
                    return "signal"
                return "none"


gp = FakeGameplay()
gp.options = FakeOptions()
_detect = lambda: []
_scan = lambda: [{"device": DEVICES[1], "name": " USB Audio Device",
                  "rms": 0.30, "stereo": True, "guitar_pedal": False}]
res = gp._select_instrument()
check("name miss -> signal scan picks generic pedal",
      res == "signal" and gp.instrument_input.chosen == DEVICES[1]
      and gp.options.value == DEVICES[1],
      f"res={res} chosen={gp.instrument_input.chosen}")

gp2 = FakeGameplay()
gp2.options = FakeOptions()
_scan = lambda: []
res2 = gp2._select_instrument()
check("no name, no signal -> manual selection requested",
      res2 == "none" and gp2.instrument_input.chosen is None, f"res={res2}")


# --------------------------------------------- 3. real probe on this machine
print()
print("[real scan] probing this machine's capture devices (0.5s each)...")
try:
    found = ii.scan_for_signal_devices()
    if found:
        for d in found:
            print(f"  signal on {d['name']}: RMS={d['rms']:.4f} "
                  f"stereo={d['stereo']} guitar/pedal={d['guitar_pedal']}")
    else:
        print("  no device above the signal threshold (all quiet - expected "
              "when nothing is being played into them)")
    check("real scan ran without error", True)
except Exception as e:
    check("real scan ran without error", False, str(e))


print()
if fails:
    print("RESULT: FAIL -", len(fails), "failed:", fails)
    sys.exit(1)
print("RESULT: ALL PASS - signal scan finds the pedal even with a generic name")
