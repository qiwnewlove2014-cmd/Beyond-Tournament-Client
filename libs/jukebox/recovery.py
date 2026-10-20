"""The watchdog: whether what a listener hears is still what is playing.

One gameplay frame asks one question per phase -- retire what is finishing, keep the rooms
current, fire the notes that are waiting, ask every output how it is, and turn that answer
into at most one resync and the rebuilds it owes. The state and the phases it drives (the
players, their entries, the rooms, the note clocks) stay in `player.py`; the verdict and the
ladder that repairs it live here.

Recover a stream that stopped without a stop packet; the server's jukebox_resync reply is the
authoritative play state plus warm-up frames. The ladder, so every failure ends in a rebuild
rather than an infinite silent wait: frames stopped arriving -> resync + warm-up un-stick
(5 s), escalating to a full rebuild at 12 s; frames arriving but no audible progress -> full
rebuild at 10 s; a relay_pending placeholder whose follow-up never lands -> resync at 15 s.

It is a base class of `JukeboxPlayer` and not a helper taking a player: every phase reads the
same `self.players`, `self._lock` and strike counters as the rest of the class, so the split
moved statements between files without rewriting one of them. A test that patches a name this
code READS (`log_line`, `speak`, `time`) must patch it here, in the module that owns it.
"""

import time

import cyal

from ..deferred_log import log_deferred as log_line
from ..speech import speak


class RecoveryWatchdog:
    """One frame's verdict on every output, and the ladder that repairs it."""

    RELAY_STARTUP_TIMEOUT = 7.0
    RELAY_STALL_TIMEOUT = 5.0
    # A stall that survived a warm-up un-stick attempt escalates to a full
    # rebuild (chronic loss of the unreliable relay channel).
    RELAY_HARD_STALL_TIMEOUT = 12.0
    # Frames still arriving but OpenAL making no audible progress for this
    # long -> full rebuild (dead sources / lost context after an audio device
    # switch — the packet-based stall check can never see this state).
    RELAY_AUDIO_STALL_TIMEOUT = 10.0
    # Playback started but the speakers stopped CONSUMING buffers for this long -> the
    # stream is slowly starving (underrunning): frames arrive just often enough to dodge
    # the packet stall check and buffers still queue, yet the listener hears silence or a
    # stutter loop. Only real output consumption proves otherwise.
    RELAY_OUTPUT_STALL_TIMEOUT = 8.0
    # Direct playback has no relay to announce a mid-song death and ffmpeg can hang without
    # exiting, so DIRECT_OUTPUT_STALL_TIMEOUT = 8.0 s of no buffer consumed while the thread is
    # alive and playing means rebuild.
    DIRECT_OUTPUT_STALL_TIMEOUT = 8.0
    # Frame-rate starvation: a trickling channel passes every liveness check while the listener
    # hears a sped-up stutter. A healthy relay delivers 25 frames/s, so below half of that over
    # RELAY_STARVE_WINDOW = 10.0 s the rebuild ladder (ending in direct TCP) starts mid-song.
    RELAY_STARVE_WINDOW = 10.0
    RELAY_STARVE_MIN_FPS = 12.5
    # A relay_pending placeholder whose follow-up play event never landed
    # (server worker died mid-retry) asks for a resync after this long.
    RELAY_PENDING_TIMEOUT = 15.0
    RECOVERY_COOLDOWN = 5.0
    # RELAY_UNSTICK_TRIES = 2: this many failed resync + warm-up un-sticks rebuild immediately
    # instead of waiting for the hard-stall timeout (observed after map reloads).
    RELAY_UNSTICK_TRIES = 2
    # RELAY_DIRECT_FALLBACK_AFTER = 3 consecutive relay-recovery failures: play this song
    # directly over HTTP/ffmpeg. Chronic loss of one peer's unreliable relay channel is never
    # fixed by resyncing - the warm-up frames use the same lost channel.
    RELAY_DIRECT_FALLBACK_AFTER = 3
    # Direct stays sticky for this long (or until a map change) so later songs
    # don't re-pay the failing-relay window; relay is then tried again.
    DIRECT_FALLBACK_TTL = 10 * 60.0
    # A pending relay route that never turns into a real receiver (play was
    # rejected, or the server switched relay identity) is swept after this.
    RELAY_PENDING_TTL = 10.0
    # A deliberate hold is not a fault. A room that ran low pauses every speaker while it
    # refills, and start_playback() re-forms it (realign, then _play_ready) before it is
    # audible again -- for a frame or two every speaker is stopped with audio queued. One
    # snapshot of that was read as a dead output and rebuilt a healthy room, which cut the
    # song and restarted it at the room's position: heard as the song starting mid-way.
    # The reading has to persist before it counts. A genuinely dead room is still caught
    # in 1.5 s, with the packet/output ladders (8-12 s) behind it.
    STOPPED_SOURCES_PERSIST = 1.5
    # A cabinet the Server says is playing while this client has no output for it at all
    # (the play event was missed, or a rebuild removed the entry) is the one failure no
    # phase in _scan_outputs can see: every phase asks an existing entry how it is. Ask
    # the Server for the authoritative play after this long, then play the song here
    # directly rather than wait for a cabinet menu that may never open.
    UNHEARD_RESYNC_AFTER = 5.0
    UNHEARD_DIRECT_AFTER = 15.0

    def update(self):
        """One gameplay frame's health check, one phase per question.

        Recover a stream that stopped without a stop packet; the server's
        jukebox_resync reply is the authoritative play state plus warm-up frames.
        The ladder, so every failure ends in a rebuild rather than an infinite
        silent wait: frames stopped arriving -> resync + warm-up un-stick (5 s),
        escalating to a full rebuild at 12 s; frames arriving but no audible
        progress -> full rebuild at 10 s; a relay_pending placeholder whose
        follow-up never lands -> resync at 15 s.

        The order is the whole method: retire what is finishing, keep the rooms
        current, fire the notes that are waiting, ask every output how it is, and
        turn that answer into at most one resync and the rebuilds it owes.
        """
        now = time.monotonic()
        self._sweep_retiring_direct()

        self._refresh_cinema(now)
        self._pump_note_clocks()
        rebuilds, stalled_ids, needs_resync = self._scan_outputs(now)
        # A cabinet the Server is playing that this client has nothing for has no entry
        # for any phase above to ask about (see _adopt_unheard_cabinets).
        needs_resync = self._adopt_unheard_cabinets(now) or needs_resync
        self._recover(rebuilds, stalled_ids, needs_resync)

    def _scan_outputs(self, now):
        """Ask every playing output how it is, and collect this frame's faults.

        Returns ``(rebuilds, stalled_ids, needs_resync)``: the cabinets that need
        a rebuild and why, the relays relying on a warm-up un-stick this cycle, and
        whether the server's authoritative state must be asked for at all.
        """
        rebuilds = []  # [(jukebox_id, reason)]
        stalled_ids = []  # relays relying on a warm-up un-stick this cycle
        needs_resync = False
        with self._lock:
            self._drop_stale_relay_routes(now)
            for jukebox_id, entry in list(self.players.items()):
                transport = entry.get("transport")
                streamer = entry.get("streamer")
                created_at = float(entry.get("created_at") or now)
                age = now - created_at
                if transport == "relay" and streamer is not None:
                    faults, stalled, resync = self._relay_fault(
                        jukebox_id, entry, streamer, now, age)
                    rebuilds.extend(faults)
                    stalled_ids.extend(stalled)
                elif transport == "relay_pending":
                    # The server said a relay is coming but the follow-up play
                    # event never landed (worker died mid-retry): ask for the
                    # authoritative state instead of waiting for a relog.
                    resync = age >= self.RELAY_PENDING_TIMEOUT
                elif transport == "direct" and streamer is not None:
                    faults, _, resync = self._direct_fault(jukebox_id, streamer, now)
                    rebuilds.extend(faults)
                else:
                    continue
                needs_resync = needs_resync or resync
        if rebuilds:
            needs_resync = True
        return rebuilds, stalled_ids, needs_resync

    def _adopt_unheard_cabinets(self, now):
        """Adopt a cabinet the Server is playing and this client has nothing for.

        Every phase above asks a *known* output how it is; a cabinet whose play event
        never reached play() (or whose entry a rebuild removed) has no entry to ask, so
        nothing noticed it. The listener then sat in silence while the room heard the
        song, and the only thing that ever brought it back was opening the cabinet menu
        -- which re-offers the play event, now from the middle of the track -- or a full
        requeue. Reconcile the Server's own cached state against this client's outputs
        instead: ask for the authoritative play first, then play the song here directly
        rather than wait for a menu that may never open.

        Returns True when a resync is owed.
        """
        playing = self._server_playing_boxes()
        with self._lock:
            for jukebox_id in [jid for jid in self._unheard_since if jid not in playing]:
                self._unheard_since.pop(jukebox_id, None)
        needs_resync = False
        for jukebox_id, box in playing.items():
            if self._has_output_for(jukebox_id):
                self._unheard_since.pop(jukebox_id, None)
                continue
            started = self._unheard_since.setdefault(jukebox_id, now)
            age = now - float(started)
            if age >= self.UNHEARD_DIRECT_AFTER:
                self._play_unheard(jukebox_id, box)
            elif age >= self.UNHEARD_RESYNC_AFTER:
                needs_resync = True
        return needs_resync

    def _server_playing_boxes(self):
        """The Server's own answer: which cabinets are playing a song right now."""
        gp = getattr(self.game, "gameplay", None)
        state = getattr(gp, "jukebox_state", None)
        boxes = state.get("jukeboxes") if isinstance(state, dict) else None
        if not isinstance(boxes, dict):
            return {}
        playing = {}
        for jukebox_id, box in boxes.items():
            if not isinstance(box, dict) or not box.get("current"):
                continue
            if box.get("paused"):
                continue
            playing[str(jukebox_id)] = box
        return playing

    def _has_output_for(self, jukebox_id):
        """True while this cabinet has an entry, a reload's re-offer due, or a tail."""
        with self._lock:
            if jukebox_id in self.players:
                return True
            if jukebox_id in self._pending_map_change:
                # A map reload is in flight; the Server re-offers every playing
                # song right after it, and adoption must not race that.
                return True
            for entry in self._retiring_direct:
                if entry.get("id") == jukebox_id:
                    return True
        return False

    def _play_unheard(self, jukebox_id, box):
        """Play this cabinet's song here, from where the Server says it is."""
        current = box.get("current") or {}
        url = str(current.get("url") or "")
        key = (jukebox_id, url)
        if not url or key in self._unheard_played:
            return False
        if len(self._unheard_played) >= 64:
            self._unheard_played.clear()
        self._unheard_played.add(key)
        duration = int(current.get("duration") or 0)
        position = self._dated_position(box)
        if position is not None and duration > 0:
            position = min(position, max(0.0, duration - 2.0))
        # A needle the client can date is the room's own position and the song is
        # joined there. One it cannot date may belong to the song that just
        # ended -- joining there is exactly how a fresh song started in the
        # middle -- so that song is played from its intro, which is the one thing
        # that is always true of a song this machine has never heard.
        join = position is not None
        self._switch_to_direct(jukebox_id, {
            "x": float(box.get("x") or 0.0),
            "y": float(box.get("y") or 0.0),
            "z": float(box.get("z") or 0.0),
            "title": str(current.get("title") or ""),
            "url": url,
            "duration": duration,
            "start_offset": float(position or 0.0),
            "http_headers": None,
            "canonical_url": url,
            "received_at": time.monotonic(),
        }, sticky=False, join_playing_room=join)
        log_line(f"[Jukebox] {jukebox_id}: this machine had nothing playing while the "
                 f"Server was playing {str(current.get('title') or '')!r}; "
                 f"playing it directly from {float(position or 0.0):.0f}s")
        return True

    @staticmethod
    def _dated_position(box):
        """The song's position now, when the payload that carried it can be dated.

        ``jukebox_state`` sends the needle with the song it belongs to and stamps
        when it was read (``position_at``, written by the packet handler); a
        ``jukebox_queue_update`` after a song change drops both. Without a stamp
        the number may be the previous song's, so it is not used at all.
        """
        read_at = box.get("position_at")
        if read_at is None:
            return None
        try:
            return max(0.0, float(box.get("position") or 0.0)
                       + max(0.0, time.monotonic() - float(read_at)))
        except (TypeError, ValueError):
            return None

    def _drop_stale_relay_routes(self, now):
        """Forget buffered routes whose play event never arrived.

        A route is buffered for a play event that is still sitting on the deferred
        game queue; one that never became a receiver (the play was rejected, or
        the server switched relay identity mid-flight) must not hold its frames
        forever.
        """
        # Drop pending relay routes that never became receivers (play
        # rejected, or the server switched relay identity mid-flight).
        stale_pending = [
            key for key, entry in self._relay_pending.items()
            if now - entry["at"] > self.RELAY_PENDING_TTL
        ]
        for key in stale_pending:
            del self._relay_pending[key]

    def _relay_fault(self, jukebox_id, entry, streamer, now, age):
        """One relay entry's verdict: what is wrong, and what it owes this frame.

        Returns ``(faults, stalled, resync)``. ``faults`` is this entry's own list
        of ``(jukebox_id, reason)`` because a relay can fail two ways at once (a
        starved arrival rate plus sources that stopped consuming); ``stalled``
        means it is waiting on the warm-up replay, and ``resync`` that the server
        must be asked for the authoritative state.

        Frames arriving, audio progressing, the speaker consuming and the arrival
        RATE are four different questions: everything but the rate can look green
        while the listener hears a sped-up stutter fading to silence (a trickling
        channel), which is why the rate is measured over a window. A room that ran
        low holds every speaker on purpose while it rebuilds its depth, which looks
        exactly like "sources stopped with queued audio" from here -- it is left
        alone, because a rebuild would drop the audio it is holding.
        """
        rebuilds = []  # [(jukebox_id, reason)] for this entry only
        stalled_ids = []
        needs_resync = False
        alive = streamer.is_alive() if hasattr(streamer, "is_alive") else True
        last_packet = getattr(streamer, "last_packet_at", None)
        stall_age = (now - float(last_packet)) if last_packet is not None else None
        no_packets = (
            age >= self.RELAY_STARTUP_TIMEOUT and last_packet is None
        ) or (
            stall_age is not None and stall_age >= self.RELAY_STALL_TIMEOUT
        )
        last_audio = getattr(streamer, "last_audio_activity", None)
        has_started = bool(getattr(streamer, "_play_started", False) or age >= self.RELAY_STARTUP_TIMEOUT)
        stuck_audio = bool(
            alive
            and last_packet is not None
            and stall_age is not None and stall_age < 1.0
            and has_started
            and last_audio is not None
            and now - float(last_audio) >= self.RELAY_AUDIO_STALL_TIMEOUT
        )
        if (alive and has_started and last_audio is None
                and getattr(streamer, "failure_reason", None)
                and age >= self.RELAY_AUDIO_STALL_TIMEOUT):
            stuck_audio = True
        # Underrun watchdog: frames may arrive and buffers may queue, but if the speakers have
        # not consumed anything for a while the listener is hearing silence/stutter that every
        # packet-based check calls "healthy".
        last_output = getattr(streamer, "last_output_at", None)
        starved_output = bool(
            alive
            and has_started
            and last_output is not None
            and now - float(last_output) >= self.RELAY_OUTPUT_STALL_TIMEOUT
        )
        hard_stall = bool(
            no_packets and stall_age is not None
            and stall_age >= self.RELAY_HARD_STALL_TIMEOUT
        )
        # Frame-rate starvation: everything above can look green while the listener hears a
        # sped-up stutter fading to silence. Only the arrival RATE over a window exposes the
        # trickling channel (healthy = 25 fps).
        starved_rate = False
        if (alive and not no_packets and not stuck_audio
                and not starved_output
                and has_started):
            frames_now = int(getattr(streamer, "received_frames", 0) or 0)
            check = entry.get("frame_rate_check")
            if not isinstance(check, list) or len(check) != 2:
                entry["frame_rate_check"] = [now, frames_now]
            elif now - float(check[0]) >= self.RELAY_STARVE_WINDOW:
                window = now - float(check[0])
                fps = (frames_now - int(check[1])) / window
                entry["frame_rate_check"] = [now, frames_now]
                if fps < self.RELAY_STARVE_MIN_FPS:
                    starved_rate = True
                    rebuilds.append(
                        (jukebox_id, f"frame starvation ({fps:.1f} fps)")
                    )
        # A room that ran low holds every speaker on purpose while it rebuilds its depth
        # (CinemaSpeakerBank), which looks exactly like "sources stopped with queued audio" from here.
        # Leave it alone: the packet and output watches still own a room that never refills, and a
        # rebuild would drop the audio it is holding.
        room_refilling = bool(getattr(entry.get("cinema"),
                                       "awaiting_refill", False))
        stopped_sources = False
        entry_sources = self._entry_sources(entry)
        if (entry_sources and not room_refilling
                and age >= self.RELAY_STARTUP_TIMEOUT):
            try:
                # A room is as healthy as its busiest speaker: any
                # one of them playing with audio queued means the
                # stream reached OpenAL.
                halted = (
                    any(source.buffers_queued > 0 for source in entry_sources)
                    and not any(source.state == cyal.SourceState.PLAYING
                                for source in entry_sources)
                )
            except Exception:
                halted = False
            # A room that ended a refill hold re-forms before it is audible again
            # (start_playback -> realign -> _play_ready); for a frame or two every
            # speaker is stopped with audio queued, which is exactly this reading. Only
            # a stop that PERSISTS is a dead room -- rebuilding on the snapshot cut a
            # healthy song and restarted it at the room's own position.
            if halted:
                since = entry.get("stopped_since")
                if since is None:
                    entry["stopped_since"] = now
                elif now - float(since) >= self.STOPPED_SOURCES_PERSIST:
                    stopped_sources = True
            else:
                entry.pop("stopped_since", None)
        else:
            entry.pop("stopped_since", None)
        if (alive and not no_packets and not stuck_audio
                and not starved_output and not starved_rate
                and not stopped_sources
                and last_audio is not None):
            # Relay demonstrably working — clear the strike and
            # un-stick counters.
            self._relay_fail_counts[jukebox_id] = 0
            self._stall_unsticks[jukebox_id] = 0
        if not alive:
            rebuilds.append((jukebox_id, "receiver thread died"))
        elif stopped_sources:
            rebuilds.append((jukebox_id, "sources stopped with queued audio"))
        elif stuck_audio:
            rebuilds.append((jukebox_id, "frames arriving but no audio progress"))
        elif starved_output:
            rebuilds.append((jukebox_id, "speaker stopped consuming (underrun)"))
        elif no_packets:
            if hard_stall:
                # The stall survived a warm-up un-stick attempt —
                # stop waiting and rebuild from scratch.
                rebuilds.append((jukebox_id, f"no frames for {stall_age:.0f}s"))
            else:
                stalled_ids.append(jukebox_id)
            needs_resync = True

        return rebuilds, stalled_ids, needs_resync

    def _direct_fault(self, jukebox_id, streamer, now):
        """One direct entry's verdict: a failed decoder or a speaker that stopped.

        A direct fallback has no server relay to signal a mid-song decoder failure,
        so only an explicit failure is restarted; a normally completed stream is
        allowed to await the server's next-song timer. The thread can also stay
        alive while OpenAL stops consuming (a dead sink, an ffmpeg hang that never
        exits): last_output_at only advances when the speakers finish a buffer, so
        a stream with no audible progress is rebuilt.
        """
        rebuilds = []
        stalled_ids = []
        needs_resync = False
        alive = streamer.is_alive() if hasattr(streamer, "is_alive") else True
        # Direct fallback has no server relay to signal a mid-song decoder failure. Restart
        # only an explicit failure; a normally completed stream is allowed to await the
        # server's next-song timer.
        if (not alive
                and getattr(streamer, "failure_reason", None)):
            rebuilds.append((jukebox_id, "direct streamer failed"))
        else:
            # Output-stall watchdog: the thread can stay alive while OpenAL stops consuming (a dead sink,
            # an ffmpeg hang that never exits). last_output_at only advances when the speakers finish a
            # buffer, so a stream with no audible progress is rebuilt; a clean end exits within a second.
            has_started = bool(
                getattr(streamer, "ready_event", None)
                and streamer.ready_event.is_set())
            last_output = getattr(streamer, "last_output_at", None)
            if (alive and has_started
                    and getattr(streamer, "running", False)
                    and last_output is not None
                    and now - float(last_output)
                    >= self.DIRECT_OUTPUT_STALL_TIMEOUT):
                rebuilds.append(
                    (jukebox_id,
                     "direct output stalled (speaker not consuming)"))

        return rebuilds, stalled_ids, needs_resync

    def _recover(self, rebuilds, stalled_ids, needs_resync):
        """Send at most one resync, and rebuild only what the resync cannot fix.

        A rebuild is never started without the recovery request: the request is
        what brings the warm-up frames the rebuild then relies on.
        """
        if rebuilds:
            needs_resync = True
        if not needs_resync:
            return
        # Do not remove a dead route unless this call actually sent the recovery
        # request. Otherwise a cooldown could leave no route behind to trigger the
        # next retry.
        if not self.request_resync("relay recovery"):
            return
        self._count_unsticks(stalled_ids, rebuilds)
        self._rebuild(rebuilds)

    def _count_unsticks(self, stalled_ids, rebuilds):
        """Escalate a relay whose warm-up replay did not hold into a rebuild.

        The resync just went out: every still-stalled relay that is NOT being
        rebuilt now owes its recovery to the warm-up replay. Count these un-stick
        attempts -- when they repeat without holding (observed after map reloads:
        the warm-up blip plays but live frames never follow), rebuild right away
        instead of waiting out the hard-stall timeout.
        """
        for jukebox_id in stalled_ids:
            if any(jid == jukebox_id for jid, _ in rebuilds):
                continue
            unsticks = self._stall_unsticks.get(jukebox_id, 0) + 1
            self._stall_unsticks[jukebox_id] = unsticks
            if unsticks >= self.RELAY_UNSTICK_TRIES:
                rebuilds.append((jukebox_id, "warm-up un-stick did not hold"))

    def _rebuild(self, rebuilds):
        """Stop and rebuild each faulted cabinet, going direct after three strikes.

        A dead receiver must be removed before the matching jukebox_play arrives;
        otherwise idempotent same-identity handling would retain it. The resync
        reply rebuilds it with fresh sources, and the buffered pending-route frames
        plus the server's warm-up replay bridge the gap seamlessly. A strike is per
        EXECUTED recovery attempt (not per scan frame): three failed rebuild cycles
        in a row means resyncing cannot fix this connection, so that cabinet plays
        direct instead of looping rebuilds forever on a lost unreliable channel.
        """
        direct_switches = []
        for jukebox_id, reason in rebuilds:
            log_line(f"[Jukebox] auto-recovery: rebuilding {jukebox_id} ({reason})")
            params = None
            with self._lock:
                entry = self.players.get(jukebox_id)
                # A strike per EXECUTED recovery attempt (not per scan
                # frame): three failed rebuild cycles in a row means
                # resyncing cannot fix this connection.
                strikes = self._relay_fail_counts.get(jukebox_id, 0) + 1
                self._relay_fail_counts[jukebox_id] = strikes
                if entry is not None and strikes >= self.RELAY_DIRECT_FALLBACK_AFTER:
                    # Snapshot before stop() removes the entry.
                    params = dict(entry.get("play_params") or {})
            self.stop(jukebox_id)
            if params is not None:
                direct_switches.append((jukebox_id, params))
        for jukebox_id, params in direct_switches:
            # Relay keeps failing on this connection: switch this one
            # jukebox to local direct playback (TCP-based) instead of
            # looping rebuilds forever on a lost unreliable channel.
            self._switch_to_direct(jukebox_id, params)

    def _switch_to_direct(self, jukebox_id, params, sticky=True, join_playing_room=True):
        # Emergency: play the current song locally over HTTP when the relay channel is chronically
        # unusable on this connection - direct playback does not depend on it at all, so it survives
        # exactly the cases resyncing cannot fix (where even the warm-up frames die). Other listeners
        # keep hearing the server relay unaffected.
        params = dict(params or {})
        if sticky:
            # Only a cabinet whose own relay keeps failing stays direct for the TTL. A
            # song adopted because this machine never heard it start must not: the next
            # relay offer (make-before-break) then brings it back onto the room's stream.
            self._direct_fallback_until[jukebox_id] = time.monotonic() + self.DIRECT_FALLBACK_TTL
        if not params.get("url"):
            return
        # Continue from the song's current wall-clock position: the original
        # offset plus everything elapsed since the play event arrived.
        now = time.monotonic()
        offset = float(params.get("start_offset") or 0.0) + max(
            0.0, now - float(params.get("received_at") or now))
        if not sticky:
            # Adopted because this machine never heard the song start, not because
            # its relay failed: the log has to say which, or a reader of it learns
            # the wrong story about the cabinet.
            log_line(f"[Jukebox] {jukebox_id}: no output for a song the Server is "
                     f"playing; adopting it as direct playback")
        else:
            log_line(f"[Jukebox] auto-recovery: {jukebox_id} relay unusable — direct playback")
        speak("Jukebox playing directly.")
        self.play(
            jukebox_id, params["x"], params["y"], params["z"],
            params.get("title", ""), params["url"], params.get("duration") or 0,
            transport="direct", start_offset=offset,
            http_headers=params.get("http_headers"),
            canonical_url=params.get("canonical_url"),
            # This machine fell back while the rest of the room still hears the relay: no lead-in, or it
            # trails by exactly DIRECT_LEAD_IN_S for the whole song with no sender-lag report to compensate
            # (its own anchor says it started on time), and it must JOIN the playing room at its position,
            # never start from 0. Residual resolve/startup overrun is reported through direct_late_s.
            room_lead_in_s=0.0,
            join_playing_room=join_playing_room,
        )

    def request_resync(self, reason="manual recovery", *, raise_errors=False):
        # Ask the server for current jukebox routes and relay warm-up frames. This is safe after
        # a UI transition: the server remains the playback authority, and the cooldown prevents
        # repeated requests from creating needless traffic.
        now = time.monotonic()
        with self._lock:
            # A UI close needs no server round-trip when this map has no
            # active jukebox playback known to the client -- but "this client
            # knows of none" is exactly the state a missed play event leaves,
            # and refusing the request there made the recovery ladder
            # unreachable in the one case it exists for. The Server's own
            # cached answer decides.
            if not self.players and not self._server_playing_boxes():
                return False
            if now - self._last_recovery_request_at < self.RECOVERY_COOLDOWN:
                return False
            self._last_recovery_request_at = now
        try:
            from .. import consts
            self.game.network.send(consts.CHANNEL_MISC, "jukebox_resync")
            log_line(f"[Jukebox] requested resync: {reason}")
            return True
        except Exception:
            if raise_errors:
                raise
            return False
