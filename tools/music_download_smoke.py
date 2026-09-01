"""Optional live smoke test for Music Bot downloads.

Downloads the short public youtube-dl test video into an isolated temporary
directory, converts every exposed format, verifies the container signature,
then removes only that temporary directory.  No game/server state is loaded.

Run: python tools/music_download_smoke.py
"""

from pathlib import Path
import shutil
import sys
import tempfile


CLIENT_ROOT = Path(__file__).resolve().parents[1]
if str(CLIENT_ROOT) not in sys.path:
    sys.path.insert(0, str(CLIENT_ROOT))

from libs.music_downloader import MusicDownloadManager  # noqa: E402


# A short, stable public video keeps the live smoke test fast while still
# exercising YouTube extraction, media download, ffmpeg and every output codec.
TEST_URL = "https://www.youtube.com/watch?v=jNQXAC9IVRw"
FORMATS = (
    ("mp3", "192", ".mp3"),
    ("m4a", "192", ".m4a"),
    ("ogg", "192", ".ogg"),
    ("opus", "192", ".opus"),
    ("flac", None, ".flac"),
    ("wav", None, ".wav"),
)


def find_ffmpeg():
    bundled = CLIENT_ROOT / "ffmpeg.exe"
    if bundled.is_file():
        return str(bundled)
    return shutil.which("ffmpeg")


def valid_signature(path):
    data = path.read_bytes()[:16]
    suffix = path.suffix.lower()
    if suffix == ".mp3":
        return data.startswith(b"ID3") or (len(data) >= 2 and data[0] == 0xFF)
    if suffix == ".m4a":
        return len(data) >= 8 and data[4:8] == b"ftyp"
    if suffix in (".ogg", ".opus"):
        return data.startswith(b"OggS")
    if suffix == ".flac":
        return data.startswith(b"fLaC")
    if suffix == ".wav":
        return data.startswith(b"RIFF") and data[8:12] == b"WAVE"
    return False


def main():
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        print("FAIL: ffmpeg was not found")
        return 1

    try:
        import yt_dlp
    except Exception as exc:
        print(f"FAIL: yt-dlp is unavailable: {exc}")
        return 1

    manager = MusicDownloadManager(None, lambda: None, ffmpeg)
    failures = []
    with tempfile.TemporaryDirectory(prefix="bt-music-download-smoke-") as temporary:
        root = Path(temporary).resolve()
        for output_format, quality, extension in FORMATS:
            destination = root / output_format
            destination.mkdir()
            try:
                result = manager._download_track(
                    TEST_URL, str(destination), output_format, quality
                )
            except Exception as exc:
                failures.append(f"{output_format}: {type(exc).__name__}: {exc}")
                continue
            files = list(destination.glob(f"*{extension}"))
            if result != 0 or len(files) != 1:
                failures.append(
                    f"{output_format}: result={result}, files={[path.name for path in files]}"
                )
                continue
            if files[0].stat().st_size <= 0 or not valid_signature(files[0]):
                failures.append(f"{output_format}: invalid or empty output {files[0].name}")
                continue
            print(f"PASS: {output_format} -> {files[0].name} ({files[0].stat().st_size} bytes)")

    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        return 1
    print("PASS: all Music Bot download formats completed in an isolated temporary directory")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
