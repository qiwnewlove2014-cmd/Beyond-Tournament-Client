"""Bake five tuned notes from one drum stroke, without changing its length.

A drum hit cannot be tuned with OpenAL's source ``pitch``: that is varispeed, so
the pitch and the duration move together -- a fifth down makes a 1.6 s hit 2.1 s
long ("it sounds slow"), a fifth up makes it 1.2 s ("it sounds squeezed"). This
tool decouples them the only way that works offline: **WSOLA time-stretch, then
resample**. Stretch by the pitch ratio first (same pitch, longer), then resample
by that ratio (back to the original length, pitch multiplied). Measured on this
kit: every note comes out at the source's own 1.594 s.

What it cannot fix is the timbre: moving a drum's modes by a semitone or five
still changes its body. That is why the scale is built from a stroke whose own
pitch is only 4-5 semitones away from its notes (the per Hi stroke measures
~275.6 Hz, essentially C#4, and A minor pentatonic sits at -4..+5 semitones).
Beyond about +/-6 the same code produces a different drum, not a tuned one.

The notes are relative to the *measured* stroke, not to an assumed C#4, so the
intervals between them are exact (equal temperament) however flat the source is.
Each note also gets a small constant-power pan, so a scale reads left to right
the way a keyboard does.

Run from ``client/``::

    python tools/percussion_tune_bake.py            # write the five notes
    python tools/percussion_tune_bake.py --measure  # only report the shipped ones

ffmpeg (resolved to an absolute path, argv list) does the OGG encode; numpy does
the DSP. Nothing here runs in the game -- the kit is just files.
"""

import argparse
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import wave

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
CLIENT = os.path.dirname(HERE)
DATA = os.path.join(CLIENT, "data")
SOURCE = os.path.join(DATA, "drums", "per", "per Hi.ogg")
KIT_DIR = os.path.join(DATA, "drums", "per_tuned")

SR = 44100
# The stroke's own pitch, measured with libs.pitch.yin_pitch on its body (60 ms
# window, 100 ms after the attack). Every target is a ratio from this number.
REFERENCE_HZ = 275.6

# A minor pentatonic: A3 C4 D4 E4 G4. The pan spreads the five notes left to
# right by a fifth of the field, the way a keyboard diagram reads.
NOTES = (
    ("A3", 220.00, -0.20),
    ("C4", 261.63, -0.10),
    ("D4", 293.66, 0.00),
    ("E4", 329.63, 0.10),
    ("G4", 392.00, 0.20),
)

# The default kit's Kick: the level every kit is mixed against (libs/drums.py).
ANCHOR_LUFS = -18.75
ANCHOR_SCALE = 0.34
PLAY_VOLUME = 300


def _ffmpeg():
    found = shutil.which("ffmpeg")
    if not found:
        raise SystemExit("ffmpeg was not found on PATH; it is needed to encode")
    return os.path.abspath(found)


def _run(argv):
    completed = subprocess.run(
        argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        check=False, text=True, encoding="utf-8", errors="replace",
    )
    if completed.returncode != 0:
        raise SystemExit(f"{os.path.basename(argv[0])} failed: "
                         f"{completed.stderr.strip()[:400]}")
    return completed.stderr


def decode_mono(path):
    """Decode an OGG to a mono float array (the stroke is panned; sum it)."""
    sys.path.insert(0, CLIENT)
    from libs.safe_vorbis import load_vorbis_pcm

    relative = os.path.relpath(path, os.getcwd())
    pcm = load_vorbis_pcm(relative, max_pcm_bytes=32 * 1024 * 1024)
    data = np.frombuffer(pcm.buffer, dtype=np.int16).astype(np.float64) / 32768.0
    if pcm.channels == 2:
        data = data.reshape(-1, 2).mean(axis=1)
    return data, pcm.frequency


def stretch(x, factor, frame=2048, search=256):
    """WSOLA time-stretch by ``factor`` (>1 = longer). Pitch is untouched."""
    if abs(factor - 1.0) < 1e-6:
        return x.copy()
    hop = frame // 2
    window = np.hanning(frame)
    out_len = int(round(len(x) * factor)) + frame
    out = np.zeros(out_len)
    norm = np.zeros(out_len)
    analysis = 0.0
    out_pos = 0
    previous = None
    while out_pos + frame < out_len:
        target = int(round(analysis))
        if previous is None or len(x) - frame <= 0:
            take = min(max(target, 0), max(len(x) - frame, 0))
        else:
            low = max(0, target - search)
            high = min(len(x) - frame, target + search)
            if high <= low:
                take = min(max(target, 0), max(len(x) - frame, 0))
            else:
                segment = x[low:high + frame]
                corr = np.correlate(segment, previous[:frame // 2], mode="valid")
                take = low + int(np.argmax(corr))
        piece = x[take:take + frame]
        if len(piece) < frame:
            break
        out[out_pos:out_pos + frame] += piece * window
        norm[out_pos:out_pos + frame] += window
        previous = piece
        analysis += hop / factor
        out_pos += hop
    norm[norm < 1e-6] = 1.0
    return (out / norm)[:int(round(len(x) * factor))]


def resample(x, ratio):
    """Linear resample: length / ratio, pitch * ratio."""
    if abs(ratio - 1.0) < 1e-9:
        return x.copy()
    count = int(round(len(x) / ratio))
    idx = np.linspace(0, len(x) - 1, count)
    return np.interp(idx, np.arange(len(x)), x)


def tune(x, semitones):
    """Pitch by ``semitones``, keeping the duration."""
    ratio = 2.0 ** (semitones / 12.0)
    return resample(stretch(x, ratio), ratio)


def pan(x, position):
    """Constant-power pan of a mono signal; returns interleaved stereo."""
    theta = (position + 1.0) * math.pi / 4.0
    left = x * math.cos(theta)
    right = x * math.sin(theta)
    stereo = np.stack((left, right), axis=1)
    peak = float(np.max(np.abs(stereo))) if stereo.size else 0.0
    if peak > 0.98:
        stereo *= 0.98 / peak
    return stereo


def write_wav(path, stereo, rate=SR):
    pcm = np.clip(stereo * 32767.0, -32768, 32767).astype(np.int16)
    with wave.open(path, "wb") as handle:
        handle.setnchannels(2)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(pcm.tobytes())


def encode(wav_path, ogg_path):
    _run([
        _ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
        "-i", os.path.abspath(wav_path),
        "-c:a", "libvorbis", "-q:a", "6", os.path.abspath(ogg_path),
    ])


def measure(path):
    text = _run([
        _ffmpeg(), "-hide_banner", "-nostats", "-i", os.path.abspath(path),
        "-af", "loudnorm=I=-16:TP=-1.5:LRA=11:print_format=json",
        "-f", "null", os.devnull,
    ])
    match = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", text, re.S)
    if not match:
        raise SystemExit(f"could not read the loudness report for {path}")
    import json
    report = json.loads(match.group(0))
    return float(report["input_i"]), float(report["input_tp"])


def expected_scale(lufs):
    play_gain_db = 20.0 * math.log10(PLAY_VOLUME / 100.0)
    anchor = ANCHOR_LUFS + 20.0 * math.log10(ANCHOR_SCALE) + play_gain_db
    return 10.0 ** ((anchor - play_gain_db - lufs) / 20.0)


def spectral_stretch(source, output, lo=0.60, hi=1.70, steps=2201):
    """How far ``output``'s spectrum is stretched against ``source``'s.

    A drum is inharmonic, so a period tracker (YIN) locks a different mode after
    a shift and reports errors of tens of cents on a perfectly shifted file --
    measured: E4 reads -41 cents while its spectral stretch is 1.2325 against a
    requested 1.2318. The spectral envelope is what moves when a sound is tuned,
    so that is what this measures: for each candidate ratio, compare the source's
    log spectrum with the output sampled at ``f * ratio``, and keep the best
    correlation.
    """
    def spectrum(x):
        start = int(0.08 * SR)
        window = np.hanning(8192)
        segment = x[start:start + 8192] * window
        return (np.fft.rfftfreq(8192, 1.0 / SR), np.abs(np.fft.rfft(segment)))

    source_f, source_m = spectrum(source)
    output_f, output_m = spectrum(output)
    band = (source_f > 80) & (source_f < 4000)
    source_f, source_m = source_f[band], source_m[band]
    a = np.log(source_m + 1e-9)
    a -= a.mean()
    norm_a = np.linalg.norm(a) + 1e-12
    best_ratio, best_score = 1.0, -1e9
    for ratio in np.linspace(lo, hi, steps):
        warped = np.log(np.interp(source_f * ratio, output_f, output_m) + 1e-9)
        warped -= warped.mean()
        score = float(np.dot(a, warped) / (norm_a * (np.linalg.norm(warped) + 1e-12)))
        if score > best_score:
            best_ratio, best_score = ratio, score
    return best_ratio, best_score


def measure_kit(directory=KIT_DIR):
    source, rate = decode_mono(SOURCE)
    print(f"{'note':5s} {'target':>8s} {'stretch':>8s} {'cents':>7s} "
          f"{'length':>7s} {'LUFS':>7s} {'scale':>6s}")
    stretches = {}
    for name, target, _pan_position in NOTES:
        path = os.path.join(directory, f"per {name}.ogg")
        x, rate = decode_mono(path)
        stretch, _score = spectral_stretch(source, x)
        stretches[name] = stretch
        cents = 1200.0 * math.log2(stretch / (target / REFERENCE_HZ))
        lufs, _peak = measure(path)
        print(f"{name:5s} {target:8.2f} {stretch:8.4f} {cents:+7.1f} "
              f"{len(x) / rate:7.3f} {lufs:7.2f} {expected_scale(lufs):6.2f}")

    print("\ninterval check against equal temperament (relative to A3):")
    root_measured = 1200.0 * math.log2(stretches[NOTES[0][0]])
    for name, target, _pan_position in NOTES:
        measured = 1200.0 * math.log2(stretches[name]) - root_measured
        wanted = 1200.0 * math.log2(target / NOTES[0][1])
        print(f"  {name}: {measured:+7.1f} cents above A3 "
              f"(target {wanted:+7.1f})")


def bake(directory=KIT_DIR):
    """Bake the five notes and verify each against its own spectrum.

    The shift ratio is exact by construction (stretch by r, resample by r); what
    needs proving is that the *result* was stretched by the ratio asked for, so
    every note is measured with ``spectral_stretch`` and reported in cents. No
    calibration loop: a period tracker's reading of a shifted drum is not a
    trustworthy thing to steer by (see ``spectral_stretch``).
    """
    os.makedirs(directory, exist_ok=True)
    source, rate = decode_mono(SOURCE)
    print(f"source {os.path.basename(SOURCE)}: {len(source) / rate:.3f}s mono, "
          f"reference {REFERENCE_HZ:.2f} Hz")
    with tempfile.TemporaryDirectory() as tmp:
        for name, target, pan_position in NOTES:
            semitones = 12.0 * math.log2(target / REFERENCE_HZ)
            tuned = tune(source, semitones)
            stereo = pan(tuned, pan_position)
            wav_path = os.path.join(tmp, f"{name}.wav")
            write_wav(wav_path, stereo, rate)
            ogg_path = os.path.join(directory, f"per {name}.ogg")
            encode(wav_path, ogg_path)
            made, _rate = decode_mono(ogg_path)
            stretch, _score = spectral_stretch(source, made)
            cents = 1200.0 * math.log2(stretch / (target / REFERENCE_HZ))
            lufs, peak = measure(ogg_path)
            print(f"  {name}: {semitones:+6.2f} st, pan {pan_position:+.2f}, "
                  f"{len(tuned) / rate:.3f}s, stretch {stretch:.4f} "
                  f"({cents:+.1f} cents), {lufs:.2f} LUFS, {peak:.2f} dBTP, "
                  f"scale {expected_scale(lufs):.2f}")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bake", action="store_true",
                        help="write the five notes (default when --measure is absent)")
    parser.add_argument("--measure", action="store_true",
                        help="only measure the notes already in the kit folder")
    parser.add_argument("--kit", default=KIT_DIR)
    args = parser.parse_args(argv)
    if args.measure:
        measure_kit(args.kit)
        return 0
    return bake(args.kit)


if __name__ == "__main__":
    sys.exit(main())
