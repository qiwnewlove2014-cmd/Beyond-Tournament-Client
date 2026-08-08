import time

from .entity import Entity


class Motorcycle(Entity):
    """Client presentation for a server-authoritative motorcycle."""

    entity_type = "motorcycle"
    # motorstart.ogg is 1.629 seconds. Starting the loop 0.15 seconds early
    # creates a short crossfade instead of an audible gap at the seam.
    engine_crossfade_start = 1.48

    def __init__(self, game, map, x, y, z, hp=500, name="motorcycle"):
        super().__init__(game, map, x, y, z, hp, name=name)
        self.engine_on = False
        self.rider_name = ""
        self.target_speed = 0.0
        self.current_speed = 0.0
        self._last_audio_update = time.monotonic()
        self._wind_id = f"motorcycle_wind_{name}"
        self._wind_reverb_slot = None
        self._engine_start_at = 0.0
        self._engine_waiting_to_start = False
        self.surface = self._surface_at(x, y, z)
        self._next_terrain_sound_at = 0.0
        self._sync_initial_reverb()

    def _surface_at(self, x, y, z):
        tile = str(self.map.get_tile_at(x, y, z) or "").lower()
        if "water" in tile:
            return "water"
        if "mud" in tile or tile == "wet_ground":
            return "mud"
        if not tile or tile == "air":
            return "air"
        return "ground"

    def _sync_initial_reverb(self):
        """Attach the map's current shared reverb slot even while stationary."""
        reverb = self.map.get_reverb_at(self.x, self.y, self.z)
        slot = reverb.reverb if reverb and reverb.reverb else None
        self.soundgroup.apply_effect(slot, 0)

    def _category_scale(self):
        category = self.game.audio_mngr.volume_categories.get("miscelaneous", [100])
        # Master volume is already applied once by the OpenAL listener.
        return category[0] / 100.0

    def _ensure_engine(self, start_paused=False):
        sound = self.soundgroup.labeled_sources.get("motorcycle_engine")
        if sound and sound.source:
            if start_paused:
                sound.source.gain = 0.0
                sound.source.pause()
            return sound
        sound = self.soundgroup.play(
            "vehicles/motorcycle/engine.ogg",
            looping=True,
            id="motorcycle_engine",
            cat="miscelaneous",
            volume=100,
            pitch=0.75,
        )
        if sound and sound.source:
            sound.source.gain = 0.0
            sound.source.reference_distance = 4.0
            sound.source.max_distance = 55.0
            sound.source.rolloff_factor = 0.65
            if start_paused:
                sound.source.pause()
        return sound

    def _ensure_wind(self):
        # Keep the dry wind wide and stereo-direct, then add only the room tail
        # through an auxiliary EFX send. This preserves headphone width without
        # folding the original signal to mono.
        group = self.game.direct_soundgroup
        sound = group.labeled_sources.get(self._wind_id)
        if sound and sound.source:
            return sound
        sound = group.play(
            "vehicles/motorcycle/wind.ogg",
            looping=True,
            id=self._wind_id,
            cat="miscelaneous",
            volume=100,
        )
        if sound and sound.source:
            sound.source.gain = 0.0
            self._sync_wind_reverb(force=True)
        return sound

    def _sync_wind_reverb(self, force=False):
        group = getattr(self.game, "direct_soundgroup", None)
        sound = group.labeled_sources.get(self._wind_id) if group else None
        if not sound or not sound.source:
            return
        try:
            reverb = self.map.get_reverb_at(self.x, self.y, self.z)
            slot = reverb.reverb if reverb and reverb.reverb else None
            if force or slot != self._wind_reverb_slot:
                self.game.audio_mngr.efx.send(sound.source, 0, slot)
                self._wind_reverb_slot = slot
        except Exception:
            # Leave the last valid routing intact and retry on the next move.
            return

    def _detach_wind_reverb(self, sound=None):
        if self._wind_reverb_slot is None:
            return
        if sound is None:
            group = getattr(self.game, "direct_soundgroup", None)
            sound = group.labeled_sources.get(self._wind_id) if group else None
        if sound and sound.source:
            try:
                self.game.audio_mngr.efx.send(sound.source, 0, None)
            except Exception:
                pass
        self._wind_reverb_slot = None

    def _remove_direct_wind(self):
        group = getattr(self.game, "direct_soundgroup", None)
        if not group:
            return
        sound = group.labeled_sources.pop(self._wind_id, None)
        if sound:
            self._detach_wind_reverb(sound)
            sound.destroy(force=True)

    def detach_environment_effects(self):
        """Detach map-owned EFX sends before their pooled slot is released."""
        self._detach_wind_reverb()
        self.soundgroup.apply_effect(None, 0)

    def move(self, x, y, z, play_sound=True, mode="walk"):
        previous_surface = self.surface
        # Vehicle height is authoritative on the Server. Using fly mode here
        # prevents the generic client-side fall simulator from moving it again.
        super().move(x, y, z, play_sound, "fly")
        self.falling = False
        self.surface = self._surface_at(self.x, self.y, self.z)
        if self.surface == "water" and previous_surface != "water":
            self.play_sound(
                "vehicles/motorcycle/water_land.ogg",
                id="motorcycle_water_land",
                cat="miscelaneous",
                volume=85,
            )
        self._sync_wind_reverb()

    def apply_state(self, speed=0.0, engine_on=False, rider="", facing=None, initial=False):
        was_engine_on = self.engine_on
        self.target_speed = max(0.0, min(1.0, float(speed or 0.0)))
        self.engine_on = bool(engine_on)
        self.rider_name = str(rider or "")
        if facing is not None:
            self.face(float(facing), self.vfacing, self.bfacing, force=True)

        if self.engine_on and not was_engine_on:
            if initial:
                self._ensure_engine()
                self._engine_start_at = time.monotonic()
                self._engine_waiting_to_start = False
            else:
                self._ensure_engine(start_paused=True)
                self.play_sound(
                    "vehicles/motorcycle/start.ogg",
                    id="motorcycle_start",
                    cat="miscelaneous",
                )
                self._engine_start_at = time.monotonic() + self.engine_crossfade_start
                self._engine_waiting_to_start = True
        elif not self.engine_on and was_engine_on:
            was_waiting = self._engine_waiting_to_start
            self._engine_start_at = 0.0
            self._engine_waiting_to_start = False
            self.play_sound(
                "vehicles/motorcycle/stop.ogg",
                id="motorcycle_stop",
                cat="miscelaneous",
            )
            if was_waiting:
                engine = self.soundgroup.labeled_sources.pop("motorcycle_engine", None)
                if engine:
                    engine.destroy(force=True)
        local_name = getattr(getattr(self.game, "gameplay", None), "player", None)
        local_name = getattr(local_name, "name", "")
        if self.engine_on and self.rider_name == local_name:
            self._ensure_wind()

    def loop(self):
        # Entity.loop() contains legacy client-owned gravity. Motorcycle
        # movement, including falling, is replicated by the Server instead.
        self.falling = False
        now = time.monotonic()
        dt = min(0.1, max(0.0, now - self._last_audio_update))
        self._last_audio_update = now
        blend = min(1.0, dt * 5.0)
        self.current_speed += (self.target_speed - self.current_speed) * blend
        scale = self._category_scale()

        engine = self.soundgroup.labeled_sources.get("motorcycle_engine")
        if engine and engine.source:
            engine_ready = self.engine_on and now >= self._engine_start_at
            if self._engine_waiting_to_start:
                if engine_ready:
                    engine.source.gain = 0.0
                    engine.source.play()
                    self._engine_waiting_to_start = False
                else:
                    # SoundGroup volume maintenance may restore gain while the
                    # source is paused; keep it silent until the seam point.
                    engine.source.gain = 0.0
            target_gain = (0.42 + self.current_speed * 0.48) * scale if engine_ready else 0.0
            engine_blend = min(1.0, dt * (9.0 if engine_ready else 5.0))
            engine.source.gain += (target_gain - engine.source.gain) * engine_blend
            engine.source.pitch = 0.75 + self.current_speed * 0.85
            if not self.engine_on and engine.source.gain < 0.01:
                self.soundgroup.labeled_sources.pop("motorcycle_engine", None)
                engine.destroy(force=True)

        local_player = getattr(getattr(self.game, "gameplay", None), "player", None)
        is_local_rider = self.engine_on and self.rider_name == getattr(local_player, "name", "")
        rider_entity = local_player if is_local_rider else self.map.entities.get(self.rider_name)
        if self.engine_on and rider_entity is not None:
            # Keep both local and remote rider presentations from running the
            # legacy fall timer between authoritative vehicle position packets.
            rider_entity.falling = False
            rider_entity.fall_clock.restart()
        wind = self.game.direct_soundgroup.labeled_sources.get(self._wind_id)
        if is_local_rider and not wind:
            wind = self._ensure_wind()
        if wind and wind.source:
            target_wind = max(0.0, (self.current_speed - 0.12) / 0.88) * 0.7 * scale if is_local_rider else 0.0
            wind.source.gain += (target_wind - wind.source.gain) * min(1.0, dt * 3.5)
            wind.source.pitch = 0.82 + self.current_speed * 0.35
            if not is_local_rider and wind.source.gain < 0.01:
                self._remove_direct_wind()

        if (
            self.engine_on
            and self.current_speed > 0.05
            and self.surface in ("water", "mud")
            and now >= self._next_terrain_sound_at
        ):
            sound_name = (
                "water_resistance.ogg"
                if self.surface == "water"
                else "mud_resistance.ogg"
            )
            self.play_sound(
                f"vehicles/motorcycle/{sound_name}",
                id="motorcycle_terrain_resistance",
                cat="miscelaneous",
                volume=55 + int(self.current_speed * 25),
                pitch=0.9 + self.current_speed * 0.2,
            )
            # Both selected clips are about 0.8 seconds long. Never replace a
            # still-playing terrain source, avoiding pops and OpenAL churn.
            self._next_terrain_sound_at = now + max(
                0.85, 1.25 - self.current_speed * 0.4
            )

    def destroy(self):
        self.detach_environment_effects()
        self._remove_direct_wind()
        super().destroy()
