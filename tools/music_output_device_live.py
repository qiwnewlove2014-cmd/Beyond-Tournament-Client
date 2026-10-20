"""Rig: does the Music Bot's other device really play the frames it is given?

Everything here is real except the tone: a real `MusicOutputProcess` spawns the
real child (`--bt-music-device`), the child opens a real OpenAL device on this
machine, and the frames are a real 440 Hz stereo tone at the streamer's own
format (48 kHz, 20 ms, interleaved 16-bit). The child plays them out loud and
reports how many it took on stdout, which this rig reads back.

Run it (the argument is optional; without one, the machine's default device):

    python tools/music_output_device_live.py
    python tools/music_output_device_live.py "OpenAL Soft on Speakers"

The streamer's own contract is exercised too: `push` answers False when the
child's queue is full, and the rig retries that frame rather than dropping it --
the same loop `AudioStreamer._queue_local` runs.
"""

import array
import math
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from libs.music_bot import output_device


RATE = 48000
SAMPLES = 960          # 20 ms, the streamer's frame
SECONDS = 1.0
FREQUENCY = 440.0


def tone(index):
    """One frame of a stereo 16-bit tone, exactly the shape the bot sends."""
    samples = array.array("h")
    start = index * SAMPLES
    for sample in range(SAMPLES):
        value = int(12000 * math.sin(
            2.0 * math.pi * FREQUENCY * (start + sample) / RATE))
        samples.append(value)
        samples.append(value)
    return samples.tobytes()


def main(argv):
    devices = output_device.available_devices()
    print(f"devices on this machine ({len(devices)}):")
    for device in devices:
        print(f"  - {output_device.short_name(device)}  [{device}]")
    chosen = argv[0] if argv else (devices[0] if devices else "")
    if not chosen:
        print("no output device is offered by OpenAL on this machine")
        return 1

    process = output_device.MusicOutputProcess(chosen)
    process.start()
    print(f"child started for: {output_device.short_name(chosen)}")
    process.set_format(0, 2)
    process.set_gain(0, 0.5)
    frames = int(SECONDS * 1000 / 20)
    pushed = retried = 0
    started = time.monotonic()
    for index in range(frames):
        data = tone(index)
        while not process.push(data, 0):
            retried += 1
            time.sleep(0.005)
        pushed += 1
        # The streamer is paced by the decode; this rig sends at the same rate.
        target = started + (index + 1) * 0.020
        delay = target - time.monotonic()
        if delay > 0:
            time.sleep(delay)
    # Let the child play out what is queued before it is asked to finish.
    time.sleep(0.3)
    process.stop()

    status = process.status or {}
    print(f"pushed {pushed} frames ({pushed * 20} ms), retried {retried}")
    print(f"child reported: {status}")
    played = int(status.get("played_frames") or 0)
    if status.get("error"):
        print(f"FAIL: the child could not play: {status['error']}")
        return 1
    if played < frames // 2:
        print(f"FAIL: it took {played} of {frames} frames")
        return 1
    print(f"PASS: {played} frames played on "
          f"'{output_device.short_name(chosen)}'")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
