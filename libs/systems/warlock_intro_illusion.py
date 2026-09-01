"""Per-listener spatial illusion used during the Warlock introduction."""

import contextlib
import math
import time


class _IllusionState:
    def __init__(self, name, phase, started_at, sound_path=""):
        self.name = name
        self.phase = phase
        self.started_at = started_at
        self.sound_path = str(sound_path or "").replace("\\", "/").lower()
        self.sound = None
        self.position = (0.0, 0.0, 0.0)
        self.reverb_slot = object()
        self.fade_started_at = None
        self.fade_start_gain = 0.0


class WarlockIntroIllusion:
    """Moves one reusable intro source around each local listener.

    The Server remains authoritative over intro start/cue/stop timing. This
    class only renders the approved sound at a listener-relative phantom
    position, so no fake coordinates or per-frame packets enter game state.
    """

    ACTION_STOP = 0
    ACTION_MAIN = 1
    ACTION_CUE = 2
    STOP_FADE_SECONDS = 0.18
    INTRO_SPEECH_SECONDS = 49.8
    FINAL_HALF_CIRCLE_SECONDS = 7.8

    def __init__(self, gameplay, time_source=time.monotonic):
        self.gameplay = gameplay
        self._time = time_source
        self._states = {}

    @staticmethod
    def _phase_for(name):
        # Stable without Python's process-randomized hash(). Different bosses
        # therefore do not orbit a listener in lockstep.
        weighted = sum((index + 1) * ord(char) for index, char in enumerate(name))
        return (weighted % 6283) / 1000.0

    @staticmethod
    def _valid_sound(path):
        normalized = str(path or "").replace("\\", "/").lower()
        return normalized.startswith("entities/warlock/") and normalized.endswith(".ogg")

    def handle_packet(self, data):
        """Consume a backward-compatible play_sound illusion extension."""
        try:
            action = int(data.get("illusion"))
        except (TypeError, ValueError):
            return False
        if action not in (self.ACTION_STOP, self.ACTION_MAIN, self.ACTION_CUE):
            return False

        name = str(data.get("name") or "")[:128]
        if not name:
            return False
        if action == self.ACTION_STOP:
            self.stop(name)
            return True

        path = data.get("sound")
        if not self._valid_sound(path):
            return False
        volume = max(0, min(300, int(data.get("volume", 100))))
        if action == self.ACTION_MAIN:
            self.start(name, path, volume)
        else:
            self.play_cue(name, path, volume)
        return True

    def start(self, name, path, volume):
        now = self._time()
        old = self._states.pop(name, None)
        if old is not None:
            self._destroy_sound(old.sound)

        state = _IllusionState(name, self._phase_for(name), now, path)
        state.position = self._position_for(state, now)
        state.sound = self._play_spatial(path, state.position, volume)
        self._states[name] = state
        self._sync_reverb(state, force=True)

    def play_cue(self, name, path, volume):
        state = self._states.get(name)
        if state is None:
            # A cue can race ahead of the main sound on another ordered
            # channel. Seed the same deterministic illusion without retaining
            # this short one-shot as the owned main source.
            state = _IllusionState(name, self._phase_for(name), self._time())
            self._states[name] = state
        state.position = self._position_for(state, self._time())
        cue = self._play_spatial(path, state.position, volume)
        self._apply_reverb_to_sound(cue)

    def stop(self, name, immediate=False):
        state = self._states.get(name)
        if state is None:
            return
        if immediate or state.sound is None or state.sound.source is None:
            self._states.pop(name, None)
            self._destroy_sound(state.sound)
            return
        if state.fade_started_at is None:
            state.fade_started_at = self._time()
            with contextlib.suppress(Exception):
                state.fade_start_gain = float(state.sound.source.gain)

    def update(self):
        now = self._time()
        for name, state in list(self._states.items()):
            sound = state.sound
            # Cue-only placeholder state remains useful until the speech main
            # packet arrives; it owns no source and needs no per-frame work.
            if sound is None:
                continue
            if sound.source is None:
                self._states.pop(name, None)
                continue

            try:
                state.position = self._position_for(state, now)
                sound.source.position = state.position
                sound.source.velocity = (0.0, 0.0, 0.0)
                self._sync_reverb(state)

                if state.fade_started_at is not None:
                    progress = (now - state.fade_started_at) / self.STOP_FADE_SECONDS
                    if progress >= 1.0:
                        self._states.pop(name, None)
                        self._destroy_sound(sound)
                    else:
                        sound.source.gain = state.fade_start_gain * max(0.0, 1.0 - progress)
            except Exception:
                self._states.pop(name, None)
                self._destroy_sound(sound)

    def _position_for(self, state, now):
        player = self.gameplay.player
        elapsed = max(0.0, now - state.started_at)
        phase = state.phase

        # For the final 7.8 seconds of the spoken monologue, leave the pulsing
        # overhead pattern and trace a deliberate listener-relative half
        # circle: front -> right -> behind. This follows the player's current
        # facing direction even if they turn during the speech.
        final_arc_start = self.INTRO_SPEECH_SECONDS - self.FINAL_HALF_CIRCLE_SECONDS
        if (state.sound_path.endswith("/skill/intro/intro_speech.ogg")
                and elapsed >= final_arc_start):
            arc_elapsed = elapsed - final_arc_start
            entry_seconds = 0.8
            entry_progress = min(1.0, max(0.0, arc_elapsed / entry_seconds))
            entry_eased = entry_progress * entry_progress * (3.0 - 2.0 * entry_progress)
            arc_seconds = self.FINAL_HALF_CIRCLE_SECONDS - entry_seconds
            progress = min(
                1.0,
                max(0.0, (arc_elapsed - entry_seconds) / arc_seconds),
            )
            eased = progress * progress * (3.0 - 2.0 * progress)
            facing = math.radians(float(getattr(player, "angle", 0.0)))
            bearing = facing + math.pi * eased
            # Expand smoothly from the overhead hover before beginning the
            # half circle; an instant jump to the front sounded like a cut.
            radius = 0.38 + (4.2 - 0.38) * entry_eased
            tremble_x = math.sin(elapsed * 10.9 + phase) * 0.16
            tremble_y = math.sin(elapsed * 12.1 + phase * 1.4) * 0.16
            vertical = 3.2 + math.sin(elapsed * 1.05 + phase) * 0.12
            return (
                float(player.x) + math.sin(bearing) * radius + tremble_x,
                float(player.y) + math.cos(bearing) * radius + tremble_y,
                float(player.z) + vertical,
            )

        # Each eight-second phrase begins as a tight, trembling presence just
        # above the listener, then accelerates outward and hangs in the
        # distance before returning for the next phrase. One retained OpenAL
        # source performs the whole motion; no source or packet is allocated
        # per frame.
        cycle_length = 8.0
        cycle_time = elapsed % cycle_length
        # Keep rotating continuously instead of snapping to a new direction at
        # each cycle boundary.
        angle = phase + elapsed * 0.42

        # During the first eighteen seconds the whole pattern walks forward
        # relative to the listener's facing, reaches its furthest point around
        # nine seconds, then returns smoothly while the orbit/launch continues.
        center_x = float(player.x)
        center_y = float(player.y)
        if elapsed < 18.0:
            forward_distance = 5.5 * math.sin(math.pi * elapsed / 18.0)
            facing = math.radians(float(getattr(player, "angle", 0.0)))
            center_x += math.sin(facing) * forward_distance
            center_y += math.cos(facing) * forward_distance

        near_radius = 0.38
        far_radius = 18.0
        if cycle_time < 4.2:
            radius = near_radius
            height = 3.4
        elif cycle_time < 5.35:
            # Smoothstep gives the launch a clear acceleration without a hard
            # positional jump that could click or produce an uncomfortable pan.
            progress = (cycle_time - 4.2) / 1.15
            eased = progress * progress * (3.0 - 2.0 * progress)
            radius = near_radius + (far_radius - near_radius) * eased
            height = 3.4 + 1.5 * eased
        elif cycle_time < 6.35:
            radius = far_radius
            height = 4.9
        else:
            progress = (cycle_time - 6.35) / 1.65
            eased = progress * progress * (3.0 - 2.0 * progress)
            radius = far_radius + (near_radius - far_radius) * eased
            height = 4.9 + (3.4 - 4.9) * eased

        # Fast, low-amplitude motion sells the overhead vibration while the
        # horizontal launch direction changes from phrase to phrase.
        tremble_x = math.sin(elapsed * 12.7 + phase) * 0.24
        tremble_y = math.sin(elapsed * 11.1 + phase * 1.7) * 0.22
        # Keep height changes slow and slight; rapid Z bobbing made the voice
        # feel glued to the listener's head instead of hovering above it.
        vertical = height + math.sin(elapsed * 1.05 + phase * 0.8) * 0.12
        return (
            center_x + math.cos(angle) * radius + tremble_x,
            center_y + math.sin(angle) * radius + tremble_y,
            float(player.z) + vertical,
        )

    def _play_spatial(self, path, position, volume):
        player = self.gameplay.player
        return self.gameplay.game.audio_mngr.play_unbound_stereo_spatial(
            path,
            position[0], position[1], position[2],
            float(player.x), float(player.y), float(player.z),
            volume=volume,
            cat="miscelaneous",
            max_distance=20.0,
            as_mono=True,
        )

    def _current_reverb_slot(self):
        try:
            player = self.gameplay.player
            reverb = self.gameplay.map.get_reverb_at(player.x, player.y, player.z)
            return getattr(reverb, "reverb", None) if reverb else None
        except Exception:
            return None

    def _apply_reverb_to_sound(self, sound, slot=None):
        if sound is None or sound.source is None:
            return
        if slot is None:
            slot = self._current_reverb_slot()
        with contextlib.suppress(Exception):
            self.gameplay.game.audio_mngr.efx.send(sound.source, 0, slot)

    def _sync_reverb(self, state, force=False):
        slot = self._current_reverb_slot()
        if force or slot is not state.reverb_slot:
            self._apply_reverb_to_sound(state.sound, slot)
            state.reverb_slot = slot

    def _destroy_sound(self, sound):
        if sound is None:
            return
        audio_manager = self.gameplay.game.audio_mngr
        source = sound.source
        if source is not None:
            with contextlib.suppress(Exception):
                audio_manager.efx.send(source, 0, None)
        with contextlib.suppress(ValueError):
            audio_manager.unbound_sources.remove(sound)
        with contextlib.suppress(Exception):
            sound.destroy(force=True)

    def destroy(self):
        for state in list(self._states.values()):
            self._destroy_sound(state.sound)
        self._states.clear()
