"""The simulated ear: render each room's reverb offline and measure its tail.

The lifecycle simulator proves the *routing*. It cannot hear. This tool plays a
single impulse through an EAXREVERB configured with **the map's own room
parameters** (the same defaults ``Map.spawn_reverb`` uses, with the room's
``decayTime``/``diffusion``), on the OpenAL Soft **loopback** device shipped in
``client/openal.dll`` (``alcLoopbackOpenDeviceSOFT`` + ``alcRenderSamplesSOFT``),
and measures the decay of what comes back.

Why that matters: a room whose slot was handed another room's effect produces a
tail of the other room's length. That is the reported "it sounds different in
the same map", and it is a number here instead of an opinion.

The method, and what it does not claim:

* A broadband impulse renders a decay curve; the energy decay curve (EDC) is
  fitted between -5 and -35 dB and extrapolated to RT60 (standard T30).
* The driver's decay is **not** expected to equal the map's number to the
  millisecond: early reflections and the fixed late-reverb gain make the
  broadband curve shorter than ``decayTime``. So the tool fits
  ``measured = factor x decayTime`` across the map's own rooms and judges each
  room by its residual against that line. Rooms below 1 s are reported but not
  judged -- at that length the tail is a few dozen milliseconds and the early
  reflections dominate.
* It cannot hear the *game*: the source is a click at the listener, not a
  footstep behind a wall, so occlusion, distance fade and mixing are outside it.

Usage (from ``client/``):

    python tools/audio_ear_probe.py
    python tools/audio_ear_probe.py --map ../server/maps/audio_stress.map
    python tools/audio_ear_probe.py --seconds 4 --tolerance 0.25

Exit codes: 0 every judged room followed the map's own curve, 1 a room had no
tail or was far off it, 2 the loopback device could not be opened.
"""

import argparse
import ctypes
import math
import os
import sys
import time
import xml.etree.ElementTree as ElementTree

import numpy as np

# The shipped file is `openal.dll` (the loader is case-insensitive on Windows,
# a Linux checkout is not -- the exact name is what a path check sees).
DLL_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "openal.dll")

# ---- ALC ---------------------------------------------------------------
ALC_MAJOR_VERSION = 0x1000
ALC_MINOR_VERSION = 0x1001
ALC_FREQUENCY = 0x1007
ALC_FORMAT_CHANNELS_SOFT = 0x1990
ALC_FORMAT_TYPE_SOFT = 0x1991
ALC_STEREO_SOFT = 0x1501
ALC_FLOAT_SOFT = 0x1406

# ---- AL ----------------------------------------------------------------
AL_NO_ERROR = 0
AL_SOURCE_RELATIVE = 0x0202
AL_ROLLOFF_FACTOR = 0x1021
AL_BUFFER = 0x1009
AL_FORMAT_MONO_FLOAT32 = 0x10010
AL_AUXILIARY_SEND_FILTER = 0x20006
AL_FILTER_NULL = 0
AL_EFFECT_TYPE = 0x8001
AL_EFFECT_EAXREVERB = 0x8000
AL_EFFECTSLOT_EFFECT = 0x0001

# EAXREVERB parameter names -> AL enums (cyal's names; the client sets these).
EAXREVERB_PARAMS = {
    "density": 0x0001,
    "diffusion": 0x0002,
    "gain": 0x0003,
    "gainhf": 0x0004,
    "gainlf": 0x0005,
    "decay_time": 0x0006,
    "decay_hfratio": 0x0007,
    "decay_lfratio": 0x0008,
    "reflections_gain": 0x0009,
    "reflections_delay": 0x000A,
    "late_reverb_gain": 0x000C,
    "late_reverb_delay": 0x000D,
    "echo_time": 0x000F,
    "echo_depth": 0x0010,
    "modulation_time": 0x0011,
    "modulation_depth": 0x0012,
    "air_absorption_gainhf": 0x0013,
    "hfreference": 0x0014,
    "lfreference": 0x0015,
    "room_rolloff_factor": 0x0016,
}

# Map.spawn_reverb's defaults -- the effect a map gets when an attribute is
# absent. Kept in step with world_map.py; the room's own decayTime/diffusion
# replace the two that the builder can set.
ROOM_DEFAULTS = {
    "density": 1.0, "diffusion": 1.0, "gain": 0.32, "gainhf": 0.89,
    "gainlf": 1.0, "decay_hfratio": 0.83, "decay_lfratio": 1.0,
    "reflections_gain": 0.05, "reflections_delay": 0.007,
    "late_reverb_gain": 1.26, "late_reverb_delay": 0.011,
    "echo_time": 0.25, "echo_depth": 0.0, "modulation_time": 0.25,
    "modulation_depth": 0.0, "air_absorption_gainhf": 0.994,
    "hfreference": 5000.0, "lfreference": 250.0, "room_rolloff_factor": 0.0,
}

SAMPLE_RATE = 44100
CHANNELS = 2
CHUNK = 4096
# Below this, T30 is dominated by the early reflections and the measurement
# resolution; such rooms are measured and reported, never judged.
JUDGE_FROM_SECONDS = 0.0


class Loopback:
    """OpenAL Soft's null output: real DSP, no speaker."""

    def __init__(self, path=DLL_PATH, retries=8):
        if not os.path.exists(path):
            raise FileNotFoundError(path)
        self.lib = ctypes.WinDLL(path)
        self._declare()
        self.device = None
        self.context = None
        last = None
        for _ in range(retries):
            device = self.lib.alcLoopbackOpenDeviceSOFT(None)
            attrs = (ctypes.c_int * 9)(
                ALC_MAJOR_VERSION, 1, ALC_MINOR_VERSION, 1,
                ALC_FORMAT_CHANNELS_SOFT, ALC_STEREO_SOFT,
                ALC_FORMAT_TYPE_SOFT, ALC_FLOAT_SOFT, ALC_FREQUENCY,
            )
            context = self.lib.alcCreateContext(device, attrs)
            if context:
                self.lib.alcMakeContextCurrent(context)
                self.device, self.context = device, context
                return
            # The shipped build intermittently answers ALC_INVALID_VALUE here;
            # a fresh device on the next attempt works (measured 1 in 3).
            last = self.lib.alcGetError(device)
            self.lib.alcCloseDevice(device)
            time.sleep(0.05)
        raise RuntimeError(f"loopback context refused (0x{last or 0:04x})")

    def _declare(self):
        lib = self.lib
        prototypes = {
            "alcLoopbackOpenDeviceSOFT": (ctypes.c_void_p, [ctypes.c_char_p]),
            "alcCreateContext": (ctypes.c_void_p,
                                 [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)]),
            "alcMakeContextCurrent": (ctypes.c_bool, [ctypes.c_void_p]),
            "alcGetError": (ctypes.c_int, [ctypes.c_void_p]),
            "alcCloseDevice": (ctypes.c_bool, [ctypes.c_void_p]),
            "alcDestroyContext": (None, [ctypes.c_void_p]),
            "alcRenderSamplesSOFT": (None, [ctypes.c_void_p, ctypes.c_void_p,
                                            ctypes.c_int]),
            "alGetString": (ctypes.c_char_p, [ctypes.c_int]),
            "alGetError": (ctypes.c_int, []),
            "alGenEffects": (None, [ctypes.c_int, ctypes.POINTER(ctypes.c_uint)]),
            "alDeleteEffects": (None, [ctypes.c_int, ctypes.POINTER(ctypes.c_uint)]),
            "alEffecti": (None, [ctypes.c_uint, ctypes.c_int, ctypes.c_int]),
            "alEffectf": (None, [ctypes.c_uint, ctypes.c_int, ctypes.c_float]),
            "alGenAuxiliaryEffectSlots": (None, [ctypes.c_int,
                                                 ctypes.POINTER(ctypes.c_uint)]),
            "alDeleteAuxiliaryEffectSlots": (None, [ctypes.c_int,
                                                    ctypes.POINTER(ctypes.c_uint)]),
            "alAuxiliaryEffectSloti": (None, [ctypes.c_uint, ctypes.c_int,
                                              ctypes.c_int]),
            "alGenBuffers": (None, [ctypes.c_int, ctypes.POINTER(ctypes.c_uint)]),
            "alDeleteBuffers": (None, [ctypes.c_int, ctypes.POINTER(ctypes.c_uint)]),
            "alBufferData": (None, [ctypes.c_uint, ctypes.c_int, ctypes.c_void_p,
                                    ctypes.c_int, ctypes.c_int]),
            "alGenSources": (None, [ctypes.c_int, ctypes.POINTER(ctypes.c_uint)]),
            "alDeleteSources": (None, [ctypes.c_int, ctypes.POINTER(ctypes.c_uint)]),
            "alSourcei": (None, [ctypes.c_uint, ctypes.c_int, ctypes.c_int]),
            "alSourcef": (None, [ctypes.c_uint, ctypes.c_int, ctypes.c_float]),
            "alSource3i": (None, [ctypes.c_uint, ctypes.c_int, ctypes.c_int,
                                  ctypes.c_int, ctypes.c_int]),
            "alSourcePlay": (None, [ctypes.c_uint]),
        }
        for name, (restype, argtypes) in prototypes.items():
            function = getattr(lib, name)
            function.restype = restype
            function.argtypes = argtypes

    def check(self, where):
        error = self.lib.alGetError()
        if error != AL_NO_ERROR:
            raise RuntimeError(f"{where}: AL error 0x{error:04x}")

    def renderer(self):
        return self.lib.alGetString(0xB003).decode("ascii", "replace")

    def close(self):
        if self.context:
            self.lib.alcMakeContextCurrent(None)
            self.lib.alcDestroyContext(self.context)
            self.context = None
        if self.device:
            self.lib.alcCloseDevice(self.device)
            self.device = None


def _name(function):
    """One AL object name as a plain int (ctypes wrappers do not mix)."""
    value = ctypes.c_uint(0)
    function(1, ctypes.byref(value))
    return value.value


def _render_pass(api, decay, diffusion, seconds, wet):
    """One click rendered once, with the effect attached (wet) or bypassed."""
    lib = api.lib
    effect = _name(lib.alGenEffects)
    slot = _name(lib.alGenAuxiliaryEffectSlots)
    buffer = _name(lib.alGenBuffers)
    source = _name(lib.alGenSources)
    try:
        lib.alEffecti(effect, AL_EFFECT_TYPE, AL_EFFECT_EAXREVERB)
        params = dict(ROOM_DEFAULTS)
        params["decay_time"] = float(decay)
        params["diffusion"] = float(diffusion)
        for name, value in params.items():
            lib.alEffectf(effect, EAXREVERB_PARAMS[name], float(value))
        lib.alAuxiliaryEffectSloti(slot, AL_EFFECTSLOT_EFFECT, effect if wet else 0)
        api.check("effect/slot")

        impulse = np.zeros(64, dtype=np.float32)
        impulse[0] = 0.8
        lib.alBufferData(buffer, AL_FORMAT_MONO_FLOAT32, impulse.ctypes.data,
                         impulse.nbytes, SAMPLE_RATE)
        lib.alSourcei(source, AL_BUFFER, buffer)
        lib.alSourcei(source, AL_SOURCE_RELATIVE, 1)
        lib.alSourcef(source, AL_ROLLOFF_FACTOR, 0.0)
        lib.alSource3i(source, AL_AUXILIARY_SEND_FILTER, slot, 0, AL_FILTER_NULL)
        lib.alSourcePlay(source)
        api.check("source")

        total = int(SAMPLE_RATE * seconds)
        samples = np.zeros(total * CHANNELS, dtype=np.float32)
        done = 0
        while done < total:
            frames = min(CHUNK, total - done)
            lib.alcRenderSamplesSOFT(
                api.device,
                ctypes.c_void_p(samples.ctypes.data + done * 4 * CHANNELS),
                frames,
            )
            done += frames
        api.check("render")
        return samples.reshape(-1, CHANNELS)
    finally:
        lib.alSourcei(source, AL_BUFFER, 0)
        lib.alDeleteSources(1, ctypes.byref(ctypes.c_uint(source)))
        lib.alDeleteBuffers(1, ctypes.byref(ctypes.c_uint(buffer)))
        lib.alAuxiliaryEffectSloti(slot, AL_EFFECTSLOT_EFFECT, 0)
        lib.alDeleteAuxiliaryEffectSlots(1, ctypes.byref(ctypes.c_uint(slot)))
        lib.alDeleteEffects(1, ctypes.byref(ctypes.c_uint(effect)))


def render_impulse(api, decay, diffusion, seconds):
    """The **wet tail only**: the same click rendered with and without the EFX.

    A click is a sharp, loud direct sound 20-30 dB above its own reverb, and its
    energy flattens the decay curve of a short room into nonsense. Rendering the
    identical impulse twice -- once through the slot, once with the slot
    bypassed -- and subtracting leaves only what the room added, which is what
    this tool is measuring. The driver is deterministic here (modulation depth
    is 0), and both passes are 64 samples of the same buffer.
    """
    wet = _render_pass(api, decay, diffusion, seconds, True)
    dry = _render_pass(api, decay, diffusion, seconds, False)
    return wet - dry


def measure_rt60(samples):
    """T30 of the energy decay curve, extrapolated to RT60 (seconds)."""
    mono = samples.mean(axis=1)
    if not len(mono):
        return 0.0
    energy = np.cumsum((mono.astype(np.float64) ** 2)[::-1])[::-1]
    edc = 10.0 * np.log10(energy / (energy[0] + 1e-30) + 1e-30)
    times = np.arange(len(edc)) / float(SAMPLE_RATE)
    mask = (edc <= -5.0) & (edc >= -35.0)
    if mask.sum() < 32:
        return 0.0
    slope, _ = np.polyfit(times[mask], edc[mask], 1)
    if slope >= -1e-6:
        return 0.0
    return float(-60.0 / slope)


def read_rooms(path):
    root = ElementTree.parse(path).getroot()
    body = root.find("body")
    if body is None:
        raise ValueError("map has no <body>")
    rooms = []
    for node in body:
        if node.tag != "reverb":
            continue
        rooms.append({
            "id": node.get("id") or "room",
            "decay": float(node.get("decayTime") or 1.49),
            "diffusion": float(node.get("diffusion") or 1.0),
        })
    return rooms


def fit_factor(pairs):
    """Through-origin least squares measured = factor * nominal."""
    numerator = sum(measured * nominal for nominal, measured in pairs)
    denominator = sum(nominal * nominal for nominal, measured in pairs)
    return numerator / denominator if denominator else 0.0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--map", default=os.path.join(
        os.path.dirname(__file__), "..", "..", "server", "maps", "audio_stress.map"))
    parser.add_argument("--seconds", type=float, default=0.0,
                        help="render length per room (0 = 1.35 x decay + 1 s)")
    parser.add_argument("--tolerance", type=float, default=0.25,
                        help="allowed residual against the map's own curve")
    args = parser.parse_args(argv)

    path = os.path.abspath(args.map)
    if not os.path.exists(path):
        print(f"map not found: {path}")
        return 2
    rooms = read_rooms(path)
    if not rooms:
        print("the map has no <reverb> rooms")
        return 2

    try:
        api = Loopback()
    except Exception as error:
        print(f"loopback unavailable: {error}")
        return 2

    try:
        print(f"driver: {api.renderer()}")
        print(f"map: {os.path.basename(path)}  rooms: {len(rooms)}")
        measured = {}
        for room in rooms:
            key = (room["decay"], room["diffusion"])
            if key not in measured:
                seconds = args.seconds or min(9.0, room["decay"] * 1.35 + 1.0)
                measured[key] = measure_rt60(
                    render_impulse(api, room["decay"], room["diffusion"], seconds))
            room["measured"] = measured[key]

        judged = [room for room in rooms
                  if room["decay"] >= JUDGE_FROM_SECONDS and room["measured"] > 0]
        factor = fit_factor([(room["decay"], room["measured"]) for room in judged])
        print(f"\nfitted factor measured/decay = {factor:.2f} "
              f"(over {len(judged)} judged room(s))")
        print(f"\n{'room':22s} {'decay':>6s} {'measured':>9s} {'expected':>9s} "
              f"{'residual':>9s}")
        failures = 0
        for room in rooms:
            expected = room["decay"] * factor if factor else 0.0
            residual = ((room["measured"] - expected) / expected
                        if expected else 0.0)
            judged_here = room["decay"] >= JUDGE_FROM_SECONDS
            if not judged_here:
                mark = "info"
            elif room["measured"] <= 0.05:
                mark = "FAIL no tail"
                failures += 1
            elif abs(residual) > args.tolerance:
                mark = "FAIL off curve"
                failures += 1
            else:
                mark = "ok"
            print(f"{room['id']:22s} {room['decay']:6.2f} {room['measured']:9.2f} "
                  f"{expected:9.2f} {residual:+9.1%}  {mark}")

        settings = sorted((decay, value) for (decay, _diffusion), value in measured.items())
        if len(settings) >= 2:
            # Can this metric tell two rooms apart at all? A swap between the
            # two closest settings is what the method cannot see; say the number
            # rather than implying the tool is finer than it is.
            gaps = []
            for (decay_a, value_a), (decay_b, value_b) in zip(settings, settings[1:]):
                denominator = max(value_a, value_b) or 1.0
                gaps.append((abs(value_a - value_b) / denominator, decay_a, decay_b))
            gap, low, high = min(gaps)
            widest_low, widest_high = settings[0], settings[-1]
            wrong_room = abs(widest_low[1] - widest_high[0] * factor) / (
                widest_high[0] * factor)
            print(f"\nresolution: the closest settings ({low:.2f}s and {high:.2f}s) "
                  f"measure {gap:.0%} apart -- a swap between those two is below "
                  f"this method's sight")
            gross = f"{widest_high[0]:.2f}s room measuring {widest_low[1]:.2f}s (a {widest_low[0]:.2f}s tail)"
            if wrong_room > args.tolerance:
                print(f"self-check: {gross} -> {wrong_room:.0%} off, flagged (good)")
            else:
                print(f"FAIL self-check: {gross} -> only {wrong_room:.0%} off, "
                      f"NOT flagged -- the metric cannot catch a wrong room")
                failures += 1

        print(f"\n{len(rooms)} rooms, {len(measured)} distinct setting(s), "
              f"{failures} outside the map's own curve")
        return 1 if failures else 0
    finally:
        api.close()


if __name__ == "__main__":
    sys.exit(main())
