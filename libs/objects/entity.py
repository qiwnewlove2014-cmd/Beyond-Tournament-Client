import os
import time
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
            self.soundgroup.filterable = True
            try:
                self.vc_source, self.radio_source, self.music_source = self.game.audio_mngr.context.gen_sources(3)
            except Exception as e:
                print(f"[Entity] Warning: Could not allocate OpenAL sources for entity '{self.name}': {e}")
                self.vc_source = None
                self.radio_source = None
                self.music_source = None
            ex = float(self.x) if self.x is not None else 0.0
            ey = float(self.y) if self.y is not None else 0.0
            ez = float(self.z) if self.z is not None else 0.0
            if self.vc_source:
                self.vc_source.position = (ex, ey, ez)
                self.vc_source.reference_distance = 1.7  # Boost volume to ~20% at 8 meters
            if self.music_source:
                self.music_source.position = (ex, ey, ez)
                self.music_source.rolloff_factor = 0.5
                self.music_source.reference_distance = 5.0
                self.music_source.max_distance = 50.0
            if self.radio_source:
                self.radio_source.position = (0,0,0)
                self.radio_source.relative = True
                self.radio_source.gain=0.7
            try:
                self.eq_slot = self.soundgroup.parent.gen_effect(
                    "EQUALIZER",
                    ("low_gain", 0.126),
                    ("low_cutoff", 800.0),
                    ("high_gain", 0.126),
                    ("high_cutoff", 4000.0),
                    ("mid1_gain", 1.0),
                    ("mid2_gain", 1.0),
                )
                self.distortion_slot = self.soundgroup.parent.gen_effect(
                    "DISTORTION",
                    ("edge", 0.2),
                    ("gain", 0.5),
                    ("lowpass_cutoff", 8000.0),
                    ("eqcenter", 3000.0),
                    ("eqbandwidth", 1000.0),
                )
            except Exception:
                self.eq_slot = None
                self.distortion_slot = None
            try:
                if self.distortion_slot is not None: self.distortion_slot.target = self.eq_slot
            except Exception as e:
                print(f"[Entity] Warning: Failed to chain distortion to equalizer target: {e}")
            self.soundgroup.parent.efx.send(self.radio_source, 1, self.distortion_slot)
            self.vc_compression = voice_chat.voice_chat_compression(self.game)
            self.music_compression = voice_chat.MusicCompression(self.game)
            
            # Apply initial reverb to the newly created voice/music sources
            reverb = self.map.get_reverb_at(self.x, self.y, self.z)
            if reverb and reverb.reverb:
                try:
                    self.soundgroup.parent.efx.send(self.vc_source, 0, reverb.reverb, filter=self.soundgroup.filter[-1] if len(self.soundgroup.filter) > 0 else None)
                    self.soundgroup.parent.efx.send(self.music_source, 0, reverb.reverb, filter=self.soundgroup.filter[-1] if len(self.soundgroup.filter) > 0 else None)
                except Exception:
                    pass

    def sync_reverb(self):
        """Re-syncs environment Reverb EFX for this entity at current position."""
        if not getattr(self, "map", None):
            return
        try:
            reverb = self.map.get_reverb_at(self.x, self.y, self.z)
            if reverb is None:
                self.soundgroup.apply_effect(None, 0)
                if self.player:
                    if getattr(self, "vc_source", None):
                        self.game.audio_mngr.efx.send(self.vc_source, 0, None, filter=None)
                    if getattr(self, "music_source", None):
                        self.game.audio_mngr.efx.send(self.music_source, 0, None, filter=None)
            elif reverb and reverb.reverb:
                self.soundgroup.apply_effect(reverb.reverb, 0)
                if self.player:
                    flt = self.soundgroup.filter[-1] if len(self.soundgroup.filter) > 0 else None
                    if getattr(self, "vc_source", None):
                        self.game.audio_mngr.efx.send(self.vc_source, 0, reverb.reverb, filter=flt)
                    if getattr(self, "music_source", None):
                        self.game.audio_mngr.efx.send(self.music_source, 0, reverb.reverb, filter=flt)
        except Exception as e:
            log(f"[ENTITY.AUDIO] Reverb sync skipped for {self.name!r}: {e}")

    def move(self, x, y, z, play_sound=True, mode="walk"):
        x = float(x) if x is not None else (float(self.x) if self.x is not None else 0.0)
        y = float(y) if y is not None else (float(self.y) if self.y is not None else 0.0)
        z = float(z) if z is not None else (float(self.z) if self.z is not None else 0.0)
        self.x = x
        self.y = y
        self.z = z
        if callable(self.on_move):
            self.on_move(x, y, z)
        self.sync_reverb()
        if self.player:
            try:
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

                    music_max = 50.0
                    if dist <= min_dist:
                        music_gain = 1.0
                    elif dist >= music_max:
                        music_gain = 0.0
                    else:
                        music_gain = 1.0 - ((dist - min_dist) / (music_max - min_dist))
                    self.music_source.gain = music_gain
            except (cyal.exceptions.InvalidAlValueError, cyal.exceptions.InvalidOperationError) as e:
                log(f"[ENTITY.AUDIO] Voice position update skipped for {self.name!r}: {e}")
        try:
            self.soundgroup.position = (self.x, self.y, self.z)
        except (cyal.exceptions.InvalidAlValueError, cyal.exceptions.InvalidOperationError) as e:
            log(f"[ENTITY.AUDIO] SoundGroup position update skipped for {self.name!r}: {e}")
        tile = self.map.get_tile_at(self.x, self.y, self.z)
        # Flight positions are server-authoritative.  Do not let the normal
        # client fall simulation pull a flying entity back to the ground or
        # play fall/end.ogg when the flight arc lands.
        if mode == "fly":
            self.falling = False
        # start/stop falling if the current tile is air.
        elif getattr(self.game, 'pong_mode', False):
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

    def sync_network_position(self, x, y, z):
        """Update a high-rate network snapshot without touching OpenAL/EFX.

        Voice and reverb resources remain owned by the normal entity lifecycle;
        their regular loop will observe these coordinates safely.  This method is
        intentionally not a substitute for a real movement packet.
        """
        self.x = float(x) if x is not None else float(self.x or 0.0)
        self.y = float(y) if y is not None else float(self.y or 0.0)
        self.z = float(z) if z is not None else float(self.z or 0.0)
        gameplay = getattr(self.game, "gameplay", None)
        camera = getattr(gameplay, "camera", None)
        if getattr(camera, "focus_object", None) is self:
            camera.sync_network_position(self.x, self.y, self.z)

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
                        music_gain = 0.0
                    else:
                        if dist <= min_dist:
                            gain = 1.0
                        elif dist >= max_dist:
                            gain = 0.0
                        else:
                            gain = 1.0 - ((dist - min_dist) / (max_dist - min_dist))

                        music_max = 50.0
                        if dist <= min_dist:
                            music_gain = 1.0
                        elif dist >= music_max:
                            music_gain = 0.0
                        else:
                            music_gain = 1.0 - ((dist - min_dist) / (music_max - min_dist))
                    self.vc_source.gain = gain
                    self.music_source.gain = music_gain if not getattr(self, "muted_by_spectator", False) else 0.0

                    # Real-time filter sync with soundgroup filter state (optimized)
                    current_filter = self.soundgroup.filter[-1] if len(self.soundgroup.filter) > 0 else None
                    if getattr(self, '_last_applied_filter', -1) != current_filter:
                        self._last_applied_filter = current_filter
                        if current_filter is not None:
                            self.vc_source.direct_filter = current_filter
                            self.music_source.direct_filter = current_filter
                        else:
                            try: self.vc_source.direct_filter = None
                            except Exception:
                                try: del self.vc_source.direct_filter
                                except Exception: pass
                            try: self.music_source.direct_filter = None
                            except Exception:
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

                music_src = getattr(self, 'music_source', None)
                if music_src is not None:
                    try:
                        if getattr(music_src, 'buffers_queued', 0) == 0:
                            # Skip the silent keep-alive buffer while a music broadcast
                            # is actively being received — otherwise it interleaves with
                            # real audio and can leave the source stopped/silent after a
                            # broadcaster stop/restart.  The window mirrors the session
                            # reset gap used by MusicCompression.
                            if time.time() - getattr(self, '_music_last_recv', 0) < 0.5:
                                pass
                            else:
                                buffer = self.game.audio_mngr.context.gen_buffer()
                                buffer.set_data(
                                    self.game.audio_mngr.silent_buf,
                                    sample_rate=48000,
                                    format=cyal.BufferFormat.MONO16
                                )
                                music_src.queue_buffers(buffer)
                    except Exception:
                        pass
        # 🛡️ Protection: Skip tile checks if coordinates are None
        if self.x is None or self.y is None or self.z is None:
            return
        gameplay = getattr(self.game, "gameplay", None)
        if getattr(self, "is_user", False) and getattr(gameplay, "vehicle_mode", False):
            # The mounted player's height follows the Server-owned vehicle.
            # Do not also run the legacy local gravity/water-sinking simulator.
            self.falling = False
            self.fall_clock.restart()
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
        # The camera owns the focused player's hearing: it plays the swim
        # sounds (cat="self") and animates the world/voice filter. This entity
        # must not also play the same splash or fight over the same vc_source,
        # or the transition sounds doubled and the filter wobbled.
        is_focus = False
        try:
            if hasattr(self.game, "gameplay"):
                cam = getattr(self.game.gameplay, "camera", None)
                if cam is not None and getattr(cam, "focus_object", None) is self:
                    is_focus = True
        except Exception:
            pass

        # Helper to generate the automation callback with a specific filter
        def create_water_automation(filter_obj, is_focus):
            def automation_water(value):
                # Check if filter was created successfully
                if filter_obj is None:
                    return
                filter_obj.set("GAINHF", value)
                self.soundgroup.apply_filter(filter_obj, replace=True)
                if self.player:
                    if filter_obj is not None:
                        # The camera animates the focused player's voice source
                        # (world filter); touching it here too makes two
                        # automations fight over the same source each tick.
                        if not is_focus and self.vc_source is not None:
                            self.vc_source.direct_filter = filter_obj
                        if self.music_source is not None:
                            self.music_source.direct_filter = filter_obj
                    else:
                        try:
                            if self.vc_source is not None:
                                del self.vc_source.direct_filter
                        except Exception:
                            pass
                        try:
                            if self.music_source is not None:
                                del self.music_source.direct_filter
                        except Exception:
                            pass
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
            # Non-focused entities splash in 3D for everyone nearby; the camera
            # already plays the focused player's own splash (cat="self"), so
            # playing the unbound one too doubles the sound.
            if not is_focus:
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
                    step_callback = create_water_automation(filter_obj, is_focus), start_value=1.0,
                    callback=on_complete
                )
                self._water_automation = task

        if self.in_water and self.map.get_tile_at(self.x, self.y, self.z) != "underwater":
            if not is_focus:
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
                    step_callback = create_water_automation(filter_obj, is_focus), start_value=muffling,
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
                    # Same split as the enter/exit automation: the camera owns
                    # the focused player's voice source, and sources may be
                    # None if OpenAL allocation failed.
                    if not is_focus and self.vc_source is not None:
                        self.vc_source.direct_filter = filter_obj
                    if self.music_source is not None:
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
            
        # Remove from exclude_water to prevent SoundGroup memory leak
        if getattr(self, 'in_water', False):
            try:
                if hasattr(self.game, 'exclude_water') and self.soundgroup in self.game.exclude_water:
                    self.game.exclude_water.remove(self.soundgroup)
            except ValueError:
                pass
                
        # Explicitly delete water_filter
        if getattr(self, 'water_filter', None) is not None:
            try:
                self.water_filter.delete()
            except Exception:
                pass
        self.water_filter = None

        if self.player: 
            self.vc_compression.put(None)
            if hasattr(self, 'music_compression') and self.music_compression:
                try:
                    self.music_compression.close()
                except Exception:
                    pass
                self.music_compression = None
            # Release OpenAL sources for player entities
            for src_name in ['vc_source', 'radio_source', 'music_source']:
                src = getattr(self, src_name, None)
                if src:
                    try:
                        src.stop()
                        src.buffer = None
                        while getattr(src, 'buffers_queued', 0) > 0:
                            src.unqueue_buffers()
                        src.delete()
                    except Exception:
                        pass
                    setattr(self, src_name, None)
            # Return EFX effect slots to pool
            for slot_name in ['distortion_slot', 'eq_slot']:
                slot = getattr(self, slot_name, None)
                if slot:
                    self.soundgroup.parent.release_effect_slot(slot)
                    setattr(self, slot_name, None)
        if self.beacon is not None:
            self.beacon.destroy(force=True)
        super().destroy()
    

