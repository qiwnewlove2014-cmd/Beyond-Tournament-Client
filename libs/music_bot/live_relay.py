"""The live-instrument relay: guitar/mic/program frames to the party or PA.

This is the no-song sender. ``AudioStreamer`` owns the song path (ffmpeg ->
OpenAL -> network); this module owns the stand-alone relay that keeps a live
instrument or a captured program flowing when no song is playing, including
its 10 ms Party Sync jam leg, stereo mix and channel-31 PA sequencing.

``live_relay_needed`` / ``live_relay_has_input`` / ``mix_live_stereo`` /
``sequence_pa_frame`` also serve the song path's network sender, so
``streaming.py`` imports them from here and re-exports them for the existing
import sites.
"""

import threading
import time

from .. import consts
from .. import voice_chat
from .. import audio_trace
from ..external_audio import downmix_to_mono
from ..party_sync import stereo_upload_eligible


def live_relay_needed(bot):
    """Whether live input has a leg to leave on: public, PA, Party or a room.

    A song routed into a cabinet's room is uploaded even while the Broadcast
    switch is off (``cinema_force_upload``), and a Party Sync session is a
    private upload for its guests. The live relay has to follow the same legs
    or a line-in is left on the positional voice channel while the song reaches
    the room without it.
    """
    if bot is None:
        return False
    return bool(
        getattr(bot, "broadcast_enabled", False)
        or getattr(bot, "broadcast_to_megaphone", False)
        or getattr(bot, "party_sync_force_upload", False)
        or getattr(bot, "cinema_force_upload", False)
    )


def live_relay_has_input(bot):
    """True when a live queue holds a frame the relay should send.

    One answer for both senders: the relay used to start on the guitar and the
    microphone only, so a captured program with no song playing had frames in
    its queue and no thread sending them -- the party heard silence while the
    Signal row said good.
    """
    for name in ("guitar_pcm_queue", "mic_pcm_queue", "external_pcm_queue",
                 "external_live_queue"):
        try:
            queue = getattr(bot, name, None)
            if queue and len(queue) > 0:
                return True
        except Exception:
            continue
    return False


def mix_live_stereo(external_frames, centre_frames, state_key, frame_bytes=3840):
    """One stereo frame: captured sides kept, live centres placed.

    ``frame_bytes`` is the leg's own shape: 3840 for the 20 ms song path and
    1920 for the 10 ms live jam, which is the whole point of the smaller cut.
    A centre source is half as wide, so it is padded to ``frame_bytes // 2``.

    The party leg carries two sides when it is a Party Sync or a room. A
    captured program is the case this exists for -- a stereo mix folded into
    one channel loses its picture -- while a guitar or a voice is a centre
    source by nature and goes to both sides at the same level.
    """
    import audioop

    def as_frame(pcm, size):
        data = bytes(pcm or b"")[:size]
        if len(data) < size:
            data += b"\x00" * (size - len(data))
        return data

    frame_bytes = max(2, int(frame_bytes))
    centre_bytes = max(2, frame_bytes // 2)
    parts = [(as_frame(frame, frame_bytes), 0.85)
             for frame in external_frames if frame]
    centred = [(as_frame(frame, centre_bytes), 0.85)
               for frame in centre_frames if frame]
    if centred:
        mono = voice_chat.mix_audio_frames(
            centred, state_key="%s:centre" % state_key)
        if mono:
            parts.append((audioop.tostereo(bytes(mono), 2, 1.0, 1.0), 0.85))
    if not parts:
        return b""
    mixed = voice_chat.mix_audio_frames(parts, state_key=state_key)
    return as_frame(mixed, frame_bytes) if mixed else b""


def sequence_pa_frame(sender, channel, encoded):
    """Return ``(channel, payload)`` for one outgoing PA frame of ``sender``.

    The PA's listener leg can only conceal a MISSING frame if the frame says
    where it sits in the stream, so a broadcast aimed at the megaphone is
    re-framed for channel 31 (version + epoch + frameSeq in front of the same
    Opus frame) and ``sender`` remembers the sequence in ``_pa_epoch`` /
    ``_pa_seq``. Both network senders in this package hand their frame here -
    the streamer's paced loop and the live relay - so the music bot's PA audio
    travels the same wire as a player's own (voice_chat.pa_timeline_upload).

    A Server that has not advertised ``pa_timeline_v1``, and every channel that
    is not the megaphone, keep the legacy payload on their own channel.
    """
    if (channel != consts.CHANNEL_MEGAPHONE
            or not getattr(sender.game, 'pa_timeline_supported', False)):
        return channel, encoded
    sender._pa_epoch = voice_chat.pa_timeline_epoch(sender._pa_epoch)
    payload = voice_chat.pa_timeline_upload(
        encoded, sender._pa_epoch, sender._pa_seq)
    sender._pa_seq = (sender._pa_seq + 1) & 0xFFFFFFFF
    return consts.CHANNEL_MEGAPHONE_TIMELINE, payload


class LiveRelayStreamer(threading.Thread):
    """Stand-alone streaming thread for live instrument (guitar/mic) PCM when no MP3 audio is playing."""
    # The live relay's own PA sequence (channel 31), same shape as the music
    # bot's: one epoch per relay, stamped on the first PA frame sent.
    _pa_epoch = None
    _pa_seq = 0

    def __init__(self, game, bot):
        super().__init__(daemon=True)
        self.game = game
        self.bot = bot
        self.running = True
        from pyogg import OpusEncoder
        self.encoder = OpusEncoder()
        self.encoder.set_application('audio')
        self.encoder.set_channels(1)  # Opus network stream is MONO
        self.encoder.set_sampling_frequency(48000)
        # The party leg is the one leg that can carry two sides without a
        # song: a captured program is stereo material, and centring it was
        # the mix a listener could not tell apart.
        self.encoder_stereo = OpusEncoder()
        self.encoder_stereo.set_application('audio')
        self.encoder_stereo.set_channels(2)
        self.encoder_stereo.set_sampling_frequency(48000)
        self.last_send_time = None

    def stop(self):
        self.running = False
        voice_chat.reset_audio_limiter(
            voice_chat.audio_limiter_key("live-relay-mix", self))

    def _pace(self, pace):
        """Wait for the next IDEAL frame deadline, not for "sleep just ended".

        The relay used to base the next wait on the wall-clock instant after
        the previous sleep. A Windows sleep that woke 1-2 ms late therefore
        moved the deadline permanently, and a 10 ms stream ran at about 87
        frames per second: its queue filled for a few seconds and then dropped
        a burst (the every-5-7-seconds stutter). Keeping the ideal deadline,
        as AudioStreamer does, makes the next wait shorter and corrects the
        overshoot instead of accumulating it. A real stall still starts a new
        cadence rather than bursting an unbounded catch-up.
        """
        now = time.perf_counter()
        if self.last_send_time is None or now - self.last_send_time > 0.100:
            if self.last_send_time is not None and audio_trace.enabled():
                audio_trace.event(
                    "relay_stall", reason="long",
                    gap_ms=audio_trace.gap_ms(now, self.last_send_time),
                    context=audio_trace.party_context(self.game)
                )
            deadline = now
        else:
            deadline = self.last_send_time + pace
            if now < deadline:
                sleep_time = deadline - now
                if sleep_time > 0.001:
                    time.sleep(sleep_time - 0.001)
                while time.perf_counter() < deadline:
                    pass
            elif now - deadline > 0.040:
                if audio_trace.enabled():
                    audio_trace.event(
                        "relay_stall", reason="late",
                        late_ms=round((now - deadline) * 1000.0, 2),
                        context=audio_trace.party_context(self.game)
                    )
                deadline = now
        self.last_send_time = deadline

    def run(self):
        import audioop
        trace_prev_send = None
        trace_seq = 0
        trace_started_at = time.perf_counter()
        trace_last_idle = 0.0
        audio_trace.event(
            "relay_start", live_supported=bool(getattr(
                self.game, "music_live_supported", False)),
            party_upload=bool(getattr(self.bot, "party_sync_force_upload", False)),
            megaphone=bool(getattr(self.bot, "broadcast_to_megaphone", False)),
            context=audio_trace.party_context(self.game)
        )
        while self.running and live_relay_needed(self.bot):
            # If main AudioStreamer MP3 thread is active, let it handle the network stream
            if self.bot.streamer and self.bot.streamer.is_alive():
                break

            trace_on = audio_trace.enabled()
            if trace_on:
                def _qlen(name):
                    try:
                        return len(getattr(self.bot, name, None) or ())
                    except Exception:
                        return 0
                q_live0 = _qlen("external_live_queue")
                q_song0 = _qlen("external_pcm_queue")
                q_guitar0 = _qlen("guitar_pcm_queue")
                q_mic0 = _qlen("mic_pcm_queue")
            guitar_data = None
            mic_data = None
            external_frames = []
            # The live leg is a Party Sync with no song in THIS machine: the
            # Server advertised channel 25, and the PA path is excluded (it is
            # mono by design). A 10 ms frame is used only while no instrument
            # is playing, because guitar/mic frames are 20 ms -- an instrument
            # keeps the 20 ms shape and still gets the low-latency channel.
            live_leg = bool(
                getattr(self.bot, 'party_sync_force_upload', False)
                and not self.bot.broadcast_to_megaphone
                and getattr(self.game, 'music_live_supported', False)
            )
            live_10 = False
            try:
                if getattr(self.bot, 'guitar_pcm_queue', None) and self.bot.guitar_pcm_queue:
                    guitar_data = self.bot.guitar_pcm_queue.popleft()
            except Exception:
                pass
            try:
                if getattr(self.bot, 'mic_pcm_queue', None) and self.bot.mic_pcm_queue:
                    mic_data = self.bot.mic_pcm_queue.popleft()
            except Exception:
                pass
            try:
                if live_leg and guitar_data is None and mic_data is None:
                    live_queue = getattr(self.bot, 'external_live_queue', None)
                    # ONE 10 ms frame per 10 ms tick. Taking two and mixing
                    # them would sum two consecutive slices into one and
                    # swallow half of what the program played.
                    if live_queue:
                        external_frames.append(live_queue.popleft())
                        live_10 = True
                    if live_10:
                        # The song path's 20 ms queue is not read on this leg;
                        # drain it so a song starting later does not inherit a
                        # half-second of stale audio.
                        song_queue = getattr(self.bot, 'external_pcm_queue', None)
                        drained = 0
                        while song_queue and drained < 2:
                            song_queue.popleft()
                            drained += 1
                if not live_10:
                    external_queue = getattr(self.bot, 'external_pcm_queue', None)
                    # One 20 ms frame per 20 ms tick, for the same reason: a
                    # second frame mixed in here would be a summed slice, not
                    # a queued one.
                    if external_queue:
                        external_frames.append(external_queue.popleft())
            except Exception:
                external_frames = []
                live_10 = False

            if guitar_data is None and mic_data is None and not external_frames:
                if trace_on:
                    now = time.monotonic()
                    if now - trace_last_idle >= 1.0:
                        trace_last_idle = now
                        audio_trace.event(
                            "relay_idle", q_live=q_live0, q_song=q_song0,
                            q_guitar=q_guitar0, q_mic=q_mic0,
                            context=audio_trace.party_context(self.game)
                        )
                time.sleep(0.010)
                continue

            # One output frame: 10 ms stereo on a live jam with no instrument,
            # 20 ms otherwise. Live inputs are aligned and added in wide
            # precision before compression, never saturating mid-mix.
            trace_mix_start = time.perf_counter() if trace_on else 0.0
            frame_bytes = 1920 if live_10 else 3840
            mono_len = frame_bytes // 2
            mono_data = b'\x00' * mono_len
            # What the cabinets play into the owner's own ears when the singer
            # has silenced their own voice (`own_voice_from_cabinets`).
            owner_local_pcm = None
            try:
                def _align(frame, length=mono_len):
                    if len(frame) > length:
                        return frame[:length]
                    if len(frame) < length:
                        return frame + b'\x00' * (length - len(frame))
                    return frame

                mix = []
                if guitar_data is not None:
                    mix.append((_align(guitar_data), 0.85))
                mic_part = None
                if mic_data is not None:
                    mic_part = (_align(mic_data), 0.85)
                    mix.append(mic_part)
                for frame in external_frames:
                    mono_ext = downmix_to_mono(frame)
                    if mono_ext:
                        mix.append((_align(mono_ext), 0.85))
                mixed = voice_chat.mix_audio_frames(
                    mix,
                    state_key=voice_chat.audio_limiter_key(
                        "live-relay-mix", self),
                )
                if mixed:
                    mono_data = mixed

                # Stereo whenever the leg allows it: the captured program's
                # two sides survive, and guitar/voice stay centred.
                stereo_data = None
                if stereo_upload_eligible(self.bot, 2):
                    side_frame = mix_live_stereo(
                        external_frames,
                        [frame for frame in (guitar_data, mic_data) if frame],
                        voice_chat.audio_limiter_key("live-relay-stereo", self),
                        frame_bytes=frame_bytes,
                    )
                    if side_frame:
                        stereo_data = side_frame

                current_volume_scale = (self.bot.volume / 100.0) * getattr(self.bot, 'duck_multiplier', 1.0)
                # Party Sync -> cinema: the host's own guitar/mic is not in
                # external_frames (that capture is tapped at its source), so
                # add just the centre instruments to the room mixer. This is
                # what makes "everyone jamming" include the host too.
                mixer = getattr(self.bot, 'party_room_mixer', None)
                if mixer is not None and (guitar_data is not None or mic_data is not None):
                    host_parts = [
                        (_align(frame), 0.85)
                        for frame in (guitar_data, mic_data) if frame
                    ]
                    host_mix = voice_chat.mix_audio_frames(
                        host_parts,
                        state_key=voice_chat.audio_limiter_key(
                            "party-room-host", self),
                    )
                    if host_mix:
                        mixer.push("host-instrument", host_mix, stereo=False)
                if current_volume_scale != 1.0:
                    mono_data = audioop.mul(mono_data, 2, current_volume_scale)
                    if stereo_data:
                        stereo_data = audioop.mul(stereo_data, 2, current_volume_scale)

                # This leg is a guitar, a voice and a captured program with no
                # song, so the singer's switch takes only the microphone out of
                # what the cabinets play back to them; the send keeps it.
                if mic_part is not None \
                        and not voice_chat.own_voice_from_cabinets() \
                        and not voice_chat.cabinets_render_for_recording(
                            self.bot._find_gameplay(), self.bot):
                    owner_mix = voice_chat.mix_audio_frames(
                        [part for part in mix if part is not mic_part],
                        state_key=voice_chat.audio_limiter_key(
                            "own-ear-live", self),
                    )
                    if owner_mix:
                        owner_local_pcm = bytes(owner_mix)
                        if current_volume_scale != 1.0:
                            owner_local_pcm = audioop.mul(
                                owner_local_pcm, 2, current_volume_scale)
                    else:
                        # Nothing but the voice was on this leg, so silence is
                        # the whole of what is left for the owner's ear.
                        owner_local_pcm = bytes(len(mono_data))

                party_private = bool(getattr(self.bot, 'party_sync_force_upload', False))
                # The live leg was chosen at the top of the loop (it also
                # decides the frame size); a Server that has not advertised
                # channel 25 keeps the proven channel-22 leg.
                if live_leg:
                    target_channel = consts.CHANNEL_MUSICBOT_LIVE
                else:
                    target_channel = (
                        consts.CHANNEL_MUSICBOT
                        if (not self.bot.broadcast_to_megaphone or party_private)
                        else consts.CHANNEL_MEGAPHONE
                    )

                # Local zero-latency PA monitoring (the server no longer echoes
                # the megaphone broadcast back to the sender). Skipped while a
                # Party Sync session is active (private-to-guests leg).
                if self.bot.broadcast_to_megaphone and not party_private:
                    try:
                        gp = self.bot._find_gameplay()
                        if gp is not None:
                            if hasattr(voice_chat, '_feed_local_megaphone_direct'):
                                local_pcm = (owner_local_pcm
                                             if owner_local_pcm is not None
                                             else bytes(mono_data))
                                voice_chat._feed_local_megaphone_direct(gp, local_pcm, producer='music')
                    except Exception:
                        pass

                # Pace at the frame's own length: 10 ms on a live jam, 20 ms
                # everywhere else. Pacing at 20 ms would halve the jam's rate
                # and turn two 10 ms frames into an audible 20 ms hold.
                pace = 0.010 if live_10 else 0.020
                trace_mix_ms = ((time.perf_counter() - trace_mix_start) * 1000.0
                                if trace_on else 0.0)
                self._pace(pace)
                trace_late_ms = ((time.perf_counter() - self.last_send_time) * 1000.0
                                 if trace_on else 0.0)
                trace_encode_start = time.perf_counter() if trace_on else 0.0

                if stereo_data:
                    encoded = self.encoder_stereo.encode(bytearray(stereo_data))
                else:
                    encoded = self.encoder.encode(bytearray(mono_data))
                trace_encode_ms = ((time.perf_counter() - trace_encode_start) * 1000.0
                                   if trace_on else 0.0)
                # The PA's sequenced upload (channel 31), the live counterpart
                # of the music bot's: position on the frame, so a listener
                # conceals a hole rather than holding the legacy reserve.
                target_channel, encoded = sequence_pa_frame(
                    self, target_channel, encoded)
                if self.game and self.game.network:
                    trace_send_start = time.perf_counter() if trace_on else 0.0
                    self.game.network.send(target_channel, "n/a", encoded,
                                           reliable=False)
                    if trace_on:
                        trace_send_ms = (time.perf_counter() - trace_send_start) * 1000.0
                        gap_ms = audio_trace.gap_ms(trace_send_start, trace_prev_send)
                        trace_prev_send = trace_send_start
                        if live_10:
                            source = "external_live"
                        elif external_frames:
                            source = "external_song"
                        elif guitar_data is not None or mic_data is not None:
                            source = "instrument"
                        else:
                            source = "none"
                        audio_trace.event(
                            "relay_send", seq=trace_seq, source=source,
                            live=bool(live_10), frame_bytes=frame_bytes,
                            channel=target_channel, stereo=bool(stereo_data),
                            q_live=q_live0, q_song=q_song0,
                            q_guitar=q_guitar0, q_mic=q_mic0,
                            gap_ms=gap_ms, late_ms=round(trace_late_ms, 2),
                            pace_ms=round(pace * 1000.0, 2),
                            mix_ms=round(trace_mix_ms, 2),
                            encode_ms=round(trace_encode_ms, 2),
                            send_ms=round(trace_send_ms, 2),
                            context=audio_trace.party_context(self.game)
                        )
                        trace_seq += 1
            except Exception as exc:
                audio_trace.event(
                    "relay_error", error=type(exc).__name__,
                    context=audio_trace.party_context(self.game)
                )
                time.sleep(0.010)
        audio_trace.event(
            "relay_stop", sent=trace_seq,
            elapsed_s=round(time.perf_counter() - trace_started_at, 2),
            context=audio_trace.party_context(self.game)
        )
        voice_chat.reset_audio_limiter(
            voice_chat.audio_limiter_key("live-relay-mix", self))
