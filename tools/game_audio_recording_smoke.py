"""Manual Windows smoke test for isolated process-loopback recording.

It records a one-second tone rendered by this Python process and verifies that
the resulting WAV contains non-silent PCM. No game, Server, or user data is
read or changed.
"""

import math
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import threading
import wave
import winsound

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from libs.game_audio_recorder import CHANNELS, SAMPLE_RATE, ProcessLoopbackRecorder


def write_tone(path):
    frames = bytearray()
    for index in range(SAMPLE_RATE):
        sample = int(math.sin(2 * math.pi * 440 * index / SAMPLE_RATE) * 12_000)
        frames.extend(struct.pack("<hh", sample, sample))
    with wave.open(str(path), "wb") as output:
        output.setnchannels(CHANNELS)
        output.setsampwidth(2)
        output.setframerate(SAMPLE_RATE)
        output.writeframes(frames)


def main():
    with tempfile.TemporaryDirectory(prefix="bt-game-audio-") as temp:
        temp = Path(temp)
        tone_path = temp / "tone.wav"
        capture_path = temp / "capture.wav"
        write_tone(tone_path)

        stop_event = threading.Event()
        started_event = threading.Event()
        result = {}

        def capture():
            try:
                result["value"] = ProcessLoopbackRecorder().record(
                    capture_path,
                    stop_event,
                    started_event.set,
                )
            except Exception as error:
                result["error"] = error

        worker = threading.Thread(target=capture)
        worker.start()
        if not started_event.wait(12):
            stop_event.set()
            worker.join(5)
            raise RuntimeError(f"Recorder did not start: {result.get('error', 'timeout')}")

        try:
            winsound.PlaySound(str(tone_path), winsound.SND_FILENAME)
        finally:
            stop_event.set()
            worker.join(10)
        if worker.is_alive():
            raise RuntimeError("Recorder did not stop")
        if "error" in result:
            raise result["error"]

        with wave.open(str(capture_path), "rb") as recording:
            pcm = recording.readframes(recording.getnframes())
        if not pcm or not any(pcm):
            raise RuntimeError("Captured WAV was empty or silent")
        print(f"PASS: isolated process recording wrote {len(pcm)} PCM bytes")

        # Target a different, silent process and play the same tone from this
        # parent. Process-loopback must not capture the parent's audio.
        silent_target = subprocess.Popen([
            sys.executable,
            "-c",
            "import time; time.sleep(5)",
        ])
        excluded_path = temp / "excluded.wav"
        excluded_stop = threading.Event()
        excluded_started = threading.Event()
        excluded_result = {}

        def capture_silent_target():
            try:
                excluded_result["value"] = ProcessLoopbackRecorder(silent_target.pid).record(
                    excluded_path,
                    excluded_stop,
                    excluded_started.set,
                )
            except Exception as error:
                excluded_result["error"] = error

        excluded_worker = threading.Thread(target=capture_silent_target)
        excluded_worker.start()
        try:
            if not excluded_started.wait(12):
                raise RuntimeError(f"Isolation recorder did not start: {excluded_result.get('error', 'timeout')}")
            winsound.PlaySound(str(tone_path), winsound.SND_FILENAME)
        finally:
            excluded_stop.set()
            excluded_worker.join(10)
            silent_target.terminate()
            silent_target.wait(timeout=5)
        if excluded_worker.is_alive():
            raise RuntimeError("Isolation recorder did not stop")
        if "error" in excluded_result:
            raise excluded_result["error"]
        excluded_pcm = b""
        if excluded_path.exists():
            with wave.open(str(excluded_path), "rb") as recording:
                excluded_pcm = recording.readframes(recording.getnframes())
        excluded_peak = max(
            (abs(value[0]) for value in struct.iter_unpack("<h", excluded_pcm)),
            default=0,
        )
        print(f"Isolation capture: {len(excluded_pcm)} bytes, peak {excluded_peak}")
        if excluded_peak > 4:
            raise RuntimeError("Audio from a non-target process leaked into the isolated capture")
        print("PASS: audio rendered outside the target process tree was excluded")


if __name__ == "__main__":
    main()
