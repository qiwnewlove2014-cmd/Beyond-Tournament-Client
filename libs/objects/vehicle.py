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
        # Enable wall occlusion filtering for all vehicle 3D sounds (engine, horn, brake, terrain)
        self.soundgroup.filterable = True
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
        # Brake loop: pitch rides the wheel speed (high -> low as the truck
        # slows), so high speeds scream and the note drops while braking.
        self.brake_gain = self._audio_value(profile, "brakeGain", 0.85, 0.0, 1.0)
        self.brake_pitch_min = self._audio_value(profile, "brakePitchMin", 0.55, 0.1, 4.0)
        self.brake_pitch_max = self._audio_value(profile, "brakePitchMax", 1.3, 0.1, 4.0)
        # Long-vehicle layout: when the server audio profile carries a body
        # length, the engine is rendered from a row of spatial sources that
        # trail the driven path (see _build_engine_layout). 0 = classic
        # single-point engine (motorcycle).
        self.source_length = self._audio_value(
            profile, "sourceLength", 0.0, 0.0, 30.0
        )
        self.source_width = self._audio_value(
            profile, "sourceWidth", 0.0, 0.0, 10.0
        )
        self.trailer_lag_ms = self._audio_value(
            profile, "trailerLagMs", 0.0, 0.0, 3000.0
        )
        # Cabin interior audio (truck2): the exterior engine is muffled for
        # the local rider while separate stereo interior loops play through
        # the direct soundgroup; everyone else hears the exterior normally.
        self.interior_audio = bool(profile.get("interiorAudio"))
        self.interior_ext_scale = self._audio_value(
            profile, "interiorExtScale", 0.25, 0.0, 1.0
        )
        # The exterior DRIVE loop is killed for the cab driver (only outside
        # listeners hear the full drive) while the muffled idle stays faint.
        self.interior_ext_drive_scale = self._audio_value(
            profile, "interiorExtDriveScale", 0.0, 0.0, 1.0
        )
        self.interior_gain = self._audio_value(
            profile, "interiorGain", 0.9, 0.0, 1.0
        )
        # Smooth cabin context fade on enter/exit (~0.6s) instead of snapping
        # between outside and inside sound instantly.
        self._cabin_fade = 0.0
        self.cabin_fade_rate = 1.5
        # File names come from the server registry (safeFileName-validated).
        self.engine_idle_ext = str(profile.get("engineIdleExt", "engine.ogg"))
        self.engine_drive_ext = str(profile.get("engineDriveExt", "engine.ogg"))
        self.engine_idle_int = str(profile.get("engineIdleInt", "engine.ogg"))
        self.engine_drive_int = str(profile.get("engineDriveInt", "engine.ogg"))
        self.start_ext = str(profile.get("startExt", "start.ogg"))
        self.start_int = str(profile.get("startInt", "start.ogg"))
        self.stop_ext = str(profile.get("stopExt", "stop.ogg"))
        self.stop_int = str(profile.get("stopInt", "stop.ogg"))
        self._int_idle_id = f"vehicle_int_idle_{name}"
        self._int_drive_id = f"vehicle_int_drive_{name}"
        self._pos_history = []
        self._engine_sources = self._build_engine_layout()
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
        # Horn / brake loops are driven by server state (horn_on / brake_on /
        # revving) so every client hears the same horn blast and brake squeal.
        self._horn_id = f"vehicle_horn_{name}"
        self._brake_id = f"vehicle_brake_{name}"
        self.horn_on = False
        self.brake_on = False
        self.revving = False
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

    def _build_engine_layout(self):
        """4-source long-vehicle layout (front axle + rear axle, wide apart).

        Front sources sit at the cab with no lag; rear sources trail the
        driven path by trailer_lag_ms, so the rumble follows the wheels around
        corners — the front leads and the back follows, like a real long
        truck. sourceLength <= 0 keeps the classic single-point engine.
        """
        if self.source_length <= 0:
            return []
        half = self.source_length / 2.0
        halfw = self.source_width / 2.0
        return [
            {"label": "vehicle_engine_fl", "fwd": half, "lat": -halfw, "lag_ms": 0.0},
            {"label": "vehicle_engine_fr", "fwd": half, "lat": halfw, "lag_ms": 0.0},
            {"label": "vehicle_engine_rl", "fwd": -half, "lat": -halfw, "lag_ms": self.trailer_lag_ms},
            {"label": "vehicle_engine_rr", "fwd": -half, "lat": halfw, "lag_ms": self.trailer_lag_ms},
        ]

    def _push_history(self):
        now = time.monotonic()
        self._pos_history.append((now, self.x, self.y, self.z, self.hfacing))
        cutoff = now - 2.0
        while self._pos_history and self._pos_history[0][0] < cutoff:
            self._pos_history.pop(0)

    def _sample_position(self, lag_ms):
        """Newest path sample at or before now - lag_ms (None if too new)."""
        if lag_ms <= 0 or not self._pos_history:
            return None
        target = time.monotonic() - lag_ms / 1000.0
        best = None
        for sample in self._pos_history:
            if sample[0] <= target:
                best = sample
            else:
                break
        return best

    @staticmethod
    def _offset_position(x, y, z, facing, fwd, lat):
        # movement.move() convention: forward = (sin, cos), so the right-hand
        # perpendicular is (cos, -sin).
        rad = math.radians(facing)
        fx = math.sin(rad)
        fy = math.cos(rad)
        rx = math.cos(rad)
        ry = -math.sin(rad)
        return (x + fx * fwd + rx * lat, y + fy * fwd + ry * lat, z)

    def _engine_source_positions(self):
        positions = {}
        if not self._engine_sources:
            return positions
        now = time.monotonic()
        for cfg in self._engine_sources:
            sample = self._sample_position(cfg["lag_ms"])
            if sample is None:
                sample = (now, self.x, self.y, self.z, self.hfacing)
            positions[cfg["label"]] = self._offset_position(
                sample[1], sample[2], sample[3], sample[4], cfg["fwd"], cfg["lat"]
            )
        return positions

    def _engine_labels(self):
        """World-engine source labels (layout points or the single point)."""
        if self._engine_sources:
            return [cfg["label"] for cfg in self._engine_sources]
        return ["vehicle_engine"]

    def _engine_loop_file(self, drive=False, interior=False):
        """Which engine loop file to use for a world/interior idle/drive slot."""
        if self.interior_audio:
            if interior:
                return self._sound(
                    self.engine_drive_int if drive else self.engine_idle_int
                )
            return self._sound(
                self.engine_drive_ext if drive else self.engine_idle_ext
            )
        return self._sound("engine.ogg")

    def _ensure_engine_loop(self, label, filename, start_paused):
        sound = self.soundgroup.labeled_sources.get(label)
        if sound and sound.source:
            if start_paused:
                sound.source.gain = 0.0
                sound.source.pause()
            return sound
        sound = self.soundgroup.play(
            filename,
            looping=True,
            id=label,
            cat="miscelaneous",
            volume=100,
            pitch=self.engine_idle_pitch,
        )
        if sound and sound.source:
            sound.source.gain = 0.0
            sound.source.reference_distance = 4.0
            if self._engine_sources:
                sound.source.max_distance = 60.0
                sound.source.rolloff_factor = 0.6
            else:
                sound.source.max_distance = 55.0
                sound.source.rolloff_factor = 0.65
            if start_paused:
                sound.source.pause()
        return sound

    def _is_local_rider(self):
        local_name = getattr(getattr(self.game, "gameplay", None), "player", None)
        local_name = getattr(local_name, "name", "")
        return bool(local_name) and self.rider_name == local_name

    def _ensure_interior_loops(self, start_paused=False):
        group = getattr(self.game, "direct_soundgroup", None)
        if group is None:
            return
        for key, drive in (
            (self._int_idle_id, False),
            (self._int_drive_id, True),
        ):
            sound = group.labeled_sources.get(key)
            if sound and sound.source:
                if start_paused:
                    sound.source.gain = 0.0
                    sound.source.pause()
                continue
            sound = group.play(
                self._engine_loop_file(drive, True),
                looping=True,
                id=key,
                cat="miscelaneous",
                volume=100,
                pitch=self.engine_max_pitch if drive else self.engine_idle_pitch,
            )
            if sound and sound.source:
                sound.source.gain = 0.0
                if start_paused:
                    sound.source.pause()

    def _destroy_interior_loops(self):
        group = getattr(self.game, "direct_soundgroup", None)
        if group is None:
            return
        for key in (self._int_idle_id, self._int_drive_id):
            gone = group.labeled_sources.pop(key, None)
            if gone:
                gone.destroy(force=True)

    def _update_engine_positions(self):
        for label, pos in self._engine_source_positions().items():
            for key in (label, label + "_drive") if self.interior_audio else (label,):
                snd = self.soundgroup.labeled_sources.get(key)
                if snd and snd.source:
                    snd.source.position = pos

    def _front_offset(self):
        """World-space offset toward the cab (used for start/stop sounds)."""
        if not self._engine_sources:
            return (0.0, 0.0, 0.0)
        rad = math.radians(self.hfacing)
        fwd = self.source_length / 2.0
        return (math.sin(rad) * fwd, math.cos(rad) * fwd, 0.0)

    def _engine_sounds(self):
        """Currently alive world engine sources (single-point or multi-layout)."""
        result = []
        for label in self._engine_labels():
            for key in (label, label + "_drive") if self.interior_audio else (label,):
                sound = self.soundgroup.labeled_sources.get(key)
                if sound and sound.source:
                    result.append(sound)
        return result

    def _destroy_engine_sounds(self):
        for label in self._engine_labels():
            for key in (label, label + "_drive") if self.interior_audio else (label,):
                gone = self.soundgroup.labeled_sources.pop(key, None)
                if gone:
                    gone.destroy(force=True)
        self._destroy_interior_loops()

    def _ensure_engine(self, start_paused=False):
        for label in self._engine_labels():
            self._ensure_engine_loop(
                label, self._engine_loop_file(False, False), start_paused
            )
            if self.interior_audio:
                self._ensure_engine_loop(
                    label + "_drive",
                    self._engine_loop_file(True, False),
                    start_paused,
                )
        if self.interior_audio and self._is_local_rider():
            self._ensure_interior_loops(start_paused)
        self._update_engine_positions()
        return self._engine_sounds()

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
        super().detach_environment_effects()

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

    def apply_state(
        self,
        speed=0.0,
        engine_on=False,
        rider="",
        facing=None,
        initial=False,
        brake_on=False,
        horn_on=False,
        revving=False,
    ):
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

        # Record this position packet as a path sample with the correct facing
        # (apply_state runs after move()), then park the trailing engine
        # sources on the path the front has already driven.
        self._push_history()
        self._update_engine_positions()

        if self.engine_on and not was_engine_on:
            if initial:
                self._ensure_engine()
                self._engine_start_at = time.monotonic()
                self._engine_waiting_to_start = False
            else:
                self._ensure_engine(start_paused=True)
                rel = self._front_offset()
                if self.interior_audio and self._is_local_rider():
                    # Cabin start is stereo + direct; no 3D placement needed.
                    group = getattr(self.game, "direct_soundgroup", None)
                    if group:
                        group.play(
                            self._sound(self.start_int),
                            cat="miscelaneous",
                            volume=100,
                        )
                else:
                    self.play_sound(
                        self._sound(self.start_ext if self.interior_audio else "start.ogg"),
                        id="vehicle_start",
                        cat="miscelaneous",
                        rel_x=rel[0],
                        rel_y=rel[1],
                    )
                self._engine_start_at = time.monotonic() + self.engine_crossfade_start
                self._engine_waiting_to_start = True
        elif not self.engine_on and was_engine_on:
            was_waiting = self._engine_waiting_to_start
            self._engine_start_at = 0.0
            self._engine_waiting_to_start = False
            rel = self._front_offset()
            if self.interior_audio and self._is_local_rider():
                group = getattr(self.game, "direct_soundgroup", None)
                if group:
                    group.play(
                        self._sound(self.stop_int),
                        cat="miscelaneous",
                        volume=100,
                    )
            else:
                self.play_sound(
                    self._sound(self.stop_ext if self.interior_audio else "stop.ogg"),
                    id="vehicle_stop",
                    cat="miscelaneous",
                    rel_x=rel[0],
                    rel_y=rel[1],
                )
            if was_waiting:
                self._destroy_engine_sounds()
        local_name = getattr(getattr(self.game, "gameplay", None), "player", None)
        local_name = getattr(local_name, "name", "")
        if self.engine_on and self.rider_name == local_name:
            self._ensure_wind()

        # Horn: create/destroy the looping blast on the press/release edges so
        # both a short tap and a long hold sound natural.
        if horn_on and not self.horn_on:
            snd = self.soundgroup.play(
                self._sound("horn.ogg"),
                looping=True,
                id=self._horn_id,
                cat="miscelaneous",
                volume=100,
            )
            if snd and snd.source:
                snd.source.reference_distance = 4.0
                snd.source.max_distance = 60.0
                snd.source.rolloff_factor = 0.6
        elif not horn_on and self.horn_on:
            gone = self.soundgroup.labeled_sources.pop(self._horn_id, None)
            if gone:
                gone.destroy(force=True)
        self.horn_on = bool(horn_on)
        self.brake_on = bool(brake_on)
        self.revving = bool(revving)

    def loop(self):
        # Entity.loop() contains legacy client-owned gravity. Vehicle
        # movement, including falling, is replicated by the Server instead.
        self.falling = False
        now = time.monotonic()
        dt = min(0.1, max(0.0, now - self._last_audio_update))
        self._last_audio_update = now
        blend = min(1.0, dt * 5.0)
        if self.revving:
            # Burnout (gas + brake): the server reports 0 wheel speed but we
            # freeze the displayed speed at the level it had when the burnout
            # started — a free-wheel rev that holds, neither decaying to idle
            # nor jumping to max. Releasing the brake resumes from here.
            pass
        else:
            self.current_speed += (self.target_speed - self.current_speed) * blend
        scale = self._category_scale()

        # Long vehicles: keep every source parked on the driven path (samples
        # age out, so re-park each tick even while stationary).
        self._update_engine_positions()
        engine_sounds = self._engine_sounds()
        if engine_sounds:
            engine_ready = self.engine_on and now >= self._engine_start_at
            if self._engine_waiting_to_start:
                if engine_ready:
                    for snd in engine_sounds:
                        snd.source.gain = 0.0
                        snd.source.play()
                    # Cabin loops are created paused with the world engine;
                    # unpause them at the same seam so interior/exterior stay
                    # in sync.
                    group = getattr(self.game, "direct_soundgroup", None)
                    if group is not None:
                        for key in (self._int_idle_id, self._int_drive_id):
                            snd = group.labeled_sources.get(key)
                            if snd and snd.source:
                                snd.source.gain = 0.0
                                snd.source.play()
                    self._engine_waiting_to_start = False
                else:
                    # SoundGroup volume maintenance may restore gain while the
                    # source is paused; keep it silent until the seam point.
                    for snd in engine_sounds:
                        snd.source.gain = 0.0
            # Burnout: brake + gas held together — the truck stays put but the
            # engine revs at the frozen free-wheel level (see current_speed
            # freeze above), so pitch/gain keep whatever the driver had.
            engine_speed = self.current_speed
            engine_blend = min(1.0, dt * (9.0 if engine_ready else 5.0))
            is_local_rider = self.interior_audio and self._is_local_rider()
            if self.interior_audio:
                # Cabin fade: eases 0→1 while the local player rides and 1→0
                # when they climb out, so entering/exiting crossfades the
                # outside/inside mix instead of snapping instantly.
                cabin_target = 1.0 if is_local_rider else 0.0
                self._cabin_fade += (
                    (cabin_target - self._cabin_fade)
                    * min(1.0, dt * self.cabin_fade_rate)
                )
                cabin = self._cabin_fade
                # The local rider hears the exterior idle muffled
                # (interiorExtScale) and the exterior DRIVE loop killed
                # entirely (interiorExtDriveScale) — the full drive is for
                # outside listeners only. Non-riders always get the full
                # exterior at full volume.
                idle_ext_scale = 1.0 - cabin * (1.0 - self.interior_ext_scale)
                drive_ext_scale = 1.0 - cabin * (1.0 - self.interior_ext_drive_scale)
                any_audible = False
                for label in self._engine_labels():
                    idle_snd = self.soundgroup.labeled_sources.get(label)
                    drive_snd = self.soundgroup.labeled_sources.get(label + "_drive")
                    for snd, drive in ((idle_snd, False), (drive_snd, True)):
                        if not (snd and snd.source):
                            continue
                        ext_scale = drive_ext_scale if drive else idle_ext_scale
                        target_gain = (
                            self.engine_idle_gain
                            + engine_speed
                            * (self.engine_max_gain - self.engine_idle_gain)
                        ) * scale * ext_scale * (
                            engine_speed if drive else (1.0 - engine_speed)
                        ) if engine_ready else 0.0
                        snd.source.gain += (
                            (target_gain - snd.source.gain) * engine_blend
                        )
                        snd.source.pitch = (
                            self.engine_idle_pitch
                            + engine_speed
                            * (self.engine_max_pitch - self.engine_idle_pitch)
                        )
                        if snd.source.gain >= 0.01:
                            any_audible = True
                # Stereo cabin loops (local rider only). They stay alive through
                # the fade-out so leaving the cab fades instead of cutting.
                if cabin > 0.02:
                    self._ensure_interior_loops()
                    group = getattr(self.game, "direct_soundgroup", None)
                    if group is not None:
                        for key, drive in (
                            (self._int_idle_id, False),
                            (self._int_drive_id, True),
                        ):
                            snd = group.labeled_sources.get(key)
                            if not (snd and snd.source):
                                continue
                            target_gain = (
                                self.interior_gain * scale * cabin
                                * (engine_speed if drive else (1.0 - engine_speed))
                            ) if engine_ready else 0.0
                            snd.source.gain += (
                                (target_gain - snd.source.gain) * engine_blend
                            )
                            snd.source.pitch = (
                                self.engine_idle_pitch
                                + engine_speed
                                * (self.engine_max_pitch - self.engine_idle_pitch)
                            )
                else:
                    self._destroy_interior_loops()
                if not self.engine_on and not any_audible:
                    self._destroy_engine_sounds()
            else:
                target_gain = (
                    self.engine_idle_gain
                    + engine_speed * (self.engine_max_gain - self.engine_idle_gain)
                ) * scale if engine_ready else 0.0
                any_audible = False
                for snd in engine_sounds:
                    snd.source.gain += (target_gain - snd.source.gain) * engine_blend
                    snd.source.pitch = (
                        self.engine_idle_pitch
                        + engine_speed * (self.engine_max_pitch - self.engine_idle_pitch)
                    )
                    if snd.source.gain >= 0.01:
                        any_audible = True
                if not self.engine_on and not any_audible:
                    self._destroy_engine_sounds()

        # Brake loop: while the pedal is held, a looping brake sound whose
        # pitch follows the wheel speed — high at speed, dropping as the truck
        # decelerates (and low during a stationary burnout).
        if self.brake_on:
            snd = self.soundgroup.labeled_sources.get(self._brake_id)
            if not (snd and snd.source):
                snd = self.soundgroup.play(
                    self._sound("brake.ogg"),
                    looping=True,
                    id=self._brake_id,
                    cat="miscelaneous",
                    volume=100,
                )
                if snd and snd.source:
                    snd.source.gain = self.brake_gain * scale
                    snd.source.reference_distance = 4.0
                    snd.source.max_distance = 60.0
                    snd.source.rolloff_factor = 0.6
            if snd and snd.source:
                snd.source.pitch = (
                    self.brake_pitch_min
                    + self.current_speed
                    * (self.brake_pitch_max - self.brake_pitch_min)
                )
        else:
            gone = self.soundgroup.labeled_sources.pop(self._brake_id, None)
            if gone:
                gone.destroy(force=True)

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
            # No wind noise during a stationary burnout — the wheels are
            # stopped even though the engine revs.
            target_wind = (
                max(0.0, (self.current_speed - 0.12) / 0.88)
                * self.wind_max_gain
                * scale
                if (is_local_rider and not self.revving) else 0.0
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
        self._destroy_interior_loops()
        for label in (self._horn_id, self._brake_id):
            gone = self.soundgroup.labeled_sources.pop(label, None)
            if gone:
                gone.destroy(force=True)
        super().destroy()
