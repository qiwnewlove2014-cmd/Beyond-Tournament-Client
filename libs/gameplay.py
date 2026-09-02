import os
import time
import random
import contextlib
import webbrowser
from functools import partial
import cyal.exceptions
from .systems.megaphone_system import MegaphoneManager
from .systems.wall_tone_system import WallToneSystem
from .systems.compass_turn_cue import CompassTurnCue
from .systems.warlock_intro_illusion import WarlockIntroIllusion
from .midi.profiles import DRUM_MIDI_PROFILE
from .drum_handler import DrumHandler
from .guitar_handler import GuitarHandler
from .vehicle_handler import VehicleHandler
from . import drum_keyconfig
import pygame
import pyogg
from .shields import ShieldManager
from .piano_handler import PianoHandler
from . import (
    audio_manager,
    buffer,
    consts,
    state,
    map,
    voice_chat,
    world_map,
    string_utils,
    menus,
    menu,
    options,
    camera,
    options,
    volume_mixer,
    music_bot,
    movement,
)
from .speech import speak
from .audio_diagnostics import probe as audio_probe
from .tracking_description import describe_tracking_direction
from .objects import player
from .weapons import weapon, weaponmanager
import math
import cyal


class _ExitFadeState(state.State):
    """Blocks all input while the exit fade-out plays out.

    Pushed as a substate on top of Gameplay: Gameplay keeps running underneath
    (so its map audio stays alive and audibly fades instead of hard-cutting),
    but every key/event is swallowed until the fade finishes and the process
    exits.
    """

    def update(self, events):
        return True


class Gameplay(state.State):
    _AUDIO_REFRESH_COOLDOWN_SECONDS = 5.0
    _DRUM_MIDI_MIN_VOLUME = 60
    _DRUM_MIDI_MAX_VOLUME = 300
    _DRUM_MIDI_CHROMATIC_FIRST_NOTE = DRUM_MIDI_PROFILE.CHROMATIC_FIRST_NOTE
    _DRUM_MIDI_CHROMATIC_LAST_NOTE = DRUM_MIDI_PROFILE.CHROMATIC_LAST_NOTE
    _DRUM_MIDI_GM_NOTE_TO_PAD = DRUM_MIDI_PROFILE.GENERAL_MIDI_NOTE_TO_PAD

    def __init__(self, game):
        super().__init__(game)
        self.kc = game.keyconfig
        kc = self.kc  # just an alias to use inside this function.
        self.map = world_map.Map(self.game, 0, 0, 0, 10, 10, 10)
        self.player = player.Player(self.game, self.map, 0, 0, 0)
        self.map.player = self.player
        self.wall_tone = WallToneSystem(self)
        self.compass_turn_cue = CompassTurnCue(self)
        self.warlock_intro_illusion = WarlockIntroIllusion(self)
        self.camera = camera.Camera(self.game)
        self.camera.set_focus_object(self.player)
        self.music_volume = options.get("volume_music", 25)
        self.spectator_mode = False
        # When spectating a Pong match, the server sends the arena bounds so we can
        # park the listener at the field edge for stereo. None for non-pong matches.
        self.pong_arena = None
        self.running = False
        self.turning = False
        self.can_run = True
        self.wmanager = weaponmanager.weaponManager(self.game, self.player)
        self.shield_mngr = ShieldManager(self)
        self.parser = map.Map_parser(self.game, self.map)
        self.last_ping_time = time.time()
        self.pingging = False
        self.pa_test_mode = False  # PA Test Mode for testing megaphone speakers
        self.game_started = False   # Track if game has started (blocks PA Test Mode)
        self.pong_mode = False      # True when player is in an active Pong match (suppresses normal footsteps)
        self.piano_mode = False     # True when playing piano
        self.piano = PianoHandler(self)  # Piano subsystem (extracted from Gameplay)
        self.drum_mode = False      # True when playing a drumset
        self.drum = DrumHandler(self)  # Drum subsystem (extracted from Gameplay)
        self._midi_lease = None
        # 🎵 Music jukebox: server-side queue playback anchored at jukebox elements.
        self.jukebox_player = None
        self.jukebox_state = {"jukeboxes": {}}
        self._audio_refresh_in_progress = False
        self._last_audio_refresh_at = -self._AUDIO_REFRESH_COOLDOWN_SECONDS
        # Name of the currently parsed map (from parse_map), so a real map
        # transition can be told apart from a same-name reparse for jukebox
        # teardown (immediate stop vs. seamless mark-and-sweep).
        self.map_name = None
        self.vehicle = VehicleHandler(self)  # Vehicle/Horse subsystem (extracted from Gameplay)
        # ENet guarantees ordering inside one channel, not across CHANNEL_MISC
        # and CHANNEL_MAP.  Player spawn packets can therefore arrive before
        # the connected event enters this state.  Create the mapping here and
        # preserve it in enter() so those current-session packets are not lost.
        self.voice_channels = {}
        self.voice_chat = None
        self.guitar = GuitarHandler(self)  # Guitar subsystem (extracted from Gameplay)
        self.tracking_target = None
        self.tracking_clock = None
        self.facing_sound_clock = self.game.new_clock()
        self.is_facing_target = False
        self.reload_keyconfig()

    def reload_keyconfig(self):
        kc = self.game.keyconfig
        self.kc = kc
        self.keys_held = {
            kc.get("strafe_left", pygame.K_q): self.strafe_left,
            kc.get("strafe_right", pygame.K_e): self.strafe_right,
            kc.get("move_forward", pygame.K_w): self.move_forward,
            kc.get("turn_left", pygame.K_a): self.turn_left,
            kc.get("move_backward", pygame.K_s): self.move_back,
            kc.get("turn_right", pygame.K_d): self.turn_right,
            kc.get("move_up", pygame.K_PAGEUP): self.move_up,
            kc.get("move_down", pygame.K_PAGEDOWN): self.move_down,
            kc.get("pitch_down", pygame.K_k): self.pitch_down,
            kc.get("pitch_up", pygame.K_j): self.pitch_up,
            kc.get("fire_weapon", pygame.K_SPACE): self.fire_weapon_automatic,
            kc.get("run", pygame.K_LSHIFT): self.run_check,
        }
        self.keys_pressed = {
            kc.get("tracking_menu", pygame.K_t): self.open_tracking_menu,
            kc.get("voice_chat", pygame.K_g): self.voice_chat_toggle,  # Toggle mode (tap to talk, tap again to stop)
            pygame.K_RETURN: self.buffer_options,
            kc.get("open_volume_mixer", pygame.K_F7): lambda mod: self.add_substate(volume_mixer.volume_mixer(self.game, parent=self)),
            kc.get("open_staff_menu", pygame.K_F8): self.open_staff_menu,
            pygame.K_o: self.handle_o_key,  # PA Test Mode (no mod) or Options (ALT+O)
            kc.get("map_chat", pygame.K_SLASH): self.map_chat,
            kc.get("chat", pygame.K_QUOTE): self.chat,
            kc.get("move_left_in_buffer", pygame.K_COMMA): self.buffer_move_l,
            kc.get("move_right_in_buffer", pygame.K_PERIOD): self.buffer_move_r,
            kc.get("cycle_buffer_left", pygame.K_LEFTBRACKET): self.buffer_cycle_l,
            kc.get("cycle_buffer_right", pygame.K_RIGHTBRACKET): self.buffer_cycle_r,
            kc.get("move_forward", pygame.K_w): lambda mod: (
                setattr(self, "can_run", True),
                self.move_forward(
                    mod, True
                )
            ),
            kc.get("turn_left", pygame.K_a): lambda mod: self.turn_left(mod, True),
            kc.get("move_backward", pygame.K_s): lambda mod: (
                setattr(self,"can_run", True),
                self.move_back(mod, True)
            ),
            kc.get("turn_right", pygame.K_d): lambda mod: self.turn_right(mod, True),
            kc.get("pitch_down", pygame.K_k): lambda mod: self.pitch_down(mod, True),
            kc.get("pitch_up", pygame.K_j): lambda mod: self.pitch_up(mod, True),
            kc.get("reset_pitch", pygame.K_l): self.reset_pitch,
            kc.get("reset_bank", pygame.K_SEMICOLON): self.reset_bank,
            pygame.K_F4: self.toggle_sonar_and_force_quit,
            kc.get("strafe_left", pygame.K_q): self._strafe_key_down,
            kc.get("strafe_right", pygame.K_e): self._strafe_key_down,
            kc.get("quit", pygame.K_ESCAPE): self.ask_to_exit,
            kc.get("ping", pygame.K_F3): self.ping,
            kc.get("who_online", pygame.K_F1): self.who_online,
            kc.get("speak_location", pygame.K_c): self.speak_location,
            kc.get("speak_zone", pygame.K_v): self.speak_zone,
            kc.get("speak_fps", pygame.K_F11): self.speak_fps,
            kc.get("run", pygame.K_LSHIFT): self.run_start,
            kc.get("speak_server_message", pygame.K_F2): self.server_message,
            kc.get("online_server_list", pygame.K_F5): self.online_server_list,
            kc.get("snap_modifier", pygame.K_LCTRL): lambda mod: (
                setattr(self, "turn_mod", True),
            ),
            kc.get("open_inventory", pygame.K_i): self.open_inventory,
            kc.get("check_health", pygame.K_h): self.get_hp,
            kc.get("player_radar", pygame.K_y): self.player_radar,
            pygame.K_1: lambda mod: (self.number_row(mod, 1)),
            pygame.K_2: lambda mod: (self.number_row(mod, 2)),
            pygame.K_3: lambda mod: (self.number_row(mod, 3)),
            pygame.K_4: lambda mod: (self.number_row(mod, 4)),
            pygame.K_5: lambda mod: (self.number_row(mod, 5)),
            pygame.K_6: lambda mod: (self.number_row(mod, 6)),
            pygame.K_7: lambda mod: (self.number_row(mod, 7)),
            pygame.K_8: lambda mod: (self.number_row(mod, 8)),
            pygame.K_9: lambda mod: (self.number_row(mod, 9)),
            pygame.K_0: lambda mod: (self.number_row(mod, 10)),

            kc.get("fire_weapon", pygame.K_SPACE): self.fire_weapon_non_automatic,
            kc.get("reload_weapon", pygame.K_r): lambda mod: (self.wmanager.reload()),
            kc.get("check_ammo", pygame.K_z): self.ammo_check,
            kc.get("check_reserves", pygame.K_x): self.reserved_check,
            kc.get(
                "mute_current_buffer", pygame.K_BACKSLASH
            ): lambda mod: buffer.toggle_mute(),
            kc.get("interact", pygame.K_f): self.interact,
            # Enter also interacts (mount/dismount vehicles, jukeboxes, travel
            # points). Shift+Enter is the vehicle variant that leaves the
            # engine running when getting out.
            pygame.K_RETURN: self.interact,
            pygame.K_KP_ENTER: self.interact,
            kc.get("open_main_menu", pygame.K_BACKSPACE): lambda mod: (
                self.chat2("/mainmenu") if not self.substates else None
            ),
            kc.get("check_stats", pygame.K_p): self.check_stats,
            kc.get(
                "export_buffers", pygame.K_BACKQUOTE
            ): lambda mod: buffer.export_buffers(),
            kc.get("toggle_beacons", pygame.K_F6): lambda mod: self.toggle_beacons(mod),
            kc.get("open_builder", pygame.K_b): self.open_builder,
            kc.get("helper_menu", pygame.K_n): self.open_helper_menu,
            # Megaphone Settings moved to Builder Menu (press B)
            # === Music Bot Controls ===
            kc.get("music_bot_toggle", pygame.K_m): self.music_bot_control,
            kc.get("music_bot_vol_down", pygame.K_F9): lambda mod: self.music_bot_volume(-10),
            kc.get("music_bot_vol_up", pygame.K_F10): lambda mod: self.music_bot_volume(10),
            kc.get("guitar_play", pygame.K_u): self.toggle_guitar_mode,
            kc.get("raise_shield", pygame.K_s): self.start_raise_shield,
        }
        self.keys_released = {
            kc.get("raise_shield", pygame.K_s): self.stop_raise_shield,
            kc.get("strafe_left", pygame.K_q): lambda mod: (
                setattr(self, "can_run", True)
            ),
            kc.get("strafe_right", pygame.K_e): lambda mod: (
                setattr(self, "can_run", True)
            ),
            kc.get("turn_left", pygame.K_a): self.turn_stop,
            kc.get("turn_right", pygame.K_d): self.turn_stop,
            kc.get("pitch_down", pygame.K_k): self.pitch_stop,
            kc.get("pitch_up", pygame.K_j): self.pitch_stop,
            kc.get("run", pygame.K_LSHIFT): self.run_stop,
            kc.get("snap_modifier", pygame.K_LCTRL): lambda mod: (
                setattr(self, "turn_mod", False)
            ),
        }
        self.configurable_key_actions = [
            (kc.get("check_direction", pygame.K_TAB), self.check_direction_in_play),
            (kc.get("spectator_switch_player", pygame.K_TAB), self.spectator_switch_player),
            (kc.get("spectator_cycle_camera", pygame.K_p), self.cycle_spectator_camera_if_active),
        ]
        self.turn_mod = False

    def set_vehicle_session(self, data):
        self.vehicle.set_session(data)

    def set_motorcycle_session(self, data):
        self.vehicle.set_motorcycle_session(data)

    def _update_horse_wind(self):
        self.vehicle.update_wind()

    def _vehicle_key_role(self, key):
        return self.vehicle._key_role(key)

    def _send_vehicle_input(self):
        self.vehicle._send_input()

    def _handle_vehicle_control_event(self, event):
        return self.vehicle.handle_event(event)

    def _dispatch_configurable_key_actions(self, event):
        """Run every configurable action sharing this key, in menu order."""
        for bound_key, action in self.configurable_key_actions:
            if event.key == bound_key:
                action(event.mod)

    def _set_piano_soft_pedal(self, enabled, announce=True, force_network=False):
        self.piano._set_soft_pedal(enabled, announce, force_network)

    def _set_piano_chorus(self, enabled, announce=True, force_network=False):
        self.piano._set_chorus(enabled, announce, force_network)

    def _set_piano_pitch_bend(self, direction, force_network=False):
        self.piano._set_pitch_bend(direction, force_network)

    def _send_piano_pitch_bend(self, value, force=False):
        self.piano._send_pitch_bend(value, force)

    def _quantize_piano_pitch_bend(cls, value):
        return PianoHandler._quantize_pitch_bend(value)

    def _flush_piano_pitch_bend_network(self, force=False):
        self.piano._flush_pitch_bend_network(force)

    def _set_piano_midi_pitch_bend(self, value, force_network=False, source=None):
        self.piano._set_midi_pitch_bend(value, force_network, source)

    def _handle_piano_pitch_bend_key(self, key, pressed):
        self.piano.handle_pitch_bend_key(key, pressed)

    def _get_drum_key_to_pad(self):
        return self.drum.get_key_to_pad()

    def _start_drum_session(self, kit=None):
        self.drum.start(kit)
        self.drum_mode = self.drum.active

    def _end_drum_session(self, notify_server=True):
        self.drum.stop(notify_server)
        self.drum_mode = self.drum.active

    def _drum_midi_note_to_pad(cls, midi_note):
        return DRUM_MIDI_PROFILE.note_to_pad(midi_note)

    def _drum_midi_velocity_volume(cls, velocity):
        return DRUM_MIDI_PROFILE.volume(velocity)

    def _is_megaphone_owner(self):
        """True if this performer currently broadcasts to the PA.

        True when the performer holds the single music-bot PA slot OR is a
        member of the multi-owner instrument broadcast set, so several people
        can perform piano/drums/guitar through the PA at the same time
        (band / duo) instead of one lock slot per person.
        """
        mega = getattr(self, 'megaphone', None)
        if not mega:
            return False
        name = getattr(self.player, 'name', '')
        if getattr(mega, 'lock_owner', None) == name:
            return True
        return name in getattr(mega, 'lock_owners', set())

    def _attach_music_timeline(self, packet):
        """Attach the current audible Music Bot frame when one is available."""
        try:
            marker = self.music_bot.performance_timeline_marker()
        except Exception:
            marker = None
        if marker:
            packet["music_sync"] = marker
        return packet

    def _send_jam_note(self, event, packet):
        """Send one instrument note on the dedicated unreliable jam channel.

        Notes are fire-and-forget events: on the shared reliable
        CHANNEL_MAP/CHANNEL_SOUND queues a single lost world-sound packet
        stalls every following note behind an ENet retransmission
        (100-500ms spikes). A small uint16 sequence number lets listeners
        drop reordered duplicates instead of playing them late."""
        seq = getattr(self, "_jam_note_seq", 0)
        self._jam_note_seq = (seq + 1) & 0xFFFF
        packet["seq"] = seq
        self.game.network.send(consts.CHANNEL_JAM, event, packet, reliable=False)

    def _adjust_drum_volume(self, delta):
        self.drum.adjust_volume(delta)

    def _play_local_drum_hit(self, pad, velocity=None):
        self.drum.play_local_hit(pad, velocity)

    def _start_drum_midi(self):
        self.drum._start_midi()

    def _deactivate_drum_midi(self):
        self.drum._deactivate_midi()

    def _poll_drum_midi(self):
        self.drum.poll()

    # -- MIDI attribute proxies for profiles.py compatibility --
    # MIDI profiles (PianoMidiProfile in midi/profiles.py) access these
    # attributes directly on the owner (Gameplay).  After the piano refactor
    # they live on PianoHandler, so we proxy them here.
    @property
    def _piano_midi_active_notes(self):
        return self.piano._midi_active_notes

    @_piano_midi_active_notes.setter
    def _piano_midi_active_notes(self, value):
        self.piano._midi_active_notes = value

    @property
    def _piano_midi_sustained_notes(self):
        return self.piano._midi_sustained_notes

    @_piano_midi_sustained_notes.setter
    def _piano_midi_sustained_notes(self, value):
        self.piano._midi_sustained_notes = value

    @property
    def _piano_midi_sustain_sources(self):
        return self.piano._midi_sustain_sources

    @_piano_midi_sustain_sources.setter
    def _piano_midi_sustain_sources(self, value):
        self.piano._midi_sustain_sources = value

    @property
    def _piano_midi_sustain(self):
        return self.piano._midi_sustain

    @_piano_midi_sustain.setter
    def _piano_midi_sustain(self, value):
        self.piano._midi_sustain = value

    @property
    def _piano_midi_pitch_bend_value(self):
        return self.piano._midi_pitch_bend_value

    @_piano_midi_pitch_bend_value.setter
    def _piano_midi_pitch_bend_value(self, value):
        self.piano._midi_pitch_bend_value = value

    @property
    def _piano_midi_pitch_bend_source(self):
        return self.piano._midi_pitch_bend_source

    @_piano_midi_pitch_bend_source.setter
    def _piano_midi_pitch_bend_source(self, value):
        self.piano._midi_pitch_bend_source = value

    @property
    def _piano_pitch_bend_pending(self):
        return self.piano._pitch_bend_pending

    @_piano_pitch_bend_pending.setter
    def _piano_pitch_bend_pending(self, value):
        self.piano._pitch_bend_pending = value

    @property
    def _piano_pitch_bend_keys(self):
        return self.piano._pitch_bend_keys

    def _start_piano_session(self):
        self.piano.start()
        self.piano_mode = self.piano.active

    def _end_piano_session(self, notify_server=True):
        self.piano.stop(notify_server)
        self.piano_mode = self.piano.active

    def _get_piano_key_to_note(self):
        return self.piano.get_key_to_note()

    def _release_piano_note(self, note_name):
        self.piano._release_note(note_name)

    def _play_local_piano_note(self, note_name, velocity=None):
        self.piano.play_local_note(note_name, velocity)

    def _stop_local_piano_note(self, note_name):
        self.piano._stop_local_note(note_name)

    def _start_piano_midi(self):
        self.piano._start_midi()

    def _release_piano_midi_sustain(self):
        self.piano._release_midi_sustain()

    def _stop_all_piano_midi_notes(self):
        self.piano._stop_all_midi_notes()

    @staticmethod
    def _keyboard_sustain_is_down():
        try:
            return bool(pygame.key.get_pressed()[pygame.K_SPACE])
        except pygame.error:
            return False

    def _deactivate_piano_midi(self):
        self.piano._deactivate_midi()

    def _poll_piano_midi(self):
        self.piano.poll()

    def spectator_switch_player(self, mod):
        if not self.spectator_mode:
            return

        # Fade out current target's audio
        if self.camera.focus_object:
            self.fade_out_entity_audio(self.camera.focus_object)

        if hasattr(self, 'megaphone') and self.megaphone:
            self.megaphone.trigger_fade_transition(duration=0.8)

        self.game.network.send(consts.CHANNEL_MISC, "spectator_switch_player", {})

    def check_direction_in_play(self, mod=0):
        """Direction checks are available while playing, not while spectating."""
        if self.spectator_mode:
            return
        self.check_direction()

    def cycle_spectator_camera_if_active(self, mod=0):
        """Run the spectator-camera binding only during a Pong spectator view."""
        if self.spectator_mode and self.pong_arena:
            self.spectator_cycle_cam_mode()

    def check_direction(self):
        """Speak the current compass direction and spatialize cardinal alignment."""
        facing = self.player.hfacing % 360
        cardinal_directions = (0, 90, 180, 270)

        def angular_distance(first, second):
            return abs((first - second + 180) % 360 - 180)

        nearest = min(cardinal_directions, key=lambda direction: angular_distance(facing, direction))

        # Always speak the actual 16-way direction. The cue remains anchored
        # to the nearest cardinal so it can guide rotation through diagonals.
        speak(string_utils.direction(facing))

        # The source is anchored in the selected world direction, rather than
        # to the listener. Rotating away moves it naturally left or right.
        source_pos = movement.move(
            (self.player.x, self.player.y, self.player.z + 1), nearest, factor=4
        ).get_tuple
        snd = self.game.audio_mngr.play_unbound(
            "ui/facing.ogg",
            *source_pos,
            looping=False,
            volume=45,
            cat="ui",
        )
        if snd and snd.source:
            try:
                snd.source.reference_distance = 4.0
                snd.source.rolloff_factor = 0.5
            except Exception:
                pass

    def check_stats(self, mod=0):
        """Request player stats outside spectator camera mode."""
        if self.spectator_mode and self.pong_arena:
            return
        self.game.network.send(consts.CHANNEL_MISC, "stats", {})

    def spectator_cycle_cam_mode(self):
        """Cycle the Pong spectator ear: follow -> east edge -> west edge -> follow.
        east/west park the listener at the field edge facing across it, so both
        teams are heard left/right in stereo instead of from one player's head."""
        if not self.pong_arena:
            return
        order = ["follow", "east", "west"]
        try:
            idx = order.index(self.camera.spectator_cam_mode)
        except ValueError:
            idx = -1
        next_mode = order[(idx + 1) % len(order)]
        self.camera.set_spectator_cam_mode(next_mode, self.pong_arena)
        if next_mode == "follow":
            speak("Following player")
        else:
            # Announce which team is on which side based on the sideline geometry.
            # EAST (facing 270/west): Team 1 (p1_y, smaller Y) is LEFT, Team 2 RIGHT.
            # WEST (facing 90/east):  mirrored — Team 2 LEFT, Team 1 RIGHT.
            t1 = getattr(self, "pong_team1", "Team 1")
            t2 = getattr(self, "pong_team2", "Team 2")
            if next_mode == "east":
                speak(f"East side. {t1} on your left, {t2} on your right.")
            else:
                speak(f"West side. {t2} on your left, {t1} on your right.")

    def fade_out_entity_audio(self, entity):
        """Fade out or stop all sounds from an entity"""
        try:
            if hasattr(entity, 'soundgroup') and entity.soundgroup:
                # Stop all sounds from this entity's soundgroup
                entity.soundgroup.stop()
        except Exception:
            pass  # Silently ignore soundgroup errors
        
        try:
            if hasattr(entity, 'vc_source') and entity.vc_source:
                # Mute voice chat from this entity
                entity.vc_source.gain = 0.0
        except Exception:
            pass  # Silently ignore vc_source errors

    def enter(self):
        super().enter()
        self.game.network.put(("should_poll", True))
        self.ambience = self.game.audio_mngr.create_soundgroup(direct=True)
        # Do not reset voice_channels here. CHANNEL_MAP spawn packets may have
        # populated it before CHANNEL_MISC connected was delivered.
        self.megaphone = MegaphoneManager(self)
        pending_lock = getattr(self, '_pending_megaphone_lock_state', None)
        if isinstance(pending_lock, dict):
            self.megaphone.lock_owner = pending_lock.get('owner')
            owners = pending_lock.get('owners')
            if isinstance(owners, (list, tuple, set)):
                self.megaphone.lock_owners = set(owners)
        
        # === MAP MUSIC BOT ===
        self.music_bot = music_bot.MapMusicBot(self.game)
        # Wire gameplay reference into PianoAudio for megaphone broadcast routing
        self.game.audio_mngr.piano.gameplay = self
        self.game.audio_mngr.drums.gameplay = self
        



    # ============================================================================
    # PER-PLAYER MEGAPHONE SOURCE MANAGEMENT
    # Each player who speaks through the megaphone gets their own set of OpenAL
    # sources (one per physical speaker), preventing audio interleaving.
    # Max 8 concurrent players. Inactive players are auto-cleaned after 5 seconds.
    # ============================================================================

    MAX_MEGAPHONE_PLAYERS = 8





    def _check_speaker_occlusion(self, speaker_pos, player_pos):
        """Check if any solid tile blocks the path from speaker to player.
        Uses simple line-of-sight raycast to detect walls blocking sound."""
        
        # Simple implementation: check a few points along the line
        # from speaker to player for solid tiles
        try:
            sx, sy, sz = speaker_pos
            px, py, pz = player_pos
            
            # Get direction vector
            dx = px - sx
            dy = py - sy
            dz = pz - sz
            
            # Check 5 points along the line
            for i in range(1, 5):
                t = i / 5.0
                check_x = sx + dx * t
                check_y = sy + dy * t
                check_z = sz + dz * t
                
                # Check if there's a solid tile at this position
                tile = self.map.get_tile_at(int(check_x), int(check_y), int(check_z))
                if tile and hasattr(tile, 'solid') and tile.solid:
                    return True  # Blocked by wall
                    
            return False  # Clear line of sight
        except Exception:
            return False  # On error, assume not blocked
    

    def exit(self):
        super().exit()
        if getattr(self, "piano_mode", False):
            self.piano.stop(notify_server=False)
        if getattr(self, "drum_mode", False):
            self.drum.stop(notify_server=False)
        self.vehicle.cleanup()
        self.game.midi_hub.release_owner(self, reason="gameplay_exit")
        self._midi_lease = None
        if self.player.locked and self.game.network and getattr(self.game.network, 'event_handeler', None):
            self.game.network.event_handeler.death({"dead": False})
        if self.game.network:
            # NEVER join here. gameplay.exit() runs from game.pop(), which the
            # main loop invokes while holding game.lock (game.py loop_function
            # wraps st.update in the lock). The network worker acquires the
            # SAME lock around every received packet (networking.py Client.loop),
            # so if a chat echo or map packet arrived during the transition it
            # is parked on `with self.game.lock:` and join() would wait for it
            # forever -> the intermittent complete freeze seen when a chat
            # message is sent while a map transition is in flight. The worker
            # is a daemon: stop its polling and queue the terminator; it flushes
            # and exits on its own within a couple of milliseconds.
            network = self.game.network
            network.put(("should_poll", False))
            network.put(None)
            self.game.network = None
        try:
            from libs import logger as _logger
            _logger.log(
                "[TRANSITION] gameplay.exit: network teardown done (non-blocking)"
            )
        except Exception:
            pass
        self.ambience.destroy()
        self.pingging = False
        # === Cleanup Music Bot ===
        if hasattr(self, 'music_bot') and self.music_bot:
            self.music_bot.destroy()
            self.music_bot = None
        if getattr(self, 'jukebox_player', None):
            self.jukebox_player.stop_all()
            self.jukebox_player = None
        # Clear PianoAudio gameplay reference to prevent stale refs
        if hasattr(self.game, 'audio_mngr') and self.game.audio_mngr and hasattr(self.game.audio_mngr, 'piano'):
            self.game.audio_mngr.piano.reset()
            self.game.audio_mngr.piano.gameplay = None
        if hasattr(self.game, 'audio_mngr') and self.game.audio_mngr and hasattr(self.game.audio_mngr, 'drums'):
            self.game.audio_mngr.drums.reset()
            self.game.audio_mngr.drums.gameplay = None
        samples = getattr(getattr(self.game, "audio_mngr", None), "instrument_samples", None)
        if samples is not None:
            samples.clear()
        if hasattr(self, 'megaphone') and self.megaphone:
            self.megaphone._cleanup_megaphone_efx()
        if hasattr(self, 'wall_tone') and self.wall_tone:
            self.wall_tone.destroy()
        if hasattr(self, 'compass_turn_cue') and self.compass_turn_cue:
            self.compass_turn_cue.destroy()
        if hasattr(self, 'warlock_intro_illusion') and self.warlock_intro_illusion:
            self.warlock_intro_illusion.destroy()
        self.map.destroy()

                    
        # Note: EQ is currently fixed in initialization, 
        # but could be updated here if audio_manager supports parameter updates.

    def _check_speaker_occlusion(self, speaker_pos, player_pos):
        """Check if there's a solid wall/platform blocking line-of-sight between speaker and player.
        Uses a simple ray-march algorithm to check for solid tiles along the path.
        Returns True if blocked, False if clear line-of-sight."""
        
        # Get integer positions
        x1, y1, z1 = int(speaker_pos[0]), int(speaker_pos[1]), int(speaker_pos[2])
        x2, y2, z2 = int(player_pos[0]), int(player_pos[1]), int(player_pos[2])
        
        # Calculate distance and step count
        dx = x2 - x1
        dy = y2 - y1
        dz = z2 - z1
        distance = int(math.sqrt(dx*dx + dy*dy + dz*dz))
        
        if distance == 0:
            return False  # Same position, no occlusion
        
        # Step along the ray with a step size of 2.0 to reduce lookup count (walls are thick)
        # Cap max steps to 15 to prevent long-distance lookup lag spikes
        step_size = 2.0
        steps = max(1, int(distance / step_size))
        if steps > 15:
            steps = 15
        
        for i in range(1, steps):  # Skip start point (speaker), check middle points
            t = i / steps
            check_x = int(x1 + dx * t)
            check_y = int(y1 + dy * t)
            check_z = int(z1 + dz * t)
            
            # Get tile at this position
            tile = self.map.get_tile_at(check_x, check_y, check_z)
            
            # Check if tile is solid (wall or solid floor that blocks sound)
            if tile.startswith("wall"):
                return True  # Blocked by wall
            # Note: We don't block on regular floors (concrete, wood, etc.)
            # as sound can travel over/around them. Only explicit walls block.
        
        return False  # Clear line-of-sight

    def update_megaphone_settings(self, volume, bass, mid, high):
        """Forwarder for the megaphone settings menu (megaphone_settings.py calls
        self.parrent.update_megaphone_settings). Megaphone logic now lives in the
        MegaphoneManager, so delegate to it. Without this, the Speaker Vol slider
        in the settings menu silently does nothing."""
        if hasattr(self, 'megaphone') and hasattr(self.megaphone, 'update_megaphone_settings'):
            self.megaphone.update_megaphone_settings(volume, bass, mid, high)

    def update(self, events):
        audio_probe.call("gp.wind", self._update_horse_wind)
        audio_probe.call("gp.megaphone", self.megaphone.update_megaphone_audio, 0, None)
        audio_probe.call("gp.wall_tone", self.wall_tone.update)
        audio_probe.call("gp.compass", self.compass_turn_cue.update)
        warlock_intro = getattr(self, "warlock_intro_illusion", None)
        if warlock_intro is not None:
            audio_probe.call("gp.warlock_intro", warlock_intro.update)
        if self.guitar.active and self.guitar.instrument_input and not self.spectator_mode:
            # Guitar audio is raw-only: the player's own strums play back 3D
            # through the local monitor, and nearby players hear the real
            # pedal/guitar sound streamed on the 3D voice channel. Piano
            # placeholder notes are intentionally NOT played/broadcast so the
            # real sound is what comes out, not a fake piano sample.
            audio_probe.call("gp.guitar", self.guitar.instrument_input.drain_notes)
            audio_probe.call("gp.guitar", self.guitar.feed_monitor)
        if not self.spectator_mode:
            audio_probe.call("gp.player", self.player.loop)
        elif not self.substates:
            # Filter events for spectator mode when idle (Allow ESC, TAB, Chat, RETURN, Brackets, PageUp/Down, and Comma/Period)
            allowed_keys = [
                pygame.K_TAB, pygame.K_ESCAPE, pygame.K_QUOTE, pygame.K_SLASH, pygame.K_RETURN,
                pygame.K_LEFTBRACKET, pygame.K_RIGHTBRACKET, pygame.K_PAGEUP, pygame.K_PAGEDOWN,
                pygame.K_COMMA, pygame.K_PERIOD, pygame.K_p,
                self.kc.get("music_bot_toggle", pygame.K_m),
                self.kc.get("music_bot_vol_down", pygame.K_F9),
                self.kc.get("music_bot_vol_up", pygame.K_F10),
            ]
            allowed_keys.extend(key for key, _ in self.configurable_key_actions)
            events = [e for e in events if e.type == pygame.KEYDOWN and e.key in allowed_keys]
        if not self.spectator_mode:
            if not self.player.drownable and self.player.drown_clock.elapsed >= 30000 and not self.player.dead: self.player.drownable=True
            if self.player.in_water and self.player.drown_clock.elapsed>=3000 and not self.player.dead and self.player.drownable and not self.player.lock_weapon: 
                self.player.hp -= 5
                self.player.play_sound("foley/swim/drown/", looping=False, id="drown", volume=100, cat="self")
                self.game.network.send(
                    consts.CHANNEL_MISC,
                    "set_hp",
                    {"amount": self.player.hp}
                )
                self.player.drown_clock.restart()
        for entity in self.map.entities.values(): 
            entity.player_dead=True if self.player.dead else False
        audio_probe.call("gp.map", self.map.loop)
        with audio_probe.span("gp.sources"):
            for i in self.map.source_list.copy():
                i.loop(self.camera.focus_object.x, self.camera.focus_object.y, self.camera.focus_object.z)
        
        # === Music Bot loop (auto-advance tracks) ===
        if hasattr(self, 'music_bot') and self.music_bot:
            audio_probe.call("gp.music_bot", self.music_bot.loop)

        # Detect a stopped/stalled jukebox receiver and ask the server for the
        # authoritative playback state plus relay warm-up frames.  This is
        # deliberately main-thread work: it only performs throttled network
        # recovery and never touches OpenAL from the gameplay loop.
        if getattr(self, 'jukebox_player', None):
            audio_probe.call("gp.jukebox", self.jukebox_player.update)
        
        # === Tracking beacon & facing sound update ===
        if getattr(self, "tracking_target", None) is not None:
            target_type, obj, pos = self.tracking_target
            
            # Revalidate dynamic objects before updating or playing their beacon.
            if target_type == "entity":
                if audio_probe.call("gp.tracking", self._validate_tracking_target):
                    pos = (obj.x, obj.y, obj.z)
                    self.tracking_target = (target_type, obj, pos)
                    
            if getattr(self, "tracking_target", None) is not None:
                # Play facing.ogg at target's 3D coordinates every 1.2 seconds.
                # Pitch rises when facing the target and falls when walking past
                # / facing away, so it reads like a radar sweep.
                if self.tracking_clock.elapsed >= 1200:
                    self.tracking_clock.restart()
                    pitch = audio_probe.call("gp.tracking", self._beacon_pitch, pos[0], pos[1])
                    snd = audio_probe.call("gp.tracking", self.game.audio_mngr.play_unbound,
                        "ui/facing.ogg",
                        pos[0], pos[1], pos[2],
                        looping=False,
                        volume=35,
                        cat="miscelaneous",
                        pitch=pitch,
                    )
                    if snd and snd.source:
                        snd.source.reference_distance = 15.0
                        snd.source.rolloff_factor = 0.5

                        # Apply player's current reverb slot for map environmental reverb
                        reverb_slot = getattr(self, 'current_player_reverb_slot', None)
                        if reverb_slot:
                            try:
                                audio_probe.call("gp.tracking", self.game.audio_mngr.efx.send, snd.source, 3, reverb_slot)
                            except Exception:
                                pass
        

        
        should_block = audio_probe.call("gp.substate", super().update, events)
        if should_block is True:
            # some substate doesnt want us to handel events for now.
            return
        elif isinstance(should_block, list):
            events = should_block
        key = audio_probe.call("gp.input_poll", pygame.key.get_pressed)
        is_concert = getattr(self, 'concert_spectator_mode', False)
        if not self.spectator_mode or is_concert:
            if not getattr(self, 'piano_mode', False) and not getattr(self, 'drum_mode', False) and not self.vehicle.active:
                for i in self.keys_held:
                    if key[i]:
                        audio_probe.call("gp.input_held", self.keys_held[i], pygame.key.get_mods())
        if getattr(self, "piano_mode", False):
            audio_probe.call("gp.input_instrument", self._poll_piano_midi)
        elif getattr(self, "drum_mode", False):
            audio_probe.call("gp.input_instrument", self._poll_drum_midi)
        for event in events:
            if getattr(self, "drum_mode", False):
                if audio_probe.call("gp.input_instrument", self.drum.handle_event, event):
                    continue
            if getattr(self, 'piano_mode', False):
                if audio_probe.call("gp.input_instrument", self.piano.handle_event, event):
                    continue
            if self.vehicle.active and audio_probe.call("gp.input_dispatch", self.vehicle.handle_event, event):
                continue
            if event.type == pygame.KEYDOWN and event.key == pygame.K_SPACE and getattr(self.game, 'pong_mode', False):
                self.game.network.send(consts.CHANNEL_MAP, "pong_serve", {})
                continue
            if self.spectator_mode:
                allowed_keys = [
                    pygame.K_TAB, pygame.K_ESCAPE, pygame.K_QUOTE, pygame.K_SLASH, pygame.K_RETURN,
                    pygame.K_LEFTBRACKET, pygame.K_RIGHTBRACKET, pygame.K_PAGEUP, pygame.K_PAGEDOWN,
                    pygame.K_COMMA, pygame.K_PERIOD, pygame.K_p,
                    self.kc.get("music_bot_toggle", pygame.K_m),
                    self.kc.get("music_bot_vol_down", pygame.K_F9),
                    self.kc.get("music_bot_vol_up", pygame.K_F10),
                ]
                allowed_keys.extend(key for key, _ in self.configurable_key_actions)
                if event.type == pygame.KEYDOWN and event.key in allowed_keys:
                    audio_probe.call("gp.input_dispatch", self._dispatch_configurable_key_actions, event)
                    if event.key in self.keys_pressed:
                        audio_probe.call("gp.input_pressed", self.keys_pressed[event.key], event.mod)
            else:
                if event.type == pygame.KEYDOWN:
                    audio_probe.call("gp.input_dispatch", self._dispatch_configurable_key_actions, event)
                    if event.key in self.keys_pressed:
                        audio_probe.call("gp.input_pressed", self.keys_pressed[event.key], event.mod)
                elif event.type == pygame.KEYUP and event.key in self.keys_released:
                    audio_probe.call("gp.input_released", self.keys_released[event.key], event.mod)
            if not pygame.event.get_grab():
                pygame.event.set_grab(True)
            if event.type == pygame.MOUSEBUTTONDOWN and not self.spectator_mode:
                if event.button == 1:
                    self.game.mouse_buttons["left"] = True
                if event.button == 2:
                    self.game.mouse_buttons["middle"] = True
                if event.button == 3:
                    self.game.mouse_buttons["right"] = True
            if event.type == pygame.MOUSEBUTTONUP and not self.spectator_mode:
                if event.button == 1:
                    self.game.mouse_buttons["left"] = False
                if event.button == 2:
                    self.game.mouse_buttons["middle"] = False
                if event.button == 3:
                    self.game.mouse_buttons["right"] = False
            if event.type == pygame.MOUSEWHEEL and self.game_started and not self.spectator_mode:
                if not self.wmanager.activeWeapon:
                    audio_probe.call("gp.input_mouse", self.wmanager.switchWeapon, 0)
                pos = self.wmanager.weapons.index(self.wmanager.activeWeapon)
                num_weapons = len(self.wmanager.weapons)
                # Scroll through all available weapon slots cyclically
                if event.y < 0:
                    next_pos = (pos + 1) % num_weapons
                else:
                    next_pos = (pos - 1) % num_weapons
                audio_probe.call("gp.input_mouse", self.wmanager.switchWeapon, next_pos)
            if event.type == pygame.MOUSEMOTION and not self.spectator_mode:
                (x, y) = event.rel
                if x == 0:
                    audio_probe.call("gp.input_mouse", self.turn_stop, pygame.K_a)
                if x < -1 or x > 1:
                    audio_probe.call("gp.input_mouse", self.player.face, self.player.hfacing + (x / 2), self.player.vfacing)
 
        if self.game.mouse_buttons["left"] and not self.spectator_mode:
            audio_probe.call("gp.input_mouse", self.wmanager.reload)
        if self.game.mouse_buttons["middle"] and not self.spectator_mode:
            audio_probe.call("gp.input_mouse", self.interact, pygame.K_f)
        if self.game.mouse_buttons["right"] and not self.spectator_mode:
            if self.wmanager.activeWeapon and self.wmanager.activeWeapon.automatic:
                audio_probe.call("gp.input_mouse", self.fire_weapon_automatic, pygame.K_SPACE)
            elif self.wmanager.activeWeapon:
                audio_probe.call("gp.input_mouse", self.fire_weapon_non_automatic, pygame.K_SPACE)
                self.game.mouse_buttons["right"] = False

    def buffer_move_l(self, mod=0):
        if mod & pygame.KMOD_SHIFT:
            return buffer.cycle_item(3)
        buffer.cycle_item(1)

    def start_raise_shield(self, mod=0):
        if not self.spectator_mode and not getattr(self.player, 'dead', False):
            if not self.shield_mngr.equipped_shield:
                speak("No shield equipped.")
                return
            self.shield_mngr.raise_shield()
            self.game.network.send(consts.CHANNEL_MISC, "raise_shield", {"angle": self.player.hfacing})

    def stop_raise_shield(self, mod=0):
        if not self.spectator_mode and not getattr(self.player, 'dead', False) and self.shield_mngr.is_raising:
            self.shield_mngr.lower_shield()
            self.game.network.send(consts.CHANNEL_MISC, "lower_shield", {"angle": self.player.hfacing})

    # key event handelers:
    def buffer_move_r(self, mod=0):
        if mod & pygame.KMOD_SHIFT:
            return buffer.cycle_item(4)
        buffer.cycle_item(2)

    def buffer_cycle_l(self, mod=0):
        if mod & pygame.KMOD_CTRL:
            if hasattr(self, 'music_bot') and self.music_bot and self._can_use_music_bot():
                self.music_bot.previous_feed_track()
            return
        if mod & pygame.KMOD_SHIFT:
            return buffer.cycle(3)
        buffer.cycle(1)

    def buffer_cycle_r(self, mod=0):
        if mod & pygame.KMOD_CTRL:
            if hasattr(self, 'music_bot') and self.music_bot and self._can_use_music_bot():
                self.music_bot.next_feed_track()
            return
        if mod & pygame.KMOD_SHIFT:
            return buffer.cycle(4)
        buffer.cycle(2)

    def chat(self, mod=0):
        try:
            from libs import logger as _logger
            _logger.log("[CHAT] input opened")
        except Exception:
            pass
        self.add_substate(
            self.game.input.run(
                "Enter a chat message or a slash command", handeler=self.chat2
            )
        )

    def chat2(self, message):
        if len(message) > 2000 and not message.startswith("/setmapdata"):
            return speak("message too long")
        if not message.lstrip().rstrip():
            return self.cancel()
        if len(message) <= 1:
            return self.cancel("Message is too short.")
        try:
            from libs import logger as _logger
            _logger.log(
                f"[CHAT] submit: len={len(message)} "
                f"is_command={message.lstrip().startswith('/')} "
                f"network={'alive' if self.game.network else 'None'}"
            )
        except Exception:
            pass
        self.game.network.send(consts.CHANNEL_CHAT, "chat", {"message": message})
        self.pop_last_substate()
        try:
            from libs import logger as _logger
            _logger.log("[CHAT] submit done (packet queued non-blocking)")
        except Exception:
            pass

    def map_chat(self, mod=0):
        self.add_substate(
            self.game.input.run(
                "Enter a map chat message or slash command", handeler=self.map_chat2
            )
        )



    def map_chat2(self, message):
        if len(message) > 2000 and not message.startswith("/setmapdata"):
            return speak("message too long")
        if not message.lstrip().rstrip():
            return self.cancel()
        if len(message) <= 1:
            return self.cancel("Message is too short.")
        self.game.network.send(consts.CHANNEL_CHAT, "chat", {"message": f"/mc {message}"})
        self.pop_last_substate()


    def quit(self, mod):
        self.game.audio_mngr.apply_filter(None)
        self.game.network.send(consts.CHANNEL_MISC, "logout", {"message": True})
        buffer.export_buffers()
        if self.voice_chat:
            self.voice_chat.close()
        self.guitar.cleanup()

    def ping(self, mod):
        if not self.pingging:
            self.game.network.send(consts.CHANNEL_PING, "ping", {})
            self.pingging = True
            self.last_ping_time = time.time()

    def who_online(self, mod=None):
        self.game.network.send(consts.CHANNEL_MISC, "who_online", {})

    # movement
    def strafe_left(self, mod):
        if getattr(self.game, 'pong_mode', False):
            keys = pygame.key.get_pressed()
            if keys[pygame.K_q] or keys[pygame.K_e]:
                return
        self.wall_tone.preview_movement(self.player.hfacing - 90)
        tile_factor = 3.0 if self.map.get_tile_at(self.player.x, self.player.y, self.player.z) in ["deep_water", "underwater"] else 1.0
        effective_movetime = getattr(self.game, 'pong_speed', 60) if getattr(self.game, 'pong_mode', False) else (self.player.runtime if getattr(self, 'running', False) else self.player.movetime)
        if self.player.movement_clock.elapsed >= effective_movetime * tile_factor:
            self.player.movement_clock.restart()
            mode = "run" if (getattr(self, 'running', False) or getattr(self.game, 'pong_mode', False)) else "walk"
            self.player.walk(left=True, mode=mode, send=True)

    def strafe_right(self, mod):
        if getattr(self.game, 'pong_mode', False):
            keys = pygame.key.get_pressed()
            if keys[pygame.K_q] or keys[pygame.K_e]:
                return
        self.wall_tone.preview_movement(self.player.hfacing + 90)
        tile_factor = 3.0 if self.map.get_tile_at(self.player.x, self.player.y, self.player.z) in ["deep_water", "underwater"] else 1.0
        effective_movetime = getattr(self.game, 'pong_speed', 60) if getattr(self.game, 'pong_mode', False) else (self.player.runtime if getattr(self, 'running', False) else self.player.movetime)
        if self.player.movement_clock.elapsed >= effective_movetime * tile_factor:
            self.player.movement_clock.restart()
            mode = "run" if (getattr(self, 'running', False) or getattr(self.game, 'pong_mode', False)) else "walk"
            self.player.walk(right=True, mode=mode, send=True)

    def is_in_minigame(self):
        if getattr(self.game, 'pong_mode', False):
            return True
        if getattr(self, 'in_minigame_match', False):
            return True
        return False

    def move_forward(self, mod, turn=False):
        if self.is_in_minigame():
            return
        if turn and self.turn_mod:
            self.player.face(0, 0)
            self.turning = True
            return self.turn_stop(mod)
        self.wall_tone.preview_movement(self.player.hfacing)
        tile_factor = 3.0 if self.map.get_tile_at(self.player.x, self.player.y, self.player.z) in ["deep_water", "underwater"] else 1.0
        if (
            not self.turn_mod
            and self.player.movement_clock.elapsed >= self.player.movetime * tile_factor
        ):
            self.player.movement_clock.restart()
            mode = "run" if self.running else "walk"
            self.player.walk(mode=mode, send=True)

    def turn_left(self, mod, turn=False):
        if getattr(self.game, 'pong_mode', False):
            self.strafe_left(mod)
            return
        if self.player.locked:
            return
        if turn:
            if not self.turn_mod:
                return self.turn_start(mod)
            self.turning = True
            return self.player.face(self.player.hfacing - 45, self.player.vfacing)
        if (
            not self.turn_mod
            and self.player.turning_clock.elapsed >= self.player.turntime
        ):
            self.player.turning_clock.restart()
            self.turning = True
            amount = options.get_turning_step() * (2 if self.running else 1)
            self.player.face(self.player.hfacing - amount, self.player.vfacing)
            self.compass_turn_cue.on_turn(self.player.hfacing)

    def move_back(self, mod, turn=False):
        if self.is_in_minigame():
            return
        if turn and self.turn_mod:
            self.player.turning_clock.restart()
            self.player.face(self.player.hfacing + 180, 0)
            self.turning = True
            return self.turn_stop(mod)
        self.wall_tone.preview_movement(self.player.hfacing + 180)
        tile_factor = 3.0 if self.map.get_tile_at(self.player.x, self.player.y, self.player.z) in ["deep_water", "underwater"] else 1.0
        if (
            not self.turn_mod
            and self.player.movement_clock.elapsed >= self.player.movetime * tile_factor
        ):
            self.player.movement_clock.restart()
            mode = "run" if self.running else "walk"
            self.player.walk(back=True, mode=mode, send=True)

    def turn_right(self, mod, turn=False):
        if getattr(self.game, 'pong_mode', False):
            self.strafe_right(mod)
            return
        if self.player.locked:
            return
        if turn:
            if not self.turn_mod:
                return self.turn_start(mod)
            self.turning = True
            return self.player.face(self.player.hfacing + 45, self.player.vfacing)
        if (
            not self.turn_mod
            and self.player.turning_clock.elapsed >= self.player.turntime
        ):
            self.player.turning_clock.restart()
            self.turning = True
            amount = options.get_turning_step() * (2 if self.running else 1)
            self.player.face(self.player.hfacing + amount, self.player.vfacing)
            self.compass_turn_cue.on_turn(self.player.hfacing)

    def move_up(self, mod):
        tile_factor = 3.0 if self.map.get_tile_at(self.player.x, self.player.y, self.player.z) in ["deep_-water", "underwater"] else 1.0
        if self.player.movement_clock.elapsed >= self.player.movetime * tile_factor:
            self.player.movement_clock.restart()
            mode = "run" if self.running else "walk"
            self.player.walk(up=True, mode=mode, send=True)

    def move_down(self, mod):
        tile_factor = 3.0 if self.map.get_tile_at(self.player.x, self.player.y, self.player.z) in ["deep_water", "underwater"] else 1.0
        if self.player.movement_clock.elapsed >= self.player.movetime * tile_factor:
            self.player.movement_clock.restart()
            mode = "run" if self.running else "walk"
            self.player.walk(down=True, mode=mode, send=True)


    def pitch_down(self, mod, turn=False):
        if getattr(self.game, 'pong_mode', False):
            return
        if self.player.locked:
            return
        if turn:
            if not self.turn_mod:
                return self.turn_start(mod)
            self.player.turning_clock.restart()
            if self.player.vfacing >= -45:
                self.turning = True
                return self.player.face(self.player.hfacing, self.player.vfacing - 45)
        if (
            not self.turn_mod
            and self.player.turning_clock.elapsed >= self.player.turntime
        ):
            self.player.turning_clock.restart()
            self.turning = True
            if self.player.vfacing > -90:
                amount = options.get_turning_step()
                self.player.face(
                    self.player.hfacing,
                    max(-90, self.player.vfacing - amount),
                )

    def pitch_up(self, mod, turn=False):
        if getattr(self.game, 'pong_mode', False):
            return
        if self.player.locked:
            return
        if turn:
            if not self.turn_mod:
                return self.turn_start(mod)
            self.player.turning_clock.restart()
            if self.player.vfacing <= 45:
                self.turning = True
                return self.player.face(self.player.hfacing, self.player.vfacing + 45)
        if (
            not self.turn_mod
            and self.player.turning_clock.elapsed >= self.player.turntime
        ):
            self.player.turning_clock.restart()
            self.turning = True
            if self.player.vfacing < 90:
                amount = options.get_turning_step()
                self.player.face(
                    self.player.hfacing,
                    min(90, self.player.vfacing + amount),
                )

    def turn_start(self, mod):
        self.player.play_sound("foley/turn/start.ogg", cat="self")

    def turn_stop(self, mod):
        if not self.turning:
            return
        self.turning = False
        self.compass_turn_cue.stop_turning()
        if not self.player.locked:
            self.player.play_sound("foley/turn/stop.ogg", cat="self")
            if options.get("speak_on_turn", False):
                speak(f"turned to {self.player.hfacing} degrees")

    def pitch_stop(self, mod):
        if not self.turning:
            return
        self.turning = False
        if not self.player.locked:
            self.player.play_sound("foley/turn/stop.ogg", cat="self")
            speak(f"turned to {self.player.vfacing} degrees")

    def run_start(self, mod):
        if not self.running and self.can_run:
            self.player.play_sound("foley/run/start.ogg", cat="self")
            self.running = True
            self.player.movetime = self.player.runtime

    def run_stop(self, mod):
        if self.running:
            self.player.play_sound("foley/run/stop.ogg", cat="self")
            self.running = False
            self.player.movetime = self.player.walktime

    def _strafe_key_down(self, mod):
        """Strafe key pressed — only cancel running when SHIFT is NOT held.

        When the player holds SHIFT and presses a strafe direction the
        original code unconditionally called run_stop + can_run=False,
        which made running impossible with arrow keys (the default
        strafe binds).  Keeping the existing no-auto-run behaviour for
        bare strafe presses while preserving an active run when the
        player explicitly holds the run key.
        """
        if not (mod & pygame.KMOD_SHIFT):
            self.can_run = False
            self.run_stop(mod)

    # tracking system
    def get_relative_direction_string(self, tx, ty, tz):
        return describe_tracking_direction(
            tx - self.player.x, ty - self.player.y, tz - self.player.z,
            self.player.hfacing,
        )

    def _format_target_location(self, dist, tx, ty, tz):
        """Build the 'direction (X tiles away)' suffix shown next to a trackable.
        When the player is standing on the object (dist == 0), report 'right here'
        instead of a compass direction, since the bearing is meaningless there."""
        if dist <= 0:
            return "right here"
        direction_str = self.get_relative_direction_string(tx, ty, tz)
        unit = "tile" if dist == 1 else "tiles"
        return f"{direction_str} ({dist} {unit} away)"

    def _beacon_pitch(self, tx, ty):
        """Compute a tracking-beacon pitch (0.8..1.2) from how squarely the
        player is facing the target. Facing it head-on -> highest pitch;
        walking past / facing away -> lowest. This sits on top of the 3D
        positional volume, so it reads like a radar sweep."""
        dx = tx - self.player.x
        dy = ty - self.player.y
        rad = math.atan2(dx, dy)
        rel = math.degrees(rad) - self.player.hfacing
        while rel <= -180:
            rel += 360
        while rel > 180:
            rel -= 360
        # cos(0)=1 (front) -> 1.2 ; cos(90)=0 (side) -> 1.0 ; cos(180)=-1 (behind) -> 0.8
        return 1.0 + 0.2 * math.cos(math.radians(rel))

    def open_tracking_menu(self, mod):
        if self.player.dead:
            return

        if not self._validate_tracking_target() and mod & pygame.KMOD_ALT:
            return

        # If Alt+T is pressed and we are currently tracking something, report status directly
        if mod & pygame.KMOD_ALT and getattr(self, "tracking_target", None) is not None:
            target_type, obj, pos = self.tracking_target
            if target_type == "entity":
                pos = (obj.x, obj.y, obj.z)

            dist = math.floor(movement.get_3d_distance(self.player.x, self.player.y, self.player.z, pos[0], pos[1], pos[2]))
            location_str = self._format_target_location(dist, pos[0], pos[1], pos[2])

            name = self._get_target_label(target_type, obj)
            speak(f"{name}: {location_str}")
            return

        trackables = self._gather_trackables()

        # Sort closest first
        trackables.sort(key=lambda x: x[0])

        # Build menu items
        menu_items = []

        # Prepend "Stop Tracking" if currently tracking
        if getattr(self, "tracking_target", None) is not None:
            menu_items.append(("Stop Tracking", self.stop_tracking))

        for dist, label, location_str, target_info in trackables:
            callback = partial(self.start_tracking, target_info)
            menu_items.append((f"{label}: {location_str}", callback))

        menu_items.append(("Cancel", self.pop_last_substate))

        if not menu_items or (len(menu_items) == 1 and menu_items[0][0] == "Cancel"):
            speak("No trackable objects nearby.")
            return

        # Display menu using Menu
        m = menu.Menu(self.game, "Select object to track", parrent=self)
        m.add_items(menu_items)
        menus.set_default_sounds(m)
        self.add_substate(m)

    def _clean_name(self, name):
        """Clean up a raw object/entity name for display.
        Strips trailing id suffixes, maps known names, and splits CamelCase."""
        import re
        name = re.sub(r'[-_]\d+$', '', name)  # removes -11 or _1
        name = re.sub(r'\d+$', '', name)      # removes trailing numbers
        if name.lower().startswith("zomby"):
            return "Zombie"
        if name.lower() == "powerswitch":
            return "Power Switch"
        return re.sub(r'(?<!^)(?=[A-Z])', ' ', name).strip()

    def _get_target_label(self, target_type, obj):
        """Resolve the human-readable label for a tracked target of any type.
        Pulls the real item name from server-synced data automatically."""
        if target_type == "door":
            return "Door"
        if target_type == "wallbuy":
            # weaponName is the real weapon name, e.g. "MP7"
            return obj.weaponName or "Weapon Buy"
        if target_type == "interactable":
            return getattr(obj, "label", None) or "Interactable"
        if target_type == "perkMachine":
            return getattr(obj, "label", None) or "Perk Machine"
        if target_type == "minigameTable":
            return getattr(obj, "label", None) or "Arcade"
        if target_type == "zone":
            return getattr(obj, "zonename", None) or "Zone"
        if target_type == "entity":
            return self._clean_name(obj.name)
        return "Object"

    def _is_trackable_entity(self, obj):
        """Only current, non-destroyed object presentations; never player actors."""
        return (
            getattr(obj, "object_tracking", False) is True
            and not getattr(obj, "player", False)
            and not getattr(obj, "dead", False)
            and getattr(obj, "name", None) != self.player.name
            and self.map.entities.get(getattr(obj, "name", None)) is obj
        )

    def _validate_tracking_target(self):
        target = getattr(self, "tracking_target", None)
        if target is not None and target[0] == "entity" and not self._is_trackable_entity(target[1]):
            self.tracking_target = None
            speak("Tracking target lost.")
            return False
        return True

    def _gather_trackables(self):
        """Collect all trackable objects around the player.
        Returns a list of (dist, label, location_str, (type_key, obj, pos)).
        location_str is 'direction (X tiles away)' (or 'right here' when on top).
        Excludes players, animals, monsters, helper NPCs, and walls."""
        trackables = []

        # 1. Gather Doors (filter out duplicate door IDs)
        seen_door_ids = set()
        for door in self.map.door_list:
            if door.id in seen_door_ids:
                continue
            seen_door_ids.add(door.id)

            cx = (door.minx + door.maxx) / 2
            cy = (door.miny + door.maxy) / 2
            cz = (door.minz + door.maxz) / 2
            dist = math.floor(movement.get_3d_distance(self.player.x, self.player.y, self.player.z, cx, cy, cz))
            location_str = self._format_target_location(dist, cx, cy, cz)
            trackables.append((dist, "Door", location_str, ("door", door, (cx, cy, cz))))

        # 2. Gather Wallbuys (show real weapon name + cost)
        for wb in self.map.wallbuy_list:
            cx = (wb.minx + wb.maxx) / 2
            cy = (wb.miny + wb.maxy) / 2
            cz = (wb.minz + wb.maxz) / 2
            dist = math.floor(movement.get_3d_distance(self.player.x, self.player.y, self.player.z, cx, cy, cz))
            location_str = self._format_target_location(dist, cx, cy, cz)
            label = f"{wb.weaponName}, {wb.weaponCost} points" if wb.weaponName else "Weapon Buy"
            trackables.append((dist, label, location_str, ("wallbuy", wb, (cx, cy, cz))))

        # 3. Gather Interactables
        for obj in self.map.interactable_list:
            cx = (obj.minx + obj.maxx) / 2
            cy = (obj.miny + obj.maxy) / 2
            cz = (obj.minz + obj.maxz) / 2
            dist = math.floor(movement.get_3d_distance(self.player.x, self.player.y, self.player.z, cx, cy, cz))
            location_str = self._format_target_location(dist, cx, cy, cz)
            label = obj.label or "Interactable"
            trackables.append((dist, label, location_str, ("interactable", obj, (cx, cy, cz))))

        # 4. Gather Perk Machines
        for obj in self.map.perk_machine_list:
            cx = (obj.minx + obj.maxx) / 2
            cy = (obj.miny + obj.maxy) / 2
            cz = (obj.minz + obj.maxz) / 2
            dist = math.floor(movement.get_3d_distance(self.player.x, self.player.y, self.player.z, cx, cy, cz))
            location_str = self._format_target_location(dist, cx, cy, cz)
            label = obj.label or "Perk Machine"
            trackables.append((dist, label, location_str, ("perkMachine", obj, (cx, cy, cz))))

        # 5. Gather Minigame/Arcade Tables
        for obj in self.map.minigame_table_list:
            cx = (obj.minx + obj.maxx) / 2
            cy = (obj.miny + obj.maxy) / 2
            cz = (obj.minz + obj.maxz) / 2
            dist = math.floor(movement.get_3d_distance(self.player.x, self.player.y, self.player.z, cx, cy, cz))
            location_str = self._format_target_location(dist, cx, cy, cz)
            label = obj.label or "Arcade"
            trackables.append((dist, label, location_str, ("minigameTable", obj, (cx, cy, cz))))

        # 6. Gather Zones (named areas)
        for zone in self.map.zone_list:
            cx = (zone.minx + zone.maxx) / 2
            cy = (zone.miny + zone.maxy) / 2
            cz = (zone.minz + zone.maxz) / 2
            dist = math.floor(movement.get_3d_distance(self.player.x, self.player.y, self.player.z, cx, cy, cz))
            location_str = self._format_target_location(dist, cx, cy, cz)
            label = zone.zonename or "Zone"
            trackables.append((dist, label, location_str, ("zone", zone, (cx, cy, cz))))

        # 7. Gather server-identified objects, not players or living creatures.
        for name, ent in self.map.entities.items():
            if not self._is_trackable_entity(ent):
                continue
            dist = math.floor(movement.get_3d_distance(self.player.x, self.player.y, self.player.z, ent.x, ent.y, ent.z))
            location_str = self._format_target_location(dist, ent.x, ent.y, ent.z)
            cleaned_label = self._clean_name(name)
            trackables.append((dist, cleaned_label, location_str, ("entity", ent, (ent.x, ent.y, ent.z))))

        return trackables

    def start_tracking(self, target_info):
        target_type, obj, pos = target_info
        if target_type == "entity":
            if not self._is_trackable_entity(obj):
                if self._validate_tracking_target():
                    speak("This target is no longer available for object tracking.")
                if self.substates:
                    self.pop_last_substate()
                return
            pos = (obj.x, obj.y, obj.z)
        self.tracking_target = (target_type, obj, pos)

        name = self._get_target_label(target_type, obj)
        dist = math.floor(movement.get_3d_distance(
            self.player.x, self.player.y, self.player.z, *pos,
        ))
        location_str = self._format_target_location(dist, *pos)
        speak(f"{name}: {location_str}")
        self.tracking_clock = self.game.new_clock()
        self.is_facing_target = False
        if len(self.substates) > 0:
            self.pop_last_substate()

    def stop_tracking(self):
        self.tracking_target = None
        speak("Tracking stopped.")
        if len(self.substates) > 0:
            self.pop_last_substate()

    # stats
    def speak_location(self, mod):
        target = self.camera.focus_object
        template = options.get(
            "location_template",
            "{x}, \r\n{y}, \r\n{z}, \r\nOn {tile} \r\nFacing {direction} at {angle} degrees with a pitch of {pitch} degrees. \r\nYou are leaning by {lean} degrees and you are {balanced}. ",
        )
        balanced = "balanced"
        if target.bfacing < -30 or target.bfacing > 30:
            balanced = "unbalanced"
        
        # Use actual coordinates (supports negative values)
        actual_x = round(target.x)
        actual_y = round(target.y)
        actual_z = round(target.z)
        
        try:
            speak(
                template.format(
                    x=actual_x,
                    y=actual_y,
                    z=actual_z,
                    x_rounded=actual_x,
                    y_rounded=actual_y,
                    z_rounded=actual_z,
                    tile=target.map.get_tile_at(target.x, target.y, target.z),
                    direction=string_utils.direction(target.hfacing),
                    angle=target.hfacing,
                    pitch=target.vfacing,
                    lean=target.bfacing,
                    balanced=balanced,
                )
            )
        except:
            speak(
                "This location template causes an error. Check that brackets are valid and or variable names"
            )


    def speak_zone(self, mod):
        focus = self.camera.focus_object
        tp = focus.map.get_travel_point_at(focus.x, focus.y, focus.z)
        if tp:
            key = string_utils.friendly_key_name(
                self.kc.get("interact", pygame.K_f)
            ).upper()
            speak(
                f"You are at a travel point to {tp.target_map}. "
                f"Press Shift plus {key} to travel."
            )
            return
        zone_name = focus.map.get_zone_at(focus.x, focus.y, focus.z)
        speak(str(zone_name) if zone_name else "No zone")

    def speak_fps(self, mod):
        speak(f"{self.game.last_fps} FPS")

    def server_message(self, mod):
        self.game.network.send(consts.CHANNEL_MISC, "server_message")

    def online_server_list(self, mod):
        self.game.network.send(consts.CHANNEL_MISC, "who_online_m")

    def open_inventory(self, mod):
        if not self.player.dead: self.game.network.send(consts.CHANNEL_MISC, "open_inventory")

    def open_staff_menu(self, mod):
        if getattr(self, "is_staff", False):
            self.game.network.send(consts.CHANNEL_MENUS, "staff_menu_open", {})

    def get_hp(self, mod):
        if self.player.lock_weapon: return
        self.game.network.send(consts.CHANNEL_MISC, "get_hp")

    def player_radar(self, mod):
        if mod & pygame.KMOD_ALT:
            self.game.network.send(
                consts.CHANNEL_MENUS, "open_drop_menu", {}
            )
            return
        if not self.player.dead: self.game.network.send(consts.CHANNEL_MAP, "player_radar", {"radius": 5})

    def open_builder(self, mod):
        self.game.network.send(consts.CHANNEL_MAP, "open_builder", {"angle": self.player.hfacing})
    
    

    def buffer_options(self, mod):
        if not mod & pygame.KMOD_ALT and mod & pygame.KMOD_CTRL:
            self.replace_last_substate(
                self.game.input.run(
                    "Enter some text you would like to search for in your current buffer",
                    handeler=self.buffer_find,
                )
            )
        elif not mod & pygame.KMOD_CTRL and mod & pygame.KMOD_ALT:
            if urls := buffer.get_current_links():
                m = menu.Menu(
                    self.game,
                    "Choose a link to open it in your browser.",
                    autoclose=True,
                    parrent=self,
                )
                items = [
                    (buffer.format_url(i, False), partial(webbrowser.open, i["url"]))
                    for i in urls
                ]

                items.append(("Close menu", lambda: None))
                m.add_items(items)
                menus.set_default_sounds(m)
                self.add_substate(m)

    def open_helper_menu(self, mod):
        self.game.network.send(consts.CHANNEL_MISC, "open_helper_menu", {})

    def ask_to_exit(self, mod):
        if self.spectator_mode:
            self.spectator_menu(mod)
            return
        m = menu.Menu(
            self.game,
            "Are you sure you want to exit?",
            parrent=self,
        )
        items = [
            ("Yes", lambda: self._exit_faded(mod)),
            ("No", self.pop_last_substate),
        ]
        m.add_items(items)
        menus.set_default_sounds(m)
        self.add_substate(m)

    def _exit_faded(self, mod):
        """Yes on the Esc confirm: fade the map audio out, then quit.

        Announces "Disconnecting" and fades while Gameplay is still alive so
        the ambience/music actually softens to silence instead of being
        destroyed instantly; when it completes, the normal quit flow (logout
        + cleanup) runs and the server disconnect lands us back on the main
        menu.
        """
        if not self.game.start_exit_fade(
            on_faded=lambda: self.quit(mod),
            exit_after=False,  # logout returns to the main menu, not app exit
            announce="Disconnecting",  # logging the character out, not closing the app
        ):
            return
        # Block all input for the fade's duration; Gameplay underneath keeps
        # updating its audio so the fade is audible. When the fade completes,
        # quit() logs out and the server disconnect lands us on the main menu
        # (whose listener gain is restored there).
        self.replace_last_substate(_ExitFadeState(self.game))

    def spectator_menu(self, mod):
        m = menu.Menu(
            self.game,
            "Spectator Options",
            parrent=self,
        )
        items = [
            ("Leave Match", self.leave_spectator_match),
            ("View Players", self.who_online),
            ("Cancel", self.pop_last_substate),
        ]
        m.add_items(items)
        menus.set_default_sounds(m)
        self.add_substate(m)

    def leave_spectator_match(self):
        self.spectator_mode = False
        self.camera.set_focus_object(self.player)
        self.pop_last_substate()
        self.game.network.send(consts.CHANNEL_MISC, "leave_spectator", {})

    def ammo_check(self, mod):
        self.wmanager.checkAmmo()

    def reserved_check(self, mod):
        self.wmanager.checkReserves()

    def fire_weapon_automatic(self, mod):
        if (
            self.wmanager.activeWeapon is not None
            and self.wmanager.activeWeapon.automatic
            and not self.player.lock_weapon
        ):
            self.wmanager.fire(self.player.hfacing, self.player.vfacing)

    def fire_weapon_non_automatic(self, mod):
        if (
            self.wmanager.activeWeapon is not None
            and not self.wmanager.activeWeapon.automatic
            and not self.player.lock_weapon
        ):
            self.wmanager.fire(self.player.hfacing, self.player.vfacing)

    def music_down(self, mod):
        if self.music_volume > 0:
            self.game.audio_mngr.set_volume("music", self.music_volume - 5)
            self.music_volume -= 5
            options.set("volume_music", self.music_volume)
        speak(f"music volume: {str(self.music_volume)} percent. ")

    def music_up(self, mod):
        if self.music_volume < 100:
            self.game.audio_mngr.set_volume("music", self.music_volume+5)
            self.music_volume += 5
            options.set("volume_music", self.music_volume)
        speak(f"music volume: {str(self.music_volume)} percent. ")

    def _can_use_music_bot(self):
        """Use the Server snapshot; Staff retain their existing permanent access."""
        return (
            getattr(self, "can_use_music_bot", False)
            or getattr(self, "is_staff", False)
            or getattr(self, "is_builder", False)
            or getattr(self, "is_technician", False)
        )

    def music_bot_control(self, mod):
        """Music Bot controls using the configured Music Bot key:
        Key              = Open YouTube search
        Shift+Key        = Pause / Resume
        Ctrl+Key         = Stop playback
        Ctrl+Shift+Key   = Speak status
        Alt+Key          = Toggle broadcast (mute to others)
        """
        if not hasattr(self, 'music_bot') or not self.music_bot:
            return
        if not self._can_use_music_bot():
            # Regular players use the Jukebox instead — stay silent.
            return
        
        if mod & pygame.KMOD_CTRL and mod & pygame.KMOD_SHIFT:
            # Ctrl+Shift+M → Speak status
            self.music_bot.speak_status()
        elif mod & pygame.KMOD_CTRL:
            # Ctrl+M → Stop / Replay (toggle)
            if self.music_bot.playing:
                self.music_bot.stop()
                speak("Music stopped.")
            elif self.music_bot.has_last_track():
                speak(f"Replaying: {self.music_bot.last_track_title or self.music_bot.last_youtube_title}")
                self.music_bot._replay_last()
            else:
                speak("Nothing to replay. Press M to search.")
        elif mod & pygame.KMOD_SHIFT:
            # Shift+M → Pause/Resume
            self.music_bot.toggle_pause()
        elif mod & pygame.KMOD_ALT:
            # Alt+M → Toggle broadcast
            self.music_bot.toggle_broadcast()
        else:
            # M → Open YouTube search
            self.music_bot.open_search()

    def music_bot_volume(self, delta):
        """Adjust Music Bot volume through the configured volume keys."""
        if not hasattr(self, 'music_bot') or not self.music_bot:
            return
        if not self._can_use_music_bot():
            return
        new_vol = max(0, min(100, self.music_bot.volume + delta))
        self.music_bot.set_volume(new_vol)
        speak(f"Music Bot volume: {new_vol} percent.")

    def reset_pitch(self, mod):
        if mod & pygame.KMOD_CTRL:
            self.open_language_menu(mod)
            return
            
        if not self.player.locked:
            self.player.face(self.player.hfacing, 0, self.player.bfacing)
            speak("You now have a pitch of 0 degrees")
            self.player.play_sound("foley/turn/stop.ogg", cat="self")


    def open_language_menu(self, mod):
        # Ctrl is also the default snap-turn modifier. The language menu can
        # open before its KEYUP reaches Gameplay, and menus intentionally
        # consume KEYUP events. Clear the transient modifier now so leaving
        # the menu cannot keep forward/backward movement blocked.
        self.turn_mod = False
        self.game.network.send(consts.CHANNEL_MISC, "request_language_menu", {})

    def show_language_menu(self, available_langs, language_counts, current):
        if not available_langs:
            speak("No language channels available.")
            return

        m = menu.Menu(self.game, "Select your channel language", parrent=self, autoclose=False)

        def close_language_menu():
            # Only remove this menu if it is still the active substate. This
            # keeps Enter, Cancel and Escape from popping an unrelated menu if
            # two inputs arrive during the same update.
            if self.substates and self.substates[-1] is m:
                self.pop_last_substate()

        def choose_language(lang_code):
            self.set_channel_language(lang_code)
            close_language_menu()

        items = []
        for code, name in available_langs.items():
            def make_cb(c):
                return lambda: choose_language(c)
            
            count = language_counts.get(code, 0)
            player_str = f" {count} players" if count > 0 else ""
            
            display_text = f"Current {name}{player_str}" if code == current else f"{name}{player_str}"
            items.append((display_text, make_cb(code)))
        
        items.append(("Cancel", close_language_menu))
        m.add_items(items)
        menus.set_default_sounds(m)
        
        # Try to focus the current language
        try:
            curr_idx = list(available_langs.keys()).index(current)
            m.pos = curr_idx
        except ValueError:
            pass
        self.add_substate(m)
        if m.pos >= 0:
            current_item_text = m.items[m.pos][0]
            if callable(current_item_text):
                current_item_text = current_item_text()
            speak(current_item_text, interupt=False)

    def set_channel_language(self, lang_code):
        self.game.current_language = lang_code
        self.game.network.send(consts.CHANNEL_MISC, "change_language", {"language": lang_code})

    def reset_bank(self, mod):
        if not self.player.locked:
            self.player.face(self.player.hfacing, self.player.vfacing, 0)
            speak("You are now standing up streight")
            self.player.play_sound("foley/turn/stop.ogg", cat="self")

    def buffer_find(self, message):
        if message == "":
            return self.cancel()
        speak(f"Searching for {message}")
        sbuffer = buffer.buffers[buffer.bufferindex]
        sitems = sbuffer.items[sbuffer.index + 1 :]
        for i in range(len(sitems)):
            if message.lower() in sitems[i].text.lower():
                sbuffer.index = i + (len(sbuffer.items) - len(sitems))
                sbuffer.speak_item()
                break
        self.pop_last_substate()

    def interact(self, mod):
        # 📌 Send selected slot for wallbuy weapon placement
        selected_slot = getattr(self, 'selected_weapon_slot', -1)
        # Shift+interact: get out of a vehicle but keep its engine idling.
        # Harmless for every other interact target (jukeboxes, warps, ...).
        keep_engine = bool(mod & pygame.KMOD_SHIFT)
        self.game.network.send(
            consts.CHANNEL_MISC,
            "interact",
            {
                "angle": self.player.hfacing, 
                "pitch": self.player.vfacing,
                "selected_slot": selected_slot,
                "keep_engine": keep_engine,
            },
        )

    
    def number_row(self, mod, pos):
        """
        Handle number-row weapon selection.
        - Keys 1-4 are valid for weapon slots
        - Slot 1 = Knife, Slot 2 = MP7, Slot 3 = 357 Magnum, Slot 4 = Secondary (pickup)
        - With ALT: preserve original behavior (request game coords)
        """
        if self.player.lock_weapon:
            return
        # ALT still triggers "get_game_coords" as before
        if mod & pygame.KMOD_ALT:
            self.game.network.send(consts.CHANNEL_MAP, "get_game_coords", {"player": pos})
            return

        # Allow weapon slots 1-4 (indices 0-3); keys 5-0 are ignored
        if pos < 1 or pos > 4:
            return

        slot_index = pos - 1  # 1 -> 0, 2 -> 1, 3 -> 2, 4 -> 3

        # Track selected slot for wallbuy weapon placement
        self.selected_weapon_slot = slot_index

        # Switch if the index exists in weapon list
        if 0 <= slot_index < len(self.wmanager.weapons):
            self.wmanager.switchWeapon(slot_index)
        else:
            # Slot 4 may be empty - allow selecting it for pickup
            if slot_index == 3:
                speak("Empty slot selected - buy a weapon to fill it")
            else:
                speak("No weapon in that slot")

    def toggle_beacons(self, mod):
        if option := options.get("beacons"):
            speak("beacons off")
            options.set("beacons", False)
            for i in self.map.entities:
                entity = self.map.entities[i]
                if entity.player and entity.beacon is not None:
                    entity.beacon.source.pause()

        else:
            speak("beacons on")
            options.set("beacons", True)
            for i in self.map.entities:
                entity = self.map.entities[i]
                if entity.player and entity.beacon is not None:
                    entity.beacon.source.play()
                elif entity.player and entity.beacon is None: 
                    try: 
                        entity.beacon = entity.play_sound(
                            "ui/beacon.ogg", looping=True, cat="players"
                        )
                        entity.beacon.force_to_destroy = True
                        try:
                            entity.beacon.source.pitch = random.randint(98, 102) / 100
                        except AttributeError as e:
                            print(e)
                    except:
                        pass


    def open_options(self, mod):
        if mod & pygame.KMOD_ALT:
            def on_exit():
                self.pop_last_substate()
                self.reload_keyconfig()
                self._recover_streaming_audio_after_options()
            menus.options_menu(self.game, on_exit, replace_call=self.add_substate, parent=self, in_game=True)

    def _recover_streaming_audio_after_options(self):
        """Promptly recover long-running streams after closing an in-game menu.

        Options no longer creates a competing menu-music source in gameplay,
        but an output driver can still have stopped a buffered source while the
        menu was open.  The music bot can resume its existing queued buffers;
        the authoritative jukebox resync also supplies relay warm-up packets.
        Neither action recreates a song nor changes its queue position.
        """
        music = getattr(self, "music_bot", None)
        if music is not None:
            try:
                music.recover_output()
            except Exception:
                pass
        jukebox = getattr(self, "jukebox_player", None)
        if jukebox is not None:
            try:
                jukebox.request_resync("options closed")
            except Exception:
                pass

    def refresh_game_audio(self):
        """Soft-recover active game audio without rebuilding the OpenAL context.

        The Options callback runs on the gameplay/audio owner thread.  Existing
        sources, buffers, SoundGroups and pooled EFX objects are retained when
        healthy. Lost map loops or PA sources may be replaced by their owner.
        A full Client restart remains the fallback for a dead
        device or context because rebuilding the context in place would leave
        voice, jukebox and instrument owners holding invalid OpenAL objects.
        """
        now = time.monotonic()
        if getattr(self, "_audio_refresh_in_progress", False):
            speak("Audio refresh is already running.")
            return False
        last_refresh = getattr(
            self, "_last_audio_refresh_at", -self._AUDIO_REFRESH_COOLDOWN_SECONDS
        )
        if now - last_refresh < self._AUDIO_REFRESH_COOLDOWN_SECONDS:
            speak("Audio refresh is cooling down. Please wait a moment.")
            return False

        audio = getattr(self.game, "audio_mngr", None)
        context = getattr(audio, "context", None) if audio is not None else None
        try:
            connected = context is not None and context.is_connected
        except Exception:
            connected = False
        if not connected:
            speak(
                "The audio device is unavailable. Wait for automatic recovery, "
                "or use Restart Client from the main menu."
            )
            return False

        self._audio_refresh_in_progress = True
        self._last_audio_refresh_at = now
        failed = []
        pending = []

        def attempt(label, action):
            """False means failed; None is allowed for legacy void methods."""
            try:
                result = action()
                if result is False and label not in failed:
                    failed.append(label)
                return result
            except Exception:
                if label not in failed:
                    failed.append(label)
                return None

        def recover_loop(obj, *position):
            obj.recover(*position)
            # recover() historically returns "changed", not "healthy".
            # Inspect the result instead of treating an unchanged loop as bad.
            audible_at = getattr(obj, "is_audible_at", None)
            if (position and not getattr(obj, "playing", False)
                    and getattr(obj, "current_gain", 1.0) <= 0.0
                    and (not callable(audible_at) or not audible_at(*position))):
                return True  # An out-of-range spatial source stays silent.
            source = getattr(getattr(obj, "sound", None), "source", None)
            if source is None and getattr(obj, "audio_pending", False):
                if "map sounds" not in pending:
                    pending.append("map sounds")
                return None
            return source is not None and source.state == cyal.SourceState.PLAYING

        def refresh_owner(label, owner):
            if owner is None:
                return
            result = attempt(label, lambda: owner.refresh_environment_audio())
            # These owner APIs explicitly return None for an asynchronous
            # warm-up/resync, True for ready/idle, and False for a failure.
            if result is None and label not in failed:
                pending.append(label)

        try:
            def resume_device():
                # cyal versions expose paused as either a property or a
                # context-manager method. Resuming is safe and idempotent.
                context.device.resume()
                audio.muted = False
            attempt("audio device", resume_device)

            focus = getattr(getattr(self, "camera", None), "focus_object", None)
            if focus is None:
                focus = getattr(self, "player", None)
            x = getattr(focus, "x", 0.0)
            y = getattr(focus, "y", 0.0)
            z = getattr(focus, "z", 0.0)
            map_obj = getattr(self, "map", None)

            if map_obj is not None:
                # Only room ambience/music covering the listener should be
                # audible. Nearby spatial sources re-run their normal distance
                # and reverb calculation rather than being forced to play.
                ambiences = attempt("ambience", lambda: list(map_obj.get_ambiences_at(x, y, z))) or []
                for ambience in ambiences:
                    attempt("ambience", lambda obj=ambience: recover_loop(obj))
                musics = attempt("map music", lambda: list(map_obj.get_musics_at(x, y, z))) or []
                for music in musics:
                    attempt("map music", lambda obj=music: recover_loop(obj))
                sources = attempt("map sounds", lambda: list(getattr(map_obj, "source_list", ()))) or []
                for source in sources:
                    attempt("map sounds", lambda obj=source: recover_loop(obj, x, y, z))
                pannables = attempt("map sounds", lambda: list(getattr(map_obj, "pannable_list", ()))) or []
                for pannable in pannables:
                    attempt("map sounds", lambda obj=pannable: recover_loop(obj))

                # Rebind the current player and remote voice/music sources to
                # the room's existing effect slot. Never allocate a new slot.
                seen_entities = set()
                entities = attempt("room effects", lambda: list(getattr(map_obj, "entities", {}).values())) or []
                for entity in [focus] + entities:
                    if entity is None or id(entity) in seen_entities:
                        continue
                    seen_entities.add(id(entity))
                    attempt("room effects", lambda obj=entity: obj.sync_reverb())
            else:
                failed.append("map sounds")

            refresh_owner("Music Bot", getattr(self, "music_bot", None))
            refresh_owner("Jukebox", getattr(self, "jukebox_player", None))
            refresh_owner("Megaphone", getattr(self, "megaphone", None))

            # An interrupted fade can leave global gain at zero even though
            # every individual source is healthy. Restore the saved master bus.
            def restore_master():
                master = audio.volume_categories["master"][0]
                audio.listener.gain = master / 100
            attempt("master volume", restore_master)

            if failed:
                speak("Audio refresh incomplete: " + ", ".join(failed)
                      + ". Try again or use Restart Client.")
                return False
            if pending:
                speak("Audio refresh requested. Waiting for " + ", ".join(pending) + ".")
            else:
                speak("Audio refresh finished. If still silent, use Restart Client.")
            return True
        except Exception:
            speak(
                "Audio refresh could not complete. Please wait a moment and try "
                "again, or use Restart Client from the main menu."
            )
            return False
        finally:
            self._audio_refresh_in_progress = False
    
    def handle_o_key(self, mod):
        """Handle O key: PA Test Mode (no modifier) or Options Menu (ALT+O)"""
        if mod & pygame.KMOD_ALT:
            # ALT+O: Open options menu
            self.open_options(mod)
        else:
            # Plain O: Toggle PA Test Mode
            self.toggle_pa_test_mode(mod)
    
    def toggle_pa_test_mode(self, mod):
        """Toggle PA Test Mode for testing megaphone speakers in exploration mode"""
        # Cooldown to prevent rapid toggling (500ms)
        if not hasattr(self, '_pa_toggle_clock'):
            self._pa_toggle_clock = self.game.new_clock()
        if self._pa_toggle_clock.elapsed < 500:
            return  # Ignore rapid presses
        self._pa_toggle_clock.restart()
        
        # Any staff level (developer, contributor, admin, moderator, builder,
        # technician) can use this feature. can_broadcast_megaphone is the
        # server-authoritative permission and is accepted as well, so a player
        # the server authorizes is never blocked by a missing level flag.
        is_staff = getattr(self, 'is_staff', False)
        is_builder = getattr(self, 'is_builder', False)
        is_technician = getattr(self, 'is_technician', False)
        can_broadcast = getattr(self, 'can_broadcast_megaphone', False)
        if is_staff or is_builder or is_technician or can_broadcast:
            self._finish_pa_toggle()
            return

        # Normal players: remain completely silent
        return

    def _finish_pa_toggle(self):
        """Finish the PA Test Mode toggle once staff permission is confirmed."""
        # Check if game has started - block PA Test Mode if so
        if self.game_started:
            speak("System: PA Test Mode is only available before game starts.")
            return
        
        # Check if map has PA speakers (with auto-retry setup if map loaded speakers)
        if (not hasattr(self.megaphone, 'sources') or not self.megaphone.sources or consts.CHANNEL_MEGAPHONE not in self.voice_channels):
            if hasattr(self.map, 'megaphone_speakers') and self.map.megaphone_speakers:
                self.megaphone.setup_megaphone_speakers(force=True)

        if not hasattr(self.megaphone, 'sources') or not self.megaphone.sources or consts.CHANNEL_MEGAPHONE not in self.voice_channels:
            speak("System: No PA speakers available on this map.")
            return
        
        # Toggle PA Test Mode
        self.pa_test_mode = not self.pa_test_mode
        
        if self.pa_test_mode:
            from libs import logger
            logger.log("PA Test Mode activated.")
            key_name = string_utils.friendly_key_name(self.kc.get("voice_chat", pygame.K_g)).upper()
            speak(f"System: PA Test Mode activated. Press {key_name} to test speakers.")
            # NOTE: PA Test Mode deliberately does NOT touch the music bot's
            # broadcast_to_megaphone. "Broadcast to Megaphone" is an independent
            # toggle inside the Music Bot menu that the performer turns ON/OFF
            # themselves - the O key must not silently force it ON (or OFF).
        else:
            from libs import logger
            logger.log("PA Test Mode deactivated.")
            speak("System: PA Test Mode deactivated.")
            # If currently recording, switch back to default channel immediately
            if hasattr(self, 'voice_chat') and self.voice_chat and self.voice_chat.recording:
                if not hasattr(self, '_default_vc_compression'):
                    self._default_vc_compression = voice_chat.voice_chat_compression(self.game, consts.CHANNEL_VOICECHAT)
                self.voice_chat.vc_compression = self._default_vc_compression
            # NOTE: PA Test Mode OFF does not touch the music bot's megaphone
            # routing either - that toggle belongs to the Music Bot menu only.
    
    
    def toggle_sonar_and_force_quit(self, mod):
        if mod & pygame.KMOD_ALT:
            self.quit(mod)
            self.game.exit()
        setattr(
            self.camera,
            "sonar",
            self.game.toggle(
                "sonar",
                "sonar enabled",
                "sonar disabled"
            )
        )

    def run_check(self, mod):
        if self.can_run and not self.running: self.run_start(mod)
    

    def _guitar_sample_for(cls, note_name):
        return GuitarHandler._sample_for(note_name)

    def _guitar_volume(velocity):
        return GuitarHandler._volume(velocity)

    def _play_local_guitar_note(self, note_name, velocity=None):
        self.guitar.play_local_note(note_name, velocity)

    def _feed_guitar_monitor(self):
        self.guitar.feed_monitor()

    def _process_guitar_notes(self):
        self.guitar.process_notes()

    def toggle_guitar_mode(self, mod=None):
        self.guitar.toggle()
        self.guitar_mode = self.guitar.active

    def voice_chat_start(self, mod):
        """Start voice chat (Push-to-Talk)"""
        if self.voice_chat is None:
            try:
                self.voice_chat = voice_chat.VoiceChatRecord(self.game, self.player)
            except Exception as e:
                print(f"Failed to re-init voice chat: {e}")
                speak("Voice chat unavailable.")
                return

        if self.voice_chat.audio_input is None or not options.get("microphone", True) or not options.get("voice_chat", True): 
            return

        if self.voice_chat.recording:
            return # Already recording
        
        # Determine if we should use megaphone channel
        use_megaphone = False
        
        # PA Test Mode: Force megaphone channel (if available)
        if self.pa_test_mode and not self.game_started:
            if consts.CHANNEL_MEGAPHONE in self.voice_channels:
                use_megaphone = True
            else:
                speak("PA Test Mode: No speakers available.")
                return
        
        # Normal mode: Check if holding Megaphone weapon
        if not use_megaphone:
            if self.wmanager.activeWeapon and getattr(self.wmanager.activeWeapon, 'name', '').lower() == 'megaphone':
                use_megaphone = True
                
        # Megaphone availability check
        if use_megaphone:
            if (consts.CHANNEL_MEGAPHONE not in self.voice_channels or not hasattr(self.megaphone, 'sources') or not self.megaphone.sources):
                 if hasattr(self.map, 'megaphone_speakers') and self.map.megaphone_speakers:
                     self.megaphone.setup_megaphone_speakers(force=True)

            if consts.CHANNEL_MEGAPHONE not in self.voice_channels or not hasattr(self.megaphone, 'sources') or not self.megaphone.sources:
                 speak("System: No public address system available directly in this area.")
                 return
            
            # Check if megaphone is locked by a staff broadcast. Only the
            # single music-bot slot holder blocks talking - performers in the
            # multi-owner instrument set can still use the megaphone weapon
            # (voice never hijacks the PA; everyone's audio is equal-power
            # mixed), so a band can keep broadcasting while talking.
            lock_owner = getattr(self.megaphone, 'lock_owner', None)
            player_name = getattr(self.player, 'name', '')
            if (lock_owner and lock_owner != player_name
                    and player_name not in getattr(self.megaphone, 'lock_owners', set())):
                 speak(f"System: Megaphone is currently locked for a staff broadcast by {lock_owner}.")
                 return
        
        # Route to appropriate channel based on mode
        if use_megaphone and consts.CHANNEL_MEGAPHONE in self.voice_channels:
            # Use megaphone's compression (sends to CHANNEL_MEGAPHONE)
            from libs import logger
            logger.log(f"Routing voice to MEGAPHONE channel ({consts.CHANNEL_MEGAPHONE})")
            self.voice_chat.vc_compression = self.voice_channels[consts.CHANNEL_MEGAPHONE].vc_compression
        else:
            from libs import logger
            logger.log("Routing voice to STANDARD VOICECHAT channel")
            # Use default voice chat compression (sends to CHANNEL_VOICECHAT)
            # Ensure we have a default compression that sends to standard channel
            if not hasattr(self, '_default_vc_compression'):
                self._default_vc_compression = voice_chat.voice_chat_compression(self.game, consts.CHANNEL_VOICECHAT)
            self.voice_chat.vc_compression = self._default_vc_compression

        # Set the megaphone flag BEFORE recording starts so the mic capture
        # thread reads the correct routing from its very first chunk (otherwise
        # the first ~20ms of a PA session would leak onto the normal channel).
        self.voice_chat_using_megaphone = use_megaphone
        self.voice_chat.audio_input.start()
        self.voice_chat.recording = True
        self.game.direct_soundgroup.play("ui/voxon.ogg", volume=20)

    def voice_chat_stop(self, mod):
        """Stop voice chat (Push-to-Talk)"""
        if self.voice_chat is None or self.voice_chat.audio_input is None or not options.get("microphone", True) or not options.get("voice_chat", True): 
            return
            
        if not self.voice_chat.recording:
            return
            
        self.voice_chat.audio_input.stop()
        self.voice_chat.recording = False
        self.voice_chat_using_megaphone = False
        self.game.call_after(40, self.voice_chat.voice_chat_finish)
        self.game.direct_soundgroup.play("ui/voxoff.ogg")

    def voice_chat_toggle(self, mod):
        """Toggle voice chat on/off — no need to hold the key."""
        if self.voice_chat is not None and self.voice_chat.recording:
            self.voice_chat_stop(mod)
            speak("Voice chat deactivated")
            return
        self.voice_chat_start(mod)
        if self.voice_chat is not None and getattr(self.voice_chat, "recording", False):
            speak("Voice chat activated")
