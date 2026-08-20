from . import consts, movement, options
from .speech import speak
from .logger import log


class Camera:
    def __init__(self, game):
        self.game = game
        self.sonar = options.get("sonar", False)
        self.reverb = None
        
        self.soundgroup = self.game.audio_mngr.create_soundgroup(False)
        self.scans = {
            "east": ((), ""),
            "west": ((), ""),
            "north": ((), ""),
            "south": ((), ""),
        }
        self.focus_object = None
        self.currentzone = ""
        self.x = 0.0
        self.y = 0.0
        self.z = 0.0
        # Water filter state: the running automation task (only one may animate
        # the shared filter at a time, otherwise two tasks fight over GAINHF
        # and the sound wobbles) and the last GAINHF we applied so a new task
        # can ramp from the current value instead of jumping.
        self._water_automation = None
        self._water_gainhf = 1.0
        self._water_filter = None
        self._camera_recorded_depth = None
        # Sideline spectator camera (Pong). "follow" = locked to focus object (first
        # person); "east"/"west" = parked at the field edge so both teams are heard
        # in stereo (left/right).
        self.spectator_cam_mode = "follow"
        self.spectator_arena = None

    def get_water_filter(self):
        if getattr(self, "_water_filter", None) is None:
            self._water_filter = self.game.audio_mngr.gen_filter(type="LOWPASS")
        return self._water_filter

    def release_water_filter(self):
        if getattr(self, "_water_filter", None) is not None:
            self.game.audio_mngr.release_filter(self._water_filter)
            self._water_filter = None

    def __del__(self):
        wf = getattr(self, "_water_filter", None)
        if wf is not None:
            try:
                game = getattr(self, "game", None)
                if game and hasattr(game, "audio_mngr"):
                    game.audio_mngr.release_filter(wf)
            except Exception:
                pass
            try:
                self._water_filter = None
            except Exception:
                pass

    def set_focus_object(self, target):
        if self.focus_object:
            if self.focus_object.on_move == self.move:
                self.focus_object.on_move = None
            if self.focus_object.on_turn == self.turn:
                self.focus_object.on_turn = None
        self.focus_object = target
        target.on_move = self.move
        target.on_turn = self.turn
        self.move(target.x, target.y, target.z)
        self.turn(target.hfacing, target.vfacing, target.bfacing)

    def reset_spectator_cam_mode(self):
        """Return to first-person 'follow' mode (called on spectate enter/leave)."""
        self.spectator_arena = None
        if self.spectator_cam_mode != "follow":
            self.spectator_cam_mode = "follow"
            # Re-attach the focus object so its move/turn drive the listener again.
            if self.focus_object:
                self.set_focus_object(self.focus_object)

    def set_spectator_cam_mode(self, mode, arena):
        """Switch the spectator ear between first-person and the field sidelines.
        mode: "follow" | "east" | "west". arena: dict with min_x,max_x,p1_y,p2_y,z.
        In east/west the listener is parked at the field edge, facing across it,
        so both teams are heard left/right in stereo. The focus object's move/turn
        callbacks are detached so its movement can't yank the ear back.
        """
        self.spectator_cam_mode = mode
        self.spectator_arena = arena
        if mode == "follow":
            # Re-attach focus object callbacks for first-person tracking.
            if self.focus_object:
                self.set_focus_object(self.focus_object)
            return
        # Detach focus object callbacks so it stops driving the listener.
        if self.focus_object:
            if self.focus_object.on_move == self.move:
                self.focus_object.on_move = None
            if self.focus_object.on_turn == self.turn:
                self.focus_object.on_turn = None
        self._apply_sideline_position()

    def _apply_sideline_position(self):
        """Position the listener at the chosen sideline and face across the field.
        The stand-off distance scales with the field width so the camera works for
        any future arena size without manual tuning.

        NOTE: this bypasses the full move() pipeline on purpose. move() runs the
        ambience/zone/music enter-leave logic based on the listener's tile, and a
        sideline seat is outside the field where those elements don't reach — so
        going through move() would kill the ambient bed/music. Here we only push
        the raw listener position + orientation, keeping the focus object's
        ambient/music state intact."""
        arena = self.spectator_arena
        if not arena or not self.focus_object:
            return
        mid_y = (arena["p1_y"] + arena["p2_y"]) / 2
        z = arena["z"]
        # Stand-off scales with field width (~10%): a 17-wide field gives ~1.7,
        # a larger field pushes the listener further out for clean stereo.
        field_width = arena["max_x"] - arena["min_x"]
        standoff = max(1, field_width * 0.1)
        if self.spectator_cam_mode == "east":
            self.x = arena["max_x"] + standoff
            self.turn(270, 0, 0)  # face west, across the field
        elif self.spectator_cam_mode == "west":
            self.x = arena["min_x"] - standoff
            self.turn(90, 0, 0)  # face east, across the field
        else:
            return
        self.y = mid_y
        self.z = z
        # Only update the raw listener position + scanner origin, not the
        # ambience/zone/music state (which belongs to the focus object).
        self.game.audio_mngr.position = (self.x, self.y, self.z)
        self.soundgroup.position = (self.x, self.y, self.z)

    def sync_network_position(self, x, y, z):
        """Move a followed spectator listener without zone/reverb side effects."""
        if self.spectator_cam_mode != "follow":
            return
        self.x = float(x)
        self.y = float(y)
        self.z = float(z)
        try:
            self.game.audio_mngr.position = (self.x, self.y, self.z)
            self.soundgroup.position = (self.x, self.y, self.z)
            megaphone = getattr(getattr(self.game, 'gameplay', None), 'megaphone', None)
            if megaphone and hasattr(megaphone, 'request_spatial_refresh'):
                megaphone.request_spatial_refresh()
        except Exception as e:
            # Cyal/OpenAL failures must not make a network snapshot kill the game.
            log(f"[ENTITY.AUDIO] Spectator listener update skipped: {e}")

    def move(self, x, y, z):
        ambiences_to_pause = list(
            self.focus_object.map.get_ambiences_at(self.x, self.y, self.z)
        )
        musics_to_pause = list(
            self.focus_object.map.get_musics_at(self.x, self.y, self.z)
        )
        self.x = float(x) if x is not None else 0.0
        self.y = float(y) if y is not None else 0.0
        self.z = float(z) if z is not None else 0.0
        self.game.audio_mngr.position = (self.x, self.y, self.z)
        self.soundgroup.position = (self.x, self.y, self.z)
        megaphone = getattr(getattr(self.game, 'gameplay', None), 'megaphone', None)
        if megaphone and hasattr(megaphone, 'request_spatial_refresh'):
            megaphone.request_spatial_refresh()

        def muffling_at(d):
            # Same depth->GAINHF curve as Entity.water_muffling, so the world
            # (this filter) and the player's own sounds (Entity's filter) un-
            # muffle at the same rate instead of one clearing before the other.
            return 0.02 + 0.48 * max(0.0, min(1.0, d))

        def cancel_water_task():
            if self._water_automation is not None:
                if self._water_automation in self.game.automations:
                    try:
                        self.game.automations.remove(self._water_automation)
                    except ValueError:
                        pass
                self._water_automation = None

        def automation_water(value):
            # Track the last applied value so the next task can ramp from the
            # current position instead of snapping back to an old start point.
            self._water_gainhf = value
            flt = self.get_water_filter()
            if flt is None:
                return
            flt.set("GAINHF", value)
            self.game.audio_mngr.apply_filter(flt, self.game.exclude_water, replace=True)
            if hasattr(self.focus_object, "vc_source") and self.focus_object.vc_source:
                self.focus_object.vc_source.direct_filter = flt

        def start_water_task(target, duration, start_value, callback=None):
            # Only one water task may run at a time: two automations animating
            # the same shared filter (or the same vc_source) fight over GAINHF
            # every 20ms tick, which is what made the sound bend/wobble while
            # diving. Cancel any running task before starting a new one.
            cancel_water_task()
            task = self.game.automate(
                None, None,
                target, duration,
                step_callback=automation_water, start_value=start_value,
                callback=callback,
            )
            self._water_automation = task

        if not self.focus_object.in_water and self.focus_object.map.get_tile_at(self.focus_object.x, self.focus_object.y, self.focus_object.z) == "underwater":
            self.focus_object.play_sound("foley/swim/start/", cat="self")
            self.focus_object.in_water = True
            self.focus_object.drownable = False
            self.focus_object.drown_clock.restart()
            self.game.ignore_others_water = True
            self.focus_object.drown_clock.restart()
            self._camera_recorded_depth = round(self.focus_object.depth, 3)
            self.focus_object.recorded_depth = round(self.focus_object.depth, 3)
            start_water_task(muffling_at(self.focus_object.depth), 500, self._water_gainhf)
        elif self.focus_object.in_water and self.focus_object.map.get_tile_at(self.focus_object.x, self.focus_object.y, self.focus_object.z) != "underwater":
            self.focus_object.play_sound("foley/swim/end/", cat="self")
            def on_exit_complete():
                self.game.audio_mngr.apply_filter(None)
                if hasattr(self.focus_object, "vc_source") and self.focus_object.vc_source:
                    with contextlib.suppress(Exception):
                        del self.focus_object.vc_source.direct_filter
                self.release_water_filter()
            start_water_task(1.0, 500, self._water_gainhf, callback=on_exit_complete)
            self.focus_object.in_water=False
            self.focus_object.drownable = False
            self.game.ignore_others_water = False
            self._camera_recorded_depth = None
        elif self.focus_object.in_water:
            cur_depth = round(self.focus_object.depth, 3)
            if self._camera_recorded_depth is None or cur_depth != self._camera_recorded_depth:
                muffling = muffling_at(cur_depth)
                start_water_task(muffling, 100, self._water_gainhf)
                self._camera_recorded_depth = cur_depth
                self.focus_object.recorded_depth = cur_depth


        # change reverb if required.
        reverb = self.focus_object.map.get_reverb_at(self.x, self.y, self.z)
        if reverb != self.reverb and not self.focus_object.dead:
            self.reverb = reverb
            if reverb is None:
                self.focus_object.soundgroup.apply_effect(None, 0)
            else:
                self.focus_object.soundgroup.apply_effect(reverb.reverb, 0)
            
        # enter/leave zones
        zone = self.focus_object.map.get_zone_at(self.x, self.y, self.z)
        if zone and zone != self.currentzone:
            speak(f"{zone}")
            self.currentzone = zone
        # enter/leave ambiences
        for i in self.focus_object.map.get_ambiences_at(self.x, self.y, self.z):
            if i in ambiences_to_pause:
                ambiences_to_pause.remove(i)
                if not i.playing:
                    i.enter()
                continue
            i.enter()
        for i in ambiences_to_pause:
            i.leave()
        # enter/leave musics
        for i in self.focus_object.map.get_musics_at(self.x, self.y, self.z):
            if i in musics_to_pause:
                musics_to_pause.remove(i)
                if not i.playing:
                    i.enter()
                continue
            i.enter()
        for i in musics_to_pause:
            i.leave()
        if self.sonar:
            self.scan_around()

    def scan_around(self):
        self.scan_east()
        self.game.call_after(20, self.scan_north)
        self.game.call_after(40, self.scan_west)

    def turn(self, hdeg, vdeg, bdeg=0):
        self.game.audio_mngr.orientation = (hdeg, vdeg, bdeg)

    def scan_north(self):
        dist = self.x, self.y, self.z
        for _ in range(10):
            dist = movement.move(dist, (self.focus_object.hfacing) % 360).get_tuple
            if not self.focus_object.map.in_bound(*dist):
                break
            tile = self.focus_object.map.get_tile_at(*dist)
            scan = self.scans["north"]
            if not tile or tile == "air":
                if scan[0] != dist and scan[1] != "air":
                    self.scans["north"] = dist, "air"
                    self.soundgroup.play(
                        "camera/air.ogg", rel_x=(dist[0] - self.x) / 4, rel_y=(dist[1] - self.y) / 4
                    )
                break

            elif tile.startswith("wall"):
                if scan[0] != dist and scan[1] != "wall":
                    self.scans["north"] = dist, "wall"
                    self.soundgroup.play(
                        "camera/wall.ogg",
                        rel_x=dist[0] - self.x,
                        rel_y=dist[1] - self.y,
                    )
                break

        else:
            if scan[0] != dist and scan[1] != "":
                self.scans["north"] = dist, ""
                self.soundgroup.play(
                    "camera/opening.ogg", rel_x=dist[0] - self.x, rel_y=dist[1] - self.y
                )

    def scan_east(self):
        dist = self.x, self.y, self.z
        for _ in range(10):
            dist = movement.move(dist, (self.focus_object.hfacing + 90) % 360).get_tuple
            if not self.focus_object.map.in_bound(*dist):
                break
            tile = self.focus_object.map.get_tile_at(*dist)
            scan = self.scans["east"]
            if not tile or tile == "air":
                if scan[0] != dist and scan[1] != "air":
                    self.scans["east"] = dist, "air"
                    self.soundgroup.play(
                        "camera/air.ogg", rel_x=dist[0] - self.x, rel_y=dist[1] - self.y
                    )
                break
            elif tile.startswith("wall"):
                if scan[0] != dist and scan[1] != "wall":
                    self.scans["east"] = dist, "wall"
                    self.soundgroup.play(
                        "camera/wall.ogg",
                        rel_x=dist[0] - self.x,
                        rel_y=dist[1] - self.y,
                    )
                break
        else:
            if scan[0] != dist and scan[1] != "":
                self.scans["east"] = dist, ""
                self.soundgroup.play(
                    "camera/opening.ogg", rel_x=dist[0] - self.x, rel_y=dist[1] - self.y
                )

    def scan_west(self):
        dist = self.x, self.y, self.z
        for _ in range(10):
            dist = movement.move(dist, (self.focus_object.hfacing - 90) % 360).get_tuple
            if not self.focus_object.map.in_bound(*dist):
                break
            tile = self.focus_object.map.get_tile_at(*dist)
            scan = self.scans["west"]
            if not tile or tile == "air":
                if scan[0] != dist and scan[1] != "air":
                    self.scans["west"] = dist, "air"
                    self.soundgroup.play(
                        "camera/air.ogg", rel_x=dist[0] - self.x, rel_y=dist[1] - self.y
                    )
                break
            elif tile.startswith("wall"):
                if scan[0] != dist and scan[1] != "wall":
                    self.scans["west"] = dist, "wall"
                    self.soundgroup.play(
                        "camera/wall.ogg",
                        rel_x=dist[0] - self.x,
                        rel_y=dist[1] - self.y,
                    )
                break
        else:
            if scan[0] != dist and scan[1] != "":
                self.scans["west"] = dist, ""
                self.soundgroup.play(
                    "camera/opening.ogg", rel_x=dist[0] - self.x, rel_y=dist[1] - self.y
                )
