import math
import time
import pygame
from libs import options, consts, voice_chat

class MegaphoneManager:
    MAX_MEGAPHONE_PLAYERS = 8
    
    def __init__(self, gameplay):
        self.gameplay = gameplay
        self.game = gameplay.game
        self.map = gameplay.map
        self.camera = gameplay.camera
        self.player = gameplay.player
        self.voice_channels = gameplay.voice_channels
        
        self.sources = []
        # === FIXED MAP-CORNER PA SYSTEM ===
        # Speakers at ACTUAL MAP CORNERS (updated lazily when map bounds available)
        # This ensures speakers are truly environmental, not attached to any player
        
        # Flag to track if speakers have been positioned to map corners
        self.positioned = False
        
        # --- Environmental Reverb (Stadium Effect) - BALANCED ---
        self.reverb_slot = self.game.audio_mngr.gen_effect(
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
        
        # --- EQ: Crisp Megaphone PA Cabinet Effect with Rich Bass ---
        self.eq_slot = self.game.audio_mngr.gen_effect(
            "EQUALIZER",
            ("low_gain", 2.2),          # Warm & punchy bass body
            ("low_cutoff", 160.0),      # Allows low vocal fundamentals
            ("mid1_gain", 1.4),         # Presence boost for voice clarity
            ("mid1_center", 1400.0),    # Human vocal presence center
            ("mid1_width", 1.0),
            ("high_gain", 0.8),         # Crisp treble (high clarity)
            ("high_cutoff", 4000.0)
        )
        
        # --- Low-Pass Filter: Bright & Clear PA Sound ---
        self.lowpass_filter = self.game.audio_mngr.gen_filter(
            "LOWPASS",
            ("GAIN", 0.95),
            ("GAINHF", 0.85)             # Clear highs (was 0.4 muffled)
        )
        
        # --- Compressor: Make voice levels consistent ---
        self.compressor_slot = self.game.audio_mngr.gen_effect(
            "COMPRESSOR",
            ("onoff", 1)                  # Enable compressor
        )
        
        # NOTE: Echo effect removed - using only reverb for clean sound
        
        # --- Directional Filters for PA speakers ---
        # Normal: Standard PA cabinet sound
        self.normal_filter = self.lowpass_filter
        # Muffled: Heavy low-pass for behind-speaker position
        self.muffled_filter = self.game.audio_mngr.gen_filter(
            type="LOWPASS"
        )
        if self.muffled_filter:
            self.muffled_filter.set("GAIN", 0.6)  # Dampen volume a bit
            self.muffled_filter.set("GAINHF", 0.05)  # Cut high frequencies severely

        # Provide a specialized underwater filter for megaphones
        self.underwater_filter = self.game.audio_mngr.gen_filter(
            type="LOWPASS"
        )
        if self.underwater_filter:
            self.underwater_filter.set("GAIN", 0.8)  # Slightly preserve volume
            self.underwater_filter.set("GAINHF", 0.02)  # Extreme high-freq cutoff for underwater muffling
        
        # Speaker data storage for directional checking
        self.speaker_data = []
        self.muffled_check_counter = 0  # Frame counter for optimization
        self.last_megaphone_setup = 0  # Timestamp for debounce
        
        self.setup_megaphone_speakers()

    def trigger_fade_transition(self, duration=0.8):
        """Triggers a smooth volume crossfade transition when switching spectator/concert targets."""
        import time as _time
        self.concert_fade_in_start = _time.time()
        self.concert_fade_in_duration = duration
        
        if hasattr(self, 'player_sources'):
            for sid, entry in list(self.player_sources.items()):
                if 'sources' in entry and entry['sources']:
                    fade_sources = []
                    fade_filters = []
                    start_vols = []
                    for src, flt, vol in zip(entry['sources'], entry.get('filters', []), entry.get('currents_vol', [])):
                        if src:
                            fade_sources.append(src)
                            fade_filters.append(flt)
                            start_vols.append(vol)
                    
                    if fade_sources:
                        if not hasattr(self, 'fading_sources'):
                            self.fading_sources = []
                        self.fading_sources.append({
                            'sid': sid,
                            'sources': fade_sources,
                            'filters': fade_filters,
                            'start_vols': start_vols,
                            'fade_start': _time.time(),
                            'fade_duration': duration,
                            'is_concert': getattr(self.gameplay, 'concert_spectator_mode', False)
                        })
            self.player_sources.clear()

    def _check_speaker_occlusion(self, speaker_pos, player_pos):
        """Check if any solid tile blocks the path from speaker to player.
        Uses simple line-of-sight raycast to detect walls blocking sound."""
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

    def setup_megaphone_speakers(self, force=False):
        """Initializes or re-initializes megaphone speakers based on map data"""
        
        # Debounce: Prevent running more than once per second
        # This prevents crash loops if map data triggers repeated reload
        if not force and hasattr(self, 'last_megaphone_setup') and time.time() - self.last_megaphone_setup < 1.0:
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
        if hasattr(self, 'speaker_data'):
            for data in self.speaker_data:
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
        if hasattr(self, 'sources'):
            for i, src in enumerate(self.sources):
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
        if hasattr(self, 'player_sources'):
            for sid in list(self.player_sources.keys()):
                self._remove_megaphone_player(sid)

        self.sources = []
        self.speaker_data = []
        self.player_sources = {}  # {sender_id: {'sources': [...], 'last_active': float}}
        self.lock_owner = None
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
                    if self.eq_slot: 
                        self.game.audio_mngr.efx.send(src, 0, self.eq_slot, filter=p_filter)
                    if speaker_reverb_slot:  # Only apply if reverb was created (decay > 0.1)
                        self.game.audio_mngr.efx.send(src, 1, speaker_reverb_slot, filter=p_filter)
                    if self.compressor_slot: 
                        self.game.audio_mngr.efx.send(src, 2, self.compressor_slot, filter=p_filter)
                except Exception as e:
                    print(f"[MEGAPHONE EFX] Error applying effects: {e}")
                
            self.sources.append(src)
            vc_sources.append(src)
            
            # Store speaker data for directional muffled sound checking AND volume updates
            self.speaker_data.append({
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
                            if self.eq_slot:
                                self.game.audio_mngr.efx.send(reflection_src, 0, self.eq_slot, filter=r_filter)
                            if speaker_reverb_slot:
                                self.game.audio_mngr.efx.send(reflection_src, 1, speaker_reverb_slot, filter=r_filter)
                            if self.compressor_slot:
                                self.game.audio_mngr.efx.send(reflection_src, 2, self.compressor_slot, filter=r_filter)
                        except Exception:
                            pass
                    
                    self.sources.append(reflection_src)
                    vc_sources.append(reflection_src)
                    
                    # Store reflection references and states in the primary speaker's data dict
                    self.speaker_data[-1].update({
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

    def get_megaphone_player_sources(self, sender_id):
        """Get or create per-player OpenAL sources for megaphone speakers.
        Clones spatial properties from the physical speaker template sources.
        Returns a list of sources, or None if no speakers are configured."""
        import time as _time

        if not hasattr(self, 'player_sources'):
            self.player_sources = {}

        # Periodic cleanup of inactive players (throttled: at most once per second)
        if not hasattr(self, '_last_mega_cleanup') or _time.time() - self._last_mega_cleanup > 1.0:
            self.cleanup_inactive_megaphone_players()
            self._last_mega_cleanup = _time.time()

        # If sender already has sources, update timestamp and return
        if sender_id in self.player_sources:
            self.player_sources[sender_id]['last_active'] = _time.time()
            return self.player_sources[sender_id]['sources']

        # Check if we have speaker data to clone from
        if not hasattr(self, 'speaker_data') or not self.speaker_data:
            return None

        # If at capacity, evict least recently active player
        if len(self.player_sources) >= self.MAX_MEGAPHONE_PLAYERS:
            oldest_id = min(
                self.player_sources,
                key=lambda k: self.player_sources[k]['last_active']
            )
            self._remove_megaphone_player(oldest_id)

        # Create new sources cloning physical speaker positions
        sources = []
        filters = []
        global_vol = options.get("megaphone_volume", 100) / 100.0

        if getattr(self.gameplay, 'concert_spectator_mode', False):
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
            for i, spk_data in enumerate(self.speaker_data):
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
                            if hasattr(self, 'eq_slot') and self.eq_slot:
                                self.game.audio_mngr.efx.send(src, 0, self.eq_slot, filter=p_filter)
                            if spk_data.get('reverb_slot'):
                                self.game.audio_mngr.efx.send(src, 1, spk_data['reverb_slot'], filter=p_filter)
                            if hasattr(self, 'compressor_slot') and self.compressor_slot:
                                self.game.audio_mngr.efx.send(src, 2, self.compressor_slot, filter=p_filter)
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
                                if hasattr(self, 'eq_slot') and self.eq_slot:
                                    self.game.audio_mngr.efx.send(refl_src, 0, self.eq_slot, filter=r_filter)
                                if spk_data.get('reverb_slot'):
                                    self.game.audio_mngr.efx.send(refl_src, 1, spk_data['reverb_slot'], filter=r_filter)
                                if hasattr(self, 'compressor_slot') and self.compressor_slot:
                                    self.game.audio_mngr.efx.send(refl_src, 2, self.compressor_slot, filter=r_filter)
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
                spk_data = self.speaker_data[spk_idx]
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
                    
            # Set source gain to 0.0 initially so they fade in smoothly via the update loop
            for idx, src_obj in enumerate(sources):
                if src_obj:
                    src_obj.gain = 0.0
                    
            self.player_sources[sender_id] = {
                'sources': sources,
                'filters': filters,
                'last_active': _time.time(),
                'targets_vol': targets_vol,
                'currents_vol': [0.0] * len(targets_vol),
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
        if not hasattr(self, 'player_sources'):
            return
        if sender_id not in self.player_sources:
            return

        entry = self.player_sources[sender_id]
        
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

        del self.player_sources[sender_id]

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
        if not hasattr(self, 'player_sources'):
            return
        now = _time.time()
        inactive = [sid for sid, entry in self.player_sources.items()
                    if now - entry['last_active'] > 5.0]
        for sid in inactive:
            self._remove_megaphone_player(sid)
            
        # Cleanup expired fading sources
        if hasattr(self, 'fading_sources'):
            to_remove = []
            for fade_obj in self.fading_sources:
                if now - fade_obj['fade_start'] >= fade_obj['fade_duration']:
                    for f_src in fade_obj['sources']:
                        if f_src and getattr(f_src, "is_valid", lambda: False)():
                            f_src.destroy()
                    to_remove.append(fade_obj)
            for item in to_remove:
                self.fading_sources.remove(item)

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
        self.normal_filter = None

        # Return per-speaker reverb slots to pool + cleanup reflection sources
        if hasattr(self, 'speaker_data'):
            for data in self.speaker_data:
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
            self.speaker_data.clear()

        # Cleanup megaphone sources
        if hasattr(self, 'sources'):
            for src in self.sources:
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
            self.sources.clear()

    def update_megaphone_settings(self, volume, bass, mid, high):
        """Called by megaphone_settings menu to update audio in real-time"""
        # Updates global volume multiplier for all speakers
        global_vol = volume / 100.0
        
        if hasattr(self, 'speaker_data'):
            for data in self.speaker_data:
                try:
                    # Recalculate gain: Base (Map) * Global (Slider)
                    new_gain = data['base_volume'] * global_vol
                    data['source'].gain = new_gain
                except Exception as e:
                    print(f"[MEGAPHONE] Error updating volume: {e}")

    def update_megaphone_settings(self, volume, bass, mid, high):
        """Update megaphone audio settings in real-time"""
        if not hasattr(self, 'sources'):
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
        for i, src in enumerate(self.sources):
            # Update volume (apply to gain) with bounds check
            if i < len(original_gains):
                src.gain = original_gains[i] * (volume / 100.0)
            
            # Update EQ
            if hasattr(self.game.audio_mngr, 'efx') and new_eq_slot:
                self.game.audio_mngr.efx.send(src, 0, new_eq_slot)

    def open_megaphone_settings(self, mod):
        """Open megaphone settings menu (client-side only)"""
        # Open megaphone settings menu directly
        from . import megaphone_settings
        self.add_substate(megaphone_settings.megaphone_settings(self.game, self))


    def update_megaphone_audio(self, distance, listener_pos):
        # Tick megaphone delay queues to flush remaining audio when a player finishes speaking
        if hasattr(self, 'player_sources') and self.player_sources:
            if consts.CHANNEL_MEGAPHONE in self.voice_channels:
                channel = self.voice_channels[consts.CHANNEL_MEGAPHONE]
                if hasattr(channel, 'vc_compression'):
                    channel.vc_compression.put(lambda: voice_chat.tick_megaphone_delay(self))

        # === MEGAPHONE DYNAMIC REVERB SYNC ===
        # Synchronize megaphone speakers with the player's local reverb zone
        # This gives the realistic impression that the PA system is echoing inside the current room
        f_obj = self.camera.focus_object
        fx = float(getattr(f_obj, "x", 0.0) or 0.0)
        fy = float(getattr(f_obj, "y", 0.0) or 0.0)
        fz = float(getattr(f_obj, "z", 0.0) or 0.0)
        current_reverb_zone = self.map.get_reverb_at(fx, fy, fz)
        
        # PROXIMITY REVERB EFFECT: If not strictly inside a reverb zone, 
        # check if player is near one (simulates hearing reverb when walking close to a room)
        if not current_reverb_zone:
            expansion = 5.0  # units
            for r in self.map.reverb_list:
                if (r.minx - expansion <= fx <= r.maxx + expansion and
                    r.miny - expansion <= fy <= r.maxy + expansion and
                    r.minz - expansion <= fz <= r.maxz + expansion):
                    current_reverb_zone = r
                    break
                    
        new_local_reverb_slot = current_reverb_zone.reverb if current_reverb_zone else None
        
        if getattr(self, 'current_player_reverb_slot', 'UNINIT') != new_local_reverb_slot:
            self.gameplay.current_player_reverb_slot = new_local_reverb_slot
            
            # Send slot 3 is reserved for the player's local dynamic reverb
            if hasattr(self, 'speaker_data'):
                for data in self.speaker_data:
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
            if hasattr(self, 'player_sources') and not getattr(self.gameplay, 'concert_spectator_mode', False):
                for player_entry in self.player_sources.values():
                    if 'sources' in player_entry:
                        for src in player_entry['sources']:
                            try:
                                current_flt = getattr(src, 'direct_filter', None)
                                self.game.audio_mngr.efx.send(src, 3, new_local_reverb_slot, filter=current_flt)
                            except Exception:
                                pass
        
        # === DIRECTIONAL MUFFLED SOUND + LINE-OF-SIGHT OCCLUSION ===
        # Every 10 frames, check if player is behind speaker OR blocked by wall
        if hasattr(self, 'speaker_data') and hasattr(self, 'muffled_check_counter'):
            self.muffled_check_counter += 1
            if self.muffled_check_counter >= 10:
                self.muffled_check_counter = 0
                player_pos = (self.camera.focus_object.x, self.camera.focus_object.y, self.camera.focus_object.z)
                global_vol = options.get("megaphone_volume", 100) / 100.0
                is_underwater = getattr(self.camera.focus_object, 'in_water', False)
                
                for i, data in enumerate(self.speaker_data):
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
                        if hasattr(self, 'player_sources'):
                            for player_entry in self.player_sources.values():
                                if getattr(self.gameplay, 'concert_spectator_mode', False):
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
        if hasattr(self, 'speaker_data'):
            smooth_factor = 0.15  # 15% transition per frame (~250-300ms total fade duration)
            global_vol = options.get("megaphone_volume", 100) / 100.0
            
            # === SUM-SAFE EQUAL-POWER MIXING (prevents clipping when N talkover) ===
            # Instead of winner-takes-all ducking (one speaker loud, rest at 25%),
            # every currently-active speaker gets gain = 1/√N. The summed energy
            # stays ≈1.0 regardless of how many people talk over each other, so the
            # master bus never clips. Everyone is heard equally.
            now = time.time()
            active_speaker_ids = set()
            if hasattr(self, 'player_sources'):
                for sid, entry in self.player_sources.items():
                    if now - entry.get('last_active', 0.0) < 0.4:
                        active_speaker_ids.add(sid)
            n_active = len(active_speaker_ids)
            mix_gain = 1.0 / math.sqrt(n_active) if n_active > 0 else 1.0
            
            for i, data in enumerate(self.speaker_data):
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
                    if hasattr(self, 'player_sources'):
                        for sid, player_entry in self.player_sources.items():
                            if 'sources' in player_entry and 'filters' in player_entry:
                                prim_idx = 2 * i
                                refl_idx = 2 * i + 1
                                
                                # Equal-power ducking: every active speaker is attenuated
                                # by 1/√N so the summed level stays ≈ constant (sum-safe).
                                # Inactive senders (not currently speaking) keep gain 1.0 —
                                # they are silent anyway since no packets feed their sources.
                                duck_mult = mix_gain if sid in active_speaker_ids else 1.0
                                
                                 # Fade-in logic for toggling concert/spectator mode
                                concert_fade_in_mult = 1.0
                                if hasattr(self, 'concert_fade_in_start'):
                                    import time as _time
                                    elapsed = _time.time() - getattr(self, 'concert_fade_in_start', 0)
                                    dur = getattr(self, 'concert_fade_in_duration', 1.5)
                                    if elapsed <= dur:
                                        concert_fade_in_mult = elapsed / dur
                                
                                # Interpolate player primary source
                                if prim_idx < len(player_entry['sources']) and player_entry['sources'][prim_idx] is not None and player_entry['filters'][prim_idx] is not None:
                                    src = player_entry['sources'][prim_idx]
                                    flt = player_entry['filters'][prim_idx]
                                    
                                    # Volume
                                    t_vol = player_entry['targets_vol'][prim_idx] * duck_mult * concert_fade_in_mult
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
                                    t_vol = player_entry['targets_vol'][refl_idx] * duck_mult * concert_fade_in_mult
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
                            if hasattr(self, 'player_sources'):
                                for player_entry in self.player_sources.values():
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
