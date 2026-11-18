"""Spatial output shaping: distance fade, wall occlusion and stereo mixes.

This is the part of a stream that decides how loud a cabinet is at the
listener's position and what a stereo frame sounds like once a live source or
a captured program is added. It takes the stream and reads the fields it
names, so the methods on ``AudioStreamer`` stay one-line shims and the existing
patch seams (``_update_spatial_gain``, ``_mix_live_into_stereo``) keep working.

A cinema room owns its own per-speaker shaping: ``update_spatial_gain`` hands
the whole update to ``cinema.update_output()`` and never touches the plain
pair's gains.
"""

import contextlib

from .. import voice_chat
from .output import split_stereo_16


def set_cabinet_volume(stream, volume):
    """Set the cabinet's own level and re-apply the spatial fade."""
    stream.cabinet_gain = max(0.0, min(1.0, float(volume) / 100.0))
    if stream.cinema is not None:
        stream.cinema.set_cabinet_volume(volume)
    stream._update_spatial_gain()


def update_spatial_gain(stream):
    """Linear distance fade for spatial pairs (same behavior as drums' 3D
    stereo): full volume inside the reference distance, linearly down to
    true silence at max_distance, computed from the listener's position."""
    if stream.cinema is not None:
        # Per-speaker distance, occlusion and environment for the room.
        stream.cinema.update_output()
        return
    try:
        audio = getattr(stream.game, "audio_mngr", None)
        pos = getattr(audio, "position", None)
        if pos is None:
            return
        span = max(0.0001, stream.spatial_max - stream.spatial_ref)
        audible = False
        for src in (stream.spatial_src_l, stream.spatial_src_r):
            p = src.position
            dx = pos[0] - p[0]
            dy = pos[1] - p[1]
            dz = pos[2] - p[2]
            dist = (dx * dx + dy * dy + dz * dz) ** 0.5
            if dist <= stream.spatial_ref:
                g = 1.0
            elif dist >= stream.spatial_max:
                g = 0.0
            else:
                g = 1.0 - (dist - stream.spatial_ref) / span
            audible = audible or g > 0.0
            # Jukebox songs use their own mixer category ("jukebox"), so
            # lowering the music-bot/map-music slider does not silence them.
            music_gain = audio.volume_categories.get("jukebox", [100])[0] / 100.0
            src.gain = (stream.spatial_base_gain * music_gain
                        * getattr(stream, "cabinet_gain", 1.0) * g)

        if not audible:
            return  # Keep timeline playing, but do not raycast silent cabinets.
        # Wall occlusion check for spatial pair (Jukebox direct mode):
        if stream.spatial_src_l is not None and stream.spatial_src_r is not None:
            box_pos = (
                (stream.spatial_src_l.position[0] + stream.spatial_src_r.position[0]) / 2.0,
                stream.spatial_src_l.position[1],
                stream.spatial_src_l.position[2],
            )
            gp = getattr(stream.game, "gameplay", None)
            cur_map = getattr(gp, "map", None) if gp is not None else None
            if cur_map is not None and hasattr(cur_map, "valid_straight_path"):
                # Wall-thickness tiers (same 0/1/2 scale as jukebox/player.py):
                # a lone pillar tile only slightly dulls the song, while
                # thick walls keep the full standard muffle.
                tier = 0
                tfn = getattr(cur_map, "occlusion_tier", None)
                jukebox_player = getattr(stream, "jukebox_player", None)
                if jukebox_player is not None:
                    tier = jukebox_player.occlusion_tier(box_pos, pos, stream.spatial_max)
                elif tfn is not None:
                    with contextlib.suppress(Exception):
                        tier = int(tfn(box_pos, pos))
                else:
                    with contextlib.suppress(Exception):
                        tier = 2 if cur_map.valid_straight_path(box_pos, pos) is False else 0
                if tier != getattr(stream, "_last_occluded", None):
                    stream._last_occluded = tier
                    filt = None
                    if tier >= 2 and hasattr(audio, "gen_filter"):
                        if not hasattr(stream, "_occlusion_filter") or stream._occlusion_filter is None:
                            stream._occlusion_filter = audio.gen_filter(
                                "LOWPASS", ("GAINHF", 0.05), ("GAIN", 0.22))
                        filt = stream._occlusion_filter
                    elif tier == 1 and hasattr(audio, "gen_filter"):
                        if not hasattr(stream, "_light_occlusion_filter") or stream._light_occlusion_filter is None:
                            stream._light_occlusion_filter = audio.gen_filter(
                                "LOWPASS", ("GAINHF", 0.45), ("GAIN", 0.75))
                        filt = stream._light_occlusion_filter
                    for s in (stream.spatial_src_l, stream.spatial_src_r):
                        try:
                            if filt is not None:
                                s.direct_filter = filt
                            else:
                                # A wall clearing must not strip an active
                                # global filter (the underwater muffle):
                                # restore it instead of deleting it.
                                active = getattr(audio, "filter", None)
                                if active and active[-1] is not None:
                                    s.direct_filter = active[-1]
                                else:
                                    with contextlib.suppress(Exception):
                                        del s.direct_filter
                            if getattr(audio, "efx", None) is not None:
                                if hasattr(stream, "reverb_slot") and stream.reverb_slot is not None:
                                    audio.efx.send(s, 0, stream.reverb_slot, filter=filt)
                                if hasattr(stream, "eq_slot") and stream.eq_slot is not None:
                                    audio.efx.send(s, 1, stream.eq_slot, filter=filt)
                        except Exception:
                            pass
    except Exception:
        pass


def mix_live_into_stereo(payload, live_frames, state_key):
    """Add the mono mic/line-in mix to BOTH sides of a stereo frame.

    A mic or a guitar is one source with no sides of its own, so it belongs
    in the centre -- mixed into the left and the right alike -- while the
    song keeps the two sides it was uploaded with. Downmixing the song to
    carry the solo (what this path did before) left a room with no side to
    spread, and every speaker of the cabinet played the same thing for as
    long as somebody played: the report was that a solo arrives from the
    whole cabinet at once. The music keeps its own 0.75 weight and each
    live source its 0.85, exactly as the mono mix balances them, and each
    side is limited on its own so a loud solo cannot pull the image down.
    """
    if not payload or not live_frames:
        return bytes(payload) if payload else b""
    try:
        import audioop
        left, right = split_stereo_16(bytes(payload))
        sides = []
        for side, suffix in ((left, ":l"), (right, ":r")):
            mixed = voice_chat.mix_audio_frames(
                [(side, 0.75)] + list(live_frames),
                state_key=f"{state_key}{suffix}",
            )
            sides.append(mixed or side)
        return audioop.add(
            audioop.tostereo(sides[0], 2, 1.0, 0.0),
            audioop.tostereo(sides[1], 2, 0.0, 1.0),
            2,
        )
    except Exception:
        return bytes(payload)


def mix_external_into_stereo(payload, external_frames, state_key):
    """Add a captured program's own left and right to the pair.

    The line-in and mic path places one mono source in the centre on
    purpose -- a guitar has no sides. An external program does: a song, a
    stereo synth or a DAW mix was captured as two channels and the listener
    asked for them, so each side is summed with its own side of the capture
    while the song keeps its 0.75 weight per side. Nothing here decides
    that left equals right; a source set to MONO arrives with identical
    sides (``external_audio.shape_frame``) and lands in the centre by
    construction rather than by a second rule.
    """
    if not payload or not external_frames:
        return bytes(payload) if payload else b""
    try:
        import audioop
        # Sum the capture first, in its own image, so one noisy frame
        # cannot pull a side harder than the other.
        ext = voice_chat.mix_audio_frames(
            [(frame, 0.85) for frame in external_frames if frame],
            state_key=f"{state_key}:ext",
        )
        if not ext:
            return bytes(payload)
        payload = bytes(payload)
        ext = bytes(ext[:len(payload)])
        if len(ext) < len(payload):
            ext += b"\x00" * (len(payload) - len(ext))
        length = len(payload) // 4 * 4
        if length <= 0:
            return payload
        left, right = split_stereo_16(bytes(payload)[:length])
        ext_left, ext_right = split_stereo_16(bytes(ext)[:length])
        sides = []
        for side, ext_side, suffix in (
            (left, ext_left, ":l"),
            (right, ext_right, ":r"),
        ):
            mixed = voice_chat.mix_audio_frames(
                [(side, 0.75), (ext_side, 1.0)],
                state_key=f"{state_key}{suffix}",
            )
            sides.append(mixed or side)
        return audioop.add(
            audioop.tostereo(sides[0], 2, 1.0, 0.0),
            audioop.tostereo(sides[1], 2, 0.0, 1.0),
            2,
        )
    except Exception:
        return bytes(payload)
