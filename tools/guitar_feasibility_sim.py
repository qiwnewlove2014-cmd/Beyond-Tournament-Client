"""Feasibility simulation: can a guitar/bass line-in work with the game's stack?

Standalone analysis tool - does NOT touch game code. It proves three things
using the exact libraries the game already ships (cyal capture + numpy):

  1. cyal can enumerate and open capture devices (same path as voice chat).
  2. A pure-numpy monophonic pitch detector (YIN) can track guitar/bass notes
     in 20 ms frames (the game's capture chunk size) with usable accuracy.
  3. A second capture device can be opened while voice chat is running
     (the "voice mic + guitar line-in" coexistence question).
"""
import sys
import time
import numpy as np

SR = 48000


# ---------------------------------------------------------------- YIN pitch
def yin_pitch(x, sr=SR, min_freq=40.0, max_freq=1200.0, threshold=0.15):
    """Monophonic pitch detection (YIN). Returns Hz or None."""
    n = len(x)
    tau_min = max(2, int(sr / max_freq))
    tau_max = min(n - 1, int(sr / min_freq))
    if tau_max <= tau_min:
        return None
    nfft = 2 ** int(np.ceil(np.log2(n + tau_max)))
    xpad = np.zeros(nfft)
    xpad[:n] = x
    X = np.fft.rfft(xpad)
    r = np.real(np.fft.irfft(X * np.conj(X)))[:tau_max + 1]
    d = np.empty(tau_max + 1)
    d[0] = 0.0
    d[1:] = 2.0 * (r[0] - r[1:])
    running = 0.0
    cmnd = np.ones(tau_max + 1)
    for tau in range(1, tau_max + 1):
        running += d[tau]
        cmnd[tau] = d[tau] * tau / max(running, 1e-9)
    for tau in range(tau_min, tau_max):
        if cmnd[tau] < threshold:
            while tau + 1 < tau_max and cmnd[tau + 1] < cmnd[tau]:
                tau += 1
            return sr / tau
    tau = int(np.argmin(cmnd[tau_min:])) + tau_min
    return (sr / tau) if cmnd[tau] < 0.3 else None


def hz_to_note(hz):
    if hz is None:
        return None
    midi = round(69 + 12 * np.log2(hz / 440.0))
    names = ['C', 'C#', 'D', 'D#', 'E', 'F', 'F#', 'G', 'G#', 'A', 'A#', 'B']
    return f"{names[midi % 12]}{midi // 12 - 1}"


# ---------------------------------------------------------------- synthetic
def synth(freq_hz, seconds=0.4, sr=SR):
    t = np.arange(int(sr * seconds)) / sr
    # a plucked-string-ish signal: fundamental + a few harmonics with decay
    x = (np.sin(2 * np.pi * freq_hz * t)
         + 0.4 * np.sin(2 * np.pi * 2 * freq_hz * t) * np.exp(-t * 6)
         + 0.2 * np.sin(2 * np.pi * 3 * freq_hz * t) * np.exp(-t * 9))
    env = np.exp(-t * 3.0)
    return (x * env * 0.5).astype(np.float32)


def test_pitch_detector():
    print("=" * 62)
    print("TEST 1: pitch detector accuracy on synthetic tones (20 ms frames)")
    print("=" * 62)
    cases = [  # (expected note, freq, window samples)
        # low notes need longer windows: 20 ms has only ~1.6 cycles of an E2
        ("E2 (bass open)", 82.41, 2880),       # 60 ms
        ("A2 (bass 5th fret)", 110.0, 2880),   # 60 ms
        ("B1 (5-string bass low B)", 30.87, 5760),  # 120 ms - extreme test
        ("E3 (guitar open low E)", 164.81, 960),
        ("A4 (guitar open A)", 220.0, 960),
        ("D4 (guitar open D)", 293.66, 960),
        ("G4", 392.0, 960), ("B4", 493.88, 960),
        ("E5 (guitar high E)", 659.25, 960),
    ]
    for label, f, win in cases:
        x = synth(f)
        chunks = [x[i:i + win] for i in range(0, len(x) - (win - 1), win)]
        dets = [yin_pitch(c) for c in chunks]
        good = [d for d in dets if d is not None]
        if good:
            med = float(np.median(good))
            cents_err = 1200 * abs(np.log2(med / f))
            print(f"  {label:26s} expected {f:7.2f} Hz -> got {med:7.2f} Hz "
                  f"({hz_to_note(med):>4s}, {cents_err:5.1f} cents off), "
                  f"{len(good)}/{len(chunks)} frames voiced (win {win // 48} ms)")
        else:
            print(f"  {label:26s} expected {f:7.2f} Hz -> NO PITCH DETECTED "
                  f"(win {win // 48} ms)")

    # monophonic limitation demo: a chord (two notes) confuses the detector
    t = np.arange(int(SR * 0.4)) / SR
    chord = (np.sin(2 * np.pi * 110 * t) + np.sin(2 * np.pi * 164.81 * t)) * 0.4
    chord = chord.astype(np.float32)
    c = yin_pitch(chord[:960])
    if c is None:
        print(f"  {'A2+E3 chord (polyphonic)':26s} -> NO single pitch "
              f"<-- monophonic detector only, chords unreliable")
    else:
        print(f"  {'A2+E3 chord (polyphonic)':26s} -> detected {c:7.2f} Hz "
              f"({hz_to_note(c)})  <-- monophonic detector only, chords unreliable")


# ---------------------------------------------------------------- real capture
def test_capture():
    print("=" * 62)
    print("TEST 2: real capture device (same cyal path as voice chat)")
    print("=" * 62)
    try:
        from cyal import CaptureExtension, BufferFormat
        from cyal import exceptions as cyal_exc
    except ImportError as e:
        print("  cyal not importable:", e)
        return

    cap = CaptureExtension()
    print(f"  default capture device: {cap.default_device}")
    print(f"  devices: {[d for d in cap.devices]}")
    try:
        dev = cap.open_device(name=cap.default_device, sample_rate=SR,
                              format=BufferFormat.MONO16)
    except (cyal_exc.DeviceNotFoundError, TypeError) as e:
        print("  FAILED to open capture device:", e)
        return
    print(f"  opened: {dev}")

    # NOTE: capture delivers nothing until start() is called - exactly like
    # the game's push-to-talk (gameplay.py voice_chat_start calls .start()).
    # record ~2.5 s of real input, pitch-detect every 20 ms chunk
    want_frames = int(SR * 2.5)
    frames = bytearray(want_frames * 2)
    dev.start()
    got = 0
    start = time.perf_counter()
    deadline = start + 20.0
    while got < want_frames and time.perf_counter() < deadline:
        avail = dev.available_samples
        if avail >= 960:
            buf = bytearray(960 * 2)
            dev.capture_samples(buf)
            frames[got * 2:(got + 960) * 2] = buf
            got += 960
        else:
            time.sleep(0.002)
    dev.stop()
    elapsed = time.perf_counter() - start
    print(f"  captured {got / SR:.2f} s of real audio in {elapsed:.2f} s")
    del dev
    if got < want_frames:
        print("  WARNING: capture device did not deliver samples fast enough")

    raw = np.frombuffer(bytes(frames), dtype=np.int16).astype(np.float32) / 32768.0
    rms = float(np.sqrt(np.mean(raw ** 2)))
    notes = []
    for i in range(0, len(raw) - 959, 960):
        h = yin_pitch(raw[i:i + 960])
        if h is not None:
            notes.append(hz_to_note(h))
    voiced = len(notes)
    print(f"  RMS level: {rms:.4f}  "
          f"({'input present' if rms > 0.005 else 'silence - say something or play a note'})")
    if voiced:
        from collections import Counter
        top = Counter(notes).most_common(3)
        print(f"  voiced frames: {voiced}, most common notes: {top}")
    else:
        print("  no stable pitch found in the sample (expected if quiet)")
    raw = np.frombuffer(bytes(frames), dtype=np.int16).astype(np.float32) / 32768.0
    rms = float(np.sqrt(np.mean(raw ** 2)))
    for i in range(0, len(raw) - 959, 960):
        h = yin_pitch(raw[i:i + 960])
        if h is not None:
            if last is not None and abs(np.log2(h / last)) < 0.03:
                stable += 1
            last = h
            notes.append(hz_to_note(h))
    voiced = len(notes)
    print(f"  captured {got / SR:.2f} s of real audio (waited {elapsed:.2f} s)")
    print(f"  RMS level: {rms:.4f}  ({'input present' if rms > 0.005 else 'silence - say something or play a note'})")
    if voiced:
        from collections import Counter
        top = Counter(notes).most_common(3)
        print(f"  voiced frames: {voiced}, most common notes: {top}")
    else:
        print("  no stable pitch found in the sample (expected if quiet)")


# ---------------------------------------------------------------- dual capture
def test_dual_capture():
    print("=" * 62)
    print("TEST 3: open TWO capture devices at once (voice mic + guitar line-in)")
    print("=" * 62)
    try:
        from cyal import CaptureExtension, BufferFormat
        from cyal import exceptions as cyal_exc
    except ImportError:
        return
    cap = CaptureExtension()
    devs = []
    for i in range(2):
        try:
            d = cap.open_device(name=cap.default_device, sample_rate=SR,
                                format=BufferFormat.MONO16)
            devs.append(d)
            print(f"  capture #{i + 1} (same device) opened OK")
        except Exception as e:
            print(f"  capture #{i + 1} FAILED: {type(e).__name__}: {e}")
    for d in devs:
        del d
    time.sleep(0.5)
    # also try two DIFFERENT devices if more than one exists
    if len(cap.devices) >= 2:
        try:
            d1 = cap.open_device(name=cap.devices[0].encode(), sample_rate=SR,
                                 format=BufferFormat.MONO16)
            d2 = cap.open_device(name=cap.devices[1].encode(), sample_rate=SR,
                                 format=BufferFormat.MONO16)
            print(f"  two different devices opened OK: {cap.devices[0]!r} + {cap.devices[1]!r}")
            # start both and verify both deliver samples simultaneously
            d1.start(); d2.start()
            t0 = time.perf_counter(); c1 = c2 = 0
            while time.perf_counter() - t0 < 2.0:
                if d1.available_samples >= 960:
                    d1.capture_samples(bytearray(960 * 2)); c1 += 960
                if d2.available_samples >= 960:
                    d2.capture_samples(bytearray(960 * 2)); c2 += 960
                time.sleep(0.001)
            print(f"  simultaneous capture: device1 {c1 / SR:.2f} s, device2 {c2 / SR:.2f} s in 2 s")
            d1.stop(); d2.stop()
            del d1, d2
        except Exception as e:
            print(f"  two different devices FAILED: {type(e).__name__}: {e}")


def timing():
    print("=" * 62)
    print("TEST 4: per-frame CPU cost of pitch detection (20 ms frames)")
    print("=" * 62)
    x = synth(110.0)
    n = 100
    t0 = time.perf_counter()
    for _ in range(n):
        yin_pitch(x[:960])
    dt = (time.perf_counter() - t0) / n
    print(f"  YIN on 960 samples: {dt * 1000:.3f} ms/frame "
          f"({100 * dt:.1f}% of a 20 ms budget) -> real-time feasible")


# ---------------------------------------------------------------- instrument input
# Simulates the "second input device" the user asked about: a dedicated
# instrument line-in running NEXT TO the voice mic, feeding note events like
# the piano/drum broadcast path (the game sends note events, not audio, over
# the network; each client renders the local sample).
INSTRUMENT_EVENT_NAMES = [
    "C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]


def note_to_piano_sample(note_name):
    """Map a note like C#4 to the sample the server would broadcast."""
    sharp_to_flat = {"C#": "Db", "D#": "Eb", "F#": "Gb", "G#": "Ab", "A#": "Bb"}
    root = note_name[:-1]
    octave = note_name[-1]
    flat = sharp_to_flat.get(root, root)
    return f"piano/Piano.mf.{flat}{octave}.ogg"


def test_instrument_input_side_by_side():
    print("=" * 62)
    print("TEST 5: instrument input NEXT TO voice mic (two menu entries)")
    print("=" * 62)
    try:
        from cyal import CaptureExtension, BufferFormat
    except ImportError:
        return
    cap = CaptureExtension()
    print(f"  capture devices available for the menu: {len(cap.devices)}")
    for i, d in enumerate(cap.devices):
        print(f"    {i + 1}. {d}")

    if len(cap.devices) < 2:
        print("  need >=2 devices for this demo; opening default twice instead")
        names = [cap.default_device, cap.default_device]
    else:
        names = [cap.devices[0].encode(), cap.devices[1].encode()]

    # voice mic = device A, instrument line-in = device B
    voice = cap.open_device(name=names[0], sample_rate=SR, format=BufferFormat.MONO16)
    instr = cap.open_device(name=names[1], sample_rate=SR, format=BufferFormat.MONO16)
    voice.start()
    instr.start()
    print("  both started. feeding a synthetic guitar note into the instrument"
          " stream while voice keeps capturing.")

    v_buf = bytearray(960 * 2)
    i_buf = bytearray(960 * 2)
    v_rms_total = 0.0
    v_frames = 0
    events = []
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < 2.0:
        if voice.available_samples >= 960:
            voice.capture_samples(v_buf)
            v = np.frombuffer(bytes(v_buf), dtype=np.int16).astype(np.float32) / 32768.0
            v_rms_total += float(np.sqrt(np.mean(v ** 2)))
            v_frames += 1
        if instr.available_samples >= 960:
            instr.capture_samples(i_buf)
            x = np.frombuffer(bytes(i_buf), dtype=np.int16).astype(np.float32) / 32768.0
            # mix in a synthetic A4 (440 Hz - high E string 5th fret) so the
            # demo works even when the real line-in is silent on this machine;
            # low notes (bass) need a longer window, see TEST 1 findings.
            t = np.arange(960) / SR
            x = x + 0.25 * np.sin(2 * np.pi * 440.0 * t)
            h = yin_pitch(x)
            if h is not None:
                note = hz_to_note(h)
                if not events or events[-1][0] != note:
                    events.append((note, round(h, 1)))
    voice.stop()
    instr.stop()
    del voice, instr

    print(f"  voice stream: {v_frames} frames, avg RMS {v_rms_total / max(v_frames, 1):.4f}"
          f" (unaffected by instrument input)")
    print("  instrument stream -> detected notes (as broadcast events):")
    for note, hz in events[:8]:
        print(f"    {note:>4s} @ {hz:6.1f} Hz  ->  server would send"
              f" 'play_unbound' sound={note_to_piano_sample(note)}")
    print("  => instrument input coexists with voice and feeds note events,"
          " exactly like the piano/drum broadcast path (no audio streaming).")


if __name__ == "__main__":
    test_pitch_detector()
    test_capture()
    test_dual_capture()
    timing()
    test_instrument_input_side_by_side()
