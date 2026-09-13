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
from .layout import SLOT_ORDER, CinemaLayout, CinemaSpeakerSpec
from .plugin import (CinemaSpeakerHost, acquire_bank, acquire_renderer,
                     host_for, release_all, release_renderer, set_enabled)
from .profiles import (DEFAULT_PROFILE, PROFILES, CinemaProfile,
                       get_profile, profile_names)
from .router import CinemaRenderer

__all__ = [
    "SLOT_ORDER",
    "CinemaLayout",
    "CinemaProfile",
    "CinemaRenderer",
    "CinemaSpeakerBank",
    "CinemaSpeakerHost",
    "CinemaSpeakerSpec",
    "ChannelAnalyzer",
    "DEFAULT_PROFILE",
    "PROFILES",
    "acquire_bank",
    "acquire_renderer",
    "get_profile",
    "host_for",
    "mid_side",
    "mix_channels",
    "mix_samples",
    "profile_names",
    "to_samples",
    "release_all",
    "release_renderer",
    "set_enabled",
    "source_layout",
]
