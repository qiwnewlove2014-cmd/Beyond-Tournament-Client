"""
Drum handler — extracted from Gameplay to keep gameplay.py focused on
core loop orchestration.  Owns all drum state, key-to-pad mapping,
volume control, MIDI integration, and local audio.

Usage from Gameplay::

    self.drum_handler = DrumHandler(self)
    # in event loop:
    if self.drum_handler.active and self.drum_handler.handle_event(event):
        continue  # consumed
"""

import pygame

from . import consts, drum_keyconfig
from .speech import speak
from .midi.profiles import DRUM_MIDI_PROFILE


class DrumHandler:
    """Manages drum input, audio, and network replication."""

    MIN_VOLUME = 10
    MAX_VOLUME = 100

    def __init__(self, gameplay):
        self._gp = gameplay          # back-reference to Gameplay
        self.active = False
        self.volume_percent = 100
        self._pressed_keys = set()

    @property
    def _game(self):
        return self._gp.game

    def _is_megaphone_owner(self):
        return self._gp._is_megaphone_owner()

    def _attach_music_timeline(self, packet):
        return self._gp._attach_music_timeline(packet)

    def _send_jam_note(self, event, packet):
        return self._gp._send_jam_note(event, packet)

    # ------------------------------------------------------------------
    # mode start / stop
    # ------------------------------------------------------------------

    def start(self, kit=None):
        """Enter drum mode (called from Gameplay)."""
        if self._gp.piano_mode:
            self._gp.piano.stop(notify_server=True)
        self.active = True
        self._pressed_keys.clear()
        drums = self._game.audio_mngr.drums
        if kit and drums.is_valid_kit(kit):
            drums.set_active_kit(kit)
        drums.preload()
        self._start_midi()

    def stop(self, notify_server=True):
        """Exit drum mode."""
        self._pressed_keys.clear()
        if self.active:
            self._deactivate_midi()
        self.active = False
        self._gp.drum_mode = False  # Sync gameplay flag so movement resumes
        if notify_server and self._game.network:
            self._game.network.send(consts.CHANNEL_MAP, "drum_stop", {})

    # ------------------------------------------------------------------
    # key mapping
    # ------------------------------------------------------------------

    def get_key_to_pad(self):
        """Resolve configurable normalized keys to stable drum pad IDs."""
        return drum_keyconfig.key_to_pad(self._game.keyconfig)

    # ------------------------------------------------------------------
    # volume control
    # ------------------------------------------------------------------

    def adjust_volume(self, delta):
        new_vol = max(self.MIN_VOLUME, min(self.MAX_VOLUME, self.volume_percent + delta))
        if new_vol == self.volume_percent:
            return
        self.volume_percent = new_vol
        speak(f"Drum volume: {new_vol} percent")

    # ------------------------------------------------------------------
    # note playback
    # ------------------------------------------------------------------

    def play_local_hit(self, pad, velocity=None):
        """Play a drum hit locally and broadcast to other players."""
        base_volume = (
            300
            if velocity is None
            else DRUM_MIDI_PROFILE.volume(velocity)
        )
        vol_factor = self.volume_percent / 100.0
        volume = max(20, int(base_volume * vol_factor))
        is_mega_owner = self._is_megaphone_owner()

        sound = self._game.audio_mngr.drums.play_hit(
            "local", pad,
            self._gp.player.x, self._gp.player.y, self._gp.player.z,
            self._gp.player.x, self._gp.player.y, self._gp.player.z,
            volume=volume,
            via_megaphone=getattr(self._gp, 'voice_chat_using_megaphone', False) or is_mega_owner
        )
        if sound and getattr(self._gp, "map", None):
            reverb = self._gp.map.get_reverb_at(
                self._gp.player.x, self._gp.player.y, self._gp.player.z
            )
            if reverb and reverb.reverb:
                self._game.audio_mngr.drums.apply_effect_send(
                    sound, 0, reverb.reverb
                )
        if self._game.network:
            packet = {"pad": pad}
            base_vel = 127 if velocity is None else max(1, min(127, int(velocity)))
            packet["velocity"] = max(1, min(127, int(base_vel * vol_factor)))
            self._attach_music_timeline(packet)
            self._send_jam_note("play_drum_hit", packet)

    # ------------------------------------------------------------------
    # MIDI integration
    # ------------------------------------------------------------------

    def _start_midi(self):
        """Acquire the process MIDI hub with the drum profile."""
        if not self.active:
            return
        self._gp._midi_lease = self._game.midi_hub.acquire(self._gp, "drumset")

    def _deactivate_midi(self):
        lease = self._gp._midi_lease
        if lease is not None and lease.profile_id == "drumset":
            self._game.midi_hub.release(lease, reason="drum_mode_exit")
            self._gp._midi_lease = None

    def poll(self):
        """Dispatch queued MIDI events through the active drum profile."""
        self._game.midi_hub.poll()

    # ------------------------------------------------------------------
    # event handling (called from Gameplay.update)
    # ------------------------------------------------------------------

    # Keys that should always pass through to gameplay even while drumming:
    # music bot controls, chat, buffer navigation, main menu, options.
    _ALWAYS_ALLOWED_KEYS = frozenset((
        pygame.K_m,           # music bot toggle
        pygame.K_F9,          # music bot volume down
        pygame.K_F10,         # music bot volume up
        pygame.K_SLASH,       # /  map chat
        pygame.K_QUOTE,       # '  chat
        pygame.K_LEFTBRACKET,  # [  buffer cycle left
        pygame.K_RIGHTBRACKET, # ]  buffer cycle right
        pygame.K_COMMA,       # ,  buffer move left
        pygame.K_PERIOD,      # .  buffer move right
        pygame.K_BACKSPACE,   # main menu
        pygame.K_o,           # options (ALT+O) / PA test
        pygame.K_TAB,         # check direction / spectator
        pygame.K_p,           # check stats / spectator camera
    ))

    def handle_event(self, event):
        """Process a single pygame event.  Returns True if consumed.

        While drum mode is active, gameplay keys (TAG, RADAR, BUILDER,
        movement, etc.) are blocked so they don't interfere with
        drumming.  Utility keys (music bot, chat, buffer navigation,
        main menu) are allowed through so the performer can still
        control music and chat while playing.
        """
        if not self.active:
            return False

        if event.type == pygame.KEYDOWN:
            if event.key in drum_keyconfig.RESERVED_DRUM_KEYS:
                self.stop(notify_server=True)
                return True
            if event.key in (pygame.K_UP, pygame.K_PAGEUP):
                self.adjust_volume(10)
                return True
            if event.key in (pygame.K_DOWN, pygame.K_PAGEDOWN):
                self.adjust_volume(-10)
                return True
            key_to_pad = self.get_key_to_pad()
            if event.key in key_to_pad:
                if event.key in self._pressed_keys:
                    return True
                self._pressed_keys.add(event.key)
                self.play_local_hit(key_to_pad[event.key])
                return True
            # Utility keys pass through to gameplay (music bot, chat, etc.)
            if event.key in self._ALWAYS_ALLOWED_KEYS:
                return False
            # Block all other keys (TAG, RADAR, BUILDER, WASD, …)
            return True

        elif event.type == pygame.KEYUP:
            self._pressed_keys.discard(event.key)
            # Let KEYUP pass for allowed keys so held-key actions work.
            if event.key in self._ALWAYS_ALLOWED_KEYS:
                return False
            return True

        # Non-keyboard events (mouse, etc.) are not consumed.
        return False

    # ------------------------------------------------------------------
    # compatibility shims — kept so Gameplay can still delegate directly
    # ------------------------------------------------------------------

    def _get_drum_key_to_pad(self):
        return self.get_key_to_pad()

    def _start_drum_session(self, kit=None):
        self.start(kit)

    def _end_drum_session(self, notify_server=True):
        self.stop(notify_server)

    def _drum_midi_note_to_pad(cls, midi_note):
        return DRUM_MIDI_PROFILE.note_to_pad(midi_note)

    def _drum_midi_velocity_volume(cls, velocity):
        return DRUM_MIDI_PROFILE.volume(velocity)

    def _adjust_drum_volume(self, delta):
        self.adjust_volume(delta)

    def _play_local_drum_hit(self, pad, velocity=None):
        self.play_local_hit(pad, velocity)

    def _start_drum_midi(self):
        self._start_midi()

    def _deactivate_drum_midi(self):
        self._deactivate_midi()

    def _poll_drum_midi(self):
        self.poll()
