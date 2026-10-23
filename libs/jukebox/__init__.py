"""Music Jukebox - a map element that plays a shared song queue.

The queue lives on the SERVER (per map): jukebox_play / jukebox_stop / jukebox_queue_update
/ jukebox_state, and one server-owned live Opus stream per active cabinet. Decoding,
distance attenuation, reverb and local volume stay on the client, and an unavailable server
worker falls back to the direct ffmpeg path the Music Bot uses.

The package is split by what a reader is looking for:

    player.py       the cabinet's ears - JukeboxPlayer: sources, gain, EQ, the cinema
                    hand-off, the direct decode and the relay receiver's lifecycle
    recovery.py     the watchdog - update() and the relay recovery ladder (resync,
                    warm-up un-stick, rebuild, per-listener direct fallback)
    routes.py       the route bookkeeping - the frames that arrive before play()
                    does, and what a relay_pending offer means
    view.py         what the panel knows - readers over gp state (volume, repeat,
                    position, the nearest cabinet) and the one place a player is created
    cinema.py       what a room is called and how a builder chooses it - the spoken
                    sentences about a room's bands and silent speakers, and the mode /
                    reach / lock menus
    voice.py        the cabinet's own voice - the lines it says out loud (power on,
                    power off, a song being chosen), played from where it stands
    menu.py         the cabinet's own menu - play, queue, search, URL, skip, repeat,
                    and the Party Sync and My Song lines hanging off it
    widgets.py      the controls a menu opens - the EQ and volume sliders, the scrub line
    constants.py    the profile ids, the silent answer and the occlusion tiers
    relay.py        the server-owned Opus relay receiver (own thread, main-thread output)
    media_cache.py  already-playable media credentials, owned by one player

The door below re-exports the four names a caller outside the package uses, so
`from libs.jukebox import JukeboxPlayer` keeps working (gameplay, the Music Bot
controller, the cinema peer, the simulators and the tests). It re-exports nothing else on
purpose: a name with no caller outside the package belongs to its module. A test that
patches one of these names patches the module that owns it
(`libs.jukebox.recovery.speak`, `libs.jukebox.menu.open_jukebox_menu`, ...): a patched alias
on this package would not reach the code that reads it.

The rules, the watchdogs and every measured timeout: .agents/skills/jukebox-system/.
"""

from .menu import open_jukebox_menu, open_jukebox_switch_menu
from .player import JukeboxPlayer, wall_occlusion_tier
from .relay import JukeboxRelayReceiver

__all__ = [
    "JukeboxPlayer",
    "JukeboxRelayReceiver",
    "open_jukebox_menu",
    "open_jukebox_switch_menu",
    "wall_occlusion_tier",
]
