import contextlib
import traceback

class Object:
    # One-shot entity sounds (footsteps, vocal calls, impacts) fade smoothly
    # with distance: full volume inside DIST_FADE_START tiles, then a
    # smoothstep ramp down to true silence at DIST_FADE_END tiles. This fixes
    # the old behaviour where a faint step stayed audible from far away and
    # then hard-cut off at the broadcast edge instead of gradually fading.
    DIST_FADE_START = 8.0
    DIST_FADE_END = 40.0

    def __init__(self, game, map, x, y, z, radius=0.5):
        self.game = game
        self.map = map
        self.x = x
        self.y = y
        self.z = z
        self.radius = radius
        self.falling = False
        self.player=False
        self.fall_time = 80
        self.fall_clock = game.new_clock()
        self.soundgroup = self.game.audio_mngr.create_soundgroup(radius=radius)
        self.soundgroup.position = (x, y, z)

    def _distance_faded_volume(self, volume, rel_x=0, rel_y=0, rel_z=0):
        """Scale a one-shot sound's volume by distance from the listener.

        Returns 0 (skip) beyond DIST_FADE_END so far-away entity sounds fade
        out gradually instead of staying faintly audible and then cutting off
        abruptly. Local sounds (distance 0) are never touched. Any failure
        falls back to the original volume so audio can never break.
        """
        if volume <= 0:
            return 0
        try:
            from ..movement import get_3d_distance
            mgr = self.game.audio_mngr
            lx, ly, lz = mgr.position
            sx, sy, sz = self.soundgroup.position
            dist = get_3d_distance(lx, ly, lz, sx + rel_x, sy + rel_y, sz + rel_z)
        except Exception:
            return volume
        if dist <= self.DIST_FADE_START:
            return volume
        if dist >= self.DIST_FADE_END:
            return 0
        t = (dist - self.DIST_FADE_START) / (self.DIST_FADE_END - self.DIST_FADE_START)
        smooth = t * t * (3.0 - 2.0 * t)
        return int(volume * (1.0 - smooth))

    def play_sound(
        self, sound, looping=False, cat="miscelaneous", id="", rel_x=0, rel_y=0, rel_z=0, volume=100, pitch=1.0
    ):
        try:
            # Object sounds (including the local player's steps and wall
            # impacts) must stay in this object's spatial SoundGroup.  Routing
            # a focused object to direct_soundgroup strips both rel_x/rel_y
            # direction and the SoundGroup EFX send, which makes the sound
            # always play in the centre with no map reverb.
            volume = self._distance_faded_volume(volume, rel_x, rel_y, rel_z)
            if volume <= 0:
                return
            return self.soundgroup.play(
                sound,
                looping=looping,
                cat= cat,
                id=id,
                rel_x=rel_x,
                rel_y=rel_y,
                rel_z = rel_z,
                volume=volume,
                pitch=pitch
            )
        except Exception as e:
            print("\a", e)
            traceback.print_exc()

    def play_sound_dist(
        self, sound, looping=False, volume=100, id="", rel_x=0, rel_y=0, rel_z=0, cat="miscelaneous", pitch=1.0
    ):
        try:
            is_focus = False
            try:
                if hasattr(self.game, 'gameplay') and getattr(self.game.gameplay.camera, 'focus_object', None) == self:
                    is_focus = True
            except Exception:
                pass
            
            if is_focus and hasattr(self.game, 'direct_soundgroup'):
                return

            return self.soundgroup.play(
                sound,
                looping=looping,
                cat=cat,
                id=id,
                rel_x=rel_x,
                rel_y=rel_y,
                rel_z=rel_z,
                dist=True,
                volume=volume,
                pitch=pitch
            )
        except Exception as e:
            print("\a", e)
            traceback.print_exc()

    def on_hit(self, object, hp):
        pass

    def on_interact(self, object):
        pass

    def loop(self):
        pass


    def destroy(self):
        self.soundgroup.destroy()
