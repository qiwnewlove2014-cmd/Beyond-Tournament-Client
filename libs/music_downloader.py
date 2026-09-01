"""Private, background downloads for the personal Music Bot.

The Server never receives URLs, paths, progress, or downloaded media.  Tk and
yt-dlp both run off the gameplay/audio thread; only menus and notifications are
queued back to the main game thread.
"""

import os
import queue
import threading
import time
from urllib.parse import urlparse

from . import logger
from .accessible_progress import AccessibleProgressBar
from .speech import speak


FORMAT_CHOICES = (
    ("MP3 audio (.mp3)", "mp3"),
    ("M4A audio (.m4a)", "m4a"),
    ("OGG Vorbis audio (.ogg)", "ogg"),
    ("OPUS audio (.opus)", "opus"),
    ("FLAC lossless audio (.flac)", "flac"),
    ("WAV uncompressed audio (.wav)", "wav"),
)

QUALITY_CHOICES = (
    ("High quality, 320 kilobits per second", "320"),
    ("Standard quality, 192 kilobits per second", "192"),
    ("Smaller file, 128 kilobits per second", "128"),
)

LOSSLESS_FORMATS = frozenset(("flac", "wav"))
_CODEC_BY_FORMAT = {
    "mp3": "mp3",
    "m4a": "m4a",
    "ogg": "vorbis",
    "opus": "opus",
    "flac": "flac",
    "wav": "wav",
}

SOURCE_FORMATS = (
    "best[acodec!=none][vcodec!=none][height<=360]/best[acodec!=none][vcodec!=none]",
    "bestaudio/best",
)


def is_supported_music_url(value):
    """Accept only normal HTTPS YouTube pages used by the current Music Bot."""
    try:
        parsed = urlparse(str(value).strip())
        host = (parsed.hostname or "").lower()
        return parsed.scheme == "https" and (
            host == "youtube.com"
            or host.endswith(".youtube.com")
            or host == "youtu.be"
        )
    except Exception:
        return False


def filter_download_tracks(tracks):
    """Copy and bound saved entries; local files and malformed URLs stay out."""
    accepted = []
    for track in list(tracks or ())[:500]:
        if not isinstance(track, dict):
            continue
        target = str(track.get("target", "")).strip()
        if not is_supported_music_url(target):
            continue
        accepted.append({
            "title": str(track.get("title", "Unknown"))[:300],
            "target": target,
        })
    return accepted


class MusicDownloadManager:
    """Own one bounded yt-dlp job and its accessible configuration flow."""

    def __init__(self, game, parent_provider, ffmpeg_path=None):
        self.game = game
        self._parent_provider = parent_provider
        self.ffmpeg_path = ffmpeg_path
        self._lock = threading.Lock()
        self._active_label = ""
        self._cancel_event = threading.Event()
        self._worker = None
        self._closed = False
        self._preferred_source_format = SOURCE_FORMATS[0]
        self._progress_bar = AccessibleProgressBar("Music Bot download progress")
        self._progress_percent = 0
        self._progress_track_index = 0
        self._progress_track_count = 0
        self._last_progress_ui_value = -1
        self._last_progress_ui_at = 0.0
        self._track_progress = {}
        self._progress_completed = 0
        self._parallel_downloads = 2
        self._notify_each_file = True

    def is_active(self):
        with self._lock:
            return bool(self._worker and self._worker.is_alive())

    def speak_status(self):
        with self._lock:
            label = self._active_label
            active = bool(self._worker and self._worker.is_alive())
            percent = self._progress_percent
            completed = self._progress_completed
            track_count = self._progress_track_count
        if active:
            track = (
                f" {completed} of {track_count} files complete."
                if track_count > 1 else ""
            )
            speak(
                f"Downloading {label}. {percent} percent.{track} "
                "You will be notified when it finishes."
            )
        else:
            speak("No Music Bot download is active.")

    def progress_menu_label(self):
        """Callable Menu label; reads only a tiny locked status snapshot."""
        with self._lock:
            active = bool(self._worker and self._worker.is_alive())
            percent = self._progress_percent
            completed = self._progress_completed
            track_count = self._progress_track_count
        if not active:
            return "Download status: No active download"
        track = (
            f", {completed} of {track_count} files complete"
            if track_count > 1 else ""
        )
        return f"Download progress: {percent} percent{track}"

    def parallel_menu_label(self):
        with self._lock:
            count = self._parallel_downloads
        return f"Parallel downloads: {count}"

    def cycle_parallel_downloads(self):
        with self._lock:
            self._parallel_downloads = 1 if self._parallel_downloads >= 3 else self._parallel_downloads + 1
            count = self._parallel_downloads
            active = bool(self._worker and self._worker.is_alive())
        suffix = " This applies to the next batch." if active else ""
        speak(f"Parallel downloads set to {count}.{suffix}")

    def notification_menu_label(self):
        with self._lock:
            enabled = self._notify_each_file
        return f"Notify after each file: {'On' if enabled else 'Off'}"

    def toggle_file_notifications(self):
        with self._lock:
            self._notify_each_file = not self._notify_each_file
            enabled = self._notify_each_file
            active = bool(self._worker and self._worker.is_alive())
        suffix = " This applies to the next batch." if active else ""
        speak(f"Per-file notifications {'on' if enabled else 'off'}.{suffix}")

    def show_progress_bar(self):
        """Main-thread entry point used when opening the Download Music menu."""
        with self._lock:
            active = bool(self._worker and self._worker.is_alive())
            percent = self._progress_percent
        if active:
            self._progress_bar.create()
            self._progress_bar.set_value(percent)

    def hide_progress_bar(self):
        """Main-thread exit hook; downloading and progress state continue."""
        self._progress_bar.destroy()

    def cancel(self):
        if not self.is_active():
            speak("No Music Bot download is active.")
            return
        self._cancel_event.set()
        speak("Cancelling the Music Bot download. Please wait.")

    def close(self):
        self._closed = True
        self._cancel_event.set()
        self._progress_bar.destroy()

    def configure(self, tracks, label):
        tracks = filter_download_tracks(tracks)
        if not tracks:
            speak("No downloadable YouTube tracks were found in this selection.")
            return
        if self.is_active():
            speak("A Music Bot download is already active. Check its status or cancel it first.")
            return
        request = {
            "tracks": tracks,
            "label": str(label or "music")[:200],
        }
        self._show_format_menu(request)

    def _show_format_menu(self, request):
        from . import menu as menu_mod, menus
        parent = self._parent_provider()
        if not parent:
            return
        menu = menu_mod.Menu(self.game, "Download format", parrent=parent)
        items = []
        for label, value in FORMAT_CHOICES:
            def choose(fmt=value):
                parent.pop_last_substate()
                next_request = dict(request, output_format=fmt)
                if fmt in LOSSLESS_FORMATS:
                    next_request["quality"] = None
                    self._choose_folder(next_request)
                else:
                    self._show_quality_menu(next_request)
            items.append((label, choose))
        items.append(("Cancel", lambda: parent.pop_last_substate()))
        menu.add_items(items)
        menus.set_default_sounds(menu)
        parent.add_substate(menu)

    def _show_quality_menu(self, request):
        from . import menu as menu_mod, menus
        parent = self._parent_provider()
        if not parent:
            return
        menu = menu_mod.Menu(self.game, "Download quality", parrent=parent)
        items = []
        for label, value in QUALITY_CHOICES:
            def choose(quality=value):
                parent.pop_last_substate()
                self._choose_folder(dict(request, quality=quality))
            items.append((label, choose))
        items.append(("Back", lambda: (parent.pop_last_substate(), self._show_format_menu(request))))
        menu.add_items(items)
        menus.set_default_sounds(menu)
        parent.add_substate(menu)

    def _choose_folder(self, request):
        speak("Opening folder selector.")

        def select_folder():
            root = None
            try:
                import tkinter as tk
                from tkinter import filedialog
                root = tk.Tk()
                root.withdraw()
                root.attributes("-topmost", True)
                folder = filedialog.askdirectory(
                    title="Select Music Download Folder",
                    mustexist=True,
                )
                if folder:
                    folder = os.path.abspath(folder)
                    self.game.put(lambda folder=folder: self._start(request, folder))
                else:
                    self.game.put(lambda: speak("Music download cancelled. No folder was selected."))
            except Exception as exc:
                logger.log(f"[MusicDownload] Folder selector failed: {exc}")
                self.game.put(lambda: speak("Could not open the folder selector."))
            finally:
                if root is not None:
                    try:
                        root.destroy()
                    except Exception:
                        pass

        threading.Thread(
            target=select_folder,
            name="music-download-folder-dialog",
            daemon=True,
        ).start()

    def _start(self, request, folder):
        if self._closed:
            return
        if not os.path.isdir(folder):
            speak("The selected download folder is no longer available.")
            return
        if self.is_active():
            speak("A Music Bot download started while the folder selector was open.")
            return
        self._cancel_event = threading.Event()
        request = dict(request)
        label = request["label"]
        with self._lock:
            request["parallel_downloads"] = self._parallel_downloads
            request["notify_each_file"] = self._notify_each_file

        def run():
            self._run_download(request, folder)

        worker = threading.Thread(
            target=run,
            name="music-download-worker",
            daemon=True,
        )
        with self._lock:
            self._worker = worker
            self._active_label = label
            self._progress_percent = 0
            self._progress_track_index = 0
            self._progress_track_count = len(request["tracks"])
            self._progress_completed = 0
            self._track_progress = {}
            self._last_progress_ui_value = -1
            self._last_progress_ui_at = 0.0
        worker.start()
        self._play_ui_sound("ui/broadcast.ogg")
        speak(f"Downloading {label} in the background. You can continue playing.")

    def _progress_hook(self, status, track_index=None):
        if self._cancel_event.is_set():
            raise RuntimeError("Music download cancelled")
        if not isinstance(status, dict):
            return
        total = status.get("total_bytes") or status.get("total_bytes_estimate") or 0
        downloaded = status.get("downloaded_bytes") or 0
        if status.get("status") == "finished":
            track_fraction = 0.99  # ffmpeg conversion still follows this hook.
        elif total > 0:
            track_fraction = max(0.0, min(0.99, float(downloaded) / float(total)))
        else:
            return

        with self._lock:
            index = max(1, track_index or self._progress_track_index)
            count = max(1, self._progress_track_count)
            previous = self._track_progress.get(index, 0.0)
            self._track_progress[index] = max(previous, track_fraction)
            overall = int(max(0, min(99, (sum(self._track_progress.values()) / count) * 100)))
        self._set_progress(overall)

    def _set_progress(self, percent, force=False):
        percent = max(0, min(100, int(percent)))
        now = time.monotonic()
        queue_update = False
        with self._lock:
            self._progress_percent = percent
            changed = percent != self._last_progress_ui_value
            interval_ready = now - self._last_progress_ui_at >= 0.1
            if changed and (force or interval_ready):
                self._last_progress_ui_value = percent
                self._last_progress_ui_at = now
                queue_update = True
        if not queue_update:
            return
        try:
            self.game.put(
                lambda value=percent: self._progress_bar.set_value(value)
            )
        except Exception:
            pass

    def _begin_track_progress(self, index, count):
        with self._lock:
            self._progress_track_index = index
            self._progress_track_count = count
            self._track_progress.setdefault(index, 0.0)

    def _finish_track_progress(self, index, count):
        with self._lock:
            self._track_progress[index] = 1.0
            self._progress_completed = sum(
                1 for value in self._track_progress.values() if value >= 1.0
            )
            overall = int((sum(self._track_progress.values()) / max(1, count)) * 100)
        self._set_progress(overall, force=True)

    def _ydl_options(self, folder, output_format, quality, allow_playlist,
                     source_format=None, progress_hook=None):
        codec = _CODEC_BY_FORMAT[output_format]
        postprocessor = {
            "key": "FFmpegExtractAudio",
            "preferredcodec": codec,
        }
        if quality is not None:
            postprocessor["preferredquality"] = str(quality)
        options = {
            # Google may intermittently reject either a progressive or an
            # audio-only CDN route. _download_track retries the alternate
            # family on 403 rather than assuming one route is always reliable.
            "format": source_format or SOURCE_FORMATS[0],
            "outtmpl": os.path.join(folder, "%(title).180B.%(ext)s"),
            "windowsfilenames": True,
            "noplaylist": not allow_playlist,
            "playlistend": 500,
            "quiet": True,
            "no_warnings": True,
            # Match the stable Music Bot playback resolver.  YouTube's default
            # client route can expose signed media URLs that immediately 403,
            # while the Android/Web profiles return a usable URL/header pair.
            "extractor_args": {
                "youtube": {"player_client": ["android", "web"]},
            },
            "continuedl": True,
            "overwrites": False,
            "socket_timeout": 15,
            "retries": 3,
            "fragment_retries": 3,
            "progress_hooks": [progress_hook or self._progress_hook],
            "postprocessors": [postprocessor],
        }
        if self.ffmpeg_path:
            options["ffmpeg_location"] = self.ffmpeg_path
        return options

    @staticmethod
    def _is_forbidden_error(exc):
        message = str(exc).lower()
        return "403" in message or "forbidden" in message

    def _download_track(self, target, folder, output_format, quality,
                        progress_hook=None):
        """Download one canonical page, retrying the alternate CDN route on 403."""
        import yt_dlp

        with self._lock:
            preferred_source_format = self._preferred_source_format
        source_formats = [preferred_source_format]
        source_formats.extend(
            source_format for source_format in SOURCE_FORMATS
            if source_format != preferred_source_format
        )
        for index, source_format in enumerate(source_formats):
            options = self._ydl_options(
                folder, output_format, quality, False, source_format,
                progress_hook,
            )
            try:
                with yt_dlp.YoutubeDL(options) as downloader:
                    result = downloader.download([target])
                if result == 0:
                    with self._lock:
                        self._preferred_source_format = source_format
                return result
            except Exception as exc:
                if self._cancel_event.is_set():
                    raise
                has_fallback = index + 1 < len(source_formats)
                if has_fallback and self._is_forbidden_error(exc):
                    logger.log(
                        "[MusicDownload] YouTube route returned 403; "
                        "retrying the alternate media route"
                    )
                    continue
                raise

    def _run_download(self, request, folder):
        succeeded = 0
        failed = 0
        cancelled = False
        try:
            import yt_dlp
            tracks = request["tracks"]
            track_count = len(tracks)
            work = queue.Queue()
            results = queue.Queue()
            for track_index, track in enumerate(tracks, start=1):
                work.put((track_index, track))

            def download_worker():
                while True:
                    try:
                        track_index, track = work.get_nowait()
                    except queue.Empty:
                        return
                    if self._cancel_event.is_set():
                        results.put((track_index, track, None, None, True))
                        continue
                    self._begin_track_progress(track_index, track_count)
                    result = error = None
                    was_cancelled = False
                    try:
                        result = self._download_track(
                            track["target"], folder,
                            request["output_format"], request["quality"],
                            progress_hook=lambda status, index=track_index: self._progress_hook(status, index),
                        )
                    except Exception as exc:
                        if self._cancel_event.is_set():
                            was_cancelled = True
                        else:
                            error = exc
                    if not was_cancelled:
                        self._finish_track_progress(track_index, track_count)
                    results.put((track_index, track, result, error, was_cancelled))

            worker_count = min(
                track_count,
                max(1, min(3, int(request.get("parallel_downloads", 2)))),
            )
            workers = [
                threading.Thread(
                    target=download_worker,
                    name=f"music-download-slot-{index + 1}",
                    daemon=True,
                )
                for index in range(worker_count)
            ]
            for worker in workers:
                worker.start()

            notify_each = bool(request.get("notify_each_file")) and track_count > 1
            for completion_number in range(1, track_count + 1):
                track_index, track, result, error, was_cancelled = results.get()
                if was_cancelled:
                    cancelled = True
                    continue
                try:
                    if error is not None:
                        raise error
                    success = result == 0
                    if success:
                        succeeded += 1
                    else:
                        failed += 1
                    if notify_each:
                        self._notify_track(
                            track, completion_number, track_count, success
                        )
                except Exception as exc:
                    failed += 1
                    logger.log(f"[MusicDownload] Track failed: {type(exc).__name__}: {exc}")
                    if notify_each:
                        self._notify_track(
                            track, completion_number, track_count, False
                        )
        except Exception as exc:
            cancelled = self._cancel_event.is_set()
            if not cancelled:
                failed = max(1, failed)
                logger.log(f"[MusicDownload] Job failed: {type(exc).__name__}: {exc}")
        finally:
            with self._lock:
                self._active_label = ""
                self._worker = None

        if self._closed:
            return
        if cancelled:
            self._notify("Music download cancelled.", success=False)
        elif failed:
            self._notify(
                f"Music download finished with errors. {succeeded} item{'' if succeeded == 1 else 's'} saved.",
                success=False,
                play_sound=not bool(request.get("notify_each_file") and len(request.get("tracks", ())) > 1),
                interrupt=False,
            )
        else:
            self._notify(
                f"Music download complete. Files were saved in {os.path.basename(folder) or folder}.",
                success=True,
                play_sound=not bool(request.get("notify_each_file") and len(request.get("tracks", ())) > 1),
                interrupt=False,
            )

    def _notify_track(self, track, index, count, success):
        title = str(track.get("title") or "Unknown")[:160]
        message = (
            f"Downloaded {index} of {count}: {title}."
            if success else f"Download failed for {index} of {count}: {title}."
        )

        def deliver():
            self._play_ui_sound("ui/unread.ogg" if success else "ui/warn.ogg")
            speak(message, False)
        try:
            self.game.put(deliver)
        except Exception:
            pass

    def _notify(self, message, success, play_sound=True, interrupt=True):
        def deliver():
            if success:
                self._progress_bar.set_value(100)
            self._progress_bar.destroy()
            if play_sound:
                self._play_ui_sound("ui/unread.ogg" if success else "ui/warn.ogg")
            speak(message, interrupt)
        try:
            self.game.put(deliver)
        except Exception:
            pass

    def _play_ui_sound(self, path):
        try:
            self.game.direct_soundgroup.play(path, cat="ui")
        except Exception:
            pass
