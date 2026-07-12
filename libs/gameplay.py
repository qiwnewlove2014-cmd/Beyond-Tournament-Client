import time
import random
import contextlib
import webbrowser
from functools import partial
import cyal.exceptions
import pygame
import pyogg
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
from .objects import player
from .weapons import weapon, weaponmanager
import math
import cyal


class Gameplay(state.State):
    def __init__(self, game):
        super().__init__(game)
        self.kc = game.keyconfig
        kc = self.kc  # just an alias to use inside this function.
        self.map = world_map.Map(self.game, 0, 0, 0, 10, 10, 10)
        self.player = player.Player(self.game, self.map, 0, 0, 0)
        self.map.player = self.player
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
        self.parser = map.Map_parser(self.game, self.map)
        self.last_ping_time = time.time()
        self.pingging = False
        self.pa_test_mode = False  # PA Test Mode for testing megaphone speakers
        self.game_started = False   # Track if game has started (blocks PA Test Mode)
        self.pong_mode = False      # True when player is in an active Pong match (suppresses normal footsteps)
        self.tracking_target = None
        self.tracking_clock = None
        self.facing_sound_clock = self.game.new_clock()
        self.is_facing_target = False
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
            pygame.K_TAB: self.spectator_switch_player,
            kc.get("tracking_menu", pygame.K_t): self.open_tracking_menu,
            kc.get("voice_chat", pygame.K_g): self.voice_chat_start,  # Push-to-Talk mode
            pygame.K_RETURN: self.buffer_options,
            kc.get("open_volume_mixer", pygame.K_F7): lambda mod: self.add_substate(volume_mixer.volume_mixer(self.game, parent=self)),
            pygame.K_o: self.handle_o_key,  # PA Test Mode (no mod) or Options (ALT+O)
            kc.get("map_chat", pygame.K_SLASH): self.map_chat,
            kc.get("chat", pygame.K_QUOTE): self.chat,
            kc.get("move_left_in_buffer", pygame.K_COMMA): self.buffer_move_l,
            kc.get("move_right_in_buffer", pygame.K_PERIOD): self.buffer_move_r,
            kc.get("cycle_buffer_left", pygame.K_LEFTBRACKET): self.buffer_cycle_l,
            kc.get("cycle_buffer_right", pygame.K_RIGHTBRACKET): self.buffer_cycle_r,
            kc.get("move_forward", pygame.K_w): lambda mod: (
                setattr(self, "cann_run", True),
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
            kc.get("strafe_left", pygame.K_q): lambda mod: (
                setattr(self, "can_run", False),
                self.run_stop(mod),
            ),
            kc.get("strafe_right", pygame.K_e): lambda mod: (
                setattr(self, "can_run", False),
                self.run_stop(mod),
            ),
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
            kc.get("open_main_menu", pygame.K_BACKSPACE): lambda mod: (
                self.chat2("/mainmenu")
            ),
            kc.get("check_stats", pygame.K_p): self._p_or_spectator_cam,
            pygame.K_TAB: self.spectator_switch_player,  # Switch spectator target
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
        }
        self.keys_released = {
            kc.get("voice_chat", pygame.K_g): self.voice_chat_stop,  # Push-to-Talk mode
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
        self.turn_mod = False

    def spectator_switch_player(self, mod):
        if not self.spectator_mode:
            return

        # Fade out current target's audio
        if self.camera.focus_object:
            self.fade_out_entity_audio(self.camera.focus_object)

        self.game.network.send(consts.CHANNEL_MISC, "spectator_switch_player", {})

    def _p_or_spectator_cam(self, mod):
        """P key does double duty: in spectator mode of a Pong match it cycles
        the sideline camera angle; otherwise it shows the player stats."""
        if self.spectator_mode and self.pong_arena:
            self.spectator_cycle_cam_mode()
        else:
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
        self.voice_channels = {}
        
        # === MAP MUSIC BOT ===
        self.music_bot = music_bot.MapMusicBot(self.game)
        
        self.megaphone_sources = []
        # === FIXED MAP-CORNER PA SYSTEM ===
        # Speakers at ACTUAL MAP CORNERS (updated lazily when map bounds available)
        # This ensures speakers are truly environmental, not attached to any player
        
        # Flag to track if speakers have been positioned to map corners
        self.megaphone_positioned = False
        
        # --- Environmental Reverb (Stadium Effect) - BALANCED ---
        self.megaphone_reverb_slot = self.game.audio_mngr.gen_effect(
            "EAXREVERB",
            ("decay_time", 4.0),           # Reverb duration
            ("density", 1.0),              # Max density
            ("diffusion", 1.0),            # Max diffusion for smooth blending
            ("gain", 0.6),                 # Reduced to prevent clipping
            ("gainhf", 0.4),               # Reduced high freq for warmer sound
            ("gainlf", 1.0),               # MAX value (was 1.2 - invalid!)
            ("decay_hfratio", 0.4),        # High freq decays faster (warmer)
            ("reflections_gain", 0.3),     # Reduced to prevent harshness
            ("reflections_delay", 0.03),
            ("late_reverb_gain", 0.8),     # Reduced from 1.0
            ("late_reverb_delay", 0.02)
        )
        
        # --- EQ: PA Cabinet Effect with HEAVY BASS ---
        # OpenAL EQ ranges: low_cutoff 50-800, high_cutoff 4000-16000
        self.megaphone_eq_slot = self.game.audio_mngr.gen_effect(
            "EQUALIZER",
            ("low_gain", 3.0),          # Boosted bass (safe value)
            ("low_cutoff", 200.0),      # Safe cutoff for bass (50-800 range)
            ("mid1_gain", 1.2),         # Slight mid boost for voice clarity
            ("mid1_center", 800.0),     # Lower mid center for warmer voice
            ("mid1_width", 1.0),
            ("high_gain", 0.4),         # Reduce treble - less harsh
            ("high_cutoff", 4000.0)     # MIN valid value (4000-16000 range)
        )
        
        # --- Low-Pass Filter: Heavy bass, cut highs for PA cabinet sound ---
        self.megaphone_lowpass_filter = self.game.audio_mngr.gen_filter(
            "LOWPASS",
            ("GAIN", 0.85),              # Slight volume reduction to prevent clipping
            ("GAINHF", 0.4)              # Cut high frequencies by 60% (was 40%)
        )
        
        # --- Compressor: Make voice levels consistent ---
        self.megaphone_compressor_slot = self.game.audio_mngr.gen_effect(
            "COMPRESSOR",
            ("onoff", 1)                  # Enable compressor
        )
        
        # NOTE: Echo effect removed - using only reverb for clean sound
        
        # --- Directional Filters for PA speakers ---
        # Normal: Standard PA cabinet sound
        self.megaphone_normal_filter = self.megaphone_lowpass_filter
        # Muffled: Heavy low-pass for behind-speaker position
        self.megaphone_muffled_filter = self.game.audio_mngr.gen_filter(
            type="LOWPASS"
        )
        if self.megaphone_muffled_filter:
            self.megaphone_muffled_filter.set("GAIN", 0.6)  # Dampen volume a bit
            self.megaphone_muffled_filter.set("GAINHF", 0.05)  # Cut high frequencies severely

        # Provide a specialized underwater filter for megaphones
        self.megaphone_underwater_filter = self.game.audio_mngr.gen_filter(
            type="LOWPASS"
        )
        if self.megaphone_underwater_filter:
            self.megaphone_underwater_filter.set("GAIN", 0.8)  # Slightly preserve volume
            self.megaphone_underwater_filter.set("GAINHF", 0.02)  # Extreme high-freq cutoff for underwater muffling
        
        # Speaker data storage for directional checking
        self.megaphone_speaker_data = []
        self.megaphone_muffled_check_counter = 0  # Frame counter for optimization
        self.last_megaphone_setup = 0  # Timestamp for debounce
        
        self.setup_megaphone_speakers()

    def setup_megaphone_speakers(self):
        """Initializes or re-initializes megaphone speakers based on map data"""
        
        # Debounce: Prevent running more than once per second
        # This prevents crash loops if map data triggers repeated reload
        if hasattr(self, 'last_megaphone_setup') and time.time() - self.last_megaphone_setup < 1.0:
            return
        self.last_megaphone_setup = time.time()

        # Clear speaker delay queues in voice_chat to force recalculation of propagation delays for new positions
        try:
            from libs import voice_chat
            if hasattr(voice_chat, '_speaker_delay_queues'):
                voice_chat._speaker_delay_queues.clear()
            if hasattr(voice_chat, '_speaker_last_calc_time'):
                voice_chat._speaker_last_calc_time.clear()
        except Exception as e:
            print(f"Error clearing speaker delay queues: {e}")

        # Stop existing compression thread explicitly to prevent ID overlaps
        if consts.CHANNEL_MEGAPHONE in self.voice_channels:
             try:
                 old_channel = self.voice_channels[consts.CHANNEL_MEGAPHONE]
                 if hasattr(old_channel, 'vc_compression') and old_channel.vc_compression:
                     # Force stop the thread
                     old_channel.vc_compression.running = False
                     # We can't easily join() here without blocking UI
             except Exception as e:
                 print(f"Error cleaning up old megaphone thread: {e}")

        # Return per-speaker EFX reverb slots to pool + cleanup reflection sources
        # This MUST happen before clearing the lists to prevent OpenAL resource exhaustion
        if hasattr(self, 'megaphone_speaker_data'):
            for data in self.megaphone_speaker_data:
                # Return per-speaker reverb slot to pool
                if data.get('reverb_slot'):
                    self.game.audio_mngr.release_effect_slot(data['reverb_slot'])
                # Delete ground reflection source
                if data.get('reflection_source'):
                    try:
                        data['reflection_source'].stop()
                        data['reflection_source'].buffer = None
                        data['reflection_source'].delete()
                    except Exception:
                        pass

        # Cleanup existing sources if any
        if hasattr(self, 'megaphone_sources'):
            for i, src in enumerate(self.megaphone_sources):
                if src:
                    # Detach EFX slots to prevent driver-level feedback/buzz
                    if hasattr(self.game.audio_mngr, 'efx'):
                        for send_idx in range(4):
                            try:
                                self.game.audio_mngr.efx.send(src, send_idx, None)
                            except Exception:
                                pass
                    try:
                        src.stop()
                        src.buffer = None
                    except Exception:
                        pass
                    try:
                        while src.buffers_queued > 0:
                            src.unqueue_buffers()
                    except Exception:
                        pass
                    try:
                        src.delete()
                    except Exception:
                        pass
        
        # Cleanup per-player megaphone sources
        if hasattr(self, 'megaphone_player_sources'):
            for sid in list(self.megaphone_player_sources.keys()):
                self._remove_megaphone_player(sid)

        self.megaphone_sources = []
        self.megaphone_speaker_data = []
        self.megaphone_player_sources = {}  # {sender_id: {'sources': [...], 'last_active': float}}
        self.megaphone_lock_owner = None
        vc_sources = []
        
        initial_positions = []
        speaker_data_list = [] # Store (x, y, z, volume)
        
        # Check if map has dynamic speakers defined
        if hasattr(self.map, 'megaphone_speakers') and self.map.megaphone_speakers:
            for i, spk in enumerate(self.map.megaphone_speakers):
                initial_positions.append((spk['x'], spk['y'], spk['z']))
                speaker_data_list.append(spk)
        else:
            # No speakers configured - use empty list (no sound)
            if consts.CHANNEL_MEGAPHONE in self.voice_channels:
                 # Ensure we clean up the channel if it exists but speakers are gone
                 # But we might want to keep the channel structure? 
                 # If vc_sources is empty, voice chat will crash as we saw.
                 # So if NO speakers, we should perhaps NOT register the channel?
                 pass
        
        # Calculate map center for direction targeting
        map_center_x = (self.map.minx + self.map.maxx) / 2
        map_center_y = (self.map.miny + self.map.maxy) / 2
        
        # Get global megaphone volume setting (default 100)
        global_vol = options.get("megaphone_volume", 100) / 100.0
        
        # Get player position to calculate accurate initial volumes
        try:
            player_pos = (self.camera.focus_object.x, self.camera.focus_object.y, self.camera.focus_object.z)
        except AttributeError:
            player_pos = (0.0, 0.0, 0.0)
        
        for i, pos in enumerate(initial_positions):
            base_vol = speaker_data_list[i].get('volume', 0.6)
            # Apply global volume multiplier to base volume
            final_vol = base_vol * global_vol
            
            # Get per-speaker effect settings
            speaker_delay = speaker_data_list[i].get('delay', 0.0)
            speaker_reverb_decay = speaker_data_list[i].get('reverb_decay', 2.0)
            speaker_reverb_diffusion = speaker_data_list[i].get('reverb_diffusion', 0.8)
            
            hearing_range = speaker_data_list[i].get('hearing_range', 0.0)
            
            src = self.game.audio_mngr.context.gen_source()
            # Distance attenuation
            if hearing_range > 0:
                # Custom hearing range
                # Inverse Distance Model:
                # Gain = Ref / (Ref + Rolloff * (Dist - Ref))
                # Goal: Avoid abrupt cut-off at max_distance by ensuring gain is low (~5%).
                # We use Ref = 20% of range (loud-ish area), Rolloff = 3.5 (decay).
                src.rolloff_factor = 3.5
                src.reference_distance = hearing_range * 0.2
                src.max_distance = hearing_range
            else:
                # Default / Dynamic fallback
                src.rolloff_factor = 1.0
                src.reference_distance = 10.0
                src.max_distance = 300.0
            
            # Calculate distance and initial occlusion/fade factor for the template source
            dx = player_pos[0] - pos[0]
            dy = player_pos[1] - pos[1]
            dz = player_pos[2] - pos[2]
            distance = math.sqrt(dx*dx + dy*dy + dz*dz)
            
            init_occlusion = 1.0
            if hearing_range > 0.0:
                if distance >= hearing_range:
                    init_occlusion = 0.0
                elif distance >= hearing_range * 0.8:
                    fade_start = hearing_range * 0.8
                    init_occlusion = 1.0 - ((distance - fade_start) / (hearing_range - fade_start))
                    
            src.gain = final_vol * init_occlusion
            src.relative = False     # World Anchored
            
            # Use exact position
            src.position = pos
            
            # Fixed pitch for ALL speakers — prevents drift between speakers
            # 3D positions already provide spatial separation, pitch variation causes
            # playback speed differences that accumulate into audible desync over time
            src.pitch = 1.0
            
            # Get cone properties from speaker data (with fallback to calculated direction)
            aim_yaw = speaker_data_list[i].get('aim_yaw', 0)
            aim_pitch = speaker_data_list[i].get('aim_pitch', -30)
            # Default to Omni-directional (360) to prevent "sound convergence" or "blind spots"
            # User reported sound appearing to come from only one side/converging.
            # This was because "Aim to Center" logic + Narrow Cone made speakers quiet when standing behind them.
            inner_cone = speaker_data_list[i].get('inner_cone_angle', 360)
            outer_cone = speaker_data_list[i].get('outer_cone_angle', 360)
            outer_gain = speaker_data_list[i].get('outer_cone_gain', 0.2)
            
            # Calculate direction vector from yaw and pitch
            if aim_yaw == 0:
                # If yaw is 0, calculate automatically towards map center
                dx = map_center_x - pos[0]
                dy = map_center_y - pos[1]
                length = (dx**2 + dy**2)**0.5
                if length == 0: length = 1
                # Convert pitch to vertical component (-30 degrees = slight downward)
                pitch_rad = math.radians(aim_pitch)
                direction = (dx/length * math.cos(pitch_rad), 
                            dy/length * math.cos(pitch_rad), 
                            math.sin(pitch_rad))
            else:
                # Use explicit yaw/pitch from settings
                yaw_rad = math.radians(aim_yaw)
                pitch_rad = math.radians(aim_pitch)
                # Convert spherical to cartesian (yaw: 0=North/+Y, 90=East/+X)
                direction = (
                    math.sin(yaw_rad) * math.cos(pitch_rad),  # X
                    math.cos(yaw_rad) * math.cos(pitch_rad),  # Y  
                    math.sin(pitch_rad)                        # Z
                )
            
            # Apply cone properties
            src.direction = direction
            src.cone_inner_angle = inner_cone
            src.cone_outer_angle = outer_cone
            src.cone_outer_gain = outer_gain
            
            # Per-speaker reverb effect with custom settings
            # Only create reverb if decay_time > 0.1 (otherwise it's basically disabled)
            speaker_reverb_slot = None
            if hasattr(self.game.audio_mngr, 'efx') and speaker_reverb_decay > 0.1:
                try:
                    speaker_reverb_slot = self.game.audio_mngr.gen_effect(
                        "EAXREVERB",
                        ("decay_time", speaker_reverb_decay),
                        ("diffusion", speaker_reverb_diffusion),
                        ("density", 1.0),
                        ("gain", 0.6),        # Reduced to prevent clipping
                        ("gainhf", 0.4),      # Reduced for warmer sound
                        ("gainlf", 1.0),      # MAX value (was 1.2 - invalid!)
                        ("decay_hfratio", 0.4),  # Warmer decay
                        ("reflections_gain", 0.3),  # Reduced harshness
                        ("reflections_delay", 0.03),
                        ("late_reverb_gain", 0.8),  # Reduced from 1.0
                        ("late_reverb_delay", 0.02)
                    )
                except Exception as e:
                    print(f"[MEGAPHONE] Error creating per-speaker reverb: {e}")
                    speaker_reverb_slot = None
            
            eq_bass = speaker_data_list[i].get('eq_bass', 50.0)
            eq_mid = speaker_data_list[i].get('eq_mid', 50.0)
            eq_treble = speaker_data_list[i].get('eq_treble', 50.0)

            # Create unique filter for primary speaker template
            p_filter = self.game.audio_mngr.gen_filter("BANDPASS")
            if p_filter:
                try:
                    # Convert 0-100 to 0.0-1.0
                    p_filter.set("GAINLF", eq_bass / 100.0)
                    p_filter.set("GAIN", eq_mid / 100.0)
                    p_filter.set("GAINHF", eq_treble / 100.0)
                    src.direct_filter = p_filter
                except Exception as e:
                    print(f"[MEGAPHONE] Error initializing primary filter: {e}")
            
            # Apply effects - only apply reverb if speaker_reverb_slot was created
            if hasattr(self.game.audio_mngr, 'efx'):
                try:
                    if self.megaphone_eq_slot: 
                        self.game.audio_mngr.efx.send(src, 0, self.megaphone_eq_slot, filter=p_filter)
                    if speaker_reverb_slot:  # Only apply if reverb was created (decay > 0.1)
                        self.game.audio_mngr.efx.send(src, 1, speaker_reverb_slot, filter=p_filter)
                    if self.megaphone_compressor_slot: 
                        self.game.audio_mngr.efx.send(src, 2, self.megaphone_compressor_slot, filter=p_filter)
                except Exception as e:
                    print(f"[MEGAPHONE EFX] Error applying effects: {e}")
                
            self.megaphone_sources.append(src)
            vc_sources.append(src)
            
            # Store speaker data for directional muffled sound checking AND volume updates
            self.megaphone_speaker_data.append({
                'source': src,
                'position': pos,
                'direction': direction,
                'base_volume': base_vol,
                'delay': speaker_delay,
                'hearing_range': hearing_range,  # Save for update loop!
                'reverb_slot': speaker_reverb_slot,  # Store for cleanup
                'cone_settings': {
                    'inner': inner_cone,
                    'outer': outer_cone,
                    'outer_gain': outer_gain
                },
                'filter': p_filter,
                'eq_bass': eq_bass / 100.0,
                'eq_mid': eq_mid / 100.0,
                'eq_treble': eq_treble / 100.0,
                'target_gain': eq_mid / 100.0,
                'current_gain': eq_mid / 100.0,
                'target_gainhf': eq_treble / 100.0,
                'current_gainhf': eq_treble / 100.0,
                'target_gainlf': eq_bass / 100.0,
                'current_gainlf': eq_bass / 100.0,
                'target_vol': final_vol * init_occlusion,
                'current_vol': final_vol * init_occlusion
            })
            
            # === GROUND REFLECTION (Virtual Source) ===
            # Only create ground reflection if reverb is enabled (decay > 0.1)
            # This respects user's choice when they set decay to 0
            if speaker_reverb_decay > 0.1:
                ground_level = self.map.minz  # Ground is at map minz
                
                # Place reflection source at ground level, directly below the speaker
                # This creates the effect of sound hitting the floor and bouncing up
                reflection_z = ground_level + 1  # Slightly above ground
                
                try:
                    reflection_src = self.game.audio_mngr.context.gen_source()
                    reflection_src.rolloff_factor = 0.8  # Slower falloff (echo travels far)
                    reflection_src.reference_distance = 20.0  # Wider spread
                    reflection_src.max_distance = 250.0
                    reflection_src.gain = final_vol * 0.4 * init_occlusion  # 40% volume * occlusion
                    reflection_src.relative = False
                    reflection_src.position = (pos[0], pos[1], reflection_z)
                    
                    # Point upward (reflection bounces up from ground)
                    reflection_src.direction = (0, 0, 1)
                    reflection_src.cone_inner_angle = 120  # Wide coverage
                    reflection_src.cone_outer_angle = 240
                    reflection_src.cone_outer_gain = 0.5
                    
                    # Create unique filter for reflection source
                    r_filter = self.game.audio_mngr.gen_filter("LOWPASS")
                    if r_filter:
                        try:
                            r_filter.set("GAIN", 0.6)
                            r_filter.set("GAINHF", 0.05)
                            reflection_src.direct_filter = r_filter
                        except Exception as e:
                            print(f"[MEGAPHONE] Error initializing reflection filter: {e}")
                    
                    if hasattr(self.game.audio_mngr, 'efx'):
                        try:
                            if self.megaphone_eq_slot:
                                self.game.audio_mngr.efx.send(reflection_src, 0, self.megaphone_eq_slot, filter=r_filter)
                            if speaker_reverb_slot:
                                self.game.audio_mngr.efx.send(reflection_src, 1, speaker_reverb_slot, filter=r_filter)
                            if self.megaphone_compressor_slot:
                                self.game.audio_mngr.efx.send(reflection_src, 2, self.megaphone_compressor_slot, filter=r_filter)
                        except Exception:
                            pass
                    
                    self.megaphone_sources.append(reflection_src)
                    vc_sources.append(reflection_src)
                    
                    # Store reflection references and states in the primary speaker's data dict
                    self.megaphone_speaker_data[-1].update({
                        'reflection_source': reflection_src,
                        'refl_filter': r_filter,
                        'refl_target_gain': 0.6,
                        'refl_current_gain': 0.6,
                        'refl_target_gainhf': 0.05,
                        'refl_current_gainhf': 0.05,
                        'refl_target_vol': final_vol * 0.4 * init_occlusion,
                        'refl_current_vol': final_vol * 0.4 * init_occlusion
                    })
                except Exception as e:
                    print(f"[MEGAPHONE] Error creating ground reflection: {e}")

        
        # Only register channel if we have sources (to prevent crash on empty list)
        if vc_sources:
            megaphone_channel = type('MegaphoneChannel', (), {
                'name': "MEGAPHONE_GLOBAL",
                'has_radio': False,
                'vc_source': vc_sources,
                'radio_source': None,
                'vc_compression': voice_chat.voice_chat_compression(self.game, consts.CHANNEL_MEGAPHONE)
            })()
            
            # Register megaphone channel
            self.voice_channels[consts.CHANNEL_MEGAPHONE] = megaphone_channel
        elif consts.CHANNEL_MEGAPHONE in self.voice_channels:
             # If no sources but channel existed, remove it to prevent access
             del self.voice_channels[consts.CHANNEL_MEGAPHONE]

        
        # Only create voice_chat if it doesn't exist (preserve routing state)
        # This prevents losing PA Test Mode compression reference when map reloads
        if not hasattr(self, 'voice_chat') or self.voice_chat is None:
            self.voice_chat = voice_chat.VoiceChatRecord(self.game, self.player)


    # ============================================================================
    # PER-PLAYER MEGAPHONE SOURCE MANAGEMENT
    # Each player who speaks through the megaphone gets their own set of OpenAL
    # sources (one per physical speaker), preventing audio interleaving.
    # Max 8 concurrent players. Inactive players are auto-cleaned after 5 seconds.
    # ============================================================================

    MAX_MEGAPHONE_PLAYERS = 8

    def get_megaphone_player_sources(self, sender_id):
        """Get or create per-player OpenAL sources for megaphone speakers.
        Clones spatial properties from the physical speaker template sources.
        Returns a list of sources, or None if no speakers are configured."""
        import time as _time

        if not hasattr(self, 'megaphone_player_sources'):
            self.megaphone_player_sources = {}

        # Periodic cleanup of inactive players (throttled: at most once per second)
        if not hasattr(self, '_last_mega_cleanup') or _time.time() - self._last_mega_cleanup > 1.0:
            self.cleanup_inactive_megaphone_players()
            self._last_mega_cleanup = _time.time()

        # If sender already has sources, update timestamp and return
        if sender_id in self.megaphone_player_sources:
            self.megaphone_player_sources[sender_id]['last_active'] = _time.time()
            return self.megaphone_player_sources[sender_id]['sources']

        # Check if we have speaker data to clone from
        if not hasattr(self, 'megaphone_speaker_data') or not self.megaphone_speaker_data:
            return None

        # If at capacity, evict least recently active player
        if len(self.megaphone_player_sources) >= self.MAX_MEGAPHONE_PLAYERS:
            oldest_id = min(
                self.megaphone_player_sources,
                key=lambda k: self.megaphone_player_sources[k]['last_active']
            )
            self._remove_megaphone_player(oldest_id)

        # Create new sources cloning physical speaker positions
        sources = []
        filters = []
        global_vol = options.get("megaphone_volume", 100) / 100.0

        if getattr(self, 'concert_spectator_mode', False):
            # 2D Concert Spectator Mode
            src = None
            p_filter = None
            try:
                src = self.game.audio_mngr.context.gen_source()
                src.position = [0, 0, 0]
                src.relative = True
                src.gain = global_vol
                src.rolloff_factor = 0.0

                # Apply flat EQ for Concert Mode to ensure high fidelity
                eq_bass = 1.0
                eq_treble = 1.0
                
                p_filter = self.game.audio_mngr.gen_filter("LOWPASS")
                if p_filter:
                    try:
                        p_filter.set("GAIN", eq_bass)
                        p_filter.set("GAINHF", eq_treble)
                        src.direct_filter = p_filter
                    except Exception as e:
                        pass
                
                sources.append(src)
                if p_filter:
                    filters.append(p_filter)
            except Exception as e:
                print(f"[MEGAPHONE] Error initializing 2D spectator source: {e}")
                if src: src.destroy()
                if p_filter: p_filter.destroy()
        else:
            for i, spk_data in enumerate(self.megaphone_speaker_data):
                # Skip entries that are ground reflections (they are stored under reflection_source in the primary speaker dict)
                if 'source' not in spk_data:
                    continue
    
                src = None
                p_filter = None
                try:
                    src = self.game.audio_mngr.context.gen_source()
    
                    # Clone spatial properties from template speaker
                    template = spk_data['source']
                    src.position = spk_data['position']
                    src.gain = spk_data['base_volume'] * global_vol
                    src.relative = False
                    src.rolloff_factor = template.rolloff_factor
                    src.reference_distance = template.reference_distance
                    src.max_distance = template.max_distance
                    src.pitch = template.pitch

                    # Clone cone/direction
                    src.direction = spk_data['direction']
                    cone = spk_data.get('cone_settings', {})
                    src.cone_inner_angle = cone.get('inner', 360)
                    src.cone_outer_angle = cone.get('outer', 360)
                    src.cone_outer_gain = cone.get('outer_gain', 0.2)

                    # Create unique filter for player source
                    p_filter = self.game.audio_mngr.gen_filter("LOWPASS")
                    if p_filter:
                        try:
                            p_filter.set("GAIN", 0.85)
                            p_filter.set("GAINHF", 0.4)
                            src.direct_filter = p_filter
                        except Exception as e:
                            print(f"[MEGAPHONE] Error initializing player filter: {e}")

                    # Apply shared EFX sends (eq, reverb, compressor)
                    if hasattr(self.game.audio_mngr, 'efx'):
                        try:
                            if hasattr(self, 'megaphone_eq_slot') and self.megaphone_eq_slot:
                                self.game.audio_mngr.efx.send(src, 0, self.megaphone_eq_slot, filter=p_filter)
                            if spk_data.get('reverb_slot'):
                                self.game.audio_mngr.efx.send(src, 1, spk_data['reverb_slot'], filter=p_filter)
                            if hasattr(self, 'megaphone_compressor_slot') and self.megaphone_compressor_slot:
                                self.game.audio_mngr.efx.send(src, 2, self.megaphone_compressor_slot, filter=p_filter)
                            if hasattr(self, 'current_player_reverb_slot') and self.current_player_reverb_slot not in (None, 'UNINIT'):
                                self.game.audio_mngr.efx.send(src, 3, self.current_player_reverb_slot, filter=p_filter)
                        except Exception:
                            pass

                except Exception as e:
                    print(f"[MEGAPHONE] Error creating per-player primary source for sender {sender_id}: {e}")

                sources.append(src)
                filters.append(p_filter)

                # 2. Clone reflection source if it exists
                refl_src = None
                r_filter = None
                if src is not None and 'reflection_source' in spk_data:
                    try:
                        refl_src = self.game.audio_mngr.context.gen_source()
                        refl_template = spk_data['reflection_source']
                        
                        # Reflection is at ground level, directly below the speaker
                        ground_level = self.map.minz if hasattr(self, 'map') and hasattr(self.map, 'minz') else 0.0
                        refl_src.position = (spk_data['position'][0], spk_data['position'][1], ground_level + 1.0)
                        refl_src.gain = spk_data['base_volume'] * global_vol * 0.4  # 40% volume
                        refl_src.relative = False
                        refl_src.rolloff_factor = refl_template.rolloff_factor
                        refl_src.reference_distance = refl_template.reference_distance
                        refl_src.max_distance = refl_template.max_distance
                        refl_src.pitch = refl_template.pitch
                        
                        # Point upward
                        refl_src.direction = (0, 0, 1)
                        refl_src.cone_inner_angle = refl_template.cone_inner_angle
                        refl_src.cone_outer_angle = refl_template.cone_outer_angle
                        refl_src.cone_outer_gain = refl_template.cone_outer_gain
                        
                        # Create unique filter for reflection source
                        r_filter = self.game.audio_mngr.gen_filter("LOWPASS")
                        if r_filter:
                            try:
                                r_filter.set("GAIN", 0.6)
                                r_filter.set("GAINHF", 0.05)
                                refl_src.direct_filter = r_filter
                            except Exception as e:
                                print(f"[MEGAPHONE] Error initializing player reflection filter: {e}")
                        
                        # Apply EFX sends
                        if hasattr(self.game.audio_mngr, 'efx'):
                            try:
                                if hasattr(self, 'megaphone_eq_slot') and self.megaphone_eq_slot:
                                    self.game.audio_mngr.efx.send(refl_src, 0, self.megaphone_eq_slot, filter=r_filter)
                                if spk_data.get('reverb_slot'):
                                    self.game.audio_mngr.efx.send(refl_src, 1, spk_data['reverb_slot'], filter=r_filter)
                                if hasattr(self, 'megaphone_compressor_slot') and self.megaphone_compressor_slot:
                                    self.game.audio_mngr.efx.send(refl_src, 2, self.megaphone_compressor_slot, filter=r_filter)
                            except Exception:
                                pass
                                
                    except Exception as e:
                        print(f"[MEGAPHONE] Error creating per-player reflection source for sender {sender_id}: {e}")

                sources.append(refl_src)
                filters.append(r_filter)

        if sources:
            targets_vol = []
            targets_gain = []
            targets_gainhf = []
            targets_gainlf = []
            
            # Get player position to calculate accurate initial volumes
            try:
                player_pos = (self.camera.focus_object.x, self.camera.focus_object.y, self.camera.focus_object.z)
            except AttributeError:
                player_pos = (0.0, 0.0, 0.0)
                
            for idx, src_obj in enumerate(sources):
                spk_idx = idx // 2
                is_refl = (idx % 2 == 1)
                
                # Fetch corresponding physical template data
                spk_data = self.megaphone_speaker_data[spk_idx]
                base_v = spk_data['base_volume'] * global_vol
                
                # Calculate distance and initial occlusion/fade factor
                speaker_pos = spk_data['position']
                dx = player_pos[0] - speaker_pos[0]
                dy = player_pos[1] - speaker_pos[1]
                dz = player_pos[2] - speaker_pos[2]
                distance = math.sqrt(dx*dx + dy*dy + dz*dz)
                
                hearing_range = spk_data.get('hearing_range', 0.0)
                init_occlusion = 1.0
                
                if hearing_range > 0.0:
                    if distance >= hearing_range:
                        init_occlusion = 0.0
                    elif distance >= hearing_range * 0.8:
                        fade_start = hearing_range * 0.8
                        init_occlusion = 1.0 - ((distance - fade_start) / (hearing_range - fade_start))
                
                # Apply raycast and direction filters to initial occlusion if in range
                if init_occlusion > 0.0:
                    is_blocked = self._check_speaker_occlusion(speaker_pos, player_pos)
                    dot_horizontal = (dx * spk_data['direction'][0] + dy * spk_data['direction'][1])
                    is_behind = dot_horizontal < 0
                    
                    if getattr(self.camera.focus_object, 'in_water', False):
                        depth = getattr(self.camera.focus_object, 'depth', 1.0)
                        init_occlusion *= max(0.1, depth * 0.3)
                    elif is_blocked or is_behind:
                        if is_blocked:
                            init_occlusion *= 0.3
                        else:
                            init_occlusion *= 0.5
                
                if not is_refl:
                    targets_vol.append(base_v * init_occlusion)
                    targets_gain.append(spk_data.get('target_gain', 0.85))
                    targets_gainhf.append(spk_data.get('target_gainhf', 0.4))
                    targets_gainlf.append(spk_data.get('target_gainlf', 0.5))
                else:
                    targets_vol.append(base_v * 0.4 * init_occlusion)
                    targets_gain.append(spk_data.get('refl_target_gain', 0.6))
                    targets_gainhf.append(spk_data.get('refl_target_gainhf', 0.05))
                    targets_gainlf.append(spk_data.get('refl_target_gainlf', 0.6))
                    
            # Set source gain directly to match initial volume
            for idx, src_obj in enumerate(sources):
                if src_obj:
                    src_obj.gain = targets_vol[idx]
                    
            self.megaphone_player_sources[sender_id] = {
                'sources': sources,
                'filters': filters,
                'last_active': _time.time(),
                'targets_vol': targets_vol,
                'currents_vol': list(targets_vol),
                'targets_gain': targets_gain,
                'currents_gain': list(targets_gain),
                'targets_gainhf': targets_gainhf,
                'currents_gainhf': list(targets_gainhf),
                'targets_gainlf': targets_gainlf,
                'currents_gainlf': list(targets_gainlf)
            }
            return sources
        return None

    def _remove_megaphone_player(self, sender_id):
        """Clean up a specific player's megaphone sources. Detaches EFX, drains buffers, deletes sources."""
        if not hasattr(self, 'megaphone_player_sources'):
            return
        if sender_id not in self.megaphone_player_sources:
            return

        entry = self.megaphone_player_sources[sender_id]
        
        # Delete unique filters to prevent memory leaks
        if 'filters' in entry:
            for f in entry['filters']:
                if f:
                    try:
                        f.delete()
                    except Exception:
                        pass

        for src in entry['sources']:
            if src is None:
                continue
            # Detach EFX sends to prevent driver glitches
            if hasattr(self.game.audio_mngr, 'efx'):
                for send_idx in range(4):
                    try:
                        self.game.audio_mngr.efx.send(src, send_idx, None)
                    except Exception:
                        pass
            try:
                src.stop()
                src.buffer = None
            except Exception:
                pass
            # Drain queued buffers
            try:
                while src.buffers_queued > 0:
                    src.unqueue_buffers()
            except Exception:
                pass
            # Delete source
            try:
                src.delete()
            except Exception:
                pass

        del self.megaphone_player_sources[sender_id]

        # Also clean up jitter buffer, delay queues, and tracking times keyed by this sender
        if sender_id in voice_chat._jitter_buffers:
            del voice_chat._jitter_buffers[sender_id]
            
        keys_to_remove = [k for k in voice_chat._speaker_delay_queues.keys() if k[0] == sender_id]
        for k in keys_to_remove:
            try:
                del voice_chat._speaker_delay_queues[k]
            except KeyError:
                pass
                
        if sender_id in voice_chat._last_play_times:
            del voice_chat._last_play_times[sender_id]
            
        if sender_id in voice_chat._last_packet_times:
            del voice_chat._last_packet_times[sender_id]

    def cleanup_inactive_megaphone_players(self):
        """Remove per-player sources that haven't received audio for 5+ seconds."""
        import time as _time
        if not hasattr(self, 'megaphone_player_sources'):
            return
        now = _time.time()
        inactive = [sid for sid, entry in self.megaphone_player_sources.items()
                    if now - entry['last_active'] > 5.0]
        for sid in inactive:
            self._remove_megaphone_player(sid)
            
        # Cleanup expired fading sources
        if hasattr(self, 'megaphone_fading_sources'):
            to_remove = []
            for fade_obj in self.megaphone_fading_sources:
                if now - fade_obj['fade_start'] >= fade_obj['fade_duration']:
                    for f_src in fade_obj['sources']:
                        if f_src and getattr(f_src, "is_valid", lambda: False)():
                            f_src.destroy()
                    to_remove.append(fade_obj)
            for item in to_remove:
                self.megaphone_fading_sources.remove(item)


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
    
    def _cleanup_megaphone_efx(self):
        """Return megaphone EFX slots to the pool and clean up sources.
        Slots are returned to the AudioManager pool for reuse — never deleted."""
        # Return global EFX effect slots to pool (reverb, EQ, compressor)
        for slot_name in ['megaphone_reverb_slot', 'megaphone_eq_slot', 'megaphone_compressor_slot']:
            slot = getattr(self, slot_name, None)
            if slot:
                self.game.audio_mngr.release_effect_slot(slot)
                setattr(self, slot_name, None)

        # Cleanup global EFX filters (lowpass, muffled, underwater)
        # Filters are much less limited than slots, try to delete them
        for filter_name in ['megaphone_lowpass_filter', 'megaphone_muffled_filter', 'megaphone_underwater_filter']:
            f = getattr(self, filter_name, None)
            if f:
                try:
                    f.delete()
                except Exception:
                    pass
                setattr(self, filter_name, None)
        # Also clear the normal filter alias
        self.megaphone_normal_filter = None

        # Return per-speaker reverb slots to pool + cleanup reflection sources
        if hasattr(self, 'megaphone_speaker_data'):
            for data in self.megaphone_speaker_data:
                # Delete unique filters to prevent memory leaks
                for f_key in ['filter', 'refl_filter']:
                    f = data.get(f_key)
                    if f:
                        try:
                            f.delete()
                        except Exception:
                            pass
                if data.get('reverb_slot'):
                    self.game.audio_mngr.release_effect_slot(data['reverb_slot'])
                if data.get('reflection_source'):
                    try:
                        data['reflection_source'].stop()
                        data['reflection_source'].buffer = None
                        data['reflection_source'].delete()
                    except Exception:
                        pass
            self.megaphone_speaker_data.clear()

        # Cleanup megaphone sources
        if hasattr(self, 'megaphone_sources'):
            for src in self.megaphone_sources:
                if src:
                    # Detach EFX slots to prevent driver-level feedback/buzz
                    if hasattr(self.game.audio_mngr, 'efx'):
                        for send_idx in range(4):
                            try:
                                self.game.audio_mngr.efx.send(src, send_idx, None)
                            except Exception:
                                pass
                    try:
                        src.stop()
                        src.buffer = None
                    except Exception:
                        pass
                    try:
                        src.delete()
                    except Exception:
                        pass
            self.megaphone_sources.clear()

    def exit(self):
        super().exit()
        if self.player.locked:
            self.game.network.event_handeler.death({"dead": False})
        if self.game.network:
            self.game.network.put(None)
            self.game.network.join()
            self.game.network = None
        self.ambience.destroy()
        self.pingging = False
        # === Cleanup Music Bot ===
        if hasattr(self, 'music_bot') and self.music_bot:
            self.music_bot.destroy()
            self.music_bot = None
        self._cleanup_megaphone_efx()
        self.map.destroy()

    def update_megaphone_settings(self, volume, bass, mid, high):
        """Called by megaphone_settings menu to update audio in real-time"""
        # Updates global volume multiplier for all speakers
        global_vol = volume / 100.0
        
        if hasattr(self, 'megaphone_speaker_data'):
            for data in self.megaphone_speaker_data:
                try:
                    # Recalculate gain: Base (Map) * Global (Slider)
                    new_gain = data['base_volume'] * global_vol
                    data['source'].gain = new_gain
                except Exception as e:
                    print(f"[MEGAPHONE] Error updating volume: {e}")
                    
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

    def update(self, events):
        # Tick megaphone delay queues to flush remaining audio when a player finishes speaking
        if hasattr(self, 'megaphone_player_sources') and self.megaphone_player_sources:
            if consts.CHANNEL_MEGAPHONE in self.voice_channels:
                channel = self.voice_channels[consts.CHANNEL_MEGAPHONE]
                if hasattr(channel, 'vc_compression'):
                    channel.vc_compression.put(lambda: voice_chat.tick_megaphone_delay(self))

        is_concert = getattr(self, 'concert_spectator_mode', False)
        if not self.spectator_mode or is_concert:
            self.player.loop()
        elif not self.substates:
            # Filter events for spectator mode when idle (Allow ESC, TAB, Chat, RETURN, Brackets, PageUp/Down, and Comma/Period)
            allowed_keys = [
                pygame.K_TAB, pygame.K_ESCAPE, pygame.K_QUOTE, pygame.K_SLASH, pygame.K_RETURN,
                pygame.K_LEFTBRACKET, pygame.K_RIGHTBRACKET, pygame.K_PAGEUP, pygame.K_PAGEDOWN,
                pygame.K_COMMA, pygame.K_PERIOD, pygame.K_p,
                pygame.K_m, pygame.K_F9, pygame.K_F10
            ]
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
        self.map.loop()
        for i in self.map.source_list.copy():
            i.loop(self.camera.focus_object.x, self.camera.focus_object.y, self.camera.focus_object.z)
        
        # === Music Bot loop (auto-advance tracks) ===
        if hasattr(self, 'music_bot') and self.music_bot:
            self.music_bot.loop()
        
        # === Tracking beacon & facing sound update ===
        if getattr(self, "tracking_target", None) is not None:
            target_type, obj, pos = self.tracking_target
            
            # If target is a dynamic entity, check if it's dead
            if target_type == "entity":
                if obj.dead:
                    speak("Tracking target lost.")
                    self.tracking_target = None
                else:
                    # If still in entities, update position; otherwise keep last known position
                    if obj.name in self.map.entities:
                        pos = (obj.x, obj.y, obj.z)
                        self.tracking_target = (target_type, obj, pos)
                    
            if getattr(self, "tracking_target", None) is not None:
                # Play facing.ogg at target's 3D coordinates every 1.2 seconds.
                # Pitch rises when facing the target and falls when walking past
                # / facing away, so it reads like a radar sweep.
                if self.tracking_clock.elapsed >= 1200:
                    self.tracking_clock.restart()
                    pitch = self._beacon_pitch(pos[0], pos[1])
                    snd = self.game.audio_mngr.play_unbound(
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
                                self.game.audio_mngr.efx.send(snd.source, 3, reverb_slot)
                            except Exception:
                                pass
        

        # === MEGAPHONE DYNAMIC REVERB SYNC ===
        # Synchronize megaphone speakers with the player's local reverb zone
        # This gives the realistic impression that the PA system is echoing inside the current room
        current_reverb_zone = self.map.get_reverb_at(self.camera.focus_object.x, self.camera.focus_object.y, self.camera.focus_object.z)
        
        # PROXIMITY REVERB EFFECT: If not strictly inside a reverb zone, 
        # check if player is near one (simulates hearing reverb when walking close to a room)
        if not current_reverb_zone:
            expansion = 5.0  # units
            for r in self.map.reverb_list:
                if (r.minx - expansion <= self.camera.focus_object.x <= r.maxx + expansion and
                    r.miny - expansion <= self.camera.focus_object.y <= r.maxy + expansion and
                    r.minz - expansion <= self.camera.focus_object.z <= r.maxz + expansion):
                    current_reverb_zone = r
                    break
                    
        new_local_reverb_slot = current_reverb_zone.reverb if current_reverb_zone else None
        
        if getattr(self, 'current_player_reverb_slot', 'UNINIT') != new_local_reverb_slot:
            self.current_player_reverb_slot = new_local_reverb_slot
            
            # Send slot 3 is reserved for the player's local dynamic reverb
            if hasattr(self, 'megaphone_speaker_data'):
                for data in self.megaphone_speaker_data:
                    # Sync main speaker
                    if data.get('source'):
                        try:
                            # Apply the current filter to the new send to maintain muffling consistency
                            current_flt = getattr(data['source'], 'direct_filter', None)
                            self.game.audio_mngr.efx.send(data['source'], 3, new_local_reverb_slot, filter=current_flt)
                        except Exception:
                            pass
                    # Sync ground reflection source
                    if data.get('reflection_source'):
                        try:
                            current_flt2 = getattr(data['reflection_source'], 'direct_filter', None)
                            self.game.audio_mngr.efx.send(data['reflection_source'], 3, new_local_reverb_slot, filter=current_flt2)
                        except Exception:
                            pass
            
            # ALSO sync all active per-player megaphone sources
            if hasattr(self, 'megaphone_player_sources') and not getattr(self, 'concert_spectator_mode', False):
                for player_entry in self.megaphone_player_sources.values():
                    if 'sources' in player_entry:
                        for src in player_entry['sources']:
                            try:
                                current_flt = getattr(src, 'direct_filter', None)
                                self.game.audio_mngr.efx.send(src, 3, new_local_reverb_slot, filter=current_flt)
                            except Exception:
                                pass
        
        # === DIRECTIONAL MUFFLED SOUND + LINE-OF-SIGHT OCCLUSION ===
        # Every 10 frames, check if player is behind speaker OR blocked by wall
        if hasattr(self, 'megaphone_speaker_data') and hasattr(self, 'megaphone_muffled_check_counter'):
            self.megaphone_muffled_check_counter += 1
            if self.megaphone_muffled_check_counter >= 10:
                self.megaphone_muffled_check_counter = 0
                player_pos = (self.camera.focus_object.x, self.camera.focus_object.y, self.camera.focus_object.z)
                global_vol = options.get("megaphone_volume", 100) / 100.0
                is_underwater = getattr(self.camera.focus_object, 'in_water', False)
                
                for i, data in enumerate(self.megaphone_speaker_data):
                    try:
                        speaker_pos = data['position']
                        
                        # Vector from speaker to player
                        dx = player_pos[0] - speaker_pos[0]
                        dy = player_pos[1] - speaker_pos[1]
                        dz = player_pos[2] - speaker_pos[2]
                        
                        # === DISTANCE ATTENUATION ===
                        distance = math.sqrt(dx*dx + dy*dy + dz*dz)
                        data['distance'] = distance
                        
                        # === LINE-OF-SIGHT CHECK (Throttled) ===
                        spk_hearing_range = data.get('hearing_range', 80.0)
                        if spk_hearing_range == 0.0:
                            spk_hearing_range = 80.0
                            
                        # If outside hearing range, skip the expensive ray-march check
                        if distance >= spk_hearing_range:
                            is_blocked = True
                        else:
                            # Only raycast if player is within hearing range
                            is_blocked = self._check_speaker_occlusion(speaker_pos, player_pos)
                        
                        # === DIRECTIONAL CHECK (Horizontal only) ===
                        dot_horizontal = (dx * data['direction'][0] + 
                                         dy * data['direction'][1])
                        is_behind = dot_horizontal < 0
                        
                        # Calculate transition zone factor (fade out from 80% to 100% of hearing range)
                        spk_hearing_range_raw = data.get('hearing_range', 0.0)
                        fade_factor = 1.0
                        if spk_hearing_range_raw > 0.0:
                            if distance >= spk_hearing_range_raw:
                                fade_factor = 0.0
                            elif distance >= spk_hearing_range_raw * 0.8:
                                fade_start = spk_hearing_range_raw * 0.8
                                fade_factor = 1.0 - ((distance - fade_start) / (spk_hearing_range_raw - fade_start))
                        
                        occlusion_multiplier = fade_factor
                        
                        eq_mid = data.get('eq_mid', 0.5)
                        eq_treble = data.get('eq_treble', 0.5)
                        eq_bass = data.get('eq_bass', 0.5)
                        
                        target_gain = eq_mid
                        target_gainhf = eq_treble
                        target_gainlf = eq_bass
                        
                        # Apply occlusion and underwater filters only if not fully faded out
                        if fade_factor > 0.0:
                            if is_underwater:
                                # Player is underwater - filter megaphone heavily
                                target_gain = eq_mid * 0.94
                                target_gainhf = eq_treble * 0.05
                                target_gainlf = min(1.0, eq_bass * 1.5)
                                # Extra volume attenuation based on player depth
                                depth = getattr(self.camera.focus_object, 'depth', 1.0)
                                occlusion_multiplier = fade_factor * max(0.1, depth * 0.3)
                            elif is_blocked or is_behind:
                                # Behind speaker OR blocked by wall - apply muffled filter
                                target_gain = eq_mid * 0.7
                                target_gainhf = eq_treble * 0.12
                                target_gainlf = min(1.0, eq_bass * 1.2)
                                if is_blocked:
                                    occlusion_multiplier = fade_factor * 0.3  # 30% through wall
                                else:
                                    occlusion_multiplier = fade_factor * 0.5  # 50% behind speaker
                                
                        target_vol = data['base_volume'] * global_vol * occlusion_multiplier

                        # Store targets on physical template speaker
                        data['target_vol'] = target_vol
                        data['target_gain'] = target_gain
                        data['target_gainhf'] = target_gainhf
                        data['target_gainlf'] = target_gainlf
                        
                        data['refl_target_vol'] = target_vol * 0.4
                        data['refl_target_gain'] = eq_mid * 0.7
                        data['refl_target_gainhf'] = eq_treble * 0.12
                        data['refl_target_gainlf'] = min(1.0, eq_bass * 1.2)

                        # Store targets on player cloned sources
                        if hasattr(self, 'megaphone_player_sources'):
                            for player_entry in self.megaphone_player_sources.values():
                                if getattr(self, 'concert_spectator_mode', False):
                                    if len(player_entry.get('targets_vol', [])) > 0 and i == 0:
                                        player_entry['targets_vol'][0] = data['base_volume'] * global_vol
                                        player_entry['targets_gain'][0] = 1.0
                                        player_entry['targets_gainhf'][0] = 1.0
                                        if 'targets_gainlf' in player_entry and len(player_entry['targets_gainlf']) > 0:
                                            player_entry['targets_gainlf'][0] = 1.0
                                    continue
                                    
                                if 'sources' in player_entry:
                                    prim_idx = 2 * i
                                    refl_idx = 2 * i + 1
                                    
                                    if prim_idx < len(player_entry['sources']):
                                        player_entry['targets_vol'][prim_idx] = target_vol
                                        player_entry['targets_gain'][prim_idx] = target_gain
                                        player_entry['targets_gainhf'][prim_idx] = target_gainhf
                                        player_entry['targets_gainlf'][prim_idx] = target_gainlf
                                        
                                    if refl_idx < len(player_entry['sources']):
                                        player_entry['targets_vol'][refl_idx] = target_vol * 0.4
                                        player_entry['targets_gain'][refl_idx] = eq_mid * 0.7
                                        player_entry['targets_gainhf'][refl_idx] = eq_treble * 0.12
                                        player_entry['targets_gainlf'][refl_idx] = min(1.0, eq_bass * 1.2)
                    except Exception:
                        pass

        # === SMOOTH PARAMETER INTERPOLATION (Every Frame) ===
        if hasattr(self, 'megaphone_speaker_data'):
            smooth_factor = 0.15  # 15% transition per frame (~250-300ms total fade duration)
            global_vol = options.get("megaphone_volume", 100) / 100.0
            
            # Find the most recently active speaker to implement talkover ducking
            now = time.time()
            most_recent_sender_id = None
            max_active_time = 0.0
            if hasattr(self, 'megaphone_player_sources'):
                for sid, entry in self.megaphone_player_sources.items():
                    last_act = entry.get('last_active', 0.0)
                    if last_act > max_active_time:
                        max_active_time = last_act
                        most_recent_sender_id = sid
            
            # A speaker is active if they spoke in the last 400ms
            most_recent_active = (most_recent_sender_id is not None and (now - max_active_time < 0.4))
            
            for i, data in enumerate(self.megaphone_speaker_data):
                try:
                    # 1. Interpolate physical templates
                    # Template primary source
                    if data.get('source') and data.get('filter'):
                        # LERP volume
                        t_vol = data.get('target_vol', data['base_volume'] * global_vol)
                        c_vol = data.get('current_vol', t_vol)
                        if abs(t_vol - c_vol) > 0.0001:
                            new_vol = c_vol + (t_vol - c_vol) * smooth_factor
                            data['current_vol'] = new_vol
                            data['source'].gain = new_vol
                        elif c_vol != t_vol:
                            data['current_vol'] = t_vol
                            data['source'].gain = t_vol
                        
                        # LERP filter gain
                        t_g = data.get('target_gain', 0.85)
                        c_g = data.get('current_gain', t_g)
                        if abs(t_g - c_g) > 0.001:
                            new_g = c_g + (t_g - c_g) * smooth_factor
                            data['current_gain'] = new_g
                            data['filter'].set("GAIN", new_g)
                        elif c_g != t_g:
                            data['current_gain'] = t_g
                            data['filter'].set("GAIN", t_g)
                        
                        # LERP filter gainhf
                        t_ghf = data.get('target_gainhf', 0.5)
                        c_ghf = data.get('current_gainhf', t_ghf)
                        if abs(t_ghf - c_ghf) > 0.001:
                            new_ghf = c_ghf + (t_ghf - c_ghf) * smooth_factor
                            data['current_gainhf'] = new_ghf
                            data['filter'].set("GAINHF", new_ghf)
                        elif c_ghf != t_ghf:
                            data['current_gainhf'] = t_ghf
                            data['filter'].set("GAINHF", t_ghf)
                            
                        # LERP filter gainlf
                        t_glf = data.get('target_gainlf', 0.5)
                        c_glf = data.get('current_gainlf', t_glf)
                        if abs(t_glf - c_glf) > 0.001:
                            new_glf = c_glf + (t_glf - c_glf) * smooth_factor
                            data['current_gainlf'] = new_glf
                            data['filter'].set("GAINLF", new_glf)
                        elif c_glf != t_glf:
                            data['current_gainlf'] = t_glf
                            data['filter'].set("GAINLF", t_glf)
                            
                        # CRITICAL: Re-apply filter object to source so changes take effect
                        if data.get('source'):
                            data['source'].direct_filter = data['filter']
                        
                    # Template reflection source
                    if data.get('reflection_source') and data.get('refl_filter'):
                        t_vol = data.get('refl_target_vol', data['base_volume'] * global_vol * 0.4)
                        c_vol = data.get('refl_current_vol', t_vol)
                        if abs(t_vol - c_vol) > 0.0001:
                            new_vol = c_vol + (t_vol - c_vol) * smooth_factor
                            data['refl_current_vol'] = new_vol
                            data['reflection_source'].gain = new_vol
                        elif c_vol != t_vol:
                            data['refl_current_vol'] = t_vol
                            data['reflection_source'].gain = t_vol
                        
                        t_g = data.get('refl_target_gain', 0.35)
                        c_g = data.get('refl_current_gain', t_g)
                        if abs(t_g - c_g) > 0.001:
                            new_g = c_g + (t_g - c_g) * smooth_factor
                            data['refl_current_gain'] = new_g
                            data['refl_filter'].set("GAIN", new_g)
                        elif c_g != t_g:
                            data['refl_current_gain'] = t_g
                            data['refl_filter'].set("GAIN", t_g)
                        
                        t_ghf = data.get('refl_target_gainhf', 0.06)
                        c_ghf = data.get('refl_current_gainhf', t_ghf)
                        if abs(t_ghf - c_ghf) > 0.001:
                            new_ghf = c_ghf + (t_ghf - c_ghf) * smooth_factor
                            data['refl_current_gainhf'] = new_ghf
                            data['refl_filter'].set("GAINHF", new_ghf)
                        elif c_ghf != t_ghf:
                            data['refl_current_gainhf'] = t_ghf
                            data['refl_filter'].set("GAINHF", t_ghf)

                        t_glf = data.get('refl_target_gainlf', 0.6)
                        c_glf = data.get('refl_current_gainlf', t_glf)
                        if abs(t_glf - c_glf) > 0.001:
                            new_glf = c_glf + (t_glf - c_glf) * smooth_factor
                            data['refl_current_gainlf'] = new_glf
                            data['refl_filter'].set("GAINLF", new_glf)
                        elif c_glf != t_glf:
                            data['refl_current_gainlf'] = t_glf
                            data['refl_filter'].set("GAINLF", t_glf)
                            
                        # CRITICAL: Re-apply filter object to source so changes take effect
                        if data.get('reflection_source'):
                            data['reflection_source'].direct_filter = data['refl_filter']
                        
                    # 2. Interpolate active per-player sources
                    if hasattr(self, 'megaphone_player_sources'):
                        for sid, player_entry in self.megaphone_player_sources.items():
                            if 'sources' in player_entry and 'filters' in player_entry:
                                prim_idx = 2 * i
                                refl_idx = 2 * i + 1
                                
                                # Duck player volume if another player is currently speaking more recently
                                is_other_active = most_recent_active and (sid != most_recent_sender_id)
                                duck_mult = 0.25 if is_other_active else 1.0
                                
                                # Mute own voice (Fade) if In-Ear Monitor is enabled
                                is_own_voice = sid not in getattr(self, 'voice_channels', {})
                                in_ear_mult = 0.001 if (is_own_voice and getattr(self, 'in_ear_monitor', False)) else 1.0
                                
                                # Interpolate player primary source
                                if prim_idx < len(player_entry['sources']) and player_entry['sources'][prim_idx] is not None and player_entry['filters'][prim_idx] is not None:
                                    src = player_entry['sources'][prim_idx]
                                    flt = player_entry['filters'][prim_idx]
                                    
                                    # Volume
                                    t_vol = player_entry['targets_vol'][prim_idx] * duck_mult * in_ear_mult
                                    c_vol = player_entry['currents_vol'][prim_idx]
                                    if abs(t_vol - c_vol) > 0.0001:
                                        new_vol = c_vol + (t_vol - c_vol) * smooth_factor
                                        player_entry['currents_vol'][prim_idx] = new_vol
                                        src.gain = new_vol
                                    elif c_vol != t_vol:
                                        player_entry['currents_vol'][prim_idx] = t_vol
                                        src.gain = t_vol
                                    
                                    # Filter GAIN
                                    t_g = player_entry['targets_gain'][prim_idx]
                                    c_g = player_entry['currents_gain'][prim_idx]
                                    if abs(t_g - c_g) > 0.001:
                                        new_g = c_g + (t_g - c_g) * smooth_factor
                                        player_entry['currents_gain'][prim_idx] = new_g
                                        flt.set("GAIN", new_g)
                                    elif c_g != t_g:
                                        player_entry['currents_gain'][prim_idx] = t_g
                                        flt.set("GAIN", t_g)
                                    
                                    # Filter GAINHF
                                    t_ghf = player_entry['targets_gainhf'][prim_idx]
                                    c_ghf = player_entry['currents_gainhf'][prim_idx]
                                    if abs(t_ghf - c_ghf) > 0.001:
                                        new_ghf = c_ghf + (t_ghf - c_ghf) * smooth_factor
                                        player_entry['currents_gainhf'][prim_idx] = new_ghf
                                        flt.set("GAINHF", new_ghf)
                                    elif c_ghf != t_ghf:
                                        player_entry['currents_gainhf'][prim_idx] = t_ghf
                                        flt.set("GAINHF", t_ghf)

                                    # Filter GAINLF
                                    t_glf = player_entry.get('targets_gainlf', player_entry['targets_gainhf'])[prim_idx]
                                    c_glf = player_entry.get('currents_gainlf', player_entry['currents_gainhf'])[prim_idx]
                                    if abs(t_glf - c_glf) > 0.001:
                                        new_glf = c_glf + (t_glf - c_glf) * smooth_factor
                                        if 'currents_gainlf' in player_entry:
                                            player_entry['currents_gainlf'][prim_idx] = new_glf
                                        flt.set("GAINLF", new_glf)
                                    elif c_glf != t_glf:
                                        if 'currents_gainlf' in player_entry:
                                            player_entry['currents_gainlf'][prim_idx] = t_glf
                                        flt.set("GAINLF", t_glf)
                                        
                                    # CRITICAL: Re-apply filter object to source so changes take effect
                                    src.direct_filter = flt
                                    
                                # Interpolate player reflection source
                                if refl_idx < len(player_entry['sources']) and player_entry['sources'][refl_idx] is not None and player_entry['filters'][refl_idx] is not None:
                                    src = player_entry['sources'][refl_idx]
                                    flt = player_entry['filters'][refl_idx]
                                    
                                    # Volume
                                    t_vol = player_entry['targets_vol'][refl_idx] * duck_mult * in_ear_mult
                                    c_vol = player_entry['currents_vol'][refl_idx]
                                    if abs(t_vol - c_vol) > 0.0001:
                                        new_vol = c_vol + (t_vol - c_vol) * smooth_factor
                                        player_entry['currents_vol'][refl_idx] = new_vol
                                        src.gain = new_vol
                                    elif c_vol != t_vol:
                                        player_entry['currents_vol'][refl_idx] = t_vol
                                        src.gain = t_vol
                                    
                                    # Filter GAIN
                                    t_g = player_entry['targets_gain'][refl_idx]
                                    c_g = player_entry['currents_gain'][refl_idx]
                                    if abs(t_g - c_g) > 0.001:
                                        new_g = c_g + (t_g - c_g) * smooth_factor
                                        player_entry['currents_gain'][refl_idx] = new_g
                                        flt.set("GAIN", new_g)
                                    elif c_g != t_g:
                                        player_entry['currents_gain'][refl_idx] = t_g
                                        flt.set("GAIN", t_g)
                                    
                                    # Filter GAINHF
                                    t_ghf = player_entry['targets_gainhf'][refl_idx]
                                    c_ghf = player_entry['currents_gainhf'][refl_idx]
                                    if abs(t_ghf - c_ghf) > 0.001:
                                        new_ghf = c_ghf + (t_ghf - c_ghf) * smooth_factor
                                        player_entry['currents_gainhf'][refl_idx] = new_ghf
                                        flt.set("GAINHF", new_ghf)
                                    elif c_ghf != t_ghf:
                                        player_entry['currents_gainhf'][refl_idx] = t_ghf
                                        flt.set("GAINHF", t_ghf)

                                    # Filter GAINLF
                                    t_glf = player_entry.get('targets_gainlf', player_entry['targets_gainhf'])[refl_idx]
                                    c_glf = player_entry.get('currents_gainlf', player_entry['currents_gainhf'])[refl_idx]
                                    if abs(t_glf - c_glf) > 0.001:
                                        new_glf = c_glf + (t_glf - c_glf) * smooth_factor
                                        if 'currents_gainlf' in player_entry:
                                            player_entry['currents_gainlf'][refl_idx] = new_glf
                                        flt.set("GAINLF", new_glf)
                                    elif c_glf != t_glf:
                                        if 'currents_gainlf' in player_entry:
                                            player_entry['currents_gainlf'][refl_idx] = t_glf
                                        flt.set("GAINLF", t_glf)
                                        
                                    # CRITICAL: Re-apply filter object to source so changes take effect
                                    src.direct_filter = flt
                                        
                    # 3. Apply air absorption every frame using last calculated distance
                    dist = data.get('distance', 0.0)
                    if dist > 0.0:
                        air_absorption = max(0.3, 1.0 - (dist / 200.0))
                        target_abs = 1.0 - air_absorption
                        
                        last_abs = data.get('last_air_absorption', -1.0)
                        if abs(target_abs - last_abs) > 0.01:
                            data['last_air_absorption'] = target_abs
                            
                            # Apply to template primary
                            if data.get('source'):
                                try:
                                    if hasattr(data['source'], 'air_absorption_factor'):
                                        data['source'].air_absorption_factor = target_abs
                                except: pass
                                
                            # Apply to template reflection
                            if data.get('reflection_source'):
                                try:
                                    if hasattr(data['reflection_source'], 'air_absorption_factor'):
                                        data['reflection_source'].air_absorption_factor = target_abs
                                except: pass
                                
                            # Apply to player sources
                            if hasattr(self, 'megaphone_player_sources'):
                                for player_entry in self.megaphone_player_sources.values():
                                    if 'sources' in player_entry:
                                        prim_idx = 2 * i
                                        refl_idx = 2 * i + 1
                                        
                                        if prim_idx < len(player_entry['sources']) and player_entry['sources'][prim_idx] is not None:
                                            try:
                                                player_entry['sources'][prim_idx].air_absorption_factor = target_abs
                                            except: pass
                                            
                                        if refl_idx < len(player_entry['sources']) and player_entry['sources'][refl_idx] is not None:
                                            try:
                                                player_entry['sources'][refl_idx].air_absorption_factor = target_abs
                                            except: pass
                except Exception as e:
                    print(f"[MEGAPHONE] Error in per-frame interpolation loop: {e}")
        
        should_block = super().update(events)
        if should_block is True:
            # some substate doesnt want us to handel events for now.
            return
        elif isinstance(should_block, list):
            events = should_block
        key = pygame.key.get_pressed()
        is_concert = getattr(self, 'concert_spectator_mode', False)
        if not self.spectator_mode or is_concert:
            for i in self.keys_held:
                if key[i]:
                    self.keys_held[i](pygame.key.get_mods())
        for event in events:
            if event.type == pygame.KEYDOWN and event.key == pygame.K_SPACE and getattr(self.game, 'pong_mode', False):
                self.game.network.send(consts.CHANNEL_MAP, "pong_serve", {})
                continue
            if self.spectator_mode and not getattr(self, 'concert_spectator_mode', False):
                allowed_keys = [
                    pygame.K_TAB, pygame.K_ESCAPE, pygame.K_QUOTE, pygame.K_SLASH, pygame.K_RETURN,
                    pygame.K_LEFTBRACKET, pygame.K_RIGHTBRACKET, pygame.K_PAGEUP, pygame.K_PAGEDOWN,
                    pygame.K_COMMA, pygame.K_PERIOD, pygame.K_p,
                    pygame.K_m, pygame.K_F9, pygame.K_F10
                ]
                if event.type == pygame.KEYDOWN and event.key in self.keys_pressed and event.key in allowed_keys:
                    self.keys_pressed[event.key](event.mod)
            else:
                if event.type == pygame.KEYDOWN and event.key in self.keys_pressed:
                    self.keys_pressed[event.key](event.mod)
                elif event.type == pygame.KEYUP and event.key in self.keys_released:
                    self.keys_released[event.key](event.mod)
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
            if event.type == pygame.MOUSEWHEEL and not self.spectator_mode:
                if not self.wmanager.activeWeapon:
                    self.wmanager.switchWeapon(0)
                pos = self.wmanager.weapons.index(self.wmanager.activeWeapon)
                num_weapons = len(self.wmanager.weapons)
                # Scroll through all available weapon slots cyclically
                if event.y < 0:
                    next_pos = (pos + 1) % num_weapons
                else:
                    next_pos = (pos - 1) % num_weapons
                self.wmanager.switchWeapon(next_pos)
            if event.type == pygame.MOUSEMOTION and not self.spectator_mode:
                (x, y) = event.rel
                if x == 0:
                    self.turn_stop(pygame.K_a)
                if x < -1 or x > 1:
                    self.player.face(self.player.hfacing + (x / 2), self.player.vfacing)
 
        if self.game.mouse_buttons["left"] and not self.spectator_mode:
            self.wmanager.reload()
        if self.game.mouse_buttons["middle"] and not self.spectator_mode:
            self.interact(pygame.K_f)
        if self.game.mouse_buttons["right"] and not self.spectator_mode:
            if self.wmanager.activeWeapon and self.wmanager.activeWeapon.automatic:
                self.fire_weapon_automatic(pygame.K_SPACE)
            elif self.wmanager.activeWeapon:
                self.fire_weapon_non_automatic(pygame.K_SPACE)
                self.game.mouse_buttons["right"] = False

    def buffer_move_l(self, mod):
        if mod & pygame.KMOD_SHIFT:
            return buffer.cycle_item(3)
        buffer.cycle_item(1)

    # key event handelers:
    def buffer_move_r(self, mod):
        if mod & pygame.KMOD_SHIFT:
            return buffer.cycle_item(4)
        buffer.cycle_item(2)

    def buffer_cycle_l(self, mod):
        if mod & pygame.KMOD_SHIFT:
            return buffer.cycle(3)
        buffer.cycle(1)

    def buffer_cycle_r(self, mod):
        if mod & pygame.KMOD_SHIFT:
            return buffer.cycle(4)
        buffer.cycle(2)

    def chat(self, mod):
        self.replace_last_substate(
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
        self.game.network.send(consts.CHANNEL_CHAT, "chat", {"message": message})
        self.pop_last_substate()

    def map_chat(self, mod):
        self.replace_last_substate(
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
        tile_factor = 3.0 if self.map.get_tile_at(self.player.x, self.player.y, self.player.z) in ["deep_water", "underwater"] else 1.0
        effective_movetime = getattr(self.game, 'pong_speed', 60) if getattr(self.game, 'pong_mode', False) else (self.player.runtime if getattr(self, 'running', False) else self.player.movetime)
        if self.player.movement_clock.elapsed >= effective_movetime * tile_factor:
            self.player.movement_clock.restart()
            mode = "run" if (getattr(self, 'running', False) or getattr(self.game, 'pong_mode', False)) else "walk"
            self.player.walk(right=True, mode=mode, send=True)

    def move_forward(self, mod, turn=False):
        if getattr(self.game, 'pong_mode', False):
            return
        if turn and self.turn_mod:
            self.player.face(0, 0)
            self.turning = True
            return self.turn_stop(mod)
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
            amount=2 if self.running else 1
            self.player.face(self.player.hfacing - amount, self.player.vfacing)
            if self.player.hfacing % 45 == 0:
                speak(string_utils.direction(self.player.hfacing))

    def move_back(self, mod, turn=False):
        if getattr(self.game, 'pong_mode', False):
            return
        if turn and self.turn_mod:
            self.player.turning_clock.restart()
            self.player.face(self.player.hfacing + 180, 0)
            self.turning = True
            return self.turn_stop(mod)
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
            amount=2 if self.running else 1
            self.player.face(self.player.hfacing + amount, self.player.vfacing)
            if self.player.hfacing % 45 == 0:
                speak(string_utils.direction(self.player.hfacing))

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
                self.player.face(self.player.hfacing, self.player.vfacing - 1)

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
                self.player.face(self.player.hfacing, self.player.vfacing + 1)

    def turn_start(self, mod):
        self.player.play_sound("foley/turn/start.ogg", cat="self")

    def turn_stop(self, mod):
        if not self.turning:
            return
        self.turning = False
        if not self.player.locked:
            self.player.play_sound("foley/turn/stop.ogg", cat="self")
            if options.get("speak_on_turn", True): speak(f"turned to {self.player.hfacing} degrees")

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

    # tracking system
    def get_relative_direction_string(self, tx, ty, tz):
        dx = tx - self.player.x
        dy = ty - self.player.y
        
        rad = math.atan2(dx, dy)
        deg = math.degrees(rad)
        
        rel = deg - self.player.hfacing
        
        while rel <= -180:
            rel += 360
        while rel > 180:
            rel -= 360
            
        abs_deg = round(abs(rel))
        
        if abs_deg < 15:
            dir_str = "Straight in front"
        elif abs_deg > 165:
            dir_str = "Behind"
        elif rel > 0:
            if abs_deg < 60:
                dir_str = "Front-Right"
            elif abs_deg < 120:
                dir_str = "Right"
            else:
                dir_str = "Back-Right"
        else:
            if abs_deg < 60:
                dir_str = "Front-Left"
            elif abs_deg < 120:
                dir_str = "Left"
            else:
                dir_str = "Back-Left"
                
        diff_z = tz - self.player.z
        if diff_z > 2:
            dir_str += " (Above)"
        elif diff_z < -2:
            dir_str += " (Below)"

        return dir_str

    def _format_target_location(self, dist, tx, ty, tz):
        """Build the 'X tiles, direction' suffix shown next to a trackable.
        When the player is standing on the object (dist == 0), report 'right here'
        instead of a compass direction, since the bearing is meaningless there."""
        if dist <= 0:
            return "right here"
        direction_str = self.get_relative_direction_string(tx, ty, tz)
        return f"{dist} tiles, {direction_str}"

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

        # If Alt+T is pressed and we are currently tracking something, report status directly
        if mod & pygame.KMOD_ALT and getattr(self, "tracking_target", None) is not None:
            target_type, obj, pos = self.tracking_target
            if target_type == "entity":
                pos = (obj.x, obj.y, obj.z)

            dist = math.floor(movement.get_3d_distance(self.player.x, self.player.y, self.player.z, pos[0], pos[1], pos[2]))
            location_str = self._format_target_location(dist, pos[0], pos[1], pos[2])

            name = self._get_target_label(target_type, obj)
            speak(f"Tracking {name}: {location_str}")
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

    def _gather_trackables(self):
        """Collect all trackable objects around the player.
        Returns a list of (dist, label, location_str, (type_key, obj, pos)).
        location_str is the full 'X tiles, direction' (or 'right here' when on top).
        Excludes zombies, hellhounds, and walls."""
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

        # 7. Gather Entities (excluding local player, dead entities, zombies, and hellhounds)
        for name, ent in self.map.entities.items():
            if name == self.player.name or ent.dead:
                continue

            lower_name = name.lower()
            if lower_name.startswith("zomby") or "zombie" in lower_name or lower_name.startswith("hellhound") or "dog" in lower_name:
                continue

            ent.name = name  # Force correct name in case it was "None"
            dist = math.floor(movement.get_3d_distance(self.player.x, self.player.y, self.player.z, ent.x, ent.y, ent.z))
            location_str = self._format_target_location(dist, ent.x, ent.y, ent.z)
            cleaned_label = self._clean_name(name)
            trackables.append((dist, cleaned_label, location_str, ("entity", ent, (ent.x, ent.y, ent.z))))

        return trackables

    def start_tracking(self, target_info):
        self.tracking_target = target_info
        target_type, obj, pos = target_info

        name = self._get_target_label(target_type, obj)
        speak(f"Tracking {name}.")
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
        zone_name = self.camera.focus_object.map.get_zone_at(
            self.camera.x, self.camera.y, self.camera.z
        )
        speak(str(zone_name) if zone_name else "No zone")

    def speak_fps(self, mod):
        speak(f"{self.game.last_fps} FPS")

    def server_message(self, mod):
        self.game.network.send(consts.CHANNEL_MISC, "server_message")

    def online_server_list(self, mod):
        self.game.network.send(consts.CHANNEL_MISC, "who_online_m")

    def open_inventory(self, mod):
        if not self.player.dead: self.game.network.send(consts.CHANNEL_MISC, "open_inventory")

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
    
    def open_megaphone_settings(self, mod):
        """Open megaphone settings menu (client-side only)"""
        # Open megaphone settings menu directly
        from . import megaphone_settings
        self.add_substate(megaphone_settings.megaphone_settings(self.game, self))
    
    def update_megaphone_settings(self, volume, bass, mid, high):
        """Update megaphone audio settings in real-time"""
        if not hasattr(self, 'megaphone_sources'):
            return
        
        # Recreate EQ effect with new values
        new_eq_slot = self.game.audio_mngr.gen_effect(
            "EQUALIZER",
            ("low_gain", bass),
            ("low_cutoff", 200.0),
            ("mid1_gain", mid),
            ("mid1_center", 1200.0),
            ("mid1_width", 1.0),
            ("high_gain", high),
            ("high_cutoff", 4000.0)
        )
        
        # Get original gains (4 Corner speakers)
        original_gains = [0.6, 0.6, 0.6, 0.6]
        
        # Apply new settings to all megaphone sources
        for i, src in enumerate(self.megaphone_sources):
            # Update volume (apply to gain) with bounds check
            if i < len(original_gains):
                src.gain = original_gains[i] * (volume / 100.0)
            
            # Update EQ
            if hasattr(self.game.audio_mngr, 'efx') and new_eq_slot:
                self.game.audio_mngr.efx.send(src, 0, new_eq_slot)

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
            return self.spectator_menu(mod)
        
        m = menu.Menu(
            self.game,
            "Are you sure you want to exit?",
            parrent=self,
        )
        items = [
            ("Yes", lambda: self.quit(mod)),
            ("No", self.pop_last_substate),
        ]
        m.add_items(items)
        menus.set_default_sounds(m)
        self.add_substate(m)

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
        if getattr(self, 'concert_spectator_mode', False):
            self.game.network.send(consts.CHANNEL_CHAT, "chat", {"message": "/spec"})
            self.pop_last_substate()
            return
            
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

    def music_bot_control(self, mod):
        """Music Bot controls using M key:
        M              = Open YouTube search
        Shift+M        = Pause / Resume
        Ctrl+M         = Stop playback
        Ctrl+Shift+M   = Speak status
        Alt+M          = Toggle broadcast (mute to others)
        """
        if not hasattr(self, 'music_bot') or not self.music_bot:
            return
        
        if mod & pygame.KMOD_CTRL and mod & pygame.KMOD_SHIFT:
            # Ctrl+Shift+M → Speak status
            self.music_bot.speak_status()
        elif mod & pygame.KMOD_CTRL:
            # Ctrl+M → Stop / Replay (toggle)
            if self.music_bot.playing:
                self.music_bot.stop()
                speak("Music stopped.")
            elif self.music_bot.last_youtube_url:
                speak(f"Replaying: {self.music_bot.last_youtube_title}")
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
        """Adjust Music Bot volume by delta (F9 = down, F10 = up)"""
        if not hasattr(self, 'music_bot') or not self.music_bot:
            return
        new_vol = max(0, min(100, self.music_bot.volume + delta))
        self.music_bot.set_volume(new_vol)
        speak(f"Music Bot volume: {new_vol} percent.")

    def reset_pitch(self, mod):
        if mod & pygame.KMOD_CTRL:
            self.open_language_menu(mod)
            return
        if mod & pygame.KMOD_ALT:
            self.toggle_in_ear_monitor()
            return
            
        if not self.player.locked:
            self.player.face(self.player.hfacing, 0, self.player.bfacing)
            speak("You now have a pitch of 0 degrees")
            self.player.play_sound("foley/turn/stop.ogg", cat="self")

    def toggle_in_ear_monitor(self):
        self.in_ear_monitor = not getattr(self, "in_ear_monitor", False)
        if self.in_ear_monitor:
            speak("In-Ear Monitor Enabled. Megaphone echo is muted.")
        else:
            speak("In-Ear Monitor Disabled. Standard Megaphone audio restored.")


    def open_language_menu(self, mod):
        self.game.network.send(consts.CHANNEL_MISC, "request_language_menu", {})

    def show_language_menu(self, available_langs, language_counts, current):
        if not available_langs:
            speak("No language channels available.")
            return

        items = []
        for code, name in available_langs.items():
            def make_cb(c):
                return lambda: self.set_channel_language(c)
            
            count = language_counts.get(code, 0)
            player_str = f" {count} players" if count > 0 else ""
            
            display_text = f"Current {name}{player_str}" if code == current else f"{name}{player_str}"
            items.append((display_text, make_cb(code)))
        
        items.append(("Cancel", lambda: None))
        m = menu.Menu(self.game, "Select your channel language", parrent=self, autoclose=True)
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
        self.game.network.send(
            consts.CHANNEL_MISC,
            "interact",
            {
                "angle": self.player.hfacing, 
                "pitch": self.player.vfacing,
                "selected_slot": selected_slot,
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
            menus.options_menu(self.game, self.pop_last_substate, replace_call=self.add_substate, parent=self, in_game=True)
    
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
        
        # Staff OR Builder OR Technician can use this feature
        is_staff = getattr(self, 'is_staff', False)
        is_builder = getattr(self, 'is_builder', False)
        is_technician = getattr(self, 'is_technician', False)
        if not is_staff and not is_builder and not is_technician:
            speak("System: PA Test Mode is only available for staff, builders, and sound technicians.")
            return
        
        # Check if game has started - block PA Test Mode if so
        if self.game_started:
            speak("System: PA Test Mode is only available before game starts.")
            return
        
        # Check if map has PA speakers
        if not hasattr(self, 'megaphone_sources') or not self.megaphone_sources:
            speak("System: No PA speakers available on this map.")
            return
        
        if consts.CHANNEL_MEGAPHONE not in self.voice_channels:
            speak("System: No PA speakers available on this map.")
            return
        
        # Toggle PA Test Mode
        self.pa_test_mode = not self.pa_test_mode
        
        if self.pa_test_mode:
            from libs import logger
            logger.log("PA Test Mode activated.")
            key_name = pygame.key.name(self.kc.get("voice_chat", pygame.K_g)).upper()
            speak(f"System: PA Test Mode activated. Press {key_name} to test speakers.")
        else:
            from libs import logger
            logger.log("PA Test Mode deactivated.")
            speak("System: PA Test Mode deactivated.")
            # If currently recording, switch back to default channel immediately
            if hasattr(self, 'voice_chat') and self.voice_chat and self.voice_chat.recording:
                if not hasattr(self, '_default_vc_compression'):
                    self._default_vc_compression = voice_chat.voice_chat_compression(self.game, consts.CHANNEL_VOICECHAT)
                self.voice_chat.vc_compression = self._default_vc_compression
    
    
    def toggle_sonar_and_force_quit(self, mod):
        if mod & pygame.KMOD_ALT:
            self.quit(mod)
            self.game.quit()
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
            if consts.CHANNEL_MEGAPHONE not in self.voice_channels and not hasattr(self, 'megaphone_sources'):
                 speak("System: No public address system available directly in this area.")
                 return
            if consts.CHANNEL_MEGAPHONE not in self.voice_channels and hasattr(self, 'megaphone_sources') and not self.megaphone_sources:
                 speak("System: No public address system available directly in this area.")
                 return
            
            # Check if megaphone is locked by a staff broadcast
            lock_owner = getattr(self, 'megaphone_lock_owner', None)
            if lock_owner and lock_owner != getattr(self.player, 'name', ''):
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

        self.voice_chat.audio_input.start()
        self.voice_chat.recording = True
        self.voice_chat_using_megaphone = use_megaphone
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

