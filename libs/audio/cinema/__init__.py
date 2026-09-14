"""Cinema Speaker System — a room-filling speaker layer on top of the Jukebox.

Architecture
------------
The Jukebox is the **source**: it owns the queue, the transports (server
relay / direct ffmpeg), the shared timeline alignment, the recovery watches
and the mixer category. None of that changes here, and this package never
decodes, times, or recovers anything.

This package is the **processing layer** between one source and the room:

    Jukebox (source)
        |
        v   stereo PCM frames (s16le, 48 kHz), the only thing exchanged
    CinemaSpeakerHost          -- one renderer per playing jukebox
        |
        v
    ChannelAnalyzer            -- mono vs stereo, from the samples
        |
        v
    CinemaProfile / CinemaLayout -- Mid/Side weights per speaker slot
        |
        v
    CinemaRenderer.render()    -- one MONO feed per speaker, clamped
        |
        v
    Cinema speakers placed around the room (map elements)

Why Mid/Side instead of summing every speaker to mono: L = M + S and
R = M - S, so the front pair reconstructs the original stereo exactly while
the side/rear slots carry the *difference*, which is what makes a room feel
wide instead of hollow. Giving two speakers identical content is what
produces comb filtering, so no two slots in a profile share weights.

Where the speakers *are* is a separate problem from what they play, because
map data is hand-placed and frequently wrong. ``placement.py`` measures
every speaker against the room's geometry and resolves mislabelled, rotated,
duplicated or unpaired speakers into a working room (or refuses the room
entirely, which falls back to the plain jukebox pair), and ``listener.py``
answers the other half of the question -- what the player is facing, and
whether an aimed speaker is pointing at them.

The renderer is deliberately pure PCM: it never touches OpenAL. Every
OpenAL call in the client stays on the audio owner thread (see
``AudioManager.loop`` and the audio inbox), so source creation, buffer
upload and queueing stay with the transport that already owns them. This
layer only decides *what each speaker should sound like*.

Everything here is inert until :func:`acquire_renderer` is called with
cinema enabled; a map with no cinema speakers therefore behaves exactly
like the plain two-source jukebox it always was.
"""

from .bank import CinemaSpeakerBank
from .channel import (ChannelAnalyzer, mid_side, mix_channels, mix_samples,
                     source_layout, to_samples)
from .layout import (IDEAL_BEARING, ROOM_MAX_DISTANCE, ROOM_RADIUS,
                     ROOM_REFERENCE_DISTANCE, SLOT_ORDER, CinemaLayout,
                     CinemaSpeakerSpec, coerce_spec, slot_for_bearing)
from .listener import ListenerPose, cone_gain, facing_report
from .placement import (AUTO_PROFILE, PlacedSpeaker, RoomPlacement, RoomPlan,
                        auto_profile, exclusive_speakers, nearest_anchor,
                        resolve_room, room_profile)
from .live import (LiveRoomRouter, cabinet_mode, live_instruments_enabled,
                   route_to_room, router_for, set_live_instruments,
                   wall_filter)
from .plugin import (CINEMA_AUTO, CINEMA_OFF, CinemaSpeakerHost, acquire_bank,
                     acquire_renderer, cabinet_anchor, cinema_room, host_for,
                     map_cabinet_anchors, map_speakers, preview_room, release_all,
                     release_renderer, room_diagnosis, rooms_enabled, set_enabled,
                     set_rooms_enabled)
from .profiles import (DEFAULT_PROFILE, PROFILES, CinemaProfile,
                       get_profile, profile_names)
from .router import CinemaRenderer

__all__ = [
    "SLOT_ORDER",
    "ROOM_RADIUS",
    "ROOM_REFERENCE_DISTANCE",
    "ROOM_MAX_DISTANCE",
    "AUTO_PROFILE",
    "LiveRoomRouter",
    "route_to_room",
    "router_for",
    "wall_filter",
    "live_instruments_enabled",
    "set_live_instruments",
    "CinemaLayout",
    "CinemaProfile",
    "CinemaRenderer",
    "CinemaSpeakerBank",
    "CinemaSpeakerHost",
    "CinemaSpeakerSpec",
    "IDEAL_BEARING",
    "ListenerPose",
    "PlacedSpeaker",
    "RoomPlacement",
    "RoomPlan",
    "CINEMA_AUTO",
    "CINEMA_OFF",
    "ChannelAnalyzer",
    "DEFAULT_PROFILE",
    "PROFILES",
    "acquire_bank",
    "acquire_renderer",
    "auto_profile",
    "cabinet_anchor",
    "cinema_room",
    "coerce_spec",
    "cone_gain",
    "exclusive_speakers",
    "nearest_anchor",
    "facing_report",
    "get_profile",
    "host_for",
    "map_cabinet_anchors",
    "map_speakers",
    "mid_side",
    "preview_room",
    "resolve_room",
    "rooms_enabled",
    "set_rooms_enabled",
    "room_diagnosis",
    "room_profile",
    "slot_for_bearing",
    "mix_channels",
    "mix_samples",
    "profile_names",
    "to_samples",
    "release_all",
    "release_renderer",
    "set_enabled",
    "source_layout",
]
