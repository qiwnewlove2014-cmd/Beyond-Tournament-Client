"""Directional, spatial wall-proximity tone for the local player."""

import time

from .. import movement, options


class WallToneSystem:
    """Reuse one OpenAL source to indicate the wall in the active move direction."""

    MAX_DISTANCE = 5
    SCAN_INTERVAL = 0.05
    ACTIVITY_TIMEOUT = 0.12
    VOLUME = 70

    def __init__(self, gameplay):
        self.gameplay = gameplay
        self.sound = None
        self.target_gain = 0.0
        self.target_pitch = 1.0
        self.target_position = (0.0, 0.0, 0.0)
        self.current_gain = 0.0
        self.current_pitch = 1.0
        self._last_scan_time = 0.0
        self._last_activity_time = 0.0

    def preview_movement(self, direction):
        """Scan only the direction the player is currently attempting to move."""
        now = time.monotonic()
        self._last_activity_time = now
        if not options.get("wall_tone", False):
            self.target_gain = 0.0
            return
        if now - self._last_scan_time < self.SCAN_INTERVAL:
            return
        self._last_scan_time = now

        player = self.gameplay.player
        position = (player.x, player.y, player.z)
        for distance in range(1, self.MAX_DISTANCE + 1):
            position = movement.move(position, direction % 360).get_tuple
            if not self.gameplay.map.in_bound(*position):
                break
            tile = self.gameplay.map.get_tile_at(*position)
            if tile and tile.startswith("wall"):
                # C to G: five steps away is low; one step away is high.
                self.target_pitch = 1.0 + ((self.MAX_DISTANCE - distance) / (self.MAX_DISTANCE - 1)) * 0.5
                self.target_position = (position[0], position[1], position[2] + 1)
                self.target_gain = 1.0
                return

        self.target_gain = 0.0

    def update(self):
        """Smoothly update the single spatial source and fade when movement stops."""
        if not options.get("wall_tone", False):
            self.target_gain = 0.0
        elif time.monotonic() - self._last_activity_time > self.ACTIVITY_TIMEOUT:
            self.target_gain = 0.0

        if self.target_gain > 0.0 and self.sound is None:
            self.sound = self.gameplay.game.audio_mngr.play_unbound(
                "ui/wall.ogg",
                *self.target_position,
                looping=True,
                cat="ui",
                volume=self.VOLUME,
                pitch=self.target_pitch,
            )

        if self.sound is None or self.sound.source is None:
            return

        try:
            source = self.sound.source
            source.position = self.target_position
            # Interpolate on the main thread so pitch and volume never click
            # when the player changes from one movement direction to another.
            self.current_gain += (self.target_gain - self.current_gain) * 0.28
            self.current_pitch += (self.target_pitch - self.current_pitch) * 0.28
            source.pitch = self.current_pitch
            ui_gain = self.gameplay.game.audio_mngr.volume_categories["ui"][0] / 100.0
            source.gain = self.current_gain * (self.VOLUME / 100.0) * ui_gain
        except Exception:
            self.destroy()

    def destroy(self):
        """Release the owned source on gameplay exit or an audio-device reset."""
        if self.sound is not None:
            try:
                audio_manager = self.gameplay.game.audio_mngr
                if self.sound in audio_manager.unbound_sources:
                    audio_manager.unbound_sources.remove(self.sound)
                self.sound.destroy(force=True)
            except Exception:
                pass
        self.sound = None
