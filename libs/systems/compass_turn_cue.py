"""A smooth, spatial compass cue for local-player turning."""

import time

from .. import movement, options


class CompassTurnCue:
    """Own one looping facing source while the player is actively turning."""

    VOLUME = 85
    ACTIVITY_TIMEOUT = 0.12

    def __init__(self, gameplay):
        self.gameplay = gameplay
        self.sound = None
        self.target_gain = 0.0
        self.current_gain = 0.0
        self.target_direction = 0.0
        self.current_direction = 0.0
        self._last_turn_time = 0.0

    @staticmethod
    def _nearest_cardinal(facing):
        return min((0, 90, 180, 270), key=lambda direction: abs((facing - direction + 180) % 360 - 180))

    @staticmethod
    def _lerp_angle(current, target, amount):
        difference = (target - current + 180) % 360 - 180
        return (current + difference * amount) % 360

    def on_turn(self, facing):
        """Update the intended compass landmark from the player's actual angle."""
        if not options.get("compass_turn_cue", True):
            self.target_gain = 0.0
            return
        self._last_turn_time = time.monotonic()
        self.target_direction = self._nearest_cardinal(facing % 360)
        if self.sound is None and self.current_gain == 0.0:
            self.current_direction = self.target_direction
        self.target_gain = 1.0

    def stop_turning(self):
        self.target_gain = 0.0

    def update(self):
        """Move the source smoothly and fade it after the turn key is released."""
        if not options.get("compass_turn_cue", True):
            self.target_gain = 0.0
        elif time.monotonic() - self._last_turn_time > self.ACTIVITY_TIMEOUT:
            self.target_gain = 0.0

        if self.target_gain > 0.0 and self.sound is None:
            source_pos = self._source_position(self.current_direction)
            self.sound = self.gameplay.game.audio_mngr.play_unbound(
                "ui/direction.ogg",
                *source_pos,
                looping=True,
                cat="ui",
                volume=self.VOLUME,
            )

        if self.sound is None or self.sound.source is None:
            return

        try:
            self.current_gain += (self.target_gain - self.current_gain) * 0.25
            self.current_direction = self._lerp_angle(
                self.current_direction, self.target_direction, 0.22
            )
            source = self.sound.source
            source.position = self._source_position(self.current_direction)
            ui_gain = self.gameplay.game.audio_mngr.volume_categories["ui"][0] / 100.0
            source.gain = self.current_gain * (self.VOLUME / 100.0) * ui_gain

            # Sync environmental reverb at the player's position
            try:
                player = self.gameplay.player
                if self.gameplay.map:
                    reverb = self.gameplay.map.get_reverb_at(player.x, player.y, player.z)
                    audio_mngr = self.gameplay.game.audio_mngr
                    if reverb and hasattr(reverb, "reverb") and reverb.reverb and hasattr(audio_mngr, "efx"):
                        audio_mngr.efx.send(source, 0, reverb.reverb)
            except Exception:
                pass
        except Exception:
            self.destroy()

    def _source_position(self, direction):
        player = self.gameplay.player
        return movement.move(
            (player.x, player.y, player.z + 1), direction, factor=4
        ).get_tuple

    def destroy(self):
        if self.sound is not None:
            try:
                audio_manager = self.gameplay.game.audio_mngr
                if self.sound in audio_manager.unbound_sources:
                    audio_manager.unbound_sources.remove(self.sound)
                self.sound.destroy(force=True)
            except Exception:
                pass
        self.sound = None
