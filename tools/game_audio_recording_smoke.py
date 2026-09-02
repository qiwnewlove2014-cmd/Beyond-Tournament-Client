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
import time
import wave
import winsound

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from libs.game_audio_recorder import (
    CHANNELS,
    SAMPLE_RATE,
    MicrophoneOverlayBuffer,
    ProcessLoopbackRecorder,
    SystemOutputLoopbackRecorder,
)


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

        # Endpoint-loopback is the explicit opt-in mode used for NVDA, JAWS,
        # Narrator and other computer audio. Prove that a separate process is
        # included, which is the inverse of the game-only isolation check.
        system_path = temp / "computer-audio.wav"
        system_stop = threading.Event()
        system_started = threading.Event()
        system_result = {}

        def capture_computer_audio():
            try:
                system_result["value"] = SystemOutputLoopbackRecorder().record(
                    system_path,
                    system_stop,
                    system_started.set,
                )
            except Exception as error:
                system_result["error"] = error

        system_worker = threading.Thread(target=capture_computer_audio)
        system_worker.start()
        if not system_started.wait(12):
            system_stop.set()
            system_worker.join(5)
            raise RuntimeError(f"Computer audio recorder did not start: {system_result.get('error', 'timeout')}")
        try:
            subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "import sys,winsound; winsound.PlaySound(sys.argv[1], winsound.SND_FILENAME)",
                    str(tone_path),
                ],
                check=True,
            )
        finally:
            system_stop.set()
            system_worker.join(10)
        if system_worker.is_alive():
            raise RuntimeError("Computer audio recorder did not stop")
        if "error" in system_result:
            raise system_result["error"]
        with wave.open(str(system_path), "rb") as recording:
            system_pcm = recording.readframes(recording.getnframes())
        system_peak = max(
            (abs(value[0]) for value in struct.iter_unpack("<h", system_pcm)),
            default=0,
        )
        if system_peak < 1_000:
            raise RuntimeError("Audio from the separate process was not captured by computer audio mode")
        print(f"PASS: computer audio mode included a separate process, peak {system_peak}")

        # Verify the optional outgoing-microphone path against a silent final
        # process mix. The synthetic mono PCM uses the same 10ms cadence as
        # VoiceChatRecord without opening or reading the user's microphone.
        microphone_path = temp / "microphone-overlay.wav"
        microphone_stop = threading.Event()
        microphone_started = threading.Event()
        microphone_overlay = MicrophoneOverlayBuffer()
        microphone_result = {}

        def capture_microphone_overlay():
            try:
                microphone_result["value"] = ProcessLoopbackRecorder().record(
                    microphone_path,
                    microphone_stop,
                    microphone_started.set,
                    microphone_overlay=microphone_overlay,
                )
            except Exception as error:
                microphone_result["error"] = error

        microphone_worker = threading.Thread(target=capture_microphone_overlay)
        microphone_worker.start()
        if not microphone_started.wait(12):
            microphone_stop.set()
            microphone_worker.join(5)
            raise RuntimeError(
                f"Microphone overlay recorder did not start: {microphone_result.get('error', 'timeout')}"
            )
        phase = 0
        try:
            for _ in range(100):
                mono = bytearray()
                for _ in range(480):
                    sample = int(math.sin(2 * math.pi * 330 * phase / SAMPLE_RATE) * 8_000)
                    mono.extend(struct.pack("<h", sample))
                    phase += 1
                microphone_overlay.put_mono16(mono)
                time.sleep(0.01)
        finally:
            microphone_stop.set()
            microphone_worker.join(10)
        if microphone_worker.is_alive():
            raise RuntimeError("Microphone overlay recorder did not stop")
        if "error" in microphone_result:
            raise microphone_result["error"]
        with wave.open(str(microphone_path), "rb") as recording:
            microphone_pcm = recording.readframes(recording.getnframes())
        microphone_peak = max(
            (abs(value[0]) for value in struct.iter_unpack("<h", microphone_pcm)),
            default=0,
        )
        if microphone_peak < 1_000:
            raise RuntimeError("Synthetic transmitted microphone PCM was not mixed into the recording")
        print(f"PASS: optional transmitted microphone PCM was mixed, peak {microphone_peak}")


if __name__ == "__main__":
    main()
