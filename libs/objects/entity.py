import os
from random import randint as random
from .. import options
from .. import voice_chat
from ..logger import log

import cyal.exceptions

from .. import movement, consts
from .object import Object

from ..audio import sound

class Entity(Object):
    def __init__(self, game, map, x, y, z, hp, name="None", player=False):
        super().__init__(game, map, x, y, z)
        self._player = player
        self.is_user = False
        self.on_move = None
        self.on_turn = None
        self.in_water = False
        self.depth= 1.0
        self.recorded_depth = self.depth
        self.limit_depth=1.0
        self.movement_clock = game.new_clock()
        self.drown_clock = game.new_clock() # Required for camera follow logic causing crash if missing
        self.drownable = True
        self._hp = hp
        self.player_dead=False
        self.hfacing = 0
        self.vfacing = 0
        self.bfacing = 0
        self.fall_distance = 0
        self.beacon=None
        self.stun_time = 1000.0
        self.stunned = False
        self.stunned_clock = self.game.new_clock()
        self.name = name
        self.has_radio = False
        self.dead = False  # Required for camera.move reverb check
        self.water_filter = None
        self._water_automation = None
    
    @property
    def player(self):
        return self._player

    @property
    def water_muffling(self):
        # Maps self.depth (0.0 to 1.0) to GAINHF (0.02 to 0.50)
        # 1.0 (surface) = 0.50, 0.0 (bottom) = 0.02
        return 0.02 + 0.48 * max(0.0, min(1.0, self.depth))

    @player.setter
    def player(self, value):
        self._player = value
        if value:
            try: self.vc_source, self.radio_source, self.music_source = self.game.audio_mngr.context.gen_sources(3)
            except cyal.exceptions.InvalidOperationError as e:
                print(e)
                self.vc_source, self.radio_source, self.music_source = self.game.audio_mngr.context.gen_sources(3) 
            self.vc_source.position = (self.x, self.y, self.z)
            self.music_source.position = (self.x, self.y, self.z)
            self.music_source.rolloff_factor = 2.0
            self.music_source.reference_distance = 5.0
            self.music_source.max_distance = 150.0
            self.radio_source.position = (0,0,0)
            self.radio_source.relative = True
            self.radio_source.gain=0.7
            self.eq_slot = self.soundgroup.parent.gen_effect(
                "EQUALIZER",
                ("low_gain", 0.126),
                ("low_cutoff", 800.0),
                ("high_gain", 0.126),
                ("high_cutoff", 4000.0)
            )
            self.distortion_slot = self.soundgroup.parent.gen_effect(
                "DISTORTION",
                ("edge", 0.5),
                ("gain", 0.2)
            )
            try:
                if self.distortion_slot is not None: self.distortion_slot.target = self.eq_slot
            except Exception as e:
                print(f"[Entity] Warning: Failed to chain distortion to equalizer target: {e}")
            self.soundgroup.parent.efx.send(self.radio_source, 1, self.distortion_slot)
            self.vc_compression = voice_chat.voice_chat_compression(self.game)




    def move(self, x, y, z, play_sound=True, mode="walk"):
        self.x = x
        self.y = y
        self.z = z
        if callable(self.on_move):
            self.on_move(x, y, z)
        reverb = self.map.get_reverb_at(self.x, self.y, self.z)
        if reverb is None: 
            self.soundgroup.apply_effect(None, 0)
            if self.player: 
                self.game.audio_mngr.efx.send(self.vc_source, 0, None, filter=None)
                self.game.audio_mngr.efx.send(self.music_source, 0, None, filter=None)
        if reverb and reverb.reverb:
            try:
                self.soundgroup.apply_effect(reverb.reverb, 0) 
                if self.player: 
                    self.game.audio_mngr.efx.send(self.vc_source, 0, reverb.reverb, filter=self.soundgroup.filter[-1] if len(self.soundgroup.filter) > 0 else None)
                    self.game.audio_mngr.efx.send(self.music_source, 0, reverb.reverb, filter=self.soundgroup.filter[-1] if len(self.soundgroup.filter) > 0 else None)
            except cyal.exceptions.InvalidAlValueError as e:
                pass
            except cyal.exceptions.InvalidOperationError as e:
                pass
        if self.player:
            self.vc_source.position = (self.x, self.y, self.z)
            self.music_source.position = (self.x, self.y, self.z)
            if not self.is_user:
                dist = movement.get_3d_distance(*self.vc_source.position, *self.game.audio_mngr.position)
                max_dist = self.game.audio_mngr.max_distance
                min_dist = 5.0
                if dist <= min_dist:
                    gain = 1.0
                elif dist >= max_dist:
                    gain = 0.0
                else:
                    gain = 1.0 - ((dist - min_dist) / (max_dist - min_dist))
                self.vc_source.gain = gain
                self.music_source.gain = gain
        self.soundgroup.position = (self.x, self.y, self.z)
        tile = self.map.get_tile_at(self.x, self.y, self.z)
        # start/stop falling if the current tile is air.
        if getattr(self.game, 'pong_mode', False):
            self.falling = False
        else:
            if not self.falling and tile in ["air", ""]:
                self.fall_start()
            elif self.falling and tile not in ["air", "", "deep_water"]:
                self.fall_stop()
        if play_sound and not self.falling:
            if getattr(self.game, 'pong_mode', False) and getattr(self, 'is_user', False):
                pass # suppress normal footstep; server plays pong-specific move sound
            else:
                if mode == "run" and not os.path.exists(
                    f"{consts.SOUNDPREPEND}/steps/{tile}/run"
                ):
                    mode = "walk"
                cat="zombies"
                if self == self.map.player: cat = "self"
                elif not self.name.startswith("zomby"): cat = "players"
                self.play_sound(
                    f"steps/{tile}/{mode}",
                    rel_z=-1,
                    cat=cat
                )

    def face(self, hdeg, vdeg, bdeg=0, play_sound=False, force=False):
        if play_sound:
            self.play_sound("foley/turn/end.ogg", cat="players")
        self.hfacing = hdeg % 360
        self.vfacing = ((vdeg + 90) % 181) - 90
        self.bfacing = ((bdeg + 90) % 181) - 90
        if callable(self.on_turn):
            self.on_turn(self.hfacing, self.vfacing, self.bfacing)

    def walk(
        self, back=False, left=False, right=False, down=False, up=False, mode="walk"
    ):
        if self.map.get_tile_at(self.x, self.y, self.z) in ["deep_water", "underwater"] or not self.falling: self.fall_clock.restart()
        if self.stunned and self.stunned_clock.elapsed >= self.stun_time:
            self.stunned = False
            self.stunned_clock.restart()
        if not self.stunned:
            dist = movement.move((self.x, self.y, self.z), self.hfacing).get_tuple
            self.face(self.hfacing, 0)
            if back:
                dist = movement.move(
                    (self.x, self.y, self.z), self.hfacing + 180 % 360
                ).get_tuple
            if left:
                dist = movement.move(
                    (self.x, self.y, self.z), self.hfacing - 90 % 360
                ).get_tuple
            if right:
                dist = movement.move(
                    (self.x, self.y, self.z), self.hfacing + 90 % 360
                ).get_tuple
            if down:
                dist = (self.x, self.y, self.z - 1)
                if self.in_water:
                    if self.depth > 0.0:
                        if self.limit_depth >= 0.0: self.depth = round(self.depth - 0.1, 3) 
                        self.limit_depth -= 0.1
                    else:
                        self.depth = 0.0
                        self.limit_depth-=0.1
            if up:
                dist = (self.x, self.y, self.z + 1)
                if self.in_water:
                    if self.depth < 1.0:
                        if self.limit_depth >= 0.0: self.depth = round(self.depth + 0.1, 3)
                        self.limit_depth+=0.1
                    else: 
                        self.depth = 1.0
                        self.limit_depth = 1.0
            if self.map.in_bound(*dist):
                disttile = self.map.get_tile_at(*dist)
                if "wall" not in disttile:
                    if (up or down) and disttile in ["air", ""]:
                        return False
                    self.move(*dist, mode=mode)
                    return True
                
                # Handle wall collision sound
                bump_sound = f"walls/{disttile}.ogg"
                if getattr(self.game, 'pong_mode', False) and getattr(self, 'is_user', False):
                    bump_sound = "Pong/Border.ogg"

                self.play_sound(
                    bump_sound,
                    rel_x=dist[0] - self.x,
                    rel_y=dist[1] - self.y,
                    rel_z=1,
                )
            return False

    def fall_start(self):
        self.fall_clock.restart()
        self.play_sound("foley/fall/start.ogg")
        self.falling = True
        log(f"[DEBUG.FALL] {self.name} started falling at X={self.x}, Y={self.y}, Z={self.z}. Playing foley/fall/start.ogg")

    def fall_stop(self):
        self.falling = False
        tile = self.map.get_tile_at(self.x, self.y, self.z)
        log(f"[DEBUG.FALL] {self.name} landed on surface '{tile}' at X={self.x}, Y={self.y}, Z={self.z}. Playing foley/fall/end.ogg")
        self.play_sound("foley/fall/end.ogg")
        # sound-simulate landing hard on a platform.
        steps_count = random(3, 7)
        log(f"[DEBUG.FALL] Simulating hard landing impact: scheduling {steps_count} rapid step sounds for surface '{tile}'...")
        for i in range(steps_count):
            delay = random(10, 100)
            log(f"[DEBUG.FALL] Scheduling step sound #{i+1} with delay of {delay}ms")
            self.game.call_after(
                delay, lambda: self.move(self.x, self.y, self.z, mode="run")
            )

    def loop(self):
        if self.player:
            with self.soundgroup.parent.context.batch():
                if not self.is_user:
                    self.vc_source.position = (self.x, self.y, self.z)
                    self.music_source.position = (self.x, self.y, self.z)
                    dist = movement.get_3d_distance(*self.vc_source.position, *self.game.audio_mngr.position)
                    max_dist = self.game.audio_mngr.max_distance
                    min_dist = 5.0
                    if getattr(self, "muted_by_spectator", False):
                        gain = 0.0
                    else:
                        if dist <= min_dist:
                            gain = 1.0
                        elif dist >= max_dist:
                            gain = 0.0
                        else:
                            gain = 1.0 - ((dist - min_dist) / (max_dist - min_dist))
                    self.vc_source.gain = gain
                    self.music_source.gain = gain

                    # Real-time filter sync with soundgroup filter state (optimized)
                    current_filter = self.soundgroup.filter[-1] if len(self.soundgroup.filter) > 0 else None
                    if getattr(self, '_last_applied_filter', -1) != current_filter:
                        self._last_applied_filter = current_filter
                        if current_filter is not None:
                            self.vc_source.direct_filter = current_filter
                            self.music_source.direct_filter = current_filter
                        else:
                            try: del self.vc_source.direct_filter
                            except Exception: pass
                            try: del self.music_source.direct_filter
                            except Exception: pass

                if self.vc_source.buffers_queued == 0 and not self.is_user: 
                    try:
                        buffer = self.game.audio_mngr.context.gen_buffer()
                        buffer.set_data(
                            self.game.audio_mngr.silent_buf,
                            sample_rate=48000,
                            format=cyal.BufferFormat.MONO16
                        )
                        self.vc_source.queue_buffers(buffer)
                    except Exception as e:
                        # Prevent crash if OpenAL runs out of memory/sources
                        pass 

                if self.radio_source.buffers_queued == 0: 
                    try:
                        buffer = self.game.audio_mngr.context.gen_buffer()
                        buffer.set_data(
                            self.game.audio_mngr.silent_buf,
                            sample_rate=48000,
                            format=cyal.BufferFormat.MONO16
                        )
                        self.radio_source.queue_buffers(buffer)
                    except Exception as e:
                        pass

                if self.music_source.buffers_queued == 0: 
                    try:
                        buffer = self.game.audio_mngr.context.gen_buffer()
                        buffer.set_data(
                            self.game.audio_mngr.silent_buf,
                            sample_rate=48000,
                            format=cyal.BufferFormat.MONO16
                        )
                        self.music_source.queue_buffers(buffer)
                    except Exception as e:
                        pass
        # 🛡️ Protection: Skip tile checks if coordinates are None
        if self.x is None or self.y is None or self.z is None:
            return
        if (
            self.falling
            and self.fall_clock.elapsed >= self.fall_time
            and self.map.in_bound(self.x, self.y, self.z)
            or self.map.get_tile_at(self.x, self.y, self.z) in ["deep_water", "underwater"]
            and self.fall_clock.elapsed >= self.fall_time * 25
            and self.map.in_bound(self.x, self.y, self.z-1)
            and not self.map.get_tile_at(self.x, self.y, self.z-1).startswith("wall")
            and self.map.get_tile_at(self.x, self.y, self.z-1) not in ["air", ""]
        ):
            self.fall_clock.restart()
            self.move(self.x, self.y, self.z - 1, False)
            if self.is_user and self.game and self.game.network: 
                self.game.network.send(
                    consts.CHANNEL_MAP,
                    "move",
                    {
                        "x": self.x,
                        "y": self.y,    
                        "z": self.z,
                        "play_sound": False,
                        "mode": "walk",
                    },
                )

            if self.in_water:
                if self.depth > 0.0:
                    if self.limit_depth >= 0.0: self.depth = round(self.depth - 0.1, 3) 
                    self.limit_depth -= 0.1
                else:
                    self.depth = 0.0
                    self.limit_depth-=0.1
            if not self.in_water: self.face(random(-45, 45), random(-45, 45), random(-45, 45))
            self.fall_distance += 1
            if not self.map.in_bound(self.x, self.y, self.z):
                self.fall_stop()

    def on_hit(self):
        self.play_sound(f"entities/{self.name}/pain{random(1, 3)}.ogg)")

    def death(self):
        raise NotImplementedError

    def water_check(self):
        # Helper to generate the automation callback with a specific filter
        def create_water_automation(filter_obj):
            def automation_water(value):
                # Check if filter was created successfully
                if filter_obj is None:
                    return
                filter_obj.set("GAINHF", value)
                self.soundgroup.apply_filter(filter_obj, replace=True)
                if self.player:
                    if filter_obj is not None: 
                        self.vc_source.direct_filter = filter_obj
                        self.music_source.direct_filter = filter_obj
                    else: 
                        try: del self.vc_source.direct_filter
                        except: pass
                        try: del self.music_source.direct_filter
                        except: pass
            return automation_water
        
        def cancel_active_automation():
            if getattr(self, '_water_automation', None) is not None:
                if self._water_automation in self.game.automations:
                    try:
                        self.game.automations.remove(self._water_automation)
                    except ValueError:
                        pass
                self._water_automation = None

        def get_water_filter():
            if getattr(self, 'water_filter', None) is None:
                self.water_filter = self.game.audio_mngr.gen_filter(type="LOWPASS")
            return self.water_filter

        if not self.in_water and self.map.get_tile_at(self.x, self.y, self.z) == "underwater":
            self.game.audio_mngr.play_unbound("foley/swim/start/", self.x, self.y, self.z)
            self.in_water = True
            self.game.exclude_water.append(self.soundgroup)
            muffling = self.water_muffling
            
            # Cancel any existing water automation first
            cancel_active_automation()

            # Reuse or create water filter
            filter_obj = get_water_filter()
            
            if not self.game.ignore_others_water:
                def on_complete():
                    if getattr(self, '_water_automation', None) == task:
                        self._water_automation = None

                task = self.game.automate(
                    None, None,
                    muffling, 500,
                    step_callback = create_water_automation(filter_obj), start_value=1.0,
                    callback=on_complete
                )
                self._water_automation = task

        if self.in_water and self.map.get_tile_at(self.x, self.y, self.z) != "underwater":
            self.game.audio_mngr.play_unbound("foley/swim/end/", self.x, self.y, self.z)
            muffling = self.water_muffling
            
            # Cancel any existing water automation first
            cancel_active_automation()

            # Reuse or create water filter
            filter_obj = get_water_filter()

            if not self.game.ignore_others_water:
                def on_complete():
                    if getattr(self, '_water_automation', None) == task:
                        self._water_automation = None

                task = self.game.automate(
                    None, None,
                    1.0, 500,
                    step_callback = create_water_automation(filter_obj), start_value=muffling,
                    callback=on_complete
                )
                self._water_automation = task
            self.in_water=False
            self.game.exclude_water.pop(self.game.exclude_water.index(self.soundgroup))

        if round(self.depth, 3) != round(self.recorded_depth,3) and self.in_water:
            muffling = self.water_muffling
            
            # Cancel any active enter/exit water automation first
            cancel_active_automation()

            # Apply muffling directly to the reused water filter without spawning a new automate task
            filter_obj = get_water_filter()
            if filter_obj:
                filter_obj.set("GAINHF", muffling)
                self.soundgroup.apply_filter(filter_obj, replace=True)
                if self.player:
                    self.vc_source.direct_filter = filter_obj
                    self.music_source.direct_filter = filter_obj
            self.recorded_depth = round(self.depth,3)

    @property 
    def hp(self):
        return self._hp
    
    @hp.setter
    def hp(self, value):
        self._hp = value if 0 <= value <= 100 else self._hp
    
    def destroy(self):
        # Cancel any active water automation task
        if getattr(self, '_water_automation', None) is not None:
            if self._water_automation in self.game.automations:
                try:
                    self.game.automations.remove(self._water_automation)
                except ValueError:
                    pass
            self._water_automation = None
        self.water_filter = None

        if self.player: 
            self.vc_compression.put(None)
            # Release OpenAL sources for player entities
            for src_name in ['vc_source', 'radio_source', 'music_source']:
                src = getattr(self, src_name, None)
                if src:
                    try:
                        src.stop()
                        src.buffer = None
                        while src.buffers_queued > 0:
                            src.unqueue_buffers()
                        src.delete()
                    except Exception:
                        pass
            # Return EFX effect slots to pool
            for slot_name in ['distortion_slot', 'eq_slot']:
                slot = getattr(self, slot_name, None)
                if slot:
                    self.soundgroup.parent.release_effect_slot(slot)
                    setattr(self, slot_name, None)
        if self.beacon is not None:
            self.beacon.destroy(force=True)
        super().destroy()
    

