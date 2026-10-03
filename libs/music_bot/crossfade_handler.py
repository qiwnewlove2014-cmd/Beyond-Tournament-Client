"""Crossfade between auto-advanced tracks - the Music Bot's half (extracted from
controller.py).

With the current track's duration known, the NEXT queued track is pre-rolled
(resolve + a paused ffmpeg on its own source) during the last seconds; right
before the outro runs out it is unpaused under gain 0 and both ramp over
`MapMusicBot.CROSSFADE_SECONDS`.

**The state stays on `MapMusicBot`; the behaviour moved here.** A test reads
`crossfade_enabled`, `current_duration`, `_known_durations` and `_crossfade`
straight off the bot, and committing a fade promotes a track by writing the
bot's own playback state (its title, source, streamer, playback generation and
so on) -- so those attributes are still the bot's and this class reads and
writes them through its owner, the way `DrumHandler` writes
`gameplay.drum_mode`. The three windows (`CROSSFADE_SECONDS`,
`CROSSFADE_PREP_SECONDS`, `CROSSFADE_LAUNCH_SECONDS`) are declared on the bot
for the same reason (`MapMusicBot.CROSSFADE_SECONDS` is read off it by a test).

One rule for the reader, because this file names its owner on every line:
**`self.` is this handler, `self._bot.` is the Music Bot.** Unlike
`CinemaHandler` -- whose coupling is a handful of names, listed there as
pass-throughs -- this machine writes a wide slice of the bot's playback state
from inside `_commit_crossfade`, so an explicit `self._bot.` on each line says
whose state is touched instead of hiding it behind a property layer.

Usage from MapMusicBot::

    self._crossfade_handler = CrossfadeHandler(self)   # or built on first use
    self._crossfade_machine()._update_crossfade()      # every frame while playing

Pins: client/tests/test_music_bot_crossfade_eq.py -- its `AudioStreamer` fake is
patched here, where the candidate is built. Rules and measured numbers:
.agents/skills/music_bot_integration/.
"""

import contextlib
import threading
import time

from .. import options
from .media import YouTubeSearcher
from .streaming import AudioStreamer


class CrossfadeHandler:
    """The Music Bot's pre-roll / fade state machine (see the module docstring)."""

    def __init__(self, bot):
        self._bot = bot          # back-reference to MapMusicBot

    # ------------------------------------------------------------------
    # the machine (moved from controller.py -- `self._bot.` is the bot)
    # ------------------------------------------------------------------

    def _remaining_seconds(self):
        """Seconds left on the current track, when its duration is known."""
        if not self._bot.current_duration:
            return None
        position = self._bot.track_position()
        if position is None:
            return None
        return float(self._bot.current_duration) - position

    def _peek_next_track(self):
        """The track the normal end-of-song path would play next (no consume)."""
        if self._bot.next_up_queue:
            return self._bot.next_up_queue[0]
        if self._bot.play_queue and 0 <= self._bot.play_queue_index + 1 < len(self._bot.play_queue):
            return self._bot.play_queue[self._bot.play_queue_index + 1]
        return None

    def _consume_next_track(self):
        """Consume the next track exactly like the end-of-song advance does."""
        if self._bot.next_up_queue:
            return self._bot.next_up_queue.pop(0)
        if self._bot.play_queue and 0 <= self._bot.play_queue_index + 1 < len(self._bot.play_queue):
            self._bot.play_queue_index += 1
            return self._bot.play_queue[self._bot.play_queue_index]
        return None

    def _cancel_crossfade(self):
        """Abandon any pre-roll / in-progress fade and free its resources.

                    Never touches the CURRENT streamer/source (the caller owns those) - only the extra
                    candidate pipeline and, mid-fade, the retired outgoing stream.
                    """
        state = getattr(self._bot, "_crossfade", None)
        if not state:
            return False
        self._bot._crossfade = None
        pairs = (
            (state.get("candidate"), state.get("candidate_source")),
            (state.get("old_streamer"), state.get("old_source")),
        )
        for streamer, source in pairs:
            if streamer is not None and streamer is not self._bot.streamer:
                try:
                    streamer.stop()
                except Exception:
                    pass
            if source is not None and source is not self._bot.stream_source:
                self._bot._delete_source(source)
        return True

    def _start_crossfade_roll(self):
        """Begin pre-rolling the next queued track (called each frame)."""
        if self._bot.cinema_active_target():
            # A room is fed by one stream at a time; a pre-rolled second
            # streamer would queue its own frames into the same speakers and
            # double the audio. Tracks cut over instead of crossfading.
            return
        if not self._bot.crossfade_enabled or self._bot.is_loading_stream:
            return
        remaining = self._remaining_seconds()
        if remaining is None or remaining <= 0.0:
            return
        if remaining > self._bot.CROSSFADE_SECONDS + self._bot.CROSSFADE_PREP_SECONDS:
            return
        streamer = self._bot.streamer
        if streamer is None or not streamer.is_alive():
            return
        track = self._peek_next_track()
        if not track or not track.get("target"):
            return
        state = {
            "phase": "resolving",
            "track": dict(track),
            "old_streamer": streamer,
            "old_source": self._bot.stream_source,
            "stream_info": None,
            "resolve_done": False,
            "candidate": None,
            "candidate_source": None,
            "duration": None,
            "fade_started_at": None,
            "old_gain0": None,
        }
        self._bot._crossfade = state
        source = track.get("source", "youtube")
        target = str(track.get("target", ""))
        if source != "local" and not target.startswith(("http://", "https://")):
            self._bot._crossfade = None
            return
        if source != "local":
            webpage = str(track.get("webpage_url") or target)

            def do_resolve():
                info = YouTubeSearcher.get_stream_info(
                    webpage,
                    cancelled=lambda: self._bot._crossfade is not state,
                )
                if self._bot._crossfade is not state:
                    return
                state["stream_info"] = info if info else None
                if info:
                    self._bot._note_track_duration(webpage, info.get("duration"))
                    state["duration"] = self._bot._known_durations.get(
                        webpage) or info.get("duration")
                state["resolve_done"] = True

            threading.Thread(target=do_resolve, daemon=True).start()
        else:
            # Local files need no URL resolution; launch straight away.
            state["resolve_done"] = True
            state["stream_info"] = {"url": target, "http_headers": {}}

    def _create_crossfade_candidate(self, state):
        """Launch the pre-rolled next streamer, silent and network-muted."""
        src = self._bot._new_bot_source()
        if src is None:
            self._cancel_crossfade()
            return
        info = state.get("stream_info") or {}
        track = state["track"]
        source = track.get("source", "youtube")
        url = info.get("url") or track.get("target", "")
        canonical = None
        headers = {}
        if source != "local":
            canonical = str(track.get("webpage_url") or track.get("target", ""))
            headers = dict(info.get("http_headers") or {})
        try:
            cand = AudioStreamer(
                self._bot.game, url, src, self._bot.volume, bot=self._bot,
                http_headers=headers,
                canonical_url=canonical,
                start_paused=True,
            )
        except Exception:
            self._bot._delete_source(src)
            self._cancel_crossfade()
            return
        cand.network_muted = True  # silent until the fade actually hands over
        cand.start()
        state["candidate"] = cand
        state["candidate_source"] = src
        # Keep the candidate source silent while it pre-rolls.
        with contextlib.suppress(Exception):
            src.gain = 0.0

    def _commit_crossfade(self, state):
        """Make the pre-rolled candidate the current track and fade it in
        while the outgoing stream fades out. Returns True on success."""
        cand = state.get("candidate")
        cand_src = state.get("candidate_source")
        old_streamer = state.get("old_streamer")
        old_source = state.get("old_source")
        if cand is None or cand_src is None:
            return False
        if self._bot.streamer is not old_streamer or self._bot.stream_source is not old_source:
            return False
        if not cand.prebuffer_event.is_set():
            return False
        track = self._consume_next_track()
        if track is None:
            self._cancel_crossfade()
            return False
        self._bot._begin_playback_generation()
        title = track.get("title", "Unknown")
        target = str(track.get("target", ""))
        source = track.get("source", "youtube")
        webpage = target if source != "local" else ""
        self._bot.current_title = title
        self._bot.current_target = webpage or target
        self._bot.current_source = source
        self._bot.last_track_title = title
        self._bot.last_track_target = webpage or target
        self._bot.last_track_source = source
        if source != "local":
            self._bot.last_youtube_url = webpage
            self._bot.last_youtube_title = title
        self._bot.current_duration = state.get("duration")
        self._bot.mode = "youtube"
        self._bot.playing = True
        self._bot.paused = False
        self._bot._stream_announced = False
        self._bot._current_reverb_slot = None
        self._bot.streamer = cand
        self._bot.stream_source = cand_src
        self._bot._apply_eq_to_source(cand_src)
        state["old_gain0"] = 0.0
        if old_source is not None:
            with contextlib.suppress(Exception):
                state["old_gain0"] = float(getattr(old_source, "gain", 0.0) or 0.0)
        # Hand the room the same overlap the performer hears: the outgoing
        # network leg blends into the incoming one (old fades out, new fades
        # in) instead of hard-switching. The candidate stays network-muted
        # while the blend runs and only takes over the leg when the fade ends.
        if old_streamer is not None:
            # Drop any frames the candidate queued while pre-rolling so the
            # blend starts at the live position, not seconds behind.
            try:
                while True:
                    cand.network_queue.get_nowait()
            except Exception:
                pass
            if hasattr(old_streamer, "begin_network_crossfade"):
                old_streamer.begin_network_crossfade(
                    cand, self._bot.CROSSFADE_SECONDS)
            else:
                old_streamer.network_muted = True
        with contextlib.suppress(Exception):
            cand_src.gain = 0.0
        cand.set_pause(False)
        state["phase"] = "fading"
        state["fade_started_at"] = time.monotonic()
        return True

    def _update_fade_gains(self, base_gain):
        """Per-frame gain ramp during the overlap (main thread only)."""
        state = self._bot._crossfade
        if state is None or state.get("phase") != "fading":
            return
        elapsed = time.monotonic() - (state.get("fade_started_at") or time.monotonic())
        progress = min(1.0, max(0.0, elapsed / self._bot.CROSSFADE_SECONDS))
        src = self._bot.stream_source
        old_source = state.get("old_source")
        if src is not None:
            with contextlib.suppress(Exception):
                src.gain = base_gain * progress
        if old_source is not None:
            with contextlib.suppress(Exception):
                old_source.gain = (state.get("old_gain0") or 0.0) * (1.0 - progress)
        if progress >= 1.0:
            if src is not None:
                with contextlib.suppress(Exception):
                    src.gain = base_gain
            old_streamer = state.get("old_streamer")
            if old_streamer is not None and old_streamer is not self._bot.streamer:
                with contextlib.suppress(Exception):
                    old_streamer.stop()
            # The blend is over: the incoming stream's own network leg takes
            # over the broadcast now (it stayed muted through the overlap).
            if self._bot.streamer is not None:
                with contextlib.suppress(Exception):
                    self._bot.streamer.network_muted = False
            self._bot._delete_source(old_source)
            self._bot._crossfade = None

    def _update_crossfade(self):
        """Drive the crossfade state machine (called every frame while playing)."""
        streamer = self._bot.streamer
        if streamer is None or not streamer.is_alive():
            # The song ended while we were pre-rolling: abandon the candidate;
            # the normal end-of-song advance plays the next track instead.
            self._cancel_crossfade()
            return
        if not self._bot.crossfade_enabled or self._bot.is_loading_stream:
            return
        state = self._bot._crossfade
        if state is None:
            self._start_crossfade_roll()
            state = self._bot._crossfade
            if state is None:
                return
        if state.get("phase") == "fading":
            # The overlap is already underway; the gain ramp in
            # _update_fade_gains drives the rest of the transition.
            return
        remaining = self._remaining_seconds()
        if remaining is None or remaining <= 0.0:
            return
        if state.get("phase") == "resolving":
            if not state.get("resolve_done"):
                return
            if not state.get("stream_info"):
                # Resolution failed: let the normal end-of-song retry it.
                self._cancel_crossfade()
                return
            if (state.get("candidate") is None
                    and remaining <= self._bot.CROSSFADE_SECONDS + self._bot.CROSSFADE_LAUNCH_SECONDS):
                self._create_crossfade_candidate(state)
                if state.get("candidate") is not None:
                    # Pre-roll launched: wait for its pre-buffer, then hand over.
                    state["phase"] = "waiting"
            return
        cand = state.get("candidate")
        if cand is None:
            return
        if cand.failure_reason is not None and not cand.prebuffer_event.is_set():
            # The candidate stream failed to start; fall back to the normal
            # end-of-song path so the next track still plays.
            self._cancel_crossfade()
            return
        if cand.prebuffer_event.is_set() and remaining <= self._bot.CROSSFADE_SECONDS:
            if not self._commit_crossfade(state):
                self._cancel_crossfade()

    def _set_crossfade_enabled(self, enabled):
        """Persist the crossfade toggle; abort any roll started under it."""
        self._bot.crossfade_enabled = bool(enabled)
        options.set("music_bot_crossfade", self._bot.crossfade_enabled)
        if not self._bot.crossfade_enabled:
            self._cancel_crossfade()
