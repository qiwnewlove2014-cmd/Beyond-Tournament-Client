"""
Piano Audio Module — Manages all piano-specific audio logic.

Handles stereo buffer splitting, 3D spatial note playback, wall occlusion
filtering, active note tracking, and damper fade-out for piano performances.
"""
import contextlib
import os
import threading
import time
import array

import cyal
import cyal.exceptions
import pyogg

from . import consts
from . import path_utils
from .audio.sound import Sound


class PianoAudio:
    """Encapsulates all piano audio functionality.
    
    Requires a reference to the parent AudioManager instance to access
    shared resources (context, buffers, EFX, filters, volume categories).
    """

    def __init__(self, audio_manager):
        self.am = audio_manager
        self.gameplay = None  # Set by Gameplay.__init__ after construction
        self.active_piano_notes = {}
        self._occlusion_filter = None

    def load_stereo_split_buffers(self, path: str):
        """Split a stereo .ogg file into two MONO16 OpenAL buffers (L/R) in RAM.
        
        Returns (buf_l, buf_r). Results are cached in AudioManager.buffers.
        For mono source files, returns the same buffer for both channels.
        """
        if not os.path.isabs(path) and not path.startswith(consts.SOUNDPREPEND): path = os.path.join(consts.SOUNDPREPEND, path)
        if not path.endswith(".ogg"): path = path_utils.get_next_cycle_item(path)
        try:
            path = os.path.normpath(path) if os.path.isabs(path) else os.path.relpath(path)
        except ValueError:
            path = os.path.normpath(path)
            
        cache_key_l = f"{path}_split_L"
        cache_key_r = f"{path}_split_R"
        if cache_key_l in self.am.buffers and cache_key_r in self.am.buffers:
            return self.am.buffers[cache_key_l], self.am.buffers[cache_key_r]
        
        try:
            file = pyogg.VorbisFile(path)
            audio_data = bytes(file.buffer)
            if file.channels == 2:
                stereo_samples = array.array('h', audio_data)
                l_bytes = array.array('h', stereo_samples[0::2]).tobytes()
                r_bytes = array.array('h', stereo_samples[1::2]).tobytes()
                
                try: buf_l = self.am.context.gen_buffer()
                except cyal.exceptions.InvalidOperationError: buf_l = self.am.context.gen_buffer()
                
                try: buf_r = self.am.context.gen_buffer()
                except cyal.exceptions.InvalidOperationError: buf_r = self.am.context.gen_buffer()
                
                buf_l.set_data(l_bytes, sample_rate=file.frequency, format=cyal.BufferFormat.MONO16)
                buf_r.set_data(r_bytes, sample_rate=file.frequency, format=cyal.BufferFormat.MONO16)
                
                self.am.buffers[cache_key_l] = buf_l
                self.am.buffers[cache_key_r] = buf_r
                return buf_l, buf_r
            else:
                buf = self.am.load_buffer(path)
                return buf, buf
        except Exception as e:
            print(f"Error loading split stereo buffers for {path}: {e}")
            return None, None

    def get_occlusion_filter(self):
        """Lazy-create and return a lowpass filter for wall occlusion muffling."""
        if self._occlusion_filter is None:
            self._occlusion_filter = self.am.gen_filter(
                "LOWPASS",
                ("GAINHF", 0.15),  # Muffle high frequencies behind walls
                ("GAIN", 0.5)      # Attenuate overall direct volume
            )
        return self._occlusion_filter

    def play_note(self, peer_id, note_name, x, y, z, listener_x, listener_y, listener_z, volume=300, occluded=False):
        """Play a piano note with 3D stereo spreading (remote) or direct stereo (local).
        
        Automatically handles note re-triggering, occlusion filtering,
        and active note tracking for sustain/staccato pedal support.
        Also routes notes through PA Megaphone Speakers if broadcasting to Megaphone.
        """
        is_local = (peer_id == "local")
        snd = self.am.play_unbound_stereo_spatial(
            path=f"piano/Piano.mf.{note_name}.ogg",
            x=x, y=y, z=z,
            listener_x=listener_x,
            listener_y=listener_y,
            listener_z=listener_z,
            volume=volume,
            cat="miscelaneous",
            as_3d_stereo=not is_local,
            occluded=occluded
        )
        if snd:
            if occluded:
                filter_obj = self.get_occlusion_filter()
                if filter_obj:
                    with contextlib.suppress(Exception):
                        if isinstance(snd, (list, tuple)):
                            for s in snd:
                                if s and hasattr(s, 'source') and s.source:
                                    s.source.direct_filter = filter_obj
                        elif hasattr(snd, 'source') and snd.source:
                            snd.source.direct_filter = filter_obj
            piano_key = f"{peer_id}-{note_name}"
            # If the same peer plays the same note very rapidly, stop the old one first
            if piano_key in self.active_piano_notes:
                self.stop_note(peer_id, note_name)
            self.active_piano_notes[piano_key] = snd

        # Route through PA Megaphone speakers if performer is staff and broadcast to megaphone is active
        try:
            gp = self.gameplay
            if gp and hasattr(gp, 'music_bot') and gp.music_bot:
                if getattr(gp.music_bot, 'broadcast_to_megaphone', False):
                    self.route_to_megaphone_speakers(peer_id, note_name, volume)
        except Exception:
            pass

        return snd

    def route_to_megaphone_speakers(self, peer_id, note_name, base_volume=300):
        """Spawn a piano note at every megaphone PA speaker position with PA filter & EQ.

        Shared by the local performer (play_note) and remote listeners
        (event_handeler.play_unbound). Tracked under key "mega-<peer_id>-<note>"
        so stop_note fades every spawned source out together.
        """
        try:
            gp = self.gameplay
            if not (gp and hasattr(gp, 'megaphone') and gp.megaphone):
                return
            if not (hasattr(gp.megaphone, 'speaker_data') and gp.megaphone.speaker_data):
                return
            # Volume scales with the local Music Bot volume but is floored at 10%
            # so piano-through-PA stays audible when music is paused/muted, and
            # scaled down overall (×0.5) to avoid the PA being much louder than the
            # source piano when multiple speakers stack. Applies identically to
            # performer and listener.
            bot_vol_raw = getattr(getattr(gp, 'music_bot', None), 'volume', 50) / 100.0 if getattr(gp, 'music_bot', None) else 0.5
            bot_vol = max(0.1, bot_vol_raw) * 0.5
            for spk in gp.megaphone.speaker_data:
                spk_pos = spk.get('position', None)
                if spk_pos is None:
                    continue
                sx, sy, sz = spk_pos[0], spk_pos[1], spk_pos[2]
                mega_vol = base_volume * spk.get('base_volume', 0.6) * bot_vol
                mega_snd = self.am.play_unbound(
                    f"piano/Piano.mf.{note_name}.ogg",
                    sx, sy, sz,
                    volume=mega_vol,
                    cat="miscelaneous"
                )
                if mega_snd and hasattr(mega_snd, 'source') and mega_snd.source:
                    # Apply Megaphone PA Filter & EQ effects
                    if hasattr(gp.megaphone, 'lowpass_filter') and gp.megaphone.lowpass_filter:
                        mega_snd.source.direct_filter = gp.megaphone.lowpass_filter
                    if hasattr(self.am, 'efx'):
                        if hasattr(gp.megaphone, 'eq_slot') and gp.megaphone.eq_slot:
                            self.am.efx.send(mega_snd.source, 1, gp.megaphone.eq_slot)
                        if hasattr(gp.megaphone, 'reverb_slot') and gp.megaphone.reverb_slot:
                            self.am.efx.send(mega_snd.source, 2, gp.megaphone.reverb_slot)
                    mega_key = f"mega-{peer_id}-{note_name}"
                    if mega_key not in self.active_piano_notes:
                        self.active_piano_notes[mega_key] = []
                    elif not isinstance(self.active_piano_notes[mega_key], list):
                        self.active_piano_notes[mega_key] = [self.active_piano_notes[mega_key]]
                    self.active_piano_notes[mega_key].append(mega_snd)
        except Exception:
            pass

    def stop_note(self, peer_id, note_name):
        """Stop a piano note with a smooth 180ms damper fade-out.
        
        Handles both single Sound objects and (snd_l, snd_r) tuples
        from dual-source 3D stereo spreading.
        """
        piano_key = f"{peer_id}-{note_name}"
        mega_key = f"mega-{peer_id}-{note_name}"
        snds = self.active_piano_notes.pop(piano_key, None)
        mega_snds = self.active_piano_notes.pop(mega_key, None)
        
        all_snds = []
        if snds:
            all_snds.extend(snds if isinstance(snds, (list, tuple)) else [snds])
        if mega_snds:
            all_snds.extend(mega_snds if isinstance(mega_snds, (list, tuple)) else [mega_snds])

        if all_snds:
            for snd in all_snds:
                if snd and hasattr(snd, 'source') and snd.source:
                    # Smooth damper fade-out (~180ms) instead of harsh instant stop
                    def _fade_out(source, steps=10, duration=0.18):
                        try:
                            original_gain = source.gain
                            step_time = duration / steps
                            for i in range(steps, 0, -1):
                                source.gain = original_gain * (i / steps)
                                time.sleep(step_time)
                            source.stop()
                        except Exception:
                            pass
                    threading.Thread(target=_fade_out, args=(snd.source,), daemon=True).start()
