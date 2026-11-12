"""Where a song comes from, and the stream it starts (extracted from controller.py).

Three doors lead to the same place -- the file dialog, a YouTube search and a
pasted link -- and all three end in `_start_youtube_stream`: a playback
generation is taken, the old stream is stopped, `current_title` /
`current_target` / `current_source` are written and an `AudioStreamer` is handed
the source. The search and the link read their metadata on a worker thread and
put the answer back on the game thread, which is why every one of them carries
the generation and refuses to start a stream the player has already replaced.

**The state stays on `MapMusicBot`; the behaviour moved here.** `streamer`,
`current_title`, `is_loading_stream`, `searching`, `_gp` and the playback
generation are read straight off the bot by the menus, the crossfade machine and
the tests, so this class owns no state but its back-reference and names the bot
on every line -- `self.` is this handler, `self._bot.` is the Music Bot.

Usage from MapMusicBot (which keeps a one-line shim per entry point, so the
menus, `_seek_restart` and a test that shadows one keep their call sites):

    self._track_source()._start_youtube_stream(url, title, generation)

Pins: client/tests/test_music_bot_link.py, test_music_bot_queue_settings.py,
test_music_bot_seek.py, test_input_fields_close.py, test_youtube_resolver_integration.py
and the subtitle wiring check in test_music_bot_subtitles.py.
Rules and measured numbers: .agents/skills/music_bot_integration/.
"""

import os
import threading

from ..speech import speak
from .media import YouTubeSearcher
from .music_downloader import is_supported_music_url
from .streaming import AudioStreamer


def local_tracks(paths):
    """Build the local-file queue from the file dialog's selection.

    A path becomes the same kind of track dict a playlist/favorites entry is
    (`source` is "local"), so several chosen files can share the existing
    `play_queue` and the Ctrl+[ / ] navigation. The title is the file's stem,
    which is what the single-file dialog already used.
    """
    tracks = []
    for path in paths or ():
        path = str(path or "")
        if not path:
            continue
        tracks.append({
            "title": os.path.splitext(os.path.basename(path))[0],
            "target": path,
            "source": "local",
        })
    return tracks


#: What the picker offers and what a folder scan takes. One list, because the
#: two ways of saying "a song on this computer" must agree; ffmpeg demuxes
#: whatever is in it, video containers included.
MEDIA_PATTERNS = (
    "*.mp3 *.wav *.ogg *.flac *.m4a *.aac *.opus *.wma *.mka "
    "*.mp4 *.mkv *.webm *.mov *.m4v *.avi *.mpg *.mpeg *.wmv "
    "*.flv *.ts *.m2ts *.mts *.3gp *.3g2 *.ogv *.vob *.rmvb "
    "*.asf *.f4v *.aiff *.ape *.mid *.midi"
)
SUPPORTED_MEDIA_EXTENSIONS = frozenset(
    ".mp3 .wav .ogg .flac .m4a .aac .opus .wma .mka .mp4 .mkv .webm .mov "
    ".m4v .avi .mpg .mpeg .wmv .flv .ts .m2ts .mts .3gp .3g2 .ogv .vob "
    ".rmvb .asf .f4v .aiff .ape .mid .midi".split()
)


def music_files_in(folder):
    """Every supported media file directly inside `folder`, in name order.

    Not recursive: a folder of albums has one album per subfolder, and a scan
    that walked into them would queue a whole library from one Enter. An
    unreadable folder (permissions, a drive that went away) is an empty answer
    rather than an exception, because the caller's only useful reply is "nothing
    here" either way.
    """
    paths = []
    try:
        with os.scandir(folder) as entries:
            for entry in entries:
                try:
                    if not entry.is_file():
                        continue
                except OSError:
                    continue
                name = os.path.splitext(entry.name)[1].lower()
                if name in SUPPORTED_MEDIA_EXTENSIONS:
                    paths.append(entry.path)
    except OSError:
        return []
    return sorted(paths, key=lambda path: os.path.basename(path).lower())


class TrackSourceHandler:
    """The Music Bot's track sources: a file, a search, a link (see the module docstring)."""

    def __init__(self, bot):
        self._bot = bot          # back-reference to MapMusicBot

    # ------------------------------------------------------------------
    # where a song comes from (moved from controller.py -- `self._bot.` is the bot)
    # ------------------------------------------------------------------

    def _open_file_dialog(self):
        """Open the file picker for one or several songs.

        The picker runs in the dialog's own process (`libs/folder_dialog`), not
        a Tk built in a worker thread of this one: building Tk holds this
        interpreter for about 80 ms, which is four 20 ms audio frames (the
        measurement is in that module's docstring). One path is a song; several
        become the local batch `play_queue`, which Ctrl+[ / ] move through.
        """
        from .. import folder_dialog
        from . import gate

        if gate.blocked("local_file"):
            # Its own catalogue row: a Developer who locks the file picker
            # leaves Search working, and the door says whose decision it was.
            return

        def choose():
            answer, problem = folder_dialog.ask_files(
                "Choose Local File", pattern=MEDIA_PATTERNS)
            if problem:
                print(f"[MusicBot] Error opening file dialog: {problem}")
                self._bot.game.put(lambda: speak("Error opening file dialog."))
                return
            paths = tuple(path for path in answer.splitlines() if path.strip())
            if not paths:
                self._bot.game.put(lambda: speak("No file selected."))
                return
            tracks = local_tracks(paths)
            # One file keeps the single-track path; several become a local batch
            # the existing play_queue (and Ctrl+[ / ]) can move through without
            # starting a fresh dialog for every song.
            if len(tracks) == 1:
                track = tracks[0]
                self._bot.game.put(lambda: self._bot._start_local_file_stream(
                    track["target"], track["title"]))
            else:
                self._bot.game.put(lambda: self._bot._start_track_queue(
                    tracks, "Local files"))

        threading.Thread(target=choose, daemon=True).start()
        speak("Opening file explorer...")

    def _choose_music_folder(self):
        """Play every supported file in a folder the player picks, in name order.

        `Choose Local File` lists files, and more than one of them is a
        Ctrl+click -- the step a player who cannot see that list keeps missing.
        A folder is one answer, and what it holds is the batch.
        """
        from .. import folder_dialog
        from . import gate

        if gate.blocked("local_folder"):
            return

        def choose():
            folder, problem = folder_dialog.ask_folder("Choose a folder of music")
            if problem:
                print(f"[MusicBot] Error opening the folder dialog: {problem}")
                self._bot.game.put(lambda: speak("Error opening the folder dialog."))
                return
            if not folder:
                self._bot.game.put(lambda: speak("No folder selected."))
                return
            tracks = local_tracks(music_files_in(folder))
            if not tracks:
                self._bot.game.put(lambda: speak("No music files in that folder."))
                return
            self._bot.game.put(lambda: self._bot._start_track_queue(
                tracks, "Local files"))

        threading.Thread(target=choose, daemon=True).start()
        speak("Opening folder list...")

    def _start_local_file_stream(self, filepath, title, preserve_queue=False, preserve_feed=False,
                                 playback_generation=None):
        """Start streaming local file"""
        import os
        if not os.path.exists(filepath):
            speak("File not found.")
            return

        if playback_generation is None:
            playback_generation = self._bot._begin_playback_generation()
        if not self._bot._is_current_playback_generation(playback_generation):
            return

        speak(f"Loading local file: {title}")
        self._bot.current_title = title
        self._bot.current_target = filepath
        self._bot.current_source = "local"

        # Save for replay
        self._bot.last_track_title = title
        self._bot.last_track_target = filepath
        self._bot.last_track_source = "local"
        self._bot.is_loading_stream = True

        # Stop any current playback. Faded like any other track change: a local
        # file needs no resolve, so the outgoing song and the new one really do
        # overlap (Crossfade Between Songs sets that window).
        self._bot.stop(
            clear_queue=not preserve_queue,
            clear_feed=not preserve_feed,
            invalidate_pending=False,
            fade=True,
        )

        # Start streaming local file via ffmpeg -> AudioStreamer
        self._bot._start_youtube_stream(filepath, title, playback_generation)

    def _open_search_input(self):
        """Open the text input for search query"""
        self._bot._gp = self._bot._find_gameplay()
        if self._bot._gp:
            self._bot._gp.add_substate(self._bot.game.input.run(
                "Enter song name:",
                handeler=self._bot._on_search_submit
            ))

    def _open_link_input(self):
        """Open the text input for a pasted YouTube or Facebook link.

        The line beside "Search YouTube" for a link somebody already has in
        hand (a chat message, a browser tab): no search, no result list, the
        link is read and played -- or queued, when Queue Mode is on, exactly
        as a picked search result would be. Music Bot Mode is staff-only, so
        this is a staff shortcut rather than a new permission.
        """
        self._bot._gp = self._bot._find_gameplay()
        if self._bot._gp:
            self._bot._gp.add_substate(self._bot.game.input.run(
                "Paste a YouTube or Facebook link (video, reel or live):",
                handeler=self._bot._on_link_submit
            ))

    def _on_search_submit(self, query):
        """Called when user submits search query"""
        # ALWAYS pop the input substate first — otherwise it blocks all events!
        gp = self._bot._gp or self._bot._find_gameplay()
        if gp:
            gp.pop_last_substate()

        if not query.strip():
            speak("Search cancelled.")
            return

        speak(f"Searching: {query}")
        self._bot.searching = True

        # Search in background thread to not block game
        def do_search():
            results = YouTubeSearcher.search(query, count=5)
            self._bot.search_results = results
            self._bot.searching = False
            # Remember search durations so queued / replayed songs can later
            # crossfade even before their own URL is resolved.
            for r in results or ():
                target = r.get("webpage_url") or r.get("url") or ""
                self._bot._note_track_duration(target, r.get("duration"))
            # Show results menu on main thread
            self._bot.game.put(lambda: self._bot._show_results_menu(results))

        t = threading.Thread(target=do_search, daemon=True)
        t.start()

    def _on_link_submit(self, raw_url):
        """Called when a pasted link is submitted: read it, then play it."""
        # Pop the input substate first, for the same reason as a search: it
        # otherwise keeps every later key event.
        gp = getattr(self._bot, "_gp", None) or self._bot._find_gameplay()
        if gp:
            gp.pop_last_substate()

        url = (raw_url or "").strip()
        if not url:
            speak("Link cancelled.")
            return
        # Be kind to a link pasted without a scheme, and to an old http one:
        # the bot resolves https YouTube and Facebook pages only.
        if not url.lower().startswith(("http://", "https://")):
            url = "https://" + url
        if url.lower().startswith("http://"):
            url = "https://" + url[len("http://"):]
        if not is_supported_music_url(url):
            speak("Only YouTube and Facebook links can be played on the Music Bot.")
            return

        speak("Reading the link...")
        self._bot.searching = True

        def do_read():
            info = YouTubeSearcher.read_link(url)
            self._bot.searching = False
            self._bot.game.put(lambda: self._bot._play_read_link(info))

        threading.Thread(target=do_read, daemon=True).start()

    def _play_read_link(self, info):
        """What a pasted link becomes: a queued song in Queue Mode, else now."""
        if not info or not info.get("webpage_url"):
            speak("Could not read that link. Please check it.")
            return
        title = info.get("title") or "Audio"
        webpage_url = info["webpage_url"]
        # Queue Mode means "line it up": the same behaviour as pressing Enter
        # on a search result, so the two ways of adding a song never diverge.
        if self._bot.queue_mode:
            self._bot._enqueue_track(title, webpage_url, "youtube",
                                webpage_url=webpage_url)
            return
        self._bot._start_youtube_stream_from_search(title, webpage_url, "")

    def _start_youtube_stream_from_search(self, title, webpage_url, direct_url,
                                          http_headers=None, preserve_queue=False):
        if self._bot.is_loading_stream:
            speak("Please wait, already loading a track.")
            return

        # A manually selected search result leaves the private feed.
        self._bot._clear_personal_feed()
        playback_generation = self._bot._begin_playback_generation()

        speak(f"Loading: {title}")
        self._bot.current_title = title
        self._bot.current_target = webpage_url or direct_url
        self._bot.current_source = "youtube"

        # Save for replay
        self._bot.last_track_title = title
        self._bot.last_track_target = webpage_url or direct_url
        self._bot.last_track_source = "youtube"
        self._bot.last_youtube_url = webpage_url
        self._bot.last_youtube_title = title
        self._bot.current_duration = None  # refreshed by the resolve below
        self._bot.is_loading_stream = True

        # Stop any current playback (preserve the favorites/playlist queue
        # when this track came from the play-next queue).
        self._bot.stop(invalidate_pending=False, fade=True,
                  clear_queue=not preserve_queue)
        self._bot.is_loading_stream = True

        # Get stream URL in background
        def do_play():
            import threading
            # Resolve again at selection time so URL and authorization headers
            # are fresh. Search result direct URLs are only a fallback for
            # providers that do not expose a canonical webpage URL.
            stream_info = (
                YouTubeSearcher.get_stream_info(webpage_url,
                    cancelled=lambda: not self._bot._is_current_playback_generation(playback_generation))
                if webpage_url else None
            )
            if not self._bot._is_current_playback_generation(playback_generation):
                return
            if not stream_info and direct_url:
                stream_info = {
                    'url': direct_url,
                    'http_headers': dict(http_headers or {}),
                }
            if not stream_info:
                if self._bot._is_current_playback_generation(playback_generation):
                    speak("Failed to get audio stream.")
                    self._bot.is_loading_stream = False
                return
            self._bot._note_track_duration(webpage_url or direct_url,
                                      stream_info.get("duration"))
            # Start streaming on main thread
            self._bot.game.put(lambda: self._bot._start_youtube_stream(
                stream_info['url'], title, playback_generation,
                http_headers=stream_info.get('http_headers'),
                canonical_url=webpage_url or direct_url,
            ))

        t = threading.Thread(target=do_play, daemon=True)
        t.start()

    def _start_youtube_stream(self, audio_url, title, playback_generation=None,
                              http_headers=None, canonical_url=None,
                              start_offset=0.0, start_paused=False):
        """Start streaming from a YouTube audio URL or a local media file.

                    start_offset seeks the new decode head to a content position in seconds (ffmpeg input
                    seek, like the jukebox mid-song path); start_paused keeps the pre-buffered head silent.
                    """
        if (playback_generation is not None
                and not self._bot._is_current_playback_generation(playback_generation)):
            return
        self._bot.is_loading_stream = False
        self._bot._create_stream_source()
        if not self._bot._output_source():
            speak("Audio error.")
            return

        self._bot.current_duration = (
            self._bot._known_durations.get(canonical_url)
            if canonical_url else None
        )
        # The player's own device, when one is chosen and this song is not
        # going into a room's speakers (those are map objects on the game's
        # device). One slot per stream: a crossfade runs two at once.
        remote, remote_slot = self._bot._stream_remote()
        self._bot.streamer = AudioStreamer(
            self._bot.game, audio_url,
            self._bot._output_source(),
            self._bot.volume, bot=self._bot,
            http_headers=http_headers,
            canonical_url=canonical_url,
            start_offset=start_offset,
            start_paused=start_paused,
            # A room replaces the ear source entirely: the same interleaved
            # stereo decode is rendered into one feed per speaker.
            cinema=self._bot.cinema_bank,
            remote=remote,
            remote_slot=remote_slot or 0,
            remote_category=self._bot._music_output_category(),
        )
        self._bot.streamer.start()

        self._bot.mode = "youtube"
        self._bot.playing = True
        # A seek performed while paused stays paused: the new streamer starts
        # silent and only becomes audible when the bot is resumed.
        self._bot.paused = bool(start_paused)
        self._bot.current_title = title
        # Captions for the page this stream came from. A seek restarts the same
        # track through here, and that is on purpose: the cues are unchanged
        # and the pump reads the new stream's own position, so a seek needs no
        # caption bookkeeping at all.
        self._bot._begin_subtitles(canonical_url)
        self._bot._stream_announced = False
