"""The song path's network leg: queue, pacing, crossfade and the actual send.

One thread drains ``network_queue`` at a 20 ms ideal cadence, gates what may
leave (paused, a retired crossfade leg, another performer's PA slot), fills a
live-only frame when there is no song audio, and blends two legs while a
crossfade runs. ``send_to_network_actual`` does the per-frame work: broadcast
gate, mono downmix, live/external mix, Opus encode, timeline stamp and PA
sequencing.

Every function takes the stream and reads the fields it names, so
``AudioStreamer``'s methods stay one-line shims and the existing patch seams
(``_send_to_network_actual``, ``_mix_network_frames``,
``_claim_timeline_marker``) keep working. Pinned by
``client/tests/test_network_module.py``.
"""

import queue
import struct
import time

from .. import consts
from .. import voice_chat
from ..external_audio import downmix_to_mono
from ..party_sync import stereo_upload_eligible
from .live_relay import live_relay_has_input, live_relay_needed, sequence_pa_frame


def send_to_network(stream, data, timeline_epoch=None, timeline_seq=None):
    """Queue raw PCM chunk to be sent to the network by the sender thread."""
    item = (data, timeline_epoch, timeline_seq)
    try:
        stream.network_queue.put_nowait(item)
    except queue.Full:
        try:
            stream.network_queue.get_nowait()
            stream.network_queue.put_nowait(item)
        except queue.Empty:
            pass


def route_aligned_network_frame(stream, decoded_frame=None):
    """Advance the normal-broadcast delay line by one media frame.

    Normal Music Broadcast sends the oldest pre-buffered frame, matching the
    performer's OpenAL playhead; megaphone keeps sending the current decoded
    frame so the verified PA transport stays untouched.
    """
    if not stream.bot:
        return
    if decoded_frame is not None:
        stream._timeline_delay.append(bytes(decoded_frame))
    aligned = stream._timeline_delay.popleft() if stream._timeline_delay else None
    if getattr(stream.bot, 'broadcast_to_megaphone', False):
        if decoded_frame is not None:
            stream._send_to_network(decoded_frame)
        return
    if aligned is not None:
        epoch, seq = stream._claim_timeline_marker()
        stream._send_to_network(aligned, epoch, seq)


def begin_network_crossfade(stream, partner, seconds):
    """Blend this stream's network leg with `partner`'s for `seconds`.

    Each outgoing frame is mixed with the partner's (this stream fading out,
    the partner in) so broadcast/PA listeners hear the same crossfade the
    performer hears; when the window elapses this leg retires and the
    partner's takes over.
    """
    stream._network_mix = (partner, time.monotonic(), max(0.1, float(seconds)))


def mix_network_frames(own, partner, own_gain, partner_gain):
    """Convex blend of two stereo int16 PCM frames (gains sum to ~1).

    A convex combination can never exceed the loudest input sample, so the
    blend cannot clip; audioop clips to int16 anyway as a backstop.
    """
    try:
        import audioop
        own_gain = max(0.0, min(1.0, own_gain))
        partner_gain = max(0.0, min(1.0, partner_gain))
        if own is None:
            return audioop.mul(partner, 2, partner_gain)
        if partner is None:
            return audioop.mul(own, 2, own_gain)
        n = min(len(own), len(partner))
        out = audioop.add(
            audioop.mul(own[:n], 2, own_gain),
            audioop.mul(partner[:n], 2, partner_gain),
            2,
        )
        if len(partner) > n:
            out += audioop.mul(partner[n:], 2, partner_gain)
        return out
    except Exception:
        return own if own is not None else partner


def network_sender_loop(stream):
    """Paced network sending loop running in a separate thread.

    Decouples network scheduling sleeps from local OpenAL playback.
    """
    while stream.running:
        if getattr(stream, "network_muted", False):
            # Retired during a crossfade: drop everything (music, live
            # guitar/mic mix, PA monitor) so listeners never hear the old
            # song under the new one. Poll slowly to keep the thread alive
            # in case it is ever unmuted before being stopped.
            time.sleep(0.05)
            continue

        data = None
        timeline_epoch = timeline_seq = None
        mix = getattr(stream, "_network_mix", None)
        if mix is not None:
            partner, mix_started, mix_seconds = mix
            progress = (time.monotonic() - mix_started) / mix_seconds
            if progress >= 1.0:
                # The overlap is over: retire this network leg; the
                # incoming stream takes over the broadcast on its own now.
                stream._network_mix = None
                stream.network_muted = True
                continue
            try:
                item = stream.network_queue.get(timeout=0.1)
                if isinstance(item, tuple) and len(item) == 3:
                    data = item[0]
                else:
                    data = item
            except queue.Empty:
                data = None
            partner_data = None
            partner_queue = getattr(partner, "network_queue", None)
            if partner_queue is not None:
                try:
                    partner_item = partner_queue.get_nowait()
                    if isinstance(partner_item, tuple) and len(partner_item) == 3:
                        partner_data = partner_item[0]
                    else:
                        partner_data = partner_item
                except Exception:
                    partner_data = None
            if data is not None or partner_data is not None:
                data = stream._mix_network_frames(
                    data, partner_data, 1.0 - progress, progress)
                # The outgoing track still owns the room timeline until
                # the hand-over; the blend keeps its markers.
                timeline_epoch, timeline_seq = stream._claim_timeline_marker()
        else:
            try:
                # Wait for a packet, with timeout so we check running regularly
                item = stream.network_queue.get(timeout=0.1)
                if isinstance(item, tuple) and len(item) == 3:
                    data, timeline_epoch, timeline_seq = item
                else:
                    data, timeline_epoch, timeline_seq = item, None, None
            except queue.Empty:
                data = None
                timeline_epoch = timeline_seq = None

        # Do not leak pre-pause PCM into the next broadcast segment unless live input is present.
        if stream.paused:
            data = None

        # Single music slot: when "Broadcast to Megaphone" is ON but another
        # performer holds the music-bot PA slot, keep the MP3 private - only
        # this performer's live instruments (guitar/mic) continue into the
        # PA mix, so two people's music never overlaps on the speakers.
        if (data is not None and stream.bot
                and getattr(stream.bot, 'broadcast_to_megaphone', False)
                and not stream.bot._is_music_owner()):
            data = None

        if data is None:
            # Keep the live mix flowing on every leg that carries it -- a
            # public broadcast, the megaphone, a Party Sync session or a
            # cinema room routed without the Broadcast switch.
            if stream.bot and live_relay_needed(stream.bot):
                if live_relay_has_input(stream.bot):
                    data = b'\x00' * 3840
                    if not getattr(stream.bot, 'broadcast_to_megaphone', False):
                        timeline_epoch, timeline_seq = stream._claim_timeline_marker()

        if data is None:
            continue

        # High-resolution, deadline-based pacing. Advancing the ideal deadline by exactly 20 ms prevents
        # normal scheduler overshoot (typically 0.2-0.8 ms/frame on Windows) from accumulating into a
        # steadily growing network queue; a large stall starts a fresh cadence rather than bursting.
        now = time.perf_counter()
        target_interval = 0.020  # 20ms per buffer
        if stream.last_send_time is None or now - stream.last_send_time > 0.100:
            deadline = now
        else:
            deadline = stream.last_send_time + target_interval
            if now < deadline:
                # Sleep most of the way (subtracting 1ms margin for Windows scheduler inaccuracy)
                sleep_time = deadline - now
                if sleep_time > 0.001:
                    time.sleep(sleep_time - 0.001)
                # Spin lock for the remaining fraction of a millisecond
                while time.perf_counter() < deadline:
                    pass
            elif now - deadline > 0.040:
                # Do not emit a catch-up burst after a suspended/stalled worker.
                deadline = now
        # Keep the IDEAL deadline, not the overshot wall-clock time, so small
        # scheduling errors are corrected by the next interval instead of
        # becoming permanent drift.
        stream.last_send_time = deadline

        stream._send_to_network_actual(data, timeline_epoch, timeline_seq)


def send_to_network_actual(stream, data, timeline_epoch=None, timeline_seq=None):
    """Downmix stereo to mono, scale volume, encode as Opus, and send to network."""
    mix_limiter_key = voice_chat.audio_limiter_key("musicbot-mix", stream)
    try:
        if not stream.game or not stream.game.network:
            return

        # Broadcast? The bot broadcast is on, OR the performer enabled "Broadcast to Megaphone" (an
        # independent toggle, exactly like piano/drums), OR the player hosts an active Party Sync session
        # (private upload). A stream WITHOUT a bot (jukebox playback) NEVER sends, or the jukebox audio
        # would be re-broadcast to the whole map as this player's own music bot stream (double audio).
        if not stream.bot or not (
            stream.bot.broadcast_enabled
            or stream.bot.broadcast_to_megaphone
            or getattr(stream.bot, "party_sync_force_upload", False)
            # A song routed into a cabinet's room is uploaded whatever the Broadcast switch says: the room is
            # a place other people stand in and the frames are the only way it reaches them (see
            # libs/audio/cinema/peer.py). The switch itself is untouched and comes back when routing goes off.
            or getattr(stream.bot, "cinema_force_upload", False)
        ):
            return

        # Downmix 16-bit stereo -> 16-bit mono
        import audioop
        mono_data = audioop.tomono(data, 2, 0.5, 0.5)

        # Scale the PCM stream volume according to stream.volume and the dynamic ducking multiplier before network broadcast
        current_volume_scale = (stream.volume / 100.0)
        if stream.bot:
            current_volume_scale *= getattr(stream.bot, 'duck_multiplier', 1.0)

        if current_volume_scale != 1.0:
            try:
                mono_data = audioop.mul(mono_data, 2, current_volume_scale)
            except Exception:
                pass

        target_channel = consts.CHANNEL_MUSICBOT
        # A Party Sync session shares the bot privately with its guests:
        # always use the private MUSICBOT leg (never the PA megaphone),
        # regardless of the user's megaphone toggle, while the session is
        # active. The server relays that leg only to session guests.
        if (stream.bot and stream.bot.broadcast_to_megaphone
                and not getattr(stream.bot, "party_sync_force_upload", False)):
            target_channel = consts.CHANNEL_MEGAPHONE

        # Mix queued live input (voice mic and/or line-in guitar) into the
        # outgoing stream. This happens on BOTH broadcast paths (the 3D
        # music bot channel and the megaphone), so a guitar strum is heard
        # either way - always gated by broadcast_enabled above.
        mic_data = None
        guitar_data = None
        if stream.bot:
            try:
                if getattr(stream.bot, 'mic_pcm_queue', None) and stream.bot.mic_pcm_queue:
                    mic_data = stream.bot.mic_pcm_queue.popleft()
            except Exception:
                pass
            try:
                if getattr(stream.bot, 'guitar_pcm_queue', None) and stream.bot.guitar_pcm_queue:
                    guitar_data = stream.bot.guitar_pcm_queue.popleft()
            except Exception:
                pass
        # A captured program (a DAW, a player, the whole computer) rides
        # the same legs as the line-in but keeps its two sides: its own
        # queue, so the stereo branch can place each side.
        external_frames = []
        if stream.bot:
            try:
                external_queue = getattr(stream.bot, 'external_pcm_queue', None)
                while external_queue and len(external_frames) < 2:
                    external_frames.append(external_queue.popleft())
            except Exception:
                external_frames = []
        # The live input is kept as its own list as well as being mixed into
        # mono_data: the stereo branch needs to place it in the centre
        # without downmixing the song with it (see _mix_live_into_stereo).
        live_frames = []
        # What the cabinets play into the owner's own ears, when it is not
        # simply `mono_data`: a singer who silenced their own voice from the
        # cabinets hears the same mix without their microphone
        # (`voice_chat.own_voice_from_cabinets`). The send is untouched.
        owner_local_pcm = None
        if (mic_data is not None or guitar_data is not None
                or external_frames):
            try:
                def _align(frame):
                    if len(frame) > len(mono_data):
                        return frame[:len(mono_data)]
                    if len(frame) < len(mono_data):
                        return frame + b'\x00' * (len(mono_data) - len(frame))
                    return frame

                mic_part = None
                if mic_data is not None:
                    mic_part = (_align(mic_data), 0.85)
                    live_frames.append(mic_part)
                if guitar_data is not None:
                    live_frames.append((_align(guitar_data), 0.85))
                # The mono legs (public broadcast, PA) have one channel by
                # design, so a stereo program is averaged into them here.
                mono_live_frames = list(live_frames)
                # Party Sync -> cinema: the host's own guitar/mic is not
                # in the room yet while a song is playing (the live-only
                # relay is parked then), so tap the centre instruments
                # here too. External audio is NOT included: its source
                # already hands every 10 ms frame to the room mixer.
                mixer = getattr(stream.bot, 'party_room_mixer', None)
                if mixer is not None and live_frames:
                    host_mix = voice_chat.mix_audio_frames(
                        list(live_frames),
                        state_key=voice_chat.audio_limiter_key(
                            "party-room-host-song", stream),
                    )
                    if host_mix:
                        mixer.push("host-instrument", host_mix, stereo=False)
                for frame in external_frames:
                    mono_ext = downmix_to_mono(frame)
                    if mono_ext:
                        mono_live_frames.append((_align(mono_ext), 0.85))
                # Sum music and live inputs in wide precision, then apply
                # one limiter. audioop.add saturates at each intermediate
                # addition, so limiting its result cannot repair clipping.
                parts = [(mono_data, 0.75)] + mono_live_frames
                mixed = voice_chat.mix_audio_frames(
                    parts,
                    state_key=mix_limiter_key,
                )
                if mixed:
                    # The owner's own ear: the same mix minus their voice,
                    # built only when they have turned that copy off. The
                    # room still gets every part of it.
                    if mic_part is not None \
                            and not voice_chat.own_voice_from_cabinets() \
                            and not voice_chat.cabinets_render_for_recording(
                                stream.bot._find_gameplay(), stream.bot):
                        owner_mix = voice_chat.mix_audio_frames(
                            [part for part in parts if part is not mic_part],
                            state_key=voice_chat.audio_limiter_key(
                                "own-ear-mix", stream),
                        )
                        if owner_mix:
                            owner_local_pcm = bytes(owner_mix)
                    mono_data = mixed
            except Exception:
                live_frames = []

        # When routing through the megaphone, feed the mixed stream into the local PA sidechain (main
        # thread) so the broadcaster hears their own music/instruments with zero latency - the server no
        # longer echoes the broadcast back to the sender. Skipped while a Party Sync session is active.
        if (stream.bot and stream.bot.broadcast_to_megaphone
                and not getattr(stream.bot, "party_sync_force_upload", False)):
            try:
                gp = stream.bot._find_gameplay()
                if gp is not None:
                    if hasattr(voice_chat, '_feed_local_megaphone_direct'):
                        local_pcm = (owner_local_pcm
                                     if owner_local_pcm is not None
                                     else bytes(mono_data))
                        voice_chat._feed_local_megaphone_direct(
                            gp, local_pcm, producer='music'
                        )
            except Exception:
                pass

        # Party Sync private leg carries TRUE STEREO so guests hear real
        # left/right separation instead of the mono downmix; public
        # broadcasts and the PA megaphone keep the proven mono path.
        # Live-input (mic/guitar) mixes stay mono (built as one channel).
        # A live input no longer turns this off: the solo is one source and
        # goes in the centre of the pair, so the song keeps its image for
        # everyone in the room while somebody plays over it (see
        # party_sync.stereo_upload_eligible and _mix_live_into_stereo).
        use_stereo = stereo_upload_eligible(
            stream.bot,
            getattr(stream, "channels", 2),
        )
        if use_stereo:
            # A streamer built without a bot never reaches here (the gate above needs one),
            # and that is also the only streamer with no stereo encoder: ask for it so an
            # object missing one -- a hand-built streamer, an older one -- still uploads
            # through its mono encoder instead of going silent on this branch.
            enc = getattr(stream, "encoder_stereo", None) or stream.encoder
            payload = bytes(data) if data is not None else b""
            if current_volume_scale != 1.0 and payload:
                try:
                    payload = audioop.mul(payload, 2, current_volume_scale)
                except Exception:
                    pass
            if payload and live_frames:
                payload = stream._mix_live_into_stereo(
                    payload, live_frames, mix_limiter_key)
            if payload and external_frames:
                payload = stream._mix_external_into_stereo(
                    payload, external_frames, mix_limiter_key)
        else:
            enc = stream.encoder
            payload = mono_data
        if enc is None or not payload:
            return
        encoded = enc.encode(bytearray(payload))
        if (target_channel == consts.CHANNEL_MUSICBOT
                and getattr(stream.game, 'music_timeline_supported', False)
                and timeline_epoch is not None and timeline_seq is not None):
            target_channel = consts.CHANNEL_MUSICBOT_TIMELINE
            encoded = struct.pack(
                ">BII", 1, int(timeline_epoch) & 0xFFFFFFFF,
                int(timeline_seq) & 0xFFFFFFFF,
            ) + bytes(encoded)
            with stream._timeline_lock:
                stream._timeline_last_sent_seq = int(timeline_seq) & 0xFFFFFFFF

        # The PA's SEQUENCED upload (channel 31): the same broadcast with
        # the frame's position in front of it, so a listener conceals a
        # lost frame (Opus PLC) instead of holding the 120 ms reserve the
        # legacy payload needs (tools/megaphone_latency_sim.py measured
        # both). A Server that has not advertised the channel keeps 30.
        target_channel, encoded = sequence_pa_frame(
            stream, target_channel, encoded)

        stream.game.network.send(target_channel, "n/a", encoded, reliable=False)
    except Exception:
        pass
