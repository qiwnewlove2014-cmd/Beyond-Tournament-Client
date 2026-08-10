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
        self.soft_pedal_states = {}
        self._pedal_filters = {}
        self._pedal_filter_values = {}
        self._pedal_transitions = {}
        self._filter_cleanup_deadlines = {}
        self.pitch_bend_states = {}
        self._pitch_bend_values = {}
        self._pitch_bend_transitions = {}
        self.chorus_states = {}
        self._chorus_slots = {}
        self._chorus_transitions = {}

    _FILTER_BASE_VALUES = {
        "normal": (1.0, 1.0),
        "occluded": (0.5, 0.15),
        "pa": (0.95, 0.85),
    }
    # Keep most of the note body while the low-pass closes. A large gain drop
    # makes the pedal feel like a volume cut instead of a tonal softening.
    _SOFT_GAIN_FACTOR = 0.90
    # The piano samples contain very little energy above 3 kHz, so a mild
    # LOWPASS GAINHF value is barely audible. 0.03 also attenuates the
    # 1-3 kHz presence band enough to produce a clearly muffled tone.
    _SOFT_HIGH_FREQUENCY_FACTOR = 0.03
    _PEDAL_PRESS_TRANSITION_SECONDS = 0.28
    _PEDAL_RELEASE_TRANSITION_SECONDS = 0.22
    _FILTER_CLEANUP_DELAY_SECONDS = 0.35
    _PITCH_BEND_SEMITONES = 2.0
    _PITCH_BEND_PRESS_SECONDS = 0.30
    _PITCH_BEND_RELEASE_SECONDS = 0.22
    _CHORUS_SEND_INDEX = 3
    _CHORUS_WET_GAIN = 0.24
    _CHORUS_FADE_SECONDS = 0.12
    _CHORUS_PARAMETERS = (
        ("WAVEFORM", 0),
        ("PHASE", 90),
        ("RATE", 0.65),
        ("DEPTH", 0.18),
        ("FEEDBACK", 0.08),
        ("DELAY", 0.012),
    )

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

    @staticmethod
    def _read_filter_values(filter_obj, defaults):
        """Return (GAIN, GAINHF), falling back for unsupported EFX drivers."""
        if filter_obj is None:
            return defaults
        try:
            return (
                float(filter_obj.get_float("GAIN")),
                float(filter_obj.get_float("GAINHF")),
            )
        except Exception:
            return defaults

    def _get_filter_base_values(self, mode):
        defaults = self._FILTER_BASE_VALUES.get(mode, self._FILTER_BASE_VALUES["normal"])
        if mode == "normal":
            global_filter = self.am.filter[-1] if getattr(self.am, "filter", None) else None
            return self._read_filter_values(global_filter, defaults)
        if mode == "pa":
            gp = self.gameplay
            megaphone = getattr(gp, "megaphone", None) if gp else None
            return self._read_filter_values(getattr(megaphone, "lowpass_filter", None), defaults)
        return defaults

    def _get_filter_target_values(self, peer_id, mode, base_values=None):
        gain, gain_hf = base_values or self._get_filter_base_values(mode)
        if self.soft_pedal_states.get(str(peer_id), False):
            gain *= self._SOFT_GAIN_FACTOR
            gain_hf *= self._SOFT_HIGH_FREQUENCY_FACTOR
        return gain, gain_hf

    def _set_filter_values(self, key, gain, gain_hf):
        filter_obj = self._pedal_filters.get(key)
        if filter_obj is None:
            return
        try:
            gain = max(0.0, min(1.0, gain))
            gain_hf = max(0.0, min(1.0, gain_hf))
            filter_obj.set("GAIN", gain)
            filter_obj.set("GAINHF", gain_hf)
            self._pedal_filter_values[key] = (gain, gain_hf)
        except Exception:
            return

        # EFX copies filter parameters into a Source when the filter is
        # attached. Updating the Filter object alone does not update sources
        # that are already playing, so re-attach it after every interpolation
        # step. The short eased transition minimizes audible handoff clicks.
        peer_id, mode = key
        for snd in list(getattr(self.am, "unbound_sources", [])):
            if (
                getattr(snd, "_piano_peer_id", None) != peer_id
                or getattr(snd, "_piano_filter_mode", None) != mode
            ):
                continue
            source = getattr(snd, "source", None)
            if source is not None:
                with contextlib.suppress(Exception):
                    source.direct_filter = filter_obj
                # Auxiliary sends keep their own copy of the filter settings,
                # just like the dry/direct path. Re-attach each registered
                # piano send so reverb and PA effects follow the pedal ramp.
                for send_idx, slot in getattr(
                    snd, "_piano_effect_sends", {}
                ).items():
                    with contextlib.suppress(Exception):
                        self.am.efx.send(
                            source, send_idx, slot, filter=filter_obj
                        )

    def _get_pedal_filter(self, peer_id, mode):
        peer_id = str(peer_id)
        key = (peer_id, mode)
        filter_obj = self._pedal_filters.get(key)
        if filter_obj is not None:
            return filter_obj

        gain, gain_hf = self._get_filter_target_values(peer_id, mode)
        filter_obj = self.am.gen_filter(
            "LOWPASS",
            ("GAIN", gain),
            ("GAINHF", gain_hf),
        )
        if filter_obj is not None:
            self._pedal_filters[key] = filter_obj
            self._pedal_filter_values[key] = (gain, gain_hf)
        return filter_obj

    def get_note_filter(self, peer_id, occluded=False):
        """Return the shared realtime filter for one performer's piano notes."""
        return self._get_pedal_filter(peer_id, "occluded" if occluded else "normal")

    def set_soft_pedal(self, peer_id, enabled, animate=True):
        """Change a performer's soft pedal and retune all sounding notes smoothly."""
        peer_id = str(peer_id)
        enabled = bool(enabled)
        if self.soft_pedal_states.get(peer_id, False) == enabled:
            return False

        self.soft_pedal_states[peer_id] = enabled
        keys = [key for key in self._pedal_filters if key[0] == peer_id]
        if not keys:
            return True

        if not animate:
            for key in keys:
                self._set_filter_values(key, *self._get_filter_target_values(*key))
            self._pedal_transitions.pop(peer_id, None)
            return True

        self._pedal_transitions[peer_id] = {
            "started": time.monotonic(),
            "duration": (
                self._PEDAL_PRESS_TRANSITION_SECONDS
                if enabled
                else self._PEDAL_RELEASE_TRANSITION_SECONDS
            ),
            "starts": {
                key: self._pedal_filter_values.get(
                    key, self._get_filter_target_values(*key)
                )
                for key in keys
            },
        }
        return True

    @staticmethod
    def _pitch_ratio(semitones):
        return 2.0 ** (float(semitones) / 12.0)

    def _apply_pitch_bend(self, peer_id, semitones):
        """Apply one performer's bend to every active dry, wet, and PA source."""
        peer_id = str(peer_id)
        semitones = max(
            -self._PITCH_BEND_SEMITONES,
            min(self._PITCH_BEND_SEMITONES, float(semitones)),
        )
        self._pitch_bend_values[peer_id] = semitones
        pitch = self._pitch_ratio(semitones)
        for snd in list(getattr(self.am, "unbound_sources", [])):
            if getattr(snd, "_piano_peer_id", None) != peer_id:
                continue
            source = getattr(snd, "source", None)
            if source is not None:
                with contextlib.suppress(Exception):
                    source.pitch = pitch

    def set_pitch_bend_value(
        self, peer_id, value, animate=False, transition_seconds=0.06
    ):
        """Set a normalized continuous pitch bend in the range -1.0..+1.0."""
        if isinstance(value, bool):
            return False
        try:
            value = float(value)
        except (TypeError, ValueError):
            return False
        if not -1.0 <= value <= 1.0:
            return False

        peer_id = str(peer_id)
        if self.pitch_bend_states.get(peer_id, 0.0) == value:
            if not animate and peer_id in self._pitch_bend_transitions:
                self._pitch_bend_transitions.pop(peer_id, None)
                self._apply_pitch_bend(
                    peer_id, value * self._PITCH_BEND_SEMITONES
                )
                return True
            return False
        self.pitch_bend_states[peer_id] = value
        start = self._pitch_bend_values.get(peer_id, 0.0)
        target = value * self._PITCH_BEND_SEMITONES
        if not animate:
            self._pitch_bend_transitions.pop(peer_id, None)
            self._apply_pitch_bend(peer_id, target)
            return True

        self._pitch_bend_transitions[peer_id] = {
            "started": time.monotonic(),
            "duration": max(0.012, float(transition_seconds)),
            "start": start,
            "target": target,
        }
        return True

    def set_pitch_bend_14bit(self, peer_id, value, animate=False):
        """Apply a centered MIDI/packet bend value in the -8192..8191 range."""
        if isinstance(value, bool) or not isinstance(value, int):
            return False
        if not -8192 <= value <= 8191:
            return False
        normalized = value / (8192.0 if value < 0 else 8191.0)
        return self.set_pitch_bend_value(
            peer_id, normalized, animate=animate, transition_seconds=0.06
        )

    def set_pitch_bend(self, peer_id, direction, animate=True):
        """Move the computer-keyboard pitch lever to -1, 0, or +1."""
        if isinstance(direction, bool) or direction not in (-1, 0, 1):
            return False
        peer_id = str(peer_id)
        start = self._pitch_bend_values.get(peer_id, 0.0)
        target = float(direction) * self._PITCH_BEND_SEMITONES
        base_duration = (
            self._PITCH_BEND_RELEASE_SECONDS
            if direction == 0
            else self._PITCH_BEND_PRESS_SECONDS
        )
        distance_scale = abs(target - start) / self._PITCH_BEND_SEMITONES
        return self.set_pitch_bend_value(
            peer_id,
            direction,
            animate=animate,
            transition_seconds=max(0.04, base_duration * distance_scale),
        )

    @staticmethod
    def _iter_sounds(sounds):
        if not sounds:
            return []
        return sounds if isinstance(sounds, (list, tuple)) else [sounds]

    def _tag_sounds(self, sounds, peer_id, mode):
        peer_id = str(peer_id)
        pitch = self._pitch_ratio(self._pitch_bend_values.get(peer_id, 0.0))
        for snd in self._iter_sounds(sounds):
            if snd is not None:
                snd._piano_peer_id = peer_id
                snd._piano_filter_mode = mode
                source = getattr(snd, "source", None)
                if source is not None:
                    with contextlib.suppress(Exception):
                        source.pitch = pitch

    def apply_effect_send(self, sounds, send_idx, slot):
        """Route tagged piano sounds through an effect using their pedal filter."""
        if slot is None or not hasattr(self.am, "efx"):
            return
        for snd in self._iter_sounds(sounds):
            if snd is None:
                continue
            peer_id = getattr(snd, "_piano_peer_id", None)
            mode = getattr(snd, "_piano_filter_mode", None)
            source = getattr(snd, "source", None)
            if peer_id is None or mode is None or source is None:
                continue
            filter_obj = self._get_pedal_filter(peer_id, mode)
            effect_sends = getattr(snd, "_piano_effect_sends", None)
            if effect_sends is None:
                effect_sends = {}
                snd._piano_effect_sends = effect_sends
            effect_sends[send_idx] = slot
            with contextlib.suppress(Exception):
                self.am.efx.send(
                    source, send_idx, slot, filter=filter_obj
                )

    def _iter_peer_sounds(self, peer_id):
        """Yield each live Sound tagged for one piano performer once."""
        peer_id = str(peer_id)
        candidates = list(getattr(self.am, "unbound_sources", []))
        for sounds in self.active_piano_notes.values():
            candidates.extend(self._iter_sounds(sounds))
        seen = set()
        for snd in candidates:
            if (
                snd is None
                or id(snd) in seen
                or getattr(snd, "_piano_peer_id", None) != peer_id
            ):
                continue
            seen.add(id(snd))
            yield snd

    def _get_chorus_slot(self, peer_id, initial_gain=None):
        """Lazily borrow one pooled Chorus slot for a performer."""
        peer_id = str(peer_id)
        slot = self._chorus_slots.get(peer_id)
        if slot is not None:
            return slot
        try:
            slot = self.am.gen_effect("CHORUS", *self._CHORUS_PARAMETERS)
            if slot is not None:
                slot.gain = (
                    self._CHORUS_WET_GAIN
                    if initial_gain is None
                    and self.chorus_states.get(peer_id, False)
                    else float(initial_gain or 0.0)
                )
                self._chorus_slots[peer_id] = slot
        except Exception as error:
            print(f"[PianoAudio] Chorus unavailable: {error}")
            slot = None
        return slot

    def apply_chorus_send(self, sounds, peer_id):
        """Route new performer sounds through Chorus when their state is on."""
        peer_id = str(peer_id)
        if not self.chorus_states.get(peer_id, False):
            return
        slot = self._get_chorus_slot(peer_id)
        if slot is not None:
            self.apply_effect_send(sounds, self._CHORUS_SEND_INDEX, slot)

    def _release_chorus_slot(self, peer_id):
        """Detach every send before returning this performer's slot to the pool."""
        peer_id = str(peer_id)
        slot = self._chorus_slots.pop(peer_id, None)
        self._chorus_transitions.pop(peer_id, None)
        if slot is None:
            return
        for snd in self._iter_peer_sounds(peer_id):
            source = getattr(snd, "source", None)
            sends = getattr(snd, "_piano_effect_sends", {})
            if source is not None and hasattr(self.am, "efx"):
                with contextlib.suppress(Exception):
                    self.am.efx.send(source, self._CHORUS_SEND_INDEX, None)
            sends.pop(self._CHORUS_SEND_INDEX, None)
        with contextlib.suppress(Exception):
            slot.unload()
        self.am.release_effect_slot(slot)

    def set_chorus(self, peer_id, enabled, animate=True):
        """Enable or disable one performer's Chorus with a short wet fade."""
        peer_id = str(peer_id)
        enabled = bool(enabled)
        previous = self.chorus_states.get(peer_id, False)
        self.chorus_states[peer_id] = enabled

        # Repeated note packets carry the current state for recovery, but must
        # not restart an in-progress fade on every played note.
        if previous == enabled:
            if not enabled or peer_id in self._chorus_slots:
                return False

        if enabled:
            peer_sounds = list(self._iter_peer_sounds(peer_id))
            if not peer_sounds:
                # Keep only the boolean while silent. The next note lazily
                # acquires a slot at the target wet gain.
                self._chorus_transitions.pop(peer_id, None)
                return previous != enabled
            slot = self._get_chorus_slot(peer_id, initial_gain=0.0)
            if slot is None:
                return previous != enabled
            self.apply_effect_send(
                peer_sounds,
                self._CHORUS_SEND_INDEX,
                slot,
            )
        else:
            slot = self._chorus_slots.get(peer_id)
            if slot is None:
                self._chorus_transitions.pop(peer_id, None)
                return previous != enabled

        try:
            current_gain = float(slot.gain)
        except Exception:
            current_gain = self._CHORUS_WET_GAIN if previous else 0.0
        target_gain = self._CHORUS_WET_GAIN if enabled else 0.0
        if not animate:
            with contextlib.suppress(Exception):
                slot.gain = target_gain
            self._chorus_transitions.pop(peer_id, None)
            if not enabled:
                self._release_chorus_slot(peer_id)
            return previous != enabled

        if abs(current_gain - target_gain) <= 0.001:
            if not enabled:
                self._release_chorus_slot(peer_id)
            return previous != enabled
        self._chorus_transitions[peer_id] = {
            "started": time.monotonic(),
            "duration": self._CHORUS_FADE_SECONDS,
            "start": current_gain,
            "target": target_gain,
            "release": not enabled,
        }
        return previous != enabled

    def _peer_has_active_notes(self, peer_id):
        normal_prefix = f"{peer_id}-"
        mega_prefix = f"mega-{peer_id}-"
        return any(
            key.startswith(normal_prefix) or key.startswith(mega_prefix)
            for key in self.active_piano_notes
        )

    def _schedule_filter_cleanup(self, peer_id):
        self._filter_cleanup_deadlines[str(peer_id)] = (
            time.monotonic() + self._FILTER_CLEANUP_DELAY_SECONDS
        )

    def _delete_peer_filters(self, peer_id):
        peer_id = str(peer_id)
        # Faded sources can remain in AudioManager until its next cleanup pass.
        # Detach them before deleting the filters they reference.
        for snd in list(getattr(self.am, "unbound_sources", [])):
            if getattr(snd, "_piano_peer_id", None) != peer_id:
                continue
            source = getattr(snd, "source", None)
            if source is not None:
                if hasattr(self.am, "efx"):
                    for send_idx in getattr(
                        snd, "_piano_effect_sends", {}
                    ):
                        with contextlib.suppress(Exception):
                            self.am.efx.send(source, send_idx, None)
                with contextlib.suppress(Exception):
                    del source.direct_filter

        for key in [key for key in self._pedal_filters if key[0] == peer_id]:
            filter_obj = self._pedal_filters.pop(key, None)
            self._pedal_filter_values.pop(key, None)
            if filter_obj is not None:
                # cyal.Filter has no public delete(); dropping the final owner
                # after detaching sources releases it through the wrapper.
                del filter_obj
        self._pedal_transitions.pop(peer_id, None)
        self._filter_cleanup_deadlines.pop(peer_id, None)

    def remove_peer(self, peer_id):
        """Stop and release all piano state when a remote performer leaves."""
        peer_id = str(peer_id)
        normal_prefix = f"{peer_id}-"
        mega_prefix = f"mega-{peer_id}-"
        sounds_to_stop = []
        for key in list(self.active_piano_notes):
            if key.startswith(normal_prefix) or key.startswith(mega_prefix):
                sounds_to_stop.extend(
                    self._iter_sounds(self.active_piano_notes.pop(key, None))
                )

        # Filters and auxiliary sends must be detached before the pooled
        # Chorus slot can be returned safely.
        self._delete_peer_filters(peer_id)
        self._release_chorus_slot(peer_id)
        for snd in sounds_to_stop:
            source = getattr(snd, "source", None)
            if source is not None:
                with contextlib.suppress(Exception):
                    source.stop()
        self.soft_pedal_states.pop(peer_id, None)
        self.pitch_bend_states.pop(peer_id, None)
        self._pitch_bend_values.pop(peer_id, None)
        self._pitch_bend_transitions.pop(peer_id, None)
        self.chorus_states.pop(peer_id, None)

    def update(self):
        """Advance pedal, bend, and Chorus transitions on the audio/game thread."""
        now = time.monotonic()
        transitioning_peers = set()
        base_values_by_mode = {}

        def target_values(key):
            mode = key[1]
            if mode not in base_values_by_mode:
                base_values_by_mode[mode] = self._get_filter_base_values(mode)
            return self._get_filter_target_values(
                *key, base_values=base_values_by_mode[mode]
            )

        for peer_id, transition in list(self._pedal_transitions.items()):
            transitioning_peers.add(peer_id)
            linear_progress = min(
                1.0,
                (now - transition["started"]) / transition["duration"],
            )
            # Smoothstep has zero slope at both ends, avoiding the perceived
            # "drop" of a linear low-pass change while keeping input latency
            # immediate and the full transition short.
            progress = linear_progress * linear_progress * (
                3.0 - (2.0 * linear_progress)
            )
            for key, start_values in transition["starts"].items():
                if key not in self._pedal_filters:
                    continue
                targets = target_values(key)
                gain = start_values[0] + (targets[0] - start_values[0]) * progress
                gain_hf = start_values[1] + (targets[1] - start_values[1]) * progress
                self._set_filter_values(key, gain, gain_hf)
            if linear_progress >= 1.0:
                self._pedal_transitions.pop(peer_id, None)

        # Keep neutral/soft filters aligned with dynamic global and PA filters.
        for key in list(self._pedal_filters):
            if key[0] in transitioning_peers:
                continue
            targets = target_values(key)
            current_values = self._pedal_filter_values.get(key)
            if current_values is None or any(
                abs(current - target) > 0.001
                for current, target in zip(current_values, targets)
            ):
                self._set_filter_values(key, *targets)

        for peer_id, deadline in list(self._filter_cleanup_deadlines.items()):
            if now >= deadline and not self._peer_has_active_notes(peer_id):
                self._delete_peer_filters(peer_id)
                # Preserve the replicated on/off state, but do not reserve a
                # scarce EFX slot while this performer is silent.
                self._release_chorus_slot(peer_id)

        for peer_id, transition in list(self._pitch_bend_transitions.items()):
            linear_progress = min(
                1.0,
                (now - transition["started"]) / transition["duration"],
            )
            progress = linear_progress * linear_progress * (
                3.0 - (2.0 * linear_progress)
            )
            semitones = transition["start"] + (
                (transition["target"] - transition["start"]) * progress
            )
            self._apply_pitch_bend(peer_id, semitones)
            if linear_progress >= 1.0:
                self._pitch_bend_transitions.pop(peer_id, None)

        for peer_id, transition in list(self._chorus_transitions.items()):
            slot = self._chorus_slots.get(peer_id)
            if slot is None:
                self._chorus_transitions.pop(peer_id, None)
                continue
            linear_progress = min(
                1.0,
                (now - transition["started"]) / transition["duration"],
            )
            progress = linear_progress * linear_progress * (
                3.0 - (2.0 * linear_progress)
            )
            gain = transition["start"] + (
                (transition["target"] - transition["start"]) * progress
            )
            with contextlib.suppress(Exception):
                slot.gain = gain
            if linear_progress >= 1.0:
                self._chorus_transitions.pop(peer_id, None)
                if transition["release"]:
                    self._release_chorus_slot(peer_id)

    def reset(self):
        """Release PianoAudio-owned filters and state during gameplay teardown."""
        owned_sounds = []
        for sounds in list(self.active_piano_notes.values()):
            owned_sounds.extend(self._iter_sounds(sounds))
        owned_sounds.extend(
            snd for snd in list(getattr(self.am, "unbound_sources", []))
            if getattr(snd, "_piano_peer_id", None) is not None
        )
        seen_sounds = set()
        for snd in owned_sounds:
            if id(snd) in seen_sounds:
                continue
            seen_sounds.add(id(snd))
            source = getattr(snd, "source", None)
            if source is not None:
                if hasattr(self.am, "efx"):
                    send_indices = set(
                        range(len(getattr(self.am, "sends", [])))
                    )
                    send_indices.update(
                        getattr(snd, "_piano_effect_sends", {})
                    )
                    for send_idx in send_indices:
                        with contextlib.suppress(Exception):
                            self.am.efx.send(source, send_idx, None)
                with contextlib.suppress(Exception):
                    source.stop()
        self.active_piano_notes.clear()
        for peer_id in {key[0] for key in self._pedal_filters}:
            self._delete_peer_filters(peer_id)
        self.soft_pedal_states.clear()
        self._pedal_transitions.clear()
        self._filter_cleanup_deadlines.clear()
        self.pitch_bend_states.clear()
        self._pitch_bend_values.clear()
        self._pitch_bend_transitions.clear()
        for peer_id in list(self._chorus_slots):
            self._release_chorus_slot(peer_id)
        self.chorus_states.clear()
        self._chorus_transitions.clear()
        if self._occlusion_filter is not None:
            self._occlusion_filter = None

    def play_note(self, peer_id, note_name, x, y, z, listener_x, listener_y, listener_z, volume=300, occluded=False, soft=None):
        """Play a piano note with 3D stereo spreading (remote) or direct stereo (local).
        
        Automatically handles note re-triggering, occlusion filtering,
        and active note tracking for sustain/staccato pedal support.
        Also routes notes through PA Megaphone Speakers if broadcasting to Megaphone.
        """
        if soft is not None:
            self.set_soft_pedal(peer_id, soft)
        is_local = (peer_id == "local")
        filter_mode = "occluded" if occluded else "normal"
        filter_obj = self.get_note_filter(peer_id, occluded=occluded)
        snd = self.am.play_unbound_stereo_spatial(
            path=f"piano/Piano.mf.{note_name}.ogg",
            x=x, y=y, z=z,
            listener_x=listener_x,
            listener_y=listener_y,
            listener_z=listener_z,
            volume=volume,
            cat="miscelaneous",
            max_distance=50.0,
            as_3d_stereo=not is_local,
            occluded=occluded,
            direct_filter=filter_obj,
            stereo_reference_distance=8.0,
        )
        if snd:
            self._tag_sounds(snd, peer_id, filter_mode)
            self.apply_chorus_send(snd, peer_id)
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
            pedal_filter = self._get_pedal_filter(peer_id, "pa")
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
                    cat="miscelaneous",
                    direct_filter=pedal_filter,
                )
                if mega_snd and hasattr(mega_snd, 'source') and mega_snd.source:
                    # Apply Megaphone PA Filter & EQ effects
                    if pedal_filter is None and hasattr(gp.megaphone, 'lowpass_filter') and gp.megaphone.lowpass_filter:
                        mega_snd.source.direct_filter = gp.megaphone.lowpass_filter
                    self._tag_sounds(mega_snd, peer_id, "pa")
                    self.apply_chorus_send(mega_snd, peer_id)
                    if hasattr(self.am, 'efx'):
                        if hasattr(gp.megaphone, 'eq_slot') and gp.megaphone.eq_slot:
                            self.apply_effect_send(mega_snd, 1, gp.megaphone.eq_slot)
                        if hasattr(gp.megaphone, 'reverb_slot') and gp.megaphone.reverb_slot:
                            self.apply_effect_send(mega_snd, 2, gp.megaphone.reverb_slot)
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
        self._schedule_filter_cleanup(peer_id)
