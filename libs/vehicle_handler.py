"""
Vehicle / Horse handler — extracted from Gameplay to keep gameplay.py
focused on core loop orchestration.  Owns vehicle state, key mapping,
horn, truck command menu, and horse wind audio.

Usage from Gameplay::

    self.vehicle = VehicleHandler(self)
    # in update():
    self.vehicle.update_wind()
    # in event loop:
    if self.vehicle.active and self.vehicle.handle_event(event):
        continue  # consumed
    # session:
    self.vehicle.set_session(data)
"""

import time
from functools import partial

import pygame

from . import consts, menu
from .speech import speak


class VehicleHandler:
    """Manages vehicle/horse input, audio, and network replication."""

    def __init__(self, gameplay):
        self._gp = gameplay          # back-reference to Gameplay
        self.active = False
        self.name = None
        self.vehicle_type = None
        self._keys_down = set()
        self._last_input = (0, 0, False, False)
        self._horn_down = False

        # Horse wind state
        self._horse_wind_gain = 0.0
        self._horse_sprint_duration = 0.0
        self._last_horse_wind_time = time.monotonic()

        # Motorcycle compatibility mirrors
        self.motorcycle_mode = False
        self.motorcycle_name = None

        # Truck command menu
        self._truck_command_menu = None

    @property
    def _game(self):
        return self._gp.game

    @property
    def _keyconfig(self):
        return self._gp.kc

    # ------------------------------------------------------------------
    # session management
    # ------------------------------------------------------------------

    def set_session(self, data):
        """Enter or exit a vehicle session."""
        active = bool(data.get("active", False))
        self.active = active
        self.name = str(data.get("name", "")) if active else None
        self.vehicle_type = str(data.get("vehicle_type", "vehicle")) if active else None
        self._keys_down.clear()
        self._last_input = (0, 0, False, False)
        self._horn_down = False
        self.motorcycle_mode = active and self.vehicle_type == "motorcycle"
        self.motorcycle_name = self.name if self.motorcycle_mode else None
        if not active:
            # Clean up horse wind audio immediately and completely upon dismount
            self._horse_wind_gain = 0.0
            self._horse_sprint_duration = 0.0
            group = getattr(self._game, "direct_soundgroup", None)
            if group:
                wind_sound = group.labeled_sources.pop("horse_wind", None)
                if wind_sound:
                    wind_sound.destroy(force=True)
            self._close_truck_command_menu()
            return
        # Suppress any normal held-key movement that was active while mounting.
        self._gp.running = False
        # The command menu is for engine vehicles only — horses send the same
        # vehicle_session packet but have no engine to start.
        if active and self.vehicle_type in ("motorcycle", "truck", "truck2"):
            self._open_truck_command_menu()

    def set_motorcycle_session(self, data):
        """Compatibility shim for motorcycle-only session packets."""
        session_data = dict(data or {})
        session_data.setdefault("vehicle_type", "motorcycle")
        self.set_session(session_data)

    # ------------------------------------------------------------------
    # truck command menu
    # ------------------------------------------------------------------

    def _open_truck_command_menu(self):
        """In-cab command menu for every drivable vehicle."""
        if self._truck_command_menu is not None:
            return
        if not (self.active and self.name):
            return
        sound = "vehicles/truck2/truck_command.ogg"
        title = "Vehicle Command"
        m = menu.Menu(
            self._game,
            title,
            parrent=self._gp,
            wrapping=True,
        )
        m.set_sounds(click=sound, enter=sound, open=sound, close=sound)
        m.add_items(
            [
                ("Start Engine", partial(self._truck_command_action, "start")),
                ("Stop Engine", partial(self._truck_command_action, "stop")),
                ("Get Out", partial(self._truck_command_action, "get_out")),
                ("Close Menu", self._close_truck_command_menu),
            ]
        )
        self._truck_command_menu = m
        self._gp.add_substate(m)

    def _close_truck_command_menu(self):
        m = self._truck_command_menu
        if m is None:
            return
        self._truck_command_menu = None
        if self._gp.substates and self._gp.substates[-1] is m:
            self._gp.pop_last_substate()

    def _truck_command_action(self, command):
        if command == "get_out":
            self._gp.interact(0)
        elif self._game.network and self.name:
            self._game.network.send(
                consts.CHANNEL_MISC,
                "vehicle_command",
                {"name": self.name, "command": command},
            )
        self._close_truck_command_menu()

    # ------------------------------------------------------------------
    # horse wind audio
    # ------------------------------------------------------------------

    def update_wind(self):
        """Update horse wind audio based on sprint duration."""
        is_riding_horse = self.active and self.vehicle_type == "horse"
        is_galloping = is_riding_horse and ("forward" in self._keys_down) and ("sprint" in self._keys_down)

        now = time.monotonic()
        dt = min(0.1, max(0.0, now - self._last_horse_wind_time))
        self._last_horse_wind_time = now

        if is_galloping:
            self._horse_sprint_duration += dt
        else:
            self._horse_sprint_duration = 0.0

        # Wind ONLY activates after sustaining gallop sprint through 2 strides (> 1.35 seconds)
        if self._horse_sprint_duration >= 1.35:
            progress = min(1.0, (self._horse_sprint_duration - 1.35) / 1.6)
            target_gain = progress * 0.50
        else:
            target_gain = 0.0

        # Smooth 60fps interpolation
        blend = min(1.0, dt * (1.6 if target_gain > self._horse_wind_gain else 4.0))
        self._horse_wind_gain += (target_gain - self._horse_wind_gain) * blend

        group = getattr(self._game, "direct_soundgroup", None)
        if not group:
            return

        wind_sound = group.labeled_sources.get("horse_wind")
        if self._horse_wind_gain > 0.01 and is_riding_horse:
            if not wind_sound or not wind_sound.source:
                wind_sound = group.play(
                    "vehicles/motorcycle/wind.ogg",
                    looping=True,
                    id="horse_wind",
                    cat="miscelaneous",
                    volume=0,
                )
            if wind_sound and wind_sound.source:
                master_cat = (group.parent.volume_categories.get("miscelaneous", [100])[0] / 100.0) if hasattr(group, "parent") else 1.0
                wind_sound.source.gain = self._horse_wind_gain * master_cat
                wind_sound.source.pitch = 0.85 + self._horse_wind_gain * 0.3
        else:
            if wind_sound:
                group.labeled_sources.pop("horse_wind", None)
                wind_sound.destroy(force=True)

    # ------------------------------------------------------------------
    # key mapping & input
    # ------------------------------------------------------------------

    def _key_role(self, key):
        """Map a pygame key to a vehicle role (forward/backward/left/right/sprint)."""
        forward = {self._keyconfig.get("move_forward", pygame.K_w), pygame.K_UP}
        backward = {self._keyconfig.get("move_backward", pygame.K_s), pygame.K_DOWN}
        left = {self._keyconfig.get("turn_left", pygame.K_a), pygame.K_LEFT}
        right = {self._keyconfig.get("turn_right", pygame.K_d), pygame.K_RIGHT}
        sprint = {self._keyconfig.get("sprint", pygame.K_LSHIFT), pygame.K_LSHIFT, pygame.K_RSHIFT}
        if key in forward:
            return "forward"
        if key in backward:
            return "backward"
        if key in left:
            return "left"
        if key in right:
            return "right"
        if key in sprint:
            return "sprint"
        return None

    def _send_input(self):
        """Send current vehicle input state to the server."""
        throttle = int("forward" in self._keys_down) - int("backward" in self._keys_down)
        steer = int("right" in self._keys_down) - int("left" in self._keys_down)
        sprint = bool("sprint" in self._keys_down)
        brake = "backward" in self._keys_down
        current = (throttle, steer, sprint, brake)
        if current == self._last_input or not self.name:
            return
        self._last_input = current
        event = "motorcycle_input" if self.vehicle_type == "motorcycle" else "vehicle_input"
        self._game.network.send(
            consts.CHANNEL_MOVEMENT,
            event,
            {"name": self.name, "throttle": throttle, "steer": steer, "sprint": sprint, "brake": brake},
        )

    # ------------------------------------------------------------------
    # event handling (called from Gameplay.update)
    # ------------------------------------------------------------------

    def handle_event(self, event):
        """Process a single pygame event.  Returns True if consumed."""
        if not self.active:
            return False

        if event.type not in (pygame.KEYDOWN, pygame.KEYUP):
            return False

        horn_keys = {pygame.K_SPACE, self._keyconfig.get("horn", pygame.K_h)}
        if event.key in horn_keys:
            if event.type == pygame.KEYDOWN and not self._horn_down:
                self._horn_down = True
                if self.name:
                    horn_event = "motorcycle_horn" if self.vehicle_type == "motorcycle" else "vehicle_horn"
                    self._game.network.send(
                        consts.CHANNEL_MOVEMENT,
                        horn_event,
                        {"name": self.name},
                    )
            elif event.type == pygame.KEYUP and self._horn_down:
                self._horn_down = False
                if self.name:
                    horn_release_event = (
                        "motorcycle_horn_release"
                        if self.vehicle_type == "motorcycle"
                        else "vehicle_horn_release"
                    )
                    self._game.network.send(
                        consts.CHANNEL_MOVEMENT,
                        horn_release_event,
                        {"name": self.name},
                    )
            return True

        role = self._key_role(event.key)
        if role is None:
            return False

        if event.type == pygame.KEYDOWN:
            self._keys_down.add(role)
        elif event.type == pygame.KEYUP:
            self._keys_down.discard(role)
        self._send_input()
        return True

    # ------------------------------------------------------------------
    # cleanup
    # ------------------------------------------------------------------

    def cleanup(self):
        """Clean up on exit."""
        self._close_truck_command_menu()

    # ------------------------------------------------------------------
    # compatibility shims — kept so Gameplay can still delegate directly
    # ------------------------------------------------------------------

    def set_vehicle_session(self, data):
        self.set_session(data)

    def set_motorcycle_session(self, data):
        self.set_session(data)

    def _update_horse_wind(self):
        self.update_wind()

    def _vehicle_key_role(self, key):
        return self._key_role(key)

    def _send_vehicle_input(self):
        self._send_input()

    def _handle_vehicle_control_event(self, event):
        return self.handle_event(event)
