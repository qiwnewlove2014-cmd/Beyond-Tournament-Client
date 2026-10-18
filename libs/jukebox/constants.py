"""The cabinet shapes shared by the player, the room and the menus.

CINEMA_PROFILES is the set of profile ids a cabinet's mode may name; _CINEMA_SILENT is
the one answer from _acquire_cinema that means "no output at all"; the occlusion tiers
are the 0/1/2 scale every playback site uses for a wall between the cabinet and a listener.

This module imports nothing from the package, so every other module here may read it
without a cycle.
"""

from ..audio.cinema import profile_names

# The profile ids a cabinet's mode may name, and the shape every jukebox
# caller can rely on (see plugin.CINEMA_AUTO / CINEMA_OFF).
CINEMA_PROFILES = frozenset(profile_names())

# The one answer from _acquire_cinema that means "no output at all": a cabinet
# in custom mode with no speaker placed for it is silent, not plain.
_CINEMA_SILENT = object()

# Shared wall occlusion tiers used by every playback site below.
# 0 = clear path · 1 = thin obstacle (a lone pillar tile): light lowpass ·
# 2 = thick wall (>= 3 tiles): full standard occlusion.
OCCLUSION_CLEAR, OCCLUSION_LIGHT, OCCLUSION_FULL = 0, 1, 2
