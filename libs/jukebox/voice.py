"""The cabinet's own voice: short lines spoken from where the cabinet stands.

A jukebox is a thing in the world, so a line it says is a *positional* sound:
full volume inside the song's own 8 m, gone at 40 m, in the reverb zone the
cabinet stands in, on the cabinet's own volume category. It comes out of the
**footprint the builder placed**, not out of the single tile the map calls the
cabinet's centre: a cabinet dragged four steps wide is heard along its whole
front, from the part of it nearest the listener, and its full volume reaches
that whole footprint plus the 8 m around it. Three lines ship in
``client/data/jukebox_voice/`` and the Server names them (``jukebox_voice``
packet, see ``server/libs/world_map/jukebox.ts``):

    power_on   the map's power came back
    power_off  the map's power went (the lever, or a round starting)
    searching  a song was accepted into this cabinet's queue

Each file must be a real Ogg Vorbis stream. The game's decoder is Vorbis only
(``libs/safe_vorbis.py``), so a WebM/Opus file renamed to ``.ogg`` loads as
silence -- ``client/tests/test_jukebox_voice.py`` pins the header for exactly
that reason.

Deliberately **not** part of the music transport: power going off stops every
jukebox source, so a "Power off" line played through them would be cut off
mid-word. The voice owns one SoundGroup per cabinet and the transport never
touches it.
"""

from ..movement import get_3d_distance

# What the Server's line name means, as a file under ``data/``.
LINES = {
    "power_on": "jukebox_voice/power_on.ogg",
    "power_off": "jukebox_voice/power_off.ogg",
    "searching": "jukebox_voice/searching.ogg",
}

# The cabinet's own slider, like the song coming out of the same box: a player
# who turns the cabinet down hears it speak quietly too.
CATEGORY = "jukebox"

# The reach is the jukebox's own (player.py PAIR_REFERENCE_DISTANCE /
# PAIR_MAX_DISTANCE): full inside 8 m, a smooth fade to true silence at 40 m.
# A voice from a cabinet nobody is standing next to is not a voice.
FADE_START = 8.0
FADE_END = 40.0

# The listener's own choice, like ``cinema_speech``: it is how *you* hear the
# cabinet and it takes nothing from anybody else. Saved, and on by default --
# the line in the cabinet menu (beside the cinema lock) is the switch.
OPTION_ENABLED = "jukebox_voice"
DEFAULT_ENABLED = True

# One labeled source per cabinet: a second line takes over from the first
# instead of playing on top of it (two voices out of one box at once is not a
# cabinet saying something, it is a bug).
SOURCE_ID = "cabinet_voice"

# Aux send 0 is the room's reverb -- the index the cabinet's own song uses.
REVERB_SEND = 0


def voice_enabled():
    """Whether this listener hears the cabinets speak."""
    try:
        from .. import options
        saved = options.get(OPTION_ENABLED)
        if saved is None:
            return DEFAULT_ENABLED
        return bool(saved)
    except Exception:
        # A missing settings back end must not silence a cabinet: the default
        # is what a player would have had anyway.
        return DEFAULT_ENABLED


def set_voice_enabled(enabled):
    """Record the listener's choice. The menu line and the audio share a key."""
    try:
        from .. import options
        options.set(OPTION_ENABLED, bool(enabled))
    except Exception:
        return False
    return True


def footprint_of(game, cabinet_id, fallback):
    """The box a cabinet occupies: ``(minx, maxx, miny, maxy, z)``.

    A cabinet is a placed *element*, and a builder may drag it wider than one
    tile. The element counts both ends of its bounds, so a one-tile cabinet is
    one tile across -- the shape every map had before any of this, and it must
    sound exactly as it did.

    ``fallback`` is the point the Server sent, used when this client has not
    parsed the element yet (a player who joined a second ago) or when the map
    has already lost it: it becomes a zero-size box, which is the old
    single-point behaviour.
    """
    try:
        x, y, z = (float(value) for value in fallback)
    except (TypeError, ValueError):
        return None
    try:
        zone = game.gameplay.map.get_jukebox(cabinet_id)
    except Exception:
        zone = None
    if zone is None:
        return (x, x, y, y, z)
    try:
        minx, maxx = sorted((float(zone.minx), float(zone.maxx)))
        miny, maxy = sorted((float(zone.miny), float(zone.maxy)))
        z = (float(zone.minz) + float(zone.maxz)) / 2.0
    except (AttributeError, TypeError, ValueError):
        return (x, x, y, y, z)
    centre_x, centre_y = (minx + maxx) / 2.0, (miny + maxy) / 2.0
    half_x, half_y = (maxx - minx + 1.0) / 2.0, (maxy - miny + 1.0) / 2.0
    return (centre_x - half_x, centre_x + half_x, centre_y - half_y, centre_y + half_y, z)


def centre_of(box):
    """The middle of a footprint, at its own height."""
    minx, maxx, miny, maxy, z = box
    return ((minx + maxx) / 2.0, (miny + maxy) / 2.0, z)


def nearest_point(box, listener):
    """Where on the cabinet a listener hears it from: the closest part of it.

    Outside the footprint that is the edge facing them, so walking along a wide
    cabinet moves the voice with them instead of leaving it pinned to the
    middle. A listener standing *on* the box (inside it) is answered by its
    centre, so the line never appears to come from inside their own head.

    One source, deliberately: identical audio from several points across a wide
    box arrives a few milliseconds apart and combs a mono voice into something
    hollow. A voice is not the song's stereo pair -- it is one thing speaking.
    """
    if box is None:
        return None
    if listener is None:
        return centre_of(box)
    try:
        lx, ly = float(listener[0]), float(listener[1])
    except (TypeError, ValueError, IndexError):
        return centre_of(box)
    minx, maxx, miny, maxy, z = box
    if minx <= lx <= maxx and miny <= ly <= maxy:
        return centre_of(box)
    return (min(maxx, max(minx, lx)), min(maxy, max(miny, ly)), z)


def volume_at(listener, box):
    """The cabinet's volume for a listener at ``listener``, 0-100.

    Measured to the footprint, not to its centre: a wide cabinet is as loud
    standing at its end as standing in front of the middle, and 0 means "do not
    play at all", which is the same answer the song gets.
    """
    if listener is None or box is None:
        return 100
    point = nearest_point(box, listener)
    try:
        distance = get_3d_distance(
            float(listener[0]), float(listener[1]), float(listener[2]),
            float(point[0]), float(point[1]), float(point[2]),
        )
    except (TypeError, ValueError, IndexError):
        return 100
    if distance <= FADE_START:
        return 100
    if distance >= FADE_END:
        return 0
    t = (distance - FADE_START) / (FADE_END - FADE_START)
    smooth = t * t * (3.0 - 2.0 * t)
    return max(0, min(100, int(round(100 * (1.0 - smooth)))))


class CabinetVoices:
    """One SoundGroup per cabinet on this client, anchored at that cabinet.

    Kept on the audio manager (``audio_mngr.jukebox_voice``) so a relogin or an
    audio rebuild takes the sources with it instead of leaving them on a dead
    map -- the same home ``cinema_speech`` has.
    """

    def __init__(self, game=None):
        self.game = game
        self.groups = {}

    def speak(self, cabinet_id, position, line):
        """Say one line from one cabinet. MAIN THREAD ONLY.

        Returns False when there is nothing to play: an unknown line, a
        cabinet the listener is too far from, or no audio at all.
        """
        path = LINES.get(str(line or "").strip().lower())
        if path is None or position is None:
            return False
        if not voice_enabled():
            return False
        audio = getattr(self.game, "audio_mngr", None)
        if audio is None:
            return False
        box = footprint_of(self.game, cabinet_id, position)
        if box is None:
            return False
        listener = getattr(audio, "position", None)
        volume = volume_at(listener, box)
        if volume <= 0:
            return False
        group = self._group(audio, cabinet_id, nearest_point(box, listener), centre_of(box))
        if group is None:
            return False
        sound = group.play(path, cat=CATEGORY, volume=volume, id=SOURCE_ID)
        if sound is None or getattr(sound, "source", None) is None:
            return False
        # The distance fade above already shaped this line; letting OpenAL
        # attenuate it again would apply the same ramp twice.
        try:
            sound.source.rolloff_factor = 0.0
        except Exception:
            pass
        return True

    def _group(self, audio, cabinet_id, point, anchor):
        """This cabinet's group, at the part of it this listener is nearest.

        ``anchor`` (the cabinet's own middle) is where the *room* is read from:
        a wide cabinet straddling two zones speaks with the reverb of the place
        it was placed, not of whichever edge you happen to stand beside.
        """
        key = str(cabinet_id)
        group = self.groups.get(key)
        if group is None:
            try:
                group = audio.create_soundgroup(radius=1.0)
            except Exception:
                return None
            self.groups[key] = group
        try:
            group.position = tuple(float(value) for value in point)
        except (TypeError, ValueError):
            return None
        # The room the cabinet stands in, exactly as its song gets it: a zone
        # with no reverb clears the send rather than keeping the last room's.
        self._send_reverb(audio, group, anchor)
        return group

    def _send_reverb(self, audio, group, position):
        slot = None
        try:
            zone = self.game.gameplay.map.get_reverb_at(*position)
            slot = getattr(zone, "reverb", None) if zone is not None else None
        except Exception:
            slot = None
        try:
            group.apply_effect(slot, REVERB_SEND)
        except Exception:
            pass

    def release(self):
        """Hand every cabinet back -- used when the map or the audio goes away."""
        for key in list(self.groups):
            group = self.groups.pop(key)
            try:
                group.destroy()
            except Exception:
                pass


def voices_for(game):
    """The client's cabinet voices, created on first use."""
    audio = getattr(game, "audio_mngr", None) if game is not None else None
    if audio is None:
        return None
    holder = getattr(audio, "jukebox_voice", None)
    if not isinstance(holder, CabinetVoices):
        holder = CabinetVoices(game)
        try:
            audio.jukebox_voice = holder
        except Exception:
            return None
    holder.game = game
    return holder


def speak(game, cabinet_id, position, line):
    """Say one cabinet line. MAIN THREAD ONLY (it creates OpenAL sources)."""
    holder = voices_for(game)
    if holder is None:
        return False
    return holder.speak(cabinet_id, position, line)


def release(game):
    """Drop every cabinet group -- a map change, a disconnect, a relogin."""
    if game is None:
        return
    holder = getattr(getattr(game, "audio_mngr", None), "jukebox_voice", None)
    if isinstance(holder, CabinetVoices):
        holder.release()
