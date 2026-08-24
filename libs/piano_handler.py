"""
Piano handler — extracted from Gameplay to keep gameplay.py focused on
core loop orchestration.  Owns all piano state, key-to-note mapping,
soft/chorus/pitch-bend controls, MIDI integration, and local audio.

Usage from Gameplay::

    self.piano = PianoHandler(self)
    # in update():
    if self.piano.active:
        self.piano.poll()
    # in event loop:
    if self.piano.active and self.piano.handle_event(event):
        continue  # consumed
    # mode transitions:
    self.piano.start()
    self.piano.stop()
"""

import os
import re
import time

import pygame

from . import consts
from .speech import speak
from .midi.profiles import PIANO_MIDI_PROFILE


class PianoHandler:
    """Manages piano input, audio, and network replication."""

    # -- octave / note constants --
    MIN_BASE_OCTAVE = 1
    MAX_BASE_OCTAVE = 6
    MIDI_MIN_NOTE = PIANO_MIDI_PROFILE.MIN_NOTE
    MIDI_MAX_NOTE = PIANO_MIDI_PROFILE.MAX_NOTE
    MIDI_MIN_VOLUME = 60
    MIDI_MAX_VOLUME = 300
    MIDI_PITCH_MIN = -8192
    MIDI_PITCH_MAX = 8191
    MIDI_PITCH_CENTER_DEADZONE = 32
    PITCH_NETWORK_STEP = 64
    PITCH_NETWORK_INTERVAL = 1.0 / 30.0

    CHROMATIC = [
        "C", "Db", "D", "Eb", "E", "F",
        "Gb", "G", "Ab", "A", "Bb", "B",
    ]

    def __init__(self, gameplay):
        self._gp = gameplay          # back-reference to Gameplay
        self.active = False
        self.octave = 4
        self.transpose = 0
        self._pressed_notes = {}     # physical key → sounding note name

        # Pedal / chorus / pitch-bend state
        self._soft_pedal = False
        self._chorus_enabled = False
        self._chorus_tab_down = False
        self._pitch_bend_direction = 0
        self._pitch_bend_keys = set()
        self._pitch_bend_value = 0
        self._midi_pitch_bend_value = 0
        self._midi_pitch_bend_source = None
        self._pitch_bend_pending = None
        self._pitch_bend_last_sent = None
        self._pitch_bend_last_send_time = 0.0

        # MIDI sustain tracking
        self._midi_active_notes = {}
        self._midi_sustained_notes = {}
        self._midi_sustain_sources = set()
        self._midi_sustain = False

        # Sustain pedal (Space) local tracking
        self._sustained_notes = []

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

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

    def start(self):
        """Enter piano mode (called from Gameplay)."""
        if self._gp.drum_mode:
            self._gp._end_drum_session(notify_server=True)
        self.active = True
        self._pressed_notes.clear()
        self._chorus_tab_down = False
        self._pitch_bend_keys.clear()
        self._midi_pitch_bend_value = 0
        self._midi_pitch_bend_source = None
        self._pitch_bend_pending = None
        self._pitch_bend_last_sent = None
        self._pitch_bend_last_send_time = 0.0
        self._set_soft_pedal(False, announce=False, force_network=True)
        self._set_chorus(False, announce=False, force_network=True)
        self._set_pitch_bend(0, force_network=True)
        self._start_midi()

    def stop(self, notify_server=True):
        """Exit piano mode."""
        self._set_soft_pedal(False, announce=False)
        self._set_chorus(False, announce=False)
        self._chorus_tab_down = False
        self._pitch_bend_keys.clear()
        self._set_pitch_bend(0)
        self._deactivate_midi()
        self.active = False
        self._gp.piano_mode = False  # Sync gameplay flag so movement resumes
        if notify_server and self._game.network:
            self._game.network.send(consts.CHANNEL_MAP, "piano_stop", {})

    # ------------------------------------------------------------------
    # pedal / chorus / pitch-bend controls
    # ------------------------------------------------------------------

    def _set_soft_pedal(self, enabled, announce=True, force_network=False):
        enabled = bool(enabled)
        changed = self._soft_pedal != enabled
        self._soft_pedal = enabled
        self._game.audio_mngr.piano.set_soft_pedal("local", enabled)
        if (changed or force_network) and self._game.network:
            self._game.network.send(
                consts.CHANNEL_MAP,
                "set_piano_soft_pedal",
                {"enabled": enabled},
            )
        if changed and announce:
            speak("Soft pedal on" if enabled else "Soft pedal off")

    def _set_chorus(self, enabled, announce=True, force_network=False):
        enabled = bool(enabled)
        changed = self._chorus_enabled != enabled
        self._chorus_enabled = enabled
        self._game.audio_mngr.piano.set_chorus("local", enabled)
        if (changed or force_network) and self._game.network:
            self._game.network.send(
                consts.CHANNEL_MAP,
                "set_piano_chorus",
                {"enabled": enabled},
            )
        if changed and announce:
            speak("Chorus on" if enabled else "Chorus off")

    def _set_pitch_bend(self, direction, force_network=False):
        if isinstance(direction, bool) or direction not in (-1, 0, 1):
            return
        direction = int(direction)
        changed = self._pitch_bend_direction != direction
        self._pitch_bend_direction = direction
        value = (
            self.MIDI_PITCH_MAX
            if direction > 0
            else self.MIDI_PITCH_MIN if direction < 0 else 0
        )
        self._pitch_bend_value = value
        self._pitch_bend_pending = None
        self._game.audio_mngr.piano.set_pitch_bend("local", direction)
        if changed or force_network:
            self._send_pitch_bend(value, force=force_network)

    def _send_pitch_bend(self, value, force=False):
        value = max(
            self.MIDI_PITCH_MIN,
            min(self.MIDI_PITCH_MAX, int(value)),
        )
        if not self._game.network:
            return
        if not force and self._pitch_bend_last_sent == value:
            return
        self._game.network.send(
            consts.CHANNEL_MAP,
            "set_piano_pitch_bend",
            {"value": value},
        )
        self._pitch_bend_last_sent = value
        self._pitch_bend_last_send_time = time.monotonic()

    @classmethod
    def _quantize_pitch_bend(cls, value):
        if value == 0:
            return 0
        quantized = int(round(value / cls.PITCH_NETWORK_STEP)) * (
            cls.PITCH_NETWORK_STEP
        )
        return max(
            cls.MIDI_PITCH_MIN,
            min(cls.MIDI_PITCH_MAX, quantized),
        )

    def _flush_pitch_bend_network(self, force=False):
        value = self._pitch_bend_pending
        if value is None:
            return
        now = time.monotonic()
        if (
            not force
            and now - self._pitch_bend_last_send_time
            < self.PITCH_NETWORK_INTERVAL
        ):
            return
        value = self._quantize_pitch_bend(value)
        self._pitch_bend_pending = None
        self._send_pitch_bend(value, force=force)

    def _set_midi_pitch_bend(
        self, value, force_network=False, source=None
    ):
        if isinstance(value, bool) or not isinstance(value, int):
            return
        if not self.MIDI_PITCH_MIN <= value <= self.MIDI_PITCH_MAX:
            return
        if abs(value) <= self.MIDI_PITCH_CENTER_DEADZONE:
            value = 0
        self._midi_pitch_bend_value = value
        if value == 0:
            self._midi_pitch_bend_source = None
        elif source is not None:
            self._midi_pitch_bend_source = source
        if self._pitch_bend_keys:
            return

        changed = self._pitch_bend_value != value
        self._pitch_bend_direction = 0
        self._pitch_bend_value = value
        if changed:
            self._game.audio_mngr.piano.set_pitch_bend_14bit(
                "local", value, animate=False
            )
        if changed or force_network:
            self._pitch_bend_pending = value
            self._flush_pitch_bend_network(
                force=force_network or value == 0
            )

    def handle_pitch_bend_key(self, key, pressed):
        """Track both arrow keys so releasing one restores the other or center."""
        if pressed:
            if key in self._pitch_bend_keys:
                return
            self._pitch_bend_keys.add(key)
            self._set_pitch_bend(1 if key == pygame.K_UP else -1)
            return

        self._pitch_bend_keys.discard(key)
        if self._pitch_bend_direction == (1 if key == pygame.K_UP else -1):
            if pygame.K_UP in self._pitch_bend_keys:
                direction = 1
            elif pygame.K_DOWN in self._pitch_bend_keys:
                direction = -1
            else:
                self._pitch_bend_direction = 0
                self._set_midi_pitch_bend(
                    self._midi_pitch_bend_value,
                    force_network=True,
                )
                return
            self._set_pitch_bend(direction)

    # ------------------------------------------------------------------
    # note mapping
    # ------------------------------------------------------------------

    def get_key_to_note(self):
        """Build the note map without duplicating unavailable edge octaves."""
        octave = max(
            self.MIN_BASE_OCTAVE,
            min(self.MAX_BASE_OCTAVE, self.octave),
        )
        key_to_note = {
            pygame.K_COMMA: f"C{octave}", pygame.K_l: f"Db{octave}",
            pygame.K_PERIOD: f"D{octave}", pygame.K_SEMICOLON: f"Eb{octave}",
            pygame.K_SLASH: f"E{octave}", pygame.K_QUOTE: f"F{octave}",
            pygame.K_q: f"C{octave}", pygame.K_2: f"Db{octave}",
            pygame.K_w: f"D{octave}", pygame.K_3: f"Eb{octave}",
            pygame.K_e: f"E{octave}", pygame.K_r: f"F{octave}",
            pygame.K_5: f"Gb{octave}", pygame.K_t: f"G{octave}",
            pygame.K_6: f"Ab{octave}", pygame.K_y: f"A{octave}",
            pygame.K_7: f"Bb{octave}", pygame.K_u: f"B{octave}",
        }
        if octave > self.MIN_BASE_OCTAVE:
            lower = octave - 1
            key_to_note.update({
                pygame.K_z: f"C{lower}", pygame.K_s: f"Db{lower}",
                pygame.K_x: f"D{lower}", pygame.K_d: f"Eb{lower}",
                pygame.K_c: f"E{lower}", pygame.K_v: f"F{lower}",
                pygame.K_g: f"Gb{lower}", pygame.K_b: f"G{lower}",
                pygame.K_h: f"Ab{lower}", pygame.K_n: f"A{lower}",
                pygame.K_j: f"Bb{lower}", pygame.K_m: f"B{lower}",
            })
        if octave < self.MAX_BASE_OCTAVE:
            upper = octave + 1
            key_to_note.update({
                pygame.K_i: f"C{upper}", pygame.K_9: f"Db{upper}",
                pygame.K_o: f"D{upper}", pygame.K_0: f"Eb{upper}",
                pygame.K_p: f"E{upper}", pygame.K_LEFTBRACKET: f"F{upper}",
                pygame.K_MINUS: f"Gb{upper}", pygame.K_RIGHTBRACKET: f"G{upper}",
                pygame.K_EQUALS: f"Ab{upper}", pygame.K_BACKSLASH: f"A{upper}",
            })
        return key_to_note

    def _apply_transpose(self, raw_note):
        """Apply semitone transpose offset to a raw note name."""
        transpose = self.transpose
        if transpose == 0:
            return raw_note
        match = re.match(r"([A-Za-z]+)(\d+)", raw_note)
        if not match:
            return raw_note
        n_str, o_num = match.group(1), int(match.group(2))
        if n_str not in self.CHROMATIC:
            return raw_note
        abs_idx = o_num * 12 + self.CHROMATIC.index(n_str) + transpose
        while abs_idx < 12:
            abs_idx += 12
        while abs_idx > 95:
            abs_idx -= 12
        return f"{self.CHROMATIC[abs_idx % 12]}{abs_idx // 12}"

    # ------------------------------------------------------------------
    # note playback
    # ------------------------------------------------------------------

    def _release_note(self, note_name):
        """Release the exact note started by a physical key press."""
        keys = pygame.key.get_pressed()
        if keys[pygame.K_SPACE]:
            if note_name not in self._sustained_notes:
                self._sustained_notes.append(note_name)
            return
        self._stop_local_note(note_name)

    @classmethod
    def _midi_note_name(cls, midi_note):
        return PIANO_MIDI_PROFILE.note_name(midi_note)

    @classmethod
    def _midi_velocity_volume(cls, velocity):
        return PIANO_MIDI_PROFILE.volume(velocity)

    def play_local_note(self, note_name, velocity=None):
        """Predict a local note immediately, then send its compact action packet."""
        volume = (
            300
            if velocity is None
            else self._midi_velocity_volume(velocity)
        )
        is_mega_owner = self._is_megaphone_owner()

        snd = self._game.audio_mngr.piano.play_note(
            "local", note_name,
            self._gp.player.x, self._gp.player.y, self._gp.player.z,
            self._gp.player.x, self._gp.player.y, self._gp.player.z,
            volume=volume,
            via_megaphone=getattr(self._gp, 'voice_chat_using_megaphone', False) or is_mega_owner
        )
        if snd and getattr(snd, "source", None) and getattr(self._gp, "map", None):
            reverb = self._gp.map.get_reverb_at(
                self._gp.player.x, self._gp.player.y, self._gp.player.z
            )
            if reverb and reverb.reverb:
                self._game.audio_mngr.piano.apply_effect_send(
                    snd, 0, reverb.reverb
                )
        packet = {"note": note_name}
        if velocity is not None:
            packet["velocity"] = max(1, min(127, int(velocity)))
        self._attach_music_timeline(packet)
        self._send_jam_note("play_piano_note", packet)

    def _stop_local_note(self, note_name):
        self._game.audio_mngr.piano.stop_note("local", note_name)
        if self._game.network:
            packet = self._attach_music_timeline({"note": note_name})
            self._game.network.send(
                consts.CHANNEL_MAP, "stop_piano_note", packet
            )

    # ------------------------------------------------------------------
    # MIDI integration
    # ------------------------------------------------------------------

    def _start_midi(self):
        if not self.active:
            return
        self._gp._midi_lease = self._game.midi_hub.acquire(self._gp, "piano")

    def _release_midi_sustain(self):
        active_note_names = set(self._midi_active_notes.values())
        sustained_note_names = set(self._midi_sustained_notes.values())
        self._midi_sustained_notes.clear()
        for note_name in sustained_note_names - active_note_names:
            self._stop_local_note(note_name)

    @staticmethod
    def _keyboard_sustain_is_down():
        try:
            return bool(pygame.key.get_pressed()[pygame.K_SPACE])
        except pygame.error:
            return False

    def _stop_all_midi_notes(self):
        note_names = set(self._midi_active_notes.values())
        note_names.update(self._midi_sustained_notes.values())
        self._midi_active_notes.clear()
        self._midi_sustained_notes.clear()
        self._midi_sustain_sources.clear()
        self._midi_sustain = False
        for note_name in note_names:
            self._stop_local_note(note_name)

    def _deactivate_midi(self):
        lease = self._gp._midi_lease
        if lease is not None and lease.profile_id == "piano":
            self._game.midi_hub.release(lease, reason="piano_mode_exit")
            self._gp._midi_lease = None

    def poll(self):
        """Dispatch queued MIDI events through the active piano profile."""
        self._game.midi_hub.poll()

    # ------------------------------------------------------------------
    # event handling (called from Gameplay.update)
    # ------------------------------------------------------------------

    # Keys that should always pass through to gameplay even while playing
    # piano: music bot controls, chat, buffer navigation, main menu.
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
        pygame.K_p,           # check stats / spectator camera
    ))

    def handle_event(self, event):
        """Process a single pygame event.  Returns True if consumed.

        While piano mode is active, gameplay keys (TAG, RADAR, BUILDER,
        movement, etc.) are blocked so they don't interfere with
        playing.  Utility keys (music bot, chat, buffer navigation,
        main menu) are allowed through so the performer can still
        control music and chat while playing.
        """
        if not self.active:
            return False

        if event.type == pygame.KEYDOWN:
            if event.key in (pygame.K_ESCAPE, pygame.K_RETURN):
                self.stop(notify_server=True)
                return True
            if event.key == pygame.K_LCTRL:
                self._set_soft_pedal(True)
                return True
            if event.key == pygame.K_TAB:
                if not self._chorus_tab_down:
                    self._chorus_tab_down = True
                    self._set_chorus(not self._chorus_enabled)
                return True
            if event.key in (pygame.K_UP, pygame.K_DOWN):
                self.handle_pitch_bend_key(event.key, True)
                return True
            if event.key in (pygame.K_LEFT, pygame.K_RIGHT):
                self._handle_octave_key(event.key)
                return True
            if event.key in (pygame.K_F1, pygame.K_F2, pygame.K_F3):
                self._handle_transpose_key(event.key)
                return True
            # Note keys
            key_to_note = self.get_key_to_note()
            if event.key in key_to_note:
                if event.key in self._pressed_notes:
                    return True  # OS key-repeat while held
                note_name = self._apply_transpose(key_to_note[event.key])
                self._pressed_notes[event.key] = note_name
                self.play_local_note(note_name)
                return True
            # Utility keys pass through to gameplay (music bot, chat, etc.)
            if event.key in self._ALWAYS_ALLOWED_KEYS:
                return False
            # Block all other keys (TAG, RADAR, BUILDER, WASD, …)
            return True

        elif event.type == pygame.KEYUP:
            if event.key == pygame.K_TAB:
                self._chorus_tab_down = False
                return True
            if event.key == pygame.K_LCTRL:
                self._set_soft_pedal(False)
                return True
            if event.key in (pygame.K_UP, pygame.K_DOWN):
                self.handle_pitch_bend_key(event.key, False)
                return True
            if event.key == pygame.K_SPACE:
                self._release_sustain_pedal()
                return True
            tracked_note = self._pressed_notes.pop(event.key, None)
            if tracked_note is not None:
                self._release_note(tracked_note)
                return True
            key_to_note = self.get_key_to_note()
            if event.key in key_to_note:
                note_name = self._apply_transpose(key_to_note[event.key])
                self._release_note(note_name)
                return True
            # Utility keys pass through to gameplay.
            if event.key in self._ALWAYS_ALLOWED_KEYS:
                return False
            # Block KEYUP for gameplay keys.
            return True

        # Non-keyboard events (mouse, etc.) are not consumed.
        return False

    def _handle_octave_key(self, key):
        current = max(
            self.MIN_BASE_OCTAVE,
            min(self.MAX_BASE_OCTAVE, self.octave),
        )
        if key == pygame.K_LEFT:
            self.octave = max(self.MIN_BASE_OCTAVE, current - 1)
        else:
            self.octave = min(self.MAX_BASE_OCTAVE, current + 1)
        speak(f"Octave {self.octave}")
        self._preload_octave(self.octave)

    def _preload_octave(self, octave):
        for n in self.CHROMATIC:
            snd = f"piano/Piano.mf.{n}{octave}.ogg"
            snd_path = os.path.join(consts.SOUNDPREPEND, snd)
            try:
                rel_snd = os.path.relpath(snd_path)
            except ValueError:
                rel_snd = os.path.normpath(snd_path)
            try:
                buf = self._game.audio_mngr.load_buffer(snd)
                if buf:
                    self._game.audio_mngr._preloaded_buffers[rel_snd] = buf
            except Exception:
                pass

    def _handle_transpose_key(self, key):
        key_names = {
            0: "C", 1: "C sharp", 2: "D", 3: "E flat", 4: "E", 5: "F",
            6: "F sharp", 7: "G", 8: "A flat", 9: "A", 10: "B flat", 11: "B",
            -1: "B", -2: "B flat", -3: "A", -4: "A flat", -5: "G", -6: "F sharp",
            -7: "F", -8: "E", -9: "E flat", -10: "D", -11: "C sharp", -12: "C"
        }
        if key == pygame.K_F1:
            self.transpose = max(-12, self.transpose - 1)
        elif key == pygame.K_F2:
            self.transpose = min(12, self.transpose + 1)
        elif key == pygame.K_F3:
            self.transpose = 0
        target = key_names.get(self.transpose, f"{self.transpose}")
        speak(f"Transpose to {target}")

    def _release_sustain_pedal(self):
        for sn in self._sustained_notes:
            self._game.audio_mngr.piano.stop_note("local", sn)
            packet = self._attach_music_timeline({"note": sn})
            self._game.network.send(
                consts.CHANNEL_MAP, "stop_piano_note", packet
            )
        self._sustained_notes = []
        if not self._midi_sustain:
            self._release_midi_sustain()

    # ------------------------------------------------------------------
    # compatibility shims — kept so Gameplay can still delegate directly
    # ------------------------------------------------------------------

    def _get_piano_key_to_note(self):
        return self.get_key_to_note()

    def _play_local_piano_note(self, note_name, velocity=None):
        self.play_local_note(note_name, velocity)

    def _stop_local_piano_note(self, note_name):
        self._stop_local_note(note_name)

    def _release_piano_note(self, note_name):
        self._release_note(note_name)

    def _handle_piano_pitch_bend_key(self, key, pressed):
        self.handle_pitch_bend_key(key, pressed)

    def _set_piano_soft_pedal(self, enabled, announce=True, force_network=False):
        self._set_soft_pedal(enabled, announce, force_network)

    def _set_piano_chorus(self, enabled, announce=True, force_network=False):
        self._set_chorus(enabled, announce, force_network)

    def _set_piano_pitch_bend(self, direction, force_network=False):
        self._set_pitch_bend(direction, force_network)

    def _set_piano_midi_pitch_bend(self, value, force_network=False, source=None):
        self._set_midi_pitch_bend(value, force_network, source)

    def _release_piano_midi_sustain(self):
        self._release_midi_sustain()

    def _start_piano_session(self):
        self.start()

    def _end_piano_session(self, notify_server=True):
        self.stop(notify_server)

    def _start_piano_midi(self):
        self._start_midi()

    def _deactivate_piano_midi(self):
        self._deactivate_midi()

    def _poll_piano_midi(self):
        self.poll()

    def _stop_all_piano_midi_notes(self):
        self._stop_all_midi_notes()

    def _flush_piano_pitch_bend_network(self, force=False):
        self._flush_pitch_bend_network(force)

    def _quantize_piano_pitch_bend(cls, value):
        return cls._quantize_pitch_bend(value)

    def _send_piano_pitch_bend(self, value, force=False):
        self._send_pitch_bend(value, force)

    @staticmethod
    def _keyboard_sustain_is_down():
        try:
            return bool(pygame.key.get_pressed()[pygame.K_SPACE])
        except pygame.error:
            return False

    @classmethod
    def _piano_midi_note_name(cls, midi_note):
        return PIANO_MIDI_PROFILE.note_name(midi_note)

    @classmethod
    def _piano_midi_velocity_volume(cls, velocity):
        return PIANO_MIDI_PROFILE.volume(velocity)
