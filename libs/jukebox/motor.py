"""The cabinet's machinery: started, run and stopped like a placed Sound Source.

A powered cabinet is a machine, not a point that shouts. Its clips are heard from
the box the builder placed, exactly the way a Sound Source element is heard: the
cabinet's own bounds are the region -- a cabinet dragged five steps wide is five
steps of sound, heard from the nearest part of that box rather than from one tile
in the middle -- and the level comes from the distance to the nearest point of the
box, height weighted 3.5x, through a smoothstep fade over 25 m (the engine's own
``SoundSource.fade_range``). There is no 8 m near shelf and no 40 m horizon here:
the numbers are the ones a placed sound already plays by.

Four optional clips, all Ogg Vorbis:

    client/data/jukebox_motor/start.ogg   the motor catching, once when it gets power
    client/data/jukebox_motor/loop.ogg    the rotation, looped while it has power
    client/data/jukebox_motor/stop.ogg    the wind-down, once, when the power goes
    client/data/ambience/room3.ogg        the fan, looped with the rotation

The fan rides with the rotation and dies with it: it starts when the cabinet gets
power, follows the same box, gain and room, and is gone when the power is. It is
not a separate thing to switch off.

The Server says when a cabinet's power changes (``jukebox_power``,
``{id, x, y, z, on}``, one packet per cabinet) and the map-join state carries a
``powered`` flag per cabinet, so a listener who joins a powered room hears the
machinery already running instead of a box that only spins up when the switch is
thrown next.

Power off -- the lever, a round starting -- stops the rotation and the fan there
and then: this is the one sound a dead cabinet must never be left making. The
wind-down is the machine's *last* sound, not a fade of the loop, so it is a fresh
source played after the loops are already dead -- the same trick the spoken
"Power off" line uses (``voice.py``), and the reason a cabinet can be heard
grinding to a stop with nothing humming underneath it. It is only said for a
cabinet that was actually running, and only where it can be heard: joining a map
that is already dark is silent, a map change throws the sources away without
pretending the machine was switched off, and a stop from beyond the fade range
says nothing.

The clips are optional on purpose: a cabinet with no machinery files is simply a
silent box, which is what every cabinet was before there was any. The lines the
cabinet *says* are a separate path (``voice.py``), and the listener's
``Cabinet voice`` switch never silences the machinery.
"""

# The cabinet's own slider: turning a cabinet down turns its motor down too.
CATEGORY = "jukebox"

FOLDER = "jukebox_motor"
START = f"{FOLDER}/start.ogg"
LOOP = f"{FOLDER}/loop.ogg"
STOP = f"{FOLDER}/stop.ogg"
# Recorded from the cabinet's own room (the owner's room3 fan): it runs with the
# rotation, at the level the owner placed it at, and there is no separate slider.
FAN = "ambience/room3.ogg"
FAN_VOLUME = 100

# A placed Sound Source's own numbers (``world_map.SoundSource``): audible inside
# 25 m of its box with a smoothstep fade, height counted 3.5x harder than ground
# distance, and a source whose distance model starts at 10 m. Copied, not
# invented, so a cabinet sounds like every other placed sound on the map.
FADE_RANGE = 25.0
Z_WEIGHT = 3.5
REFERENCE_DISTANCE = 10.0

# The engine ramps a source's gain by this much per frame instead of stepping it:
# a cabinet that pops every time the listener crosses a tile edge is a bug.
GAIN_STEP = 0.05

# Aux send 0 is the room's reverb -- the index the cabinet's song and its voice
# use, so everything out of one box stands in one room.
REVERB_SEND = 0


class CabinetMotors:
    """Every powered cabinet on this map, as far as this client is concerned.

    Kept on the audio manager (``audio_mngr.jukebox_motor``) beside the spoken
    lines, so a relogin or an audio rebuild takes the sources with it.
    """

    def __init__(self, game=None):
        self.game = game
        # cabinet id -> {"position", "loops": [(sound, volume)], "gain", "paused"}.
        # The box itself is re-read from the map each frame, so a hot-reloaded
        # element is heard where it now stands rather than where it stood then.
        self.motors = {}
        self.windups = {}     # cabinet id -> the wind-down still sounding

    def power(self, cabinet_id, on, position):
        """A cabinet has power (``on``) or has lost it. MAIN THREAD ONLY.

        Idempotent: the map-join state, a menu opening and a power packet all
        ask the same question, and a running motor is never restarted.
        """
        key = str(cabinet_id)
        if not on:
            return self.stop(key)
        if key in self.motors:
            return False
        audio = getattr(self.game, "audio_mngr", None)
        if audio is None:
            return False
        box = _region_of(self.game, cabinet_id, position)
        if box is None:
            return False
        listener = getattr(audio, "position", None)
        point = _nearest_point(box, listener)
        gain = self._gain(audio, box, listener)
        # A machine does not spin up while it is still grinding down: the box has
        # one throat, and the new spin-up takes it.
        self._end_windup(key)
        start = self._play(audio, START, point, False, 100)
        if start is not None:
            # The engine's level, not full volume: a spin-up heard from the far
            # edge of the fade is as quiet as the rotation under it.
            _set_gain(start, gain)
            self._send_reverb(audio, [start], point)
        loops = []
        # Both loops start silent and are mixed to the region level below: a loop
        # born at full volume would blip for a frame before its gain is set.
        for path, volume in ((LOOP, 100), (FAN, FAN_VOLUME)):
            sound = self._play(audio, path, point, True, 0)
            if sound is not None:
                loops.append((sound, volume))
        if start is None and not loops:
            # No machinery files yet (or they could not be loaded): a silent box,
            # and nothing held open for it.
            return False
        if loops:
            self._send_reverb(audio, [sound for sound, _volume in loops], point)
            self._apply_gain(loops, gain)
        self.motors[key] = {
            "position": _packet_point(position),
            "loops": loops,
            "gain": gain if loops else 0.0,
            "paused": False,
        }
        return True

    def tick(self, x, y, z):
        """Follow the listener: level, place and room, every frame. MAIN THREAD ONLY.

        The engine does this for a placed sound source in ``SoundSource.loop``;
        a cabinet's machinery has no map element of its own to do it for the
        cabinet, so the gameplay loop asks here instead.
        """
        if not self.motors:
            return
        audio = getattr(self.game, "audio_mngr", None)
        if audio is None:
            return
        listener = _point(x, y, z)
        for key, motor in list(self.motors.items()):
            box = _region_of(self.game, key, motor.get("position"))
            if box is None:
                continue
            target = self._gain(audio, box, listener)
            motor["gain"] = _approach(motor.get("gain", 0.0), target)
            loops = motor.get("loops")
            if not loops:
                continue
            point = _nearest_point(box, listener)
            if motor["gain"] <= 0.0 and target <= 0.0:
                # Out of earshot: stop spending a native source on silence, and
                # keep it loaded so walking back in is a play, not a reload.
                if not motor.get("paused"):
                    for sound, _volume in loops:
                        _pause(sound)
                    motor["paused"] = True
                continue
            if motor.get("paused"):
                for sound, _volume in loops:
                    _resume(sound)
                motor["paused"] = False
            self._apply_gain(loops, motor["gain"])
            for sound, _volume in loops:
                _place(sound, point)
            self._send_reverb(audio, [sound for sound, _volume in loops], point)

    def stop(self, cabinet_id):
        """Stop one cabinet's machinery dead and say the machine winding down.

        Returns whether anything was running. A cabinet that was not running --
        a box this client joined dark, or one already stopped -- says nothing: a
        wind-down with no machine in front of it is a sound out of nowhere.
        """
        key = str(cabinet_id)
        stopped, motor = self._halt(key)
        if not stopped:
            return False
        if motor is not None:
            self._wind_down(key, motor)
        return True

    def _halt(self, key):
        """Destroy a cabinet's running sources and forget it, saying nothing.

        Returns what ``stop`` needs to follow it with the wind-down: whether
        anything ran, and the state it ran in.
        """
        motor = self.motors.pop(key, None)
        if not motor:
            return False, None
        for sound, _volume in motor.get("loops", ()):
            _destroy(sound)
        return True, motor

    def _wind_down(self, key, motor):
        """The machine's last sound, from where the machine stood. MAIN THREAD ONLY.

        On its own source, not the loop's: the loop has just been destroyed, so a
        wind-down played through it would be cut off in the same instant.
        """
        self._end_windup(key)
        audio = getattr(self.game, "audio_mngr", None)
        if audio is None:
            return
        box = _region_of(self.game, key, motor.get("position"))
        if box is None:
            return
        listener = getattr(audio, "position", None)
        gain = self._gain(audio, box, listener)
        if gain <= 0.0:
            # Too far to have heard it running: a wind-down from nowhere is a
            # sound the listener has no reason to believe.
            return
        point = _nearest_point(box, listener)
        sound = self._play(audio, STOP, point, False, 100)
        if sound is None:
            return
        _set_gain(sound, gain)
        self._send_reverb(audio, [sound], point)
        self.windups[key] = sound

    def _end_windup(self, key):
        """Take back a wind-down still sounding -- a spin-up, a map change."""
        sound = self.windups.pop(key, None)
        if sound is not None:
            _destroy(sound)

    def release(self):
        """Stop every motor -- a map change, a disconnect, a relogin.

        Silent on purpose: nothing was switched off, the client is simply going
        somewhere else, and a room full of cabinets winding down over a map load
        is a lie about what happened.
        """
        for key in list(self.motors):
            self._halt(key)
        for key in list(self.windups):
            self._end_windup(key)

    def _play(self, audio, path, point, looping, volume):
        try:
            return audio.play_unbound(
                path, point[0], point[1], point[2],
                looping=looping,
                cat=CATEGORY,
                volume=volume,
                reference_distance=REFERENCE_DISTANCE,
            )
        except Exception:
            return None

    def _gain(self, audio, box, listener):
        """The level the engine would give a sound source of this box, 0-1.

        The category is the cabinet's own slider, like the song out of the same
        box; the fade is the engine's (smoothstep over ``FADE_RANGE``, height
        weighted ``Z_WEIGHT``), measured to the nearest part of the box.
        """
        if box is None:
            return 0.0
        category = 1.0
        try:
            category = float(audio.volume_categories.get(CATEGORY, [100])[0]) / 100.0
        except (AttributeError, TypeError, ValueError, IndexError):
            category = 1.0
        if listener is None:
            return max(0.0, min(1.0, category))
        distance = _distance(box, listener)
        t = max(0.0, min(1.0, 1.0 - (distance / FADE_RANGE)))
        fade = t * t * (3.0 - 2.0 * t)
        return max(0.0, min(1.0, category * fade))

    def _apply_gain(self, loops, gain):
        for sound, volume in loops:
            _set_gain(sound, gain * (volume / 100.0))

    def _send_reverb(self, audio, sounds, anchor):
        """The room the cabinet stands in, on every source of its machinery."""
        slot = None
        try:
            zone = self.game.gameplay.map.get_reverb_at(*anchor)
            slot = getattr(zone, "reverb", None) if zone is not None else None
        except Exception:
            slot = None
        if slot is None:
            return
        efx = getattr(audio, "efx", None)
        if efx is None:
            return
        for sound in sounds:
            source = getattr(sound, "source", None)
            if source is None:
                continue
            try:
                efx.send(source, REVERB_SEND, slot)
            except Exception:
                pass


def _region_of(game, cabinet_id, position):
    """The box a cabinet occupies, as a Sound Source box: min/max x, y, z.

    The element's own bounds are the region, expanded half a tile on each side so
    a cabinet placed on one tile is a whole tile of sound -- the same rule the
    voice uses. A client that has not parsed the element yet (a player who joined
    a second ago) falls back to the point the Server sent, which is a zero-size
    box and the old single-point behaviour.
    """
    try:
        point = _packet_point(position)
        if point is None:
            zone = game.gameplay.map.get_jukebox(cabinet_id)
            point = tuple(float(value) for value in zone.center)
        x, y, z = point
    except Exception:
        return None
    try:
        zone = game.gameplay.map.get_jukebox(cabinet_id)
    except Exception:
        zone = None
    if zone is None:
        return (x, x, y, y, z, z)
    try:
        minx, maxx = sorted((float(zone.minx), float(zone.maxx)))
        miny, maxy = sorted((float(zone.miny), float(zone.maxy)))
        minz, maxz = sorted((float(zone.minz), float(zone.maxz)))
    except (AttributeError, TypeError, ValueError):
        return (x, x, y, y, z, z)
    return (minx - 0.5, maxx + 0.5, miny - 0.5, maxy + 0.5, minz, maxz)


def _packet_point(position):
    try:
        x, y, z = (float(value) for value in position)
    except (TypeError, ValueError):
        return None
    return (x, y, z)


def _point(x, y, z):
    try:
        return (float(x), float(y), float(z))
    except (TypeError, ValueError):
        return None


def _distance(box, listener):
    minx, maxx, miny, maxy, minz, maxz = box
    x, y, z = listener
    dx = max(0.0, minx - x, x - maxx)
    dy = max(0.0, miny - y, y - maxy)
    dz = max(0.0, minz - z, z - maxz) * Z_WEIGHT
    return (dx * dx + dy * dy + dz * dz) ** 0.5


def _nearest_point(box, listener):
    """Where on the box a listener hears it from: the closest part of it.

    Outside the box that is the edge facing them, so walking along a wide cabinet
    moves its machinery with them instead of leaving it pinned to the middle. A
    listener standing *on* the box (inside it) is answered by its centre, so the
    machine never appears to be running inside their own head.
    """
    minx, maxx, miny, maxy, minz, maxz = box
    centre = ((minx + maxx) / 2.0, (miny + maxy) / 2.0, (minz + maxz) / 2.0)
    if listener is None:
        return centre
    x, y, z = listener
    if minx <= x <= maxx and miny <= y <= maxy and minz <= z <= maxz:
        return centre
    return (min(maxx, max(minx, x)), min(maxy, max(miny, y)), min(maxz, max(minz, z)))


def _approach(current, target):
    if current < target:
        return min(target, current + GAIN_STEP)
    if current > target:
        return max(target, current - GAIN_STEP)
    return current


def _set_gain(sound, gain):
    source = getattr(sound, "source", None)
    if source is None:
        return
    try:
        source.gain = max(0.0, min(1.0, gain))
    except Exception:
        pass


def _place(sound, point):
    source = getattr(sound, "source", None)
    if source is None:
        return
    try:
        source.position = point
    except Exception:
        pass


def _pause(sound):
    source = getattr(sound, "source", None)
    if source is None:
        return
    try:
        source.pause()
    except Exception:
        pass


def _resume(sound):
    source = getattr(sound, "source", None)
    if source is None:
        return
    try:
        source.play()
    except Exception:
        pass


def _destroy(sound):
    try:
        sound.destroy()
    except Exception:
        pass


def motors_for(game):
    """This client's cabinet machinery, created on first use."""
    audio = getattr(game, "audio_mngr", None) if game is not None else None
    if audio is None:
        return None
    holder = getattr(audio, "jukebox_motor", None)
    if not isinstance(holder, CabinetMotors):
        holder = CabinetMotors(game)
        try:
            audio.jukebox_motor = holder
        except Exception:
            return None
    holder.game = game
    return holder


def power(game, cabinet_id, on, position):
    """Start or stop one cabinet's machinery. MAIN THREAD ONLY."""
    holder = motors_for(game)
    if holder is None:
        return False
    return holder.power(cabinet_id, on, position)


def tick(game, x, y, z):
    """Follow the listener on every powered cabinet. MAIN THREAD ONLY."""
    holder = motors_for(game)
    if holder is None:
        return
    holder.tick(x, y, z)


def release(game):
    """Stop every motor -- a map change, a disconnect, a relogin."""
    if game is None:
        return
    holder = getattr(getattr(game, "audio_mngr", None), "jukebox_motor", None)
    if isinstance(holder, CabinetMotors):
        holder.release()
