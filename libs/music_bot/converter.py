"""Batch audio conversion for the Music Bot's file tools.

A player who culls a folder by ear ends up with picks in five different
formats, or with a WAV that is too big to send anywhere. This turns a list of
files -- their own selection, a folder, or the Picks -- into one target format,
writing beside each original unless an output folder was chosen.

**The original is only ever an input.** Every destination is a new name: a file
of the same name already there gets a number (`Song (2).mp3`), and a source
that would land on itself is numbered as well, so pressing Convert twice can
never eat the first result or the song it came from. `ffmpeg` is told `-n` as
the last net, and a failed run's half-written output is deleted -- it is the
file this module made, never the one it read.

The split is the download center's, on purpose: this module is the job, the
settings and the progress, `menu_pages/converter.py` is the face, and the bot
reaches it lazily as `MapMusicBot._converter()` so a bot built with `__new__`
(a test fixture, a restored session) has one on first use. Nothing here builds
Tk: the file and folder pickers are `libs/folder_dialog.py`'s child process.

Rules: .agents/skills/music_bot_integration/references/audio-converter.md
Pins: client/tests/test_music_bot_converter.py
"""

import os
import subprocess
import threading

from .. import options
from ..accessible_progress import AccessibleProgressBar
from ..speech import speak

# The catalogue the settings page lists, one line each. `args` are ffmpeg's own
# output options, so the label and the encoder can never drift apart; `note` is
# the line's Tab description, because a format nobody can explain is a format
# only its author uses.
FORMATS = (
    {
        "key": "mp3_320",
        "label": "MP3, 320 kbps",
        "extension": ".mp3",
        "args": ("-c:a", "libmp3lame", "-b:a", "320k"),
        "note": "The best an MP3 gets, and the largest of them: a song of a "
                "few minutes is a few megabytes. Right for a final mix that "
                "is shared.",
    },
    {
        "key": "mp3_192",
        "label": "MP3, 192 kbps",
        "extension": ".mp3",
        "args": ("-c:a", "libmp3lame", "-b:a", "192k"),
        "note": "The default: plays anywhere, on anything, and the loss is "
                "hard to hear on a game's speakers. About half the size of "
                "320 kbps.",
    },
    {
        "key": "mp3_128",
        "label": "MP3, 128 kbps",
        "extension": ".mp3",
        "args": ("-c:a", "libmp3lame", "-b:a", "128k"),
        "note": "The smallest of the MP3s. Good for a folder of old songs "
                "kept for reference; cymbals and strings start to smear.",
    },
    {
        "key": "ogg",
        "label": "OGG Vorbis, quality 5",
        "extension": ".ogg",
        "args": ("-c:a", "libvorbis", "-q:a", "5"),
        "note": "Smaller than MP3 at the same quality, and the same Vorbis "
                "family the game's own music uses. Not every other player "
                "opens it.",
    },
    {
        "key": "wav",
        "label": "WAV, 16-bit",
        "extension": ".wav",
        "args": ("-c:a", "pcm_s16le"),
        "note": "Uncompressed: every sample kept, and about ten megabytes a "
                "minute. For editing in another program, not for storing.",
    },
    {
        "key": "flac",
        "label": "FLAC, lossless",
        "extension": ".flac",
        "args": ("-c:a", "flac", "-compression_level", "5"),
        "note": "Uncompressed quality in roughly half the space of a WAV, "
                "and it converts back to the same samples. The archive "
                "choice.",
    },
)

FORMAT_BY_KEY = {fmt["key"]: fmt for fmt in FORMATS}
DEFAULT_FORMAT_KEY = "mp3_192"
FORMAT_OPTION = "music_bot_convert_format"
FOLDER_OPTION = "music_bot_convert_folder"


class AudioConverter:
    """One bounded conversion job, its settings and the progress it reports."""

    def __init__(self, game, ffmpeg_path=None):
        self.game = game
        self._ffmpeg_path = ffmpeg_path
        self._lock = threading.Lock()
        self._cancel_event = threading.Event()
        self._worker = None
        self._closed = False
        self._announce_each_file = True
        self._label = ""
        self._progress_percent = 0
        self._progress_completed = 0
        self._progress_total = 0
        self._progress_current = ""
        self._converted = 0
        self._failed = 0
        self._missing = 0
        self._skipped = 0
        self._last_summary = ""
        self._progress_bar = AccessibleProgressBar("Audio conversion progress")

    # === The formats ===

    def format_key(self):
        saved = options.get(FORMAT_OPTION, DEFAULT_FORMAT_KEY)
        return saved if saved in FORMAT_BY_KEY else DEFAULT_FORMAT_KEY

    def format(self):
        return FORMAT_BY_KEY[self.format_key()]

    def format_menu_label(self):
        return f"Target Format: {self.format()['label']}"

    def set_format(self, key):
        if key not in FORMAT_BY_KEY:
            return
        if self.is_active():
            speak("Wait for the conversion that is running to finish before "
                  "changing the format.")
            return
        options.set(FORMAT_OPTION, key)
        speak(f"Converting to {FORMAT_BY_KEY[key]['label']} from now on.")

    # === Where the files go ===

    @staticmethod
    def _saved_folder_value():
        value = options.get(FOLDER_OPTION, "")
        if not isinstance(value, str) or not value.strip():
            return ""
        return os.path.abspath(os.path.expanduser(value.strip()))

    def configured_folder(self):
        folder = self._saved_folder_value()
        return folder if folder and os.path.isdir(folder) else ""

    def has_saved_folder(self):
        return bool(self._saved_folder_value())

    def output_menu_label(self):
        folder = self._saved_folder_value()
        if not folder:
            return "Output: Next to the Original"
        if not os.path.isdir(folder):
            return "Output: Saved folder is unavailable -- next to the original"
        return f"Output: {folder}"

    def speak_output(self):
        speak(self.output_menu_label())

    def destination_summary(self):
        """Where this batch's converted files will land, as a sentence says it.

        Said as part of the opening line, because "which folder?" is the one
        thing a player asks after choosing what to convert: the picker they
        just closed chose the **source**, and the output folder is the line
        above it on the page.
        """
        folder = self.configured_folder()
        return f"into {folder}" if folder else "beside each original"

    def set_folder(self, folder):
        if self.is_active():
            speak("Wait for the conversion that is running to finish before "
                  "changing its folder.")
            return
        if not os.path.isdir(folder):
            speak("The selected folder is no longer available.")
            return
        folder = os.path.abspath(folder)
        options.set(FOLDER_OPTION, folder)
        speak(f"Converted files will be saved to {folder}.")

    def clear_folder(self):
        if self.is_active():
            speak("Wait for the conversion that is running to finish before "
                  "changing its folder.")
            return
        options.set(FOLDER_OPTION, "")
        speak("Converted files will be saved next to the original again.")

    # === Progress ===

    def is_active(self):
        with self._lock:
            return bool(self._worker and self._worker.is_alive())

    def progress_menu_label(self):
        """Callable Menu label; reads a tiny locked status snapshot."""
        with self._lock:
            active = bool(self._worker and self._worker.is_alive())
            completed = self._progress_completed
            total = self._progress_total
            percent = self._progress_percent
            current = self._progress_current
        if not active:
            if self._last_summary:
                return f"Conversion: {self._last_summary}"
            return "Conversion: Nothing has been converted yet"
        line = f"Conversion: {completed} of {total} files, {percent} percent"
        if current:
            line += f", converting {current}"
        return line

    def speak_status(self):
        with self._lock:
            active = bool(self._worker and self._worker.is_alive())
            completed = self._progress_completed
            total = self._progress_total
            current = self._progress_current
            percent = self._progress_percent
        if active:
            said = (f"Converting {completed} of {total} files to "
                    f"{self.format()['label']}. {percent} percent.")
            if current:
                said += f" Now converting {current}."
            speak(said + " You will be told when the batch finishes.")
            return
        if self._last_summary:
            speak(self._last_summary)
            return
        speak("No conversion is running.")

    def announce_menu_label(self):
        return f"Announce Each File: {'On' if self._announce_each_file else 'Off'}"

    def toggle_announce(self):
        with self._lock:
            self._announce_each_file = not self._announce_each_file
            enabled = self._announce_each_file
            active = bool(self._worker and self._worker.is_alive())
        suffix = " This applies to the next batch." if active else ""
        speak(f"Per-file announcements {'on' if enabled else 'off'}.{suffix}")

    def show_progress_bar(self):
        """Main-thread entry point used when opening the converter page."""
        with self._lock:
            active = bool(self._worker and self._worker.is_alive())
            percent = self._progress_percent
        if active:
            self._progress_bar.create()
            self._progress_bar.set_value(percent)

    def hide_progress_bar(self):
        """Main-thread exit hook; the conversion itself keeps running."""
        self._progress_bar.destroy()

    def cancel(self):
        if not self.is_active():
            speak("No conversion is running.")
            return
        self._cancel_event.set()
        speak("Cancelling the conversion. The files already converted are kept.")

    def close(self):
        self._closed = True
        self._cancel_event.set()
        self._progress_bar.destroy()

    # === The job ===

    def start(self, paths, label="your selection"):
        """Convert every path in `paths`; the work runs on its own thread."""
        paths = [os.path.abspath(str(path)) for path in (paths or ())
                 if str(path or "").strip()]
        # One file picked twice is one conversion, in the order it was picked.
        paths = list(dict.fromkeys(paths))
        if not paths:
            speak("Nothing to convert.")
            return False
        if self.is_active():
            speak("A conversion is already running. Check its progress or "
                  "cancel it first.")
            return False
        if not self._ffmpeg():
            speak("ffmpeg was not found, so files cannot be converted.")
            return False
        fmt = self.format()
        with self._lock:
            self._label = str(label or "")
            self._progress_percent = 0
            self._progress_completed = 0
            self._progress_total = len(paths)
            self._progress_current = ""
            self._converted = self._failed = self._missing = self._skipped = 0
            self._last_summary = ""
        self._cancel_event.clear()
        # The opening sentence comes before the thread: a one-file batch can
        # finish fast enough that its summary would otherwise be spoken first.
        speak(f"Converting {len(paths)} file{'s' if len(paths) != 1 else ''} "
              f"to {fmt['label']}, {self.destination_summary()}.")
        self._worker = threading.Thread(
            target=self._run, args=(paths, fmt),
            name="music-bot-converter", daemon=True)
        self._worker.start()
        return True

    def destination_for(self, source, format_key=None, folder=None):
        """A free path for `source` in the target format, or "".

        Never the source itself and never a name that is already there: the
        original is an input only, and two runs over one folder must leave two
        files (`Song.mp3`, `Song (2).mp3`) instead of the second eating the
        first.
        """
        fmt = FORMAT_BY_KEY.get(format_key or self.format_key())
        if fmt is None:
            return ""
        target_folder = (folder if folder is not None
                         else (self.configured_folder()
                               or os.path.dirname(source)))
        if not target_folder:
            return ""
        if not os.path.isdir(target_folder):
            try:
                os.makedirs(target_folder, exist_ok=True)
            except OSError:
                return ""
        stem = os.path.splitext(os.path.basename(source))[0]
        source_key = os.path.normcase(os.path.abspath(source))
        counter = 2
        candidate = os.path.join(target_folder, stem + fmt["extension"])
        while (os.path.normcase(os.path.abspath(candidate)) == source_key
               or os.path.exists(candidate)):
            candidate = os.path.join(
                target_folder, f"{stem} ({counter}){fmt['extension']}")
            counter += 1
        return candidate

    def _ffmpeg(self):
        if self._ffmpeg_path is None:
            from .media import FFMPEG_PATH
            self._ffmpeg_path = FFMPEG_PATH
        return self._ffmpeg_path

    def _command(self, source, destination, fmt):
        """The ffmpeg argv for one file (`-n`: never write over anything)."""
        return [
            self._ffmpeg(), "-hide_banner", "-nostdin", "-loglevel", "error",
            "-n", "-i", source,
            # Audio only: a video file's picture is what makes it big, and
            # nothing here plays a converted video.
            "-vn", "-map_metadata", "0",
            *fmt["args"],
            destination,
        ]

    def _convert(self, source, destination, fmt):
        try:
            result = subprocess.run(
                self._command(source, destination, fmt),
                capture_output=True,
                close_fds=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError:
            return False
        if result.returncode != 0 or not os.path.isfile(destination):
            # A half-written output is this module's own file, so it goes; the
            # source was only ever read.
            try:
                os.remove(destination)
            except OSError:
                pass
            return False
        return True

    def _step(self, index, total, fmt, name):
        percent = int(index * 100 / total) if total else 0
        with self._lock:
            self._progress_completed = index
            self._progress_percent = percent
            self._progress_current = "" if index >= total else name
            announce = self._announce_each_file
        # The last file needs no count of its own: the batch's summary follows
        # it immediately, and two sentences for one file is a stutter.
        if announce and index < total:
            self.game.put(lambda: speak(f"{index} of {total} done."))

    def _run(self, paths, fmt):
        converted = failed = missing = skipped = 0
        try:
            for index, source in enumerate(paths, start=1):
                if self._cancel_event.is_set() or self._closed:
                    break
                if not os.path.isfile(source):
                    missing += 1
                    self._step(index, len(paths), fmt, "")
                    continue
                destination = self.destination_for(source, fmt["key"])
                if not destination:
                    skipped += 1
                    self._step(index, len(paths), fmt, "")
                    continue
                if self._convert(source, destination, fmt):
                    converted += 1
                else:
                    failed += 1
                next_name = (os.path.basename(paths[index])
                             if index < len(paths) else "")
                self._step(index, len(paths), fmt, next_name)
        except Exception:
            # A batch must end with a sentence rather than a traceback: the
            # player only knows what happened from what is spoken, and the
            # counts already say how far it got.
            pass
        finally:
            summary = self._summary(converted, failed, missing, skipped, fmt)
            if self._cancel_event.is_set() and not self._closed:
                summary = "Conversion cancelled. " + summary
            with self._lock:
                self._converted = converted
                self._failed = failed
                self._missing = missing
                self._skipped = skipped
                self._last_summary = summary
                self._progress_current = ""
            self.game.put(lambda: speak(summary))

    @staticmethod
    def _summary(converted, failed, missing, skipped, fmt):
        parts = [f"Converted {converted} file"
                 + ("" if converted == 1 else "s")]
        if converted:
            parts[-1] += f" to {fmt['label']}"
        if failed:
            parts.append(f"{failed} did not convert")
        if missing:
            parts.append(f"{missing} missing")
        if skipped:
            parts.append(f"{skipped} skipped")
        return ", ".join(parts) + "."
