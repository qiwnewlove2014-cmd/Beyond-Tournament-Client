"""What the shipped piano range really costs the listener's sample cache.

Decodes every ``data/piano`` sample with the game's own decoder and reports the
accounted size the shared cache charges for it (stereo PCM, plus the mono and
split conversions), against the cache's own limits.  A range that does not fit
inside ``max_cache_bytes`` would make the cache evict what it just prepared --
a listener's later note for an evicted sample would have to be prepared again,
and a dropped one is silent on the machine that played it.

Measured 2026-09-21, the whole range fits with room to spare: 85 samples,
accounted 95.5 MiB of the 192 MiB limit, the largest single sample 1.24 MiB of
the 32 MiB per-sample limit (44100 Hz stereo, 2.3-2.9 s each).  So a re-decoded
sample is not what loses a note at the density a player can reach, and the range
the cache must hold is bounded by the shipped set rather than by the listener's
memory -- run this again after adding samples.
"""

import os
import sys
from pathlib import Path

CLIENT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CLIENT))

from libs.audio_manager import AudioManager  # noqa: E402


def main():
    directory = CLIENT / "data" / "piano"
    names = sorted(os.listdir(directory))
    rows = []
    total = 0
    for name in names:
        path = str(directory / name)
        decoded = AudioManager._decode_instrument_sample(path)
        pcm = bytes(decoded.buffer)
        channels = decoded.channels
        rate = decoded.frequency
        account = len(pcm) * 2.5 if channels == 2 else len(pcm)
        frames = len(pcm) // (2 * channels)
        rows.append((name, channels, rate, frames, len(pcm), int(account)))
        total += account
        del decoded

    print(f"{'sample':28s} {'ch':>2s} {'rate':>6s} {'s':>6s} "
          f"{'pcm MB':>8s} {'cache MB':>9s}")
    for name, channels, rate, frames, pcm, account in rows:
        print(f"{name:28s} {channels:2d} {rate:6d} "
              f"{frames / rate:6.2f} {pcm / 2**20:8.2f} {account / 2**20:9.2f}")
    print()
    print(f"samples:              {len(rows)}")
    print(f"accounted total:      {total / 2**20:.1f} MiB")
    print(f"max_cache_bytes:      {192.0:>7.1f} MiB "
          f"(fit: {'YES' if total <= 192 * 2**20 else 'NO -- LRU evicts'})")
    print(f"max_sample_bytes:     {32.0:>7.1f} MiB "
          f"(largest sample: {max(row[5] for row in rows) / 2**20:.2f} MiB)")
    whole = sum(row[5] for row in rows)
    print(f"whole-range batch status gate: "
          f"{'passes' if whole <= 192 * 2**20 else 'FAILS for every note'}")


if __name__ == "__main__":
    main()
