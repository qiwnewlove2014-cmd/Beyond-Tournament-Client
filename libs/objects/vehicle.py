import math
import time

from .entity import Entity


class Vehicle(Entity):
    """Client presentation for a server-authoritative vehicle."""

    entity_type = "vehicle"
    is_vehicle = True

    def __init__(
        self,
        game,
        map,
        x,
        y,
        z,
        hp=500,
        name="vehicle",
        vehicle_type="motorcycle",
        sound_profile=None,
        audio_profile=None,
    ):
        super().__init__(game, map, x, y, z, hp, name=name)
        self.vehicle_type = self._safe_profile_id(vehicle_type, "motorcycle")
        self.sound_profile = self._safe_profile_id(
            sound_profile, self.vehicle_type
        )
        profile = audio_profile if isinstance(audio_profile, dict) else {}
        self.engine_crossfade_start = self._audio_value(
            profile, "engineCrossfadeStart", 1.48, 0.0, 30.0
        )
        self.engine_idle_pitch = self._audio_value(
            profile, "engineIdlePitch", 0.75, 0.1, 4.0
        )
        self.engine_max_pitch = self._audio_value(
            profile, "engineMaxPitch", 1.6, 0.1, 4.0
        )
        self.engine_idle_gain = self._audio_value(
            profile, "engineIdleGain", 0.42, 0.0, 1.0
        )
        self.engine_max_gain = self._audio_value(
            profile, "engineMaxGain", 0.9, 0.0, 1.0
        )
        self.wind_max_gain = self._audio_value(
            profile, "windMaxGain", 0.7, 0.0, 1.0
        )
        self.engine_on = False
        self.rider_name = ""
        self.target_speed = 0.0
        self.current_speed = 0.0
        self._last_audio_update = time.monotonic()
        self._wind_id = f"vehicle_wind_{name}"
        self._wind_reverb_slot = None
        self._engine_start_at = 0.0
        self._engine_waiting_to_start = False
        self.surface = self._surface_at(x, y, z)
        self._next_terrain_sound_at = 0.0
        self._sync_initial_reverb()

    @staticmethod
    def _safe_profile_id(value, fallback):
        normalized = str(value or "").strip().lower()
        if normalized and all(c in "abcdefghijklmnopqrstuvwxyz0123456789_" for c in normalized):
            return normalized
        return fallback

    @staticmethod
    def _audio_value(profile, key, fallback, minimum, maximum):
        try:
            value = float(profile.get(key, fallback))
        except (TypeError, ValueError):
            return fallback
        if not math.isfinite(value):
            return fallback
        return max(minimum, min(maximum, value))

    def _sound(self, filename):
        return f"vehicles/{self.sound_profile}/{filename}"

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
        sound = self.soundgroup.labeled_sources.get("vehicle_engine")
        if sound and sound.source:
            if start_paused:
                sound.source.gain = 0.0
                sound.source.pause()
            return sound
        sound = self.soundgroup.play(
            self._sound("engine.ogg"),
            looping=True,
            id="vehicle_engine",
            cat="miscelaneous",
            volume=100,
            pitch=self.engine_idle_pitch,
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
            self._sound("wind.ogg"),
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
                self._sound("water_land.ogg"),
                id="vehicle_water_land",
                cat="miscelaneous",
                volume=85,
            )
        self._sync_wind_reverb()

    def apply_state(self, speed=0.0, engine_on=False, rider="", facing=None, initial=False):
        was_engine_on = self.engine_on
        try:
            normalized_speed = float(speed or 0.0)
        except (TypeError, ValueError):
            normalized_speed = 0.0
        if not math.isfinite(normalized_speed):
            normalized_speed = 0.0
        self.target_speed = max(0.0, min(1.0, normalized_speed))
        self.engine_on = bool(engine_on)
        self.rider_name = str(rider or "")
        if facing is not None:
            try:
                safe_facing = float(facing)
            except (TypeError, ValueError):
                safe_facing = 0.0
            if math.isfinite(safe_facing):
                self.face(safe_facing, self.vfacing, self.bfacing, force=True)

        if self.engine_on and not was_engine_on:
            if initial:
                self._ensure_engine()
                self._engine_start_at = time.monotonic()
                self._engine_waiting_to_start = False
            else:
                self._ensure_engine(start_paused=True)
                self.play_sound(
                    self._sound("start.ogg"),
                    id="vehicle_start",
                    cat="miscelaneous",
                )
                self._engine_start_at = time.monotonic() + self.engine_crossfade_start
                self._engine_waiting_to_start = True
        elif not self.engine_on and was_engine_on:
            was_waiting = self._engine_waiting_to_start
            self._engine_start_at = 0.0
            self._engine_waiting_to_start = False
            self.play_sound(
                self._sound("stop.ogg"),
                id="vehicle_stop",
                cat="miscelaneous",
            )
            if was_waiting:
                engine = self.soundgroup.labeled_sources.pop("vehicle_engine", None)
                if engine:
                    engine.destroy(force=True)
        local_name = getattr(getattr(self.game, "gameplay", None), "player", None)
        local_name = getattr(local_name, "name", "")
        if self.engine_on and self.rider_name == local_name:
            self._ensure_wind()

    def loop(self):
        # Entity.loop() contains legacy client-owned gravity. Vehicle
        # movement, including falling, is replicated by the Server instead.
        self.falling = False
        now = time.monotonic()
        dt = min(0.1, max(0.0, now - self._last_audio_update))
        self._last_audio_update = now
        blend = min(1.0, dt * 5.0)
        self.current_speed += (self.target_speed - self.current_speed) * blend
        scale = self._category_scale()

        engine = self.soundgroup.labeled_sources.get("vehicle_engine")
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
            target_gain = (
                self.engine_idle_gain
                + self.current_speed * (self.engine_max_gain - self.engine_idle_gain)
            ) * scale if engine_ready else 0.0
            engine_blend = min(1.0, dt * (9.0 if engine_ready else 5.0))
            engine.source.gain += (target_gain - engine.source.gain) * engine_blend
            engine.source.pitch = (
                self.engine_idle_pitch
                + self.current_speed * (self.engine_max_pitch - self.engine_idle_pitch)
            )
            if not self.engine_on and engine.source.gain < 0.01:
                self.soundgroup.labeled_sources.pop("vehicle_engine", None)
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
            target_wind = (
                max(0.0, (self.current_speed - 0.12) / 0.88)
                * self.wind_max_gain
                * scale
                if is_local_rider else 0.0
            )
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
                self._sound(sound_name),
                id="vehicle_terrain_resistance",
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
