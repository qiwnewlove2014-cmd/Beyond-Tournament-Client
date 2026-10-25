"""What the line-in guitar's harmony costs, measured on this machine.

Run from ``client/``::

    python tools/guitar_harmony_latency.py

Four numbers, because they are the four the feature is judged by:

* the line's own delay, per interval (the lead is held back by the slowest
  copy, so a bass copy sets the whole chord's delay);
* where a copy's first audio actually starts, against the string's onset;
* the largest sample-to-sample step across open, close and a bass copy joining
  -- compared with the tone's own slope, which is the floor any fade can reach;
* CPU per 20 ms frame, so a player knows what two copies cost before turning
  them on.

Everything runs through the real `GuitarHarmony` and the real shifter; only the
options table is a fake.
"""
import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from libs import instrument_harmony, options                # noqa: E402
from unittest import mock                                  # noqa: E402

SR = 48000
FRAME = 960


def lookup(**kwargs):
    table = {"guitar_harmony": True,
             "music_bot_autotune_harmony": "third",
             "music_bot_autotune_harmony2": "off"}
    table.update(kwargs)
    return lambda key, default=None: table.get(key, default)


def tone(seconds, freq=220.0):
    t = np.arange(int(SR * seconds)) / SR
    return 0.3 * np.sin(2.0 * np.pi * freq * t)


def chunks(signal):
    data = (np.clip(signal, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()
    return [data[i:i + FRAME * 2] for i in range(0, len(data), FRAME * 2)]


def play(pieces, **values):
    line = instrument_harmony.GuitarHarmony()
    out = bytearray()
    with mock.patch.object(options, "get", lookup(**values)):
        for piece in pieces:
            out += line.shape(piece)
    audio = np.frombuffer(bytes(out), dtype="<i2").astype(np.float32) / 32768.0
    return line, audio


def main():
    source = tone(3.0)
    pieces = chunks(source)
    slope = 0.3 * 2.0 * np.pi * 220.0 / SR
    print(f"tone: 0.3 at 220 Hz, {SR} Hz, {FRAME} samples a frame "
          f"(slope {slope:.4f} per sample)\n")

    print("delay of the line (lead held back to its slowest copy):")
    from libs.music_bot.transpose import ChromaticTransposer
    print(f"  the microphone's line, for comparison: "
          f"{ChromaticTransposer.LOOKAHEAD / 48.0:5.1f} ms "
          f"({ChromaticTransposer.LOOKAHEAD} samples)")
    for interval in ("third", "fifth", "sixth", "seventh", "octave",
                     "bass_third", "bass_fifth", "bass_octave"):
        line, _audio = play(pieces[:20], music_bot_autotune_harmony=interval)
        delay = line.latency_samples()
        print(f"  {interval:12s} {delay:5d} samples  {delay / 48.0:6.2f} ms")

    # A burst at frame 5: where does the copy's first audio land?
    burst = np.zeros(FRAME * 24, np.float32)
    burst[FRAME * 5:FRAME * 5 + 480] = 0.6 * np.sin(
        2.0 * np.pi * 220.0 * np.arange(480) / SR)
    line, audio = play(chunks(burst))
    onset = int(np.argmax(np.abs(audio) > 0.05))
    print(f"\nburst at sample {FRAME * 5}, output onset at {onset}: "
          f"{(onset - FRAME * 5) / 48.0:.1f} ms "
          f"({onset - FRAME * 5} samples; the line's own share is "
          f"{line.latency_samples()})")

    # Open, close, reopen, and a bass copy joining mid-chord.
    line = instrument_harmony.GuitarHarmony()
    out = bytearray()
    marks = []
    position = 0
    for state, count in (("off", 20), ("on", 30), ("off", 30), ("on", 30)):
        marks.append(position)
        lookup_now = (lookup() if state == "on" else
                      lookup(guitar_harmony=False))
        for _ in range(count):
            with mock.patch.object(options, "get", lookup_now):
                out += line.shape(pieces[position])
            position += 1
    audio = np.frombuffer(bytes(out), dtype="<i2").astype(np.float32) / 32768.0
    print("\nlargest step across a transition (the wave's own slope is "
          f"{slope:.4f}):")
    for label, mark in zip(("switch on", "switch off", "switch on again"), marks[1:]):
        window = audio[max(0, mark * FRAME - 2 * FRAME):mark * FRAME + 3 * FRAME]
        print(f"  {label:16s} {np.max(np.abs(np.diff(window))):.4f}")

    line = instrument_harmony.GuitarHarmony()
    out = bytearray()
    position = 0
    with mock.patch.object(options, "get", lookup()):
        for _ in range(30):
            out += line.shape(pieces[position])
            position += 1
    steady = np.max(np.abs(np.diff(np.frombuffer(
        bytes(out), dtype="<i2").astype(np.float32) / 32768.0)))
    with mock.patch.object(options, "get",
                           lookup(music_bot_autotune_harmony2="bass_octave")):
        for _ in range(40):
            out += line.shape(pieces[position])
            position += 1
    joined = np.max(np.abs(np.diff(np.frombuffer(
        bytes(out), dtype="<i2").astype(np.float32) / 32768.0)))
    print(f"  bass copy joining {joined:.4f} (before it joined: {steady:.4f})")

    print("\nCPU per 20 ms frame (one core is 100%):")
    for label, settings in (("one copy", {}),
                            ("two copies", {"music_bot_autotune_harmony2":
                                            "fifth"})):
        line, _audio = None, None
        line = instrument_harmony.GuitarHarmony()
        with mock.patch.object(options, "get", lookup(**settings)):
            for piece in pieces[:20]:
                line.shape(piece)
            started = time.perf_counter()
            for piece in pieces[20:220]:
                line.shape(piece)
            spent = time.perf_counter() - started
        per_frame = spent / 200.0
        print(f"  {label:12s} {per_frame * 1000:5.2f} ms/frame  "
              f"{per_frame / 0.020 * 100:5.1f}% of a core")


if __name__ == "__main__":
    main()
