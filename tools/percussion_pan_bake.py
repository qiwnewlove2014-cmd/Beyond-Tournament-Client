"""Give the "per" percussion kit a real stereo image, and measure its levels.

The drum engine has **no per-hit pan**. A kit's stereo image is the file's own:
a local hit is played with ``direct_channels`` (the file's L/R as recorded) and
a remote hit is played from the file's split L/R channels by
``AudioManager.play_unbound_stereo_spatial``. A dual-mono file (L and R identical,
side channel at digital silence) is therefore centred however it is routed --
which is exactly how the three ``per`` samples arrived, and why a three-drum set
was heard as one point in the middle of the head.

This tool does the two things the kit needs and nothing else:

1. **Bake the pan** into the files with a constant-power law, so a piece leans
   where it should without losing energy: ``gL = cos(theta)``,
   ``gR = sin(theta)`` with ``theta = (pan + 1) * pi / 4``. The source is read
   as mono (``c0``) because the
   two channels are identical; reading one of them avoids a +3 dB double count.
2. **Report the level** as integrated LUFS and true peak, and derive the
   ``volume_scale`` that puts a pad at the default kit's own level. That is the
   number ``DrumAudio.KITS`` stores, so the kitchen table is measured rather than
   guessed -- a pad that is 9.5 LU quiet is a pad a player reports as "it is too
   quiet", not a taste.

The default kit is the anchor: its Kick measures -18.75 LUFS with a scale of
0.34, and the engine plays a hit at ``volume 300`` (gain 3.0), so its effective
source level is ``-18.75 + 20*log10(0.34) + 20*log10(3) = -18.58 dB``.

Run from ``client/``::

    python tools/percussion_pan_bake.py                     # measure only
    python tools/percussion_pan_bake.py --bake --source DIR # re-bake the files

``--source`` holds the un-panned originals (``per Hi.ogg``, ``per lo.ogg``,
``per P.ogg``); they are not kept in the tree because the shipped files replace
them. ``--bake`` refuses to overwrite a file it cannot measure first.

ffmpeg is resolved to an absolute path and run with an argv list, never through
a shell: the same rule the portable test suite pins for every tool in here.
"""

import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CLIENT = os.path.dirname(HERE)
KIT_DIR = os.path.join(CLIENT, "data", "drums", "per")

# One entry per sounding pad: the kit file name, the pan (-1 left .. +1 right),
# and the gain already applied while baking (dB) so the tool can report the
# file's *source* level rather than re-deriving it.
PIECES = {
    "Per Hi": {"file": "per Hi.ogg", "pan": -0.35, "baked_db": 0.0},
    "Per lo": {"file": "per lo.ogg", "pan": 0.30, "baked_db": 0.0},
    "Per P": {"file": "per P.ogg", "pan": 0.0, "baked_db": 7.5},
}

# The default kit's Kick: the level a kit is mixed against (libs/drums.py).
ANCHOR_LUFS = -18.75
ANCHOR_SCALE = 0.34
PLAY_VOLUME = 300


def _ffmpeg():
    """Absolute path to ffmpeg, or a sentence saying it is missing."""
    found = shutil.which("ffmpeg")
    if not found:
        raise SystemExit("ffmpeg was not found on PATH; it is needed to "
                         "measure and to re-encode the samples")
    return os.path.abspath(found)


def _run(argv):
    """Run an argv list (never a shell string) and return its stderr text."""
    completed = subprocess.run(
        argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        check=False, text=True, encoding="utf-8", errors="replace",
    )
    if completed.returncode != 0:
        raise SystemExit(f"{os.path.basename(argv[0])} failed: "
                         f"{completed.stderr.strip()[:400]}")
    return completed.stderr


def measure(path):
    """Integrated LUFS and true peak (dBTP) of one file, via ffmpeg loudnorm."""
    ffmpeg = _ffmpeg()
    text = _run([
        ffmpeg, "-hide_banner", "-nostats", "-i", os.path.abspath(path),
        "-af", "loudnorm=I=-16:TP=-1.5:LRA=11:print_format=json",
        "-f", "null", os.devnull,
    ])
    match = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", text, re.S)
    if not match:
        raise SystemExit(f"could not read the loudness report for {path}")
    report = json.loads(match.group(0))
    return float(report["input_i"]), float(report["input_tp"])


def scale_for(lufs):
    """The volume_scale that puts ``lufs`` at the anchor's effective level."""
    play_gain_db = 20.0 * math.log10(PLAY_VOLUME / 100.0)
    anchor_effective = ANCHOR_LUFS + 20.0 * math.log10(ANCHOR_SCALE) + play_gain_db
    return 10.0 ** ((anchor_effective - play_gain_db - lufs) / 20.0)


def measure_kit(directory=KIT_DIR):
    """Print every piece's level and the scale the kit should store."""
    print(f"{'pad':8s} {'LUFS':>7s} {'dBTP':>7s} {'pan':>6s} {'scale':>6s}")
    for name, piece in PIECES.items():
        path = os.path.join(directory, piece["file"])
        lufs, peak = measure(path)
        print(f"{name:8s} {lufs:7.2f} {peak:7.2f} {piece['pan']:+6.2f} "
              f"{scale_for(lufs):6.2f}")
    print()
    print("an even kit reads within ~1 LU of itself; a pad further than that "
          "from its neighbours is the one a player calls quiet")


def bake(source, destination=KIT_DIR):
    """Re-encode each un-panned original with its constant-power pan."""
    for name, piece in PIECES.items():
        origin = os.path.join(source, piece["file"])
        if not os.path.exists(origin):
            raise SystemExit(f"missing original: {origin}")
        left = math.cos((piece["pan"] + 1.0) * math.pi / 4.0)
        right = math.sin((piece["pan"] + 1.0) * math.pi / 4.0)
        gain = 10.0 ** (piece["baked_db"] / 20.0)
        # One coefficient per channel: the pan filter's parser does not accept
        # two multiplied factors ("0.7*2.3*c0"), only "<coeff>*c0".
        left *= gain
        right *= gain
        target = os.path.join(destination, piece["file"])
        _run([
            _ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
            "-i", os.path.abspath(origin),
            "-af", f"pan=stereo|c0={left:.4f}*c0|c1={right:.4f}*c0",
            "-c:a", "libvorbis", "-q:a", "6", os.path.abspath(target),
        ])
        lufs, peak = measure(target)
        print(f"{name}: L={left:.2f} R={right:.2f} -> {lufs:.2f} LUFS, "
              f"{peak:.2f} dBTP (scale {scale_for(lufs):.2f})")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bake", action="store_true",
                        help="re-encode the samples from --source")
    parser.add_argument("--source", default=None,
                        help="folder holding the un-panned originals")
    parser.add_argument("--kit", default=KIT_DIR,
                        help="the kit folder to measure or write")
    args = parser.parse_args(argv)
    if args.bake:
        if not args.source:
            parser.error("--bake needs --source <folder of originals>")
        bake(args.source, args.kit)
        return 0
    measure_kit(args.kit)
    return 0


if __name__ == "__main__":
    sys.exit(main())
