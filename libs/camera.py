from . import consts, movement, options
from .speech import speak


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
        # Sideline spectator camera (Pong). "follow" = locked to focus object (first
        # person); "east"/"west" = parked at the field edge so both teams are heard
        # in stereo (left/right).
        self.spectator_cam_mode = "follow"
        self.spectator_arena = None

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

        filter = self.game.audio_mngr.gen_filter(
            type="LOWPASS"
        )
        
        def automation_water(value):
            filter.set("GAINHF", value)
            self.game.audio_mngr.apply_filter(filter, self.game.exclude_water, replace=True)
            if hasattr(self.focus_object, "vc_source") and self.focus_object.vc_source:
                self.focus_object.vc_source.direct_filter = filter
        
        
        if not self.focus_object.in_water and self.focus_object.map.get_tile_at(self.focus_object.x, self.focus_object.y, self.focus_object.z) == "underwater":
            self.focus_object.play_sound("foley/swim/start/", cat="self")
            self.focus_object.in_water = True
            self.focus_object.drownable = False
            self.focus_object.drown_clock.restart()
            self.game.ignore_others_water = True
            self.focus_object.drown_clock.restart()
            muffling = 0.05 * self.focus_object.depth
            self.game.automate(
                None, None,
                muffling, 500,
                step_callback = automation_water, start_value=1.0
            )
        if self.focus_object.in_water and self.focus_object.map.get_tile_at(self.focus_object.x, self.focus_object.y, self.focus_object.z) != "underwater":
            self.focus_object.play_sound("foley/swim/end/", cat="self")
            muffling = 0.05 * self.focus_object.depth
            self.game.automate(
                None, None,
                1.0, 500,
                step_callback = automation_water, start_value=muffling
            )
            self.focus_object.in_water=False
            self.focus_object.drownable = False
            self.game.ignore_others_water = False
        if round(self.focus_object.depth, 3) != round(self.focus_object.recorded_depth,3) and self.focus_object.in_water:
            muffling = 0.05 * round(self.focus_object.depth,3)
            self.game.automate(
                None, None,
                muffling, 50,
                step_callback = automation_water, start_value=0.05*round(self.focus_object.recorded_depth,3)
            )
            self.focus_object.recorded_depth = round(self.focus_object.depth,3)


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
