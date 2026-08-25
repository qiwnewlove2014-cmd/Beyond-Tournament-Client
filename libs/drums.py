"""Playable drumset audio with one-shot polyphony and hi-hat choking."""

import array
import contextlib
import os
import queue
import time
from collections import deque

import cyal
import cyal.exceptions
import pyogg

from . import consts
from . import path_utils


class DrumAudio:
    """Owns drum buffers, active voices, fades, and remote hit handoff."""

    # Each kit is a 17-pad tuple of (display_name, path, volume_scale, polyphony_limit).
    # A path of None marks a silent pad (e.g. Salamander only has 2 toms, so Tom 3/4
    # are silent rather than reusing Tom 2). Every kit MUST define exactly 17 pads in
    # the canonical order so the pad ID contract (0-16) stays identical across kits.
    KITS = {
        "default": (
            ("Kick", "drums/default/Drums.hit.Kick.ogg", 0.34, 6),
            ("Snare", "drums/default/Drums.hit.Snare.ogg", 0.32, 6),
            ("Rim", "drums/default/Drums.hit.Rim.ogg", 0.62, 6),
            ("Closed Hi-Hat", "drums/default/Drums.hit.HiHatClosed.ogg", 1.80, 8),
            ("Open Hi-Hat", "drums/default/Drums.hit.HiHatOpen.ogg", 1.10, 4),
            ("Foot Hi-Hat", "drums/default/Drums.hit.HiHatFoot.ogg", 4.00, 8),
            ("Tom 1", "drums/default/Drums.hit.Tom1.ogg", 0.32, 6),
            ("Tom 2", "drums/default/Drums.hit.Tom2.ogg", 0.56, 6),
            ("Tom 3", "drums/default/Drums.hit.Tom3.ogg", 0.43, 6),
            ("Tom 4", "drums/default/Drums.hit.Tom4.ogg", 0.69, 6),
            ("Crash Left", "drums/default/Drums.hit.CrashLeft.ogg", 1.20, 3),
            ("Crash Right", "drums/default/Drums.hit.CrashRight.ogg", 1.05, 3),
            ("China", "drums/default/Drums.hit.China.ogg", 0.74, 3),
            ("Splash", "drums/default/Drums.hit.Splash.ogg", 0.85, 3),
            ("Ride", "drums/default/Drums.hit.Ride.ogg", 1.75, 4),
            ("Ride Bell", "drums/default/Drums.hit.RideBell.ogg", 1.44, 4),
            ("Cowbell", "drums/default/Drums.hit.Cowbell.ogg", 0.48, 6),
            ("Rim", "drums/default/Drums.hit.Rim.ogg", 0.62, 6),
        ),
        "salamander": (
            ("Kick", "drums/salamander/Drums.hit.Kick.ogg", 0.34, 6),
            ("Snare", "drums/salamander/Drums.hit.Snare.ogg", 0.32, 6),
            ("Rim", "drums/salamander/Drums.hit.Rim.ogg", 0.62, 6),
            ("Closed Hi-Hat", "drums/salamander/Drums.hit.HiHatClosed.ogg", 1.80, 8),
            ("Open Hi-Hat", "drums/salamander/Drums.hit.HiHatOpen.ogg", 1.10, 4),
            ("Foot Hi-Hat", "drums/salamander/Drums.hit.HiHatFoot.ogg", 4.00, 8),
            ("Tom 1", "drums/salamander/Drums.hit.Tom1.ogg", 0.32, 6),
            ("Tom 2", "drums/salamander/Drums.hit.Tom2.ogg", 0.56, 6),
            # Salamander only ships 2 toms; Tom 3/4 are intentionally silent.
            ("Tom 3", None, 0.43, 6),
            ("Tom 4", None, 0.69, 6),
            ("Crash Left", "drums/salamander/Drums.hit.CrashLeft.ogg", 1.20, 3),
            ("Crash Right", "drums/salamander/Drums.hit.CrashRight.ogg", 1.05, 3),
            ("China", "drums/salamander/Drums.hit.China.ogg", 0.74, 3),
            ("Splash", "drums/salamander/Drums.hit.Splash.ogg", 0.85, 3),
            ("Ride", "drums/salamander/Drums.hit.Ride.ogg", 1.75, 4),
            ("Ride Bell", "drums/salamander/Drums.hit.RideBell.ogg", 1.44, 4),
            ("Cowbell", "drums/salamander/Drums.hit.Cowbell.ogg", 0.48, 6),
            ("Rim", "drums/salamander/Drums.hit.Rim.ogg", 0.62, 6),
        ),
        "diw": (
            ("Kick", "drums/DrumBy (Mr. Ling, Jik Jik)/Kick.ogg", 0.17, 6),
            ("Snare", "drums/DrumBy (Mr. Ling, Jik Jik)/snare1.ogg", 0.16, 6),
            ("Rim (Snare 2)", "drums/DrumBy (Mr. Ling, Jik Jik)/snare2.ogg", 0.16, 6),
            ("Closed Hi-Hat", "drums/DrumBy (Mr. Ling, Jik Jik)/hat c.ogg", 0.90, 8),
            ("Open Hi-Hat", "drums/DrumBy (Mr. Ling, Jik Jik)/hat o.ogg", 0.55, 4),
            ("Foot Hi-Hat", "drums/DrumBy (Mr. Ling, Jik Jik)/hat f.ogg", 2.00, 8),
            ("Tom 1", "drums/DrumBy (Mr. Ling, Jik Jik)/tom 1.ogg", 0.16, 6),
            ("Tom 2", "drums/DrumBy (Mr. Ling, Jik Jik)/tom 2.ogg", 0.28, 6),
            ("Tom 3", "drums/DrumBy (Mr. Ling, Jik Jik)/tom 3.ogg", 0.21, 6),
            ("Tom 4", "drums/DrumBy (Mr. Ling, Jik Jik)/tom 4.ogg", 0.34, 6),
            ("Crash Left", "drums/DrumBy (Mr. Ling, Jik Jik)/crash L.ogg", 0.60, 3),
            ("Crash Right", "drums/DrumBy (Mr. Ling, Jik Jik)/crash R.ogg", 0.52, 3),
            ("China", "drums/DrumBy (Mr. Ling, Jik Jik)/CN.ogg", 0.37, 3),
            ("Splash", "drums/DrumBy (Mr. Ling, Jik Jik)/Sp.ogg", 0.42, 3),
            ("Ride", "drums/DrumBy (Mr. Ling, Jik Jik)/ride.ogg", 0.87, 4),
            ("Ride Bell", "drums/DrumBy (Mr. Ling, Jik Jik)/bell.ogg", 0.72, 4),
            ("Cowbell", "drums/DrumBy (Mr. Ling, Jik Jik)/Cabell lum.ogg", 0.24, 6),
            ("Rim", "drums/DrumBy (Mr. Ling, Jik Jik)/rim.ogg", 0.31, 6),
        ),
    }
    DEFAULT_KIT = "default"
    # Backwards-compatible alias: the canonical 17-pad definition. Always reflects the
    # default kit so legacy callers (pad validation, key config) keep working unchanged.
    PAD_DEFS = KITS[DEFAULT_KIT]
    OPEN_HAT_PAD = 4
    HAT_CHOKE_PADS = frozenset((3, 5))
    MAX_VOICES_PER_PEER = 32
    MAX_TOTAL_VOICES = 64
    MAX_PENDING_HITS_PER_UPDATE = 64
    CHOKE_SECONDS = 0.055
    STEAL_SECONDS = 0.025

    def __init__(self, audio_manager):
        self.am = audio_manager
        self.gameplay = None
        self.active_voices = {}
        self._fades = []
        self._pending_hits = queue.Queue(maxsize=256)
        self._occlusion_filter = None
        self._light_occlusion_filter = None
        # Kit the local performer is currently playing. Remote hits carry their own
        # kit id in the packet, so this only governs local (client-prediction) hits.
        self._active_kit = self.DEFAULT_KIT

    @classmethod
    def is_valid_kit(cls, kit):
        return isinstance(kit, str) and kit in cls.KITS

    @classmethod
    def pad_defs(cls, kit=None):
        """Return the 17-pad tuple for a kit, falling back to the default kit."""
        if kit is None or not cls.is_valid_kit(kit):
            return cls.KITS[cls.DEFAULT_KIT]
        return cls.KITS[kit]

    def set_active_kit(self, kit):
        """Set the kit used by the local performer and preload it if needed."""
        if not self.is_valid_kit(kit):
            return
        self._active_kit = kit
        with contextlib.suppress(Exception):
            self.preload(kit)

    @classmethod
    def is_valid_pad(cls, pad):
        return isinstance(pad, int) and not isinstance(pad, bool) and 0 <= pad < len(cls.PAD_DEFS)

    @classmethod
    def pad_name(cls, pad):
        return cls.PAD_DEFS[pad][0] if cls.is_valid_pad(pad) else None

    @staticmethod
    def _iter_sounds(sounds):
        if sounds is None:
            return []
        if isinstance(sounds, (list, tuple)):
            flattened = []
            for sound in sounds:
                flattened.extend(DrumAudio._iter_sounds(sound))
            return flattened
        return [sounds]

    @staticmethod
    def _source_is_playing(sound):
        source = getattr(sound, "source", None)
        if source is None:
            return False
        try:
            return source.state != cyal.SourceState.STOPPED
        except Exception:
            return False

    def load_stereo_split_buffers(self, path):
        """Return cached MONO16 left/right buffers for a stereo drum sample."""
        if not os.path.isabs(path) and not path.startswith(consts.SOUNDPREPEND):
            path = os.path.join(consts.SOUNDPREPEND, path)
        if not path.endswith(".ogg"):
            path = path_utils.get_next_cycle_item(path)
        try:
            path = os.path.normpath(path) if os.path.isabs(path) else os.path.relpath(path)
        except ValueError:
            path = os.path.normpath(path)

        cache_key_l = f"{path}_drum_split_L"
        cache_key_r = f"{path}_drum_split_R"
        if cache_key_l in self.am.buffers and cache_key_r in self.am.buffers:
            return self.am.buffers[cache_key_l], self.am.buffers[cache_key_r]

        try:
            from .safe_vorbis import load_vorbis_pcm
            file = load_vorbis_pcm(path)
            audio_data = bytes(file.buffer)
            if file.channels != 2:
                buffer = self.am.load_buffer(path)
                return buffer, buffer
            stereo_samples = array.array("h", audio_data)
            left_bytes = array.array("h", stereo_samples[0::2]).tobytes()
            right_bytes = array.array("h", stereo_samples[1::2]).tobytes()
            try:
                buffer_l = self.am.context.gen_buffer()
            except cyal.exceptions.InvalidOperationError:
                buffer_l = self.am.context.gen_buffer()
            try:
                buffer_r = self.am.context.gen_buffer()
            except cyal.exceptions.InvalidOperationError:
                buffer_r = self.am.context.gen_buffer()
            buffer_l.set_data(left_bytes, sample_rate=file.frequency, format=cyal.BufferFormat.MONO16)
            buffer_r.set_data(right_bytes, sample_rate=file.frequency, format=cyal.BufferFormat.MONO16)
            self.am.buffers[cache_key_l] = buffer_l
            self.am.buffers[cache_key_r] = buffer_r
            return buffer_l, buffer_r
        except Exception as error:
            print(f"Error loading split drum buffers for {path}: {error}")
            return None, None

    def get_occlusion_filter(self):
        if self._occlusion_filter is None:
            self._occlusion_filter = self.am.gen_filter(
                "LOWPASS", ("GAINHF", 0.05), ("GAIN", 0.22)
            )
        return self._occlusion_filter

    def get_light_occlusion_filter(self):
        """Gentle lowpass for PARTIALLY occluded hits.

        A thin obstacle (a lone pillar tile between drummer and listener)
        only slightly dulls the hit, unlike the heavy full-wall filter above.
        """
        if self._light_occlusion_filter is None:
            self._light_occlusion_filter = self.am.gen_filter(
                "LOWPASS", ("GAINHF", 0.45), ("GAIN", 0.75)
            )
        return self._light_occlusion_filter

    def preload(self, kit=None):
        """Keep a drum kit resident for zero-latency first hits.

        Each kit is cached independently under kit-scoped preload keys so multiple
        kits can coexist (the local performer's kit plus kits heard from remote
        players). Silent pads (path is None) are skipped.
        """
        if kit is None:
            kit = self._active_kit
        pad_defs = self.pad_defs(kit)
        for pad, (_, path, _, _) in enumerate(pad_defs):
            if path is None:
                continue
            with contextlib.suppress(Exception):
                stereo = self.am.load_buffer(path)
                if stereo is not None:
                    self.am._preloaded_buffers[f"drums:{kit}:{pad}:stereo"] = stereo
                left, right = self.load_stereo_split_buffers(path)
                if left is not None:
                    self.am._preloaded_buffers[f"drums:{kit}:{pad}:left"] = left
                if right is not None:
                    self.am._preloaded_buffers[f"drums:{kit}:{pad}:right"] = right

    def enqueue_remote_hit(self, data):
        """Transfer a network-thread packet to the audio-owning main thread."""
        if not isinstance(data, dict) or not self.is_valid_pad(data.get("pad")):
            return
        with contextlib.suppress(queue.Full):
            self._pending_hits.put_nowait(dict(data))

    def _tag_sounds(self, sounds, peer_id, pad):
        for sound in self._iter_sounds(sounds):
            sound._drum_peer_id = str(peer_id)
            sound._drum_pad = pad
            if not hasattr(sound, "_drum_effect_sends"):
                sound._drum_effect_sends = set()

    def apply_effect_send(self, sounds, send_index, slot):
        if slot is None or not hasattr(self.am, "efx"):
            return
        for sound in self._iter_sounds(sounds):
            source = getattr(sound, "source", None)
            if source is None:
                continue
            with contextlib.suppress(Exception):
                self.am.efx.send(source, send_index, slot)
                sound._drum_effect_sends.add(send_index)

    def _schedule_fade(self, sounds, duration):
        now = time.monotonic()
        for sound in self._iter_sounds(sounds):
            source = getattr(sound, "source", None)
            if source is None:
                continue
            try:
                start_gain = float(source.gain)
            except Exception:
                continue
            self._fades.append({
                "sound": sound,
                "started": now,
                "duration": max(0.001, duration),
                "gain": start_gain,
            })

    def _remove_record(self, key, record, fade_seconds):
        voices = self.active_voices.get(key)
        if voices is not None:
            with contextlib.suppress(ValueError):
                voices.remove(record)
            if not voices:
                self.active_voices.pop(key, None)
        self._schedule_fade(record["sounds"], fade_seconds)

    def _choke_open_hat(self, peer_id):
        key = (str(peer_id), self.OPEN_HAT_PAD)
        for record in list(self.active_voices.get(key, ())):
            self._remove_record(key, record, self.CHOKE_SECONDS)

    def _oldest_record(self, predicate=None):
        candidates = []
        for key, records in self.active_voices.items():
            if predicate is not None and not predicate(key):
                continue
            for record in records:
                candidates.append((record["created"], key, record))
        return min(candidates, default=None, key=lambda item: item[0])

    def _enforce_voice_limits(self, peer_id, pad):
        key = (str(peer_id), pad)
        pad_limit = self.PAD_DEFS[pad][3]
        while len(self.active_voices.get(key, ())) > pad_limit:
            record = self.active_voices[key][0]
            self._remove_record(key, record, self.STEAL_SECONDS)

        def peer_record_count():
            return sum(len(records) for record_key, records in self.active_voices.items() if record_key[0] == str(peer_id))

        while peer_record_count() > self.MAX_VOICES_PER_PEER:
            oldest = self._oldest_record(lambda record_key: record_key[0] == str(peer_id))
            if oldest is None:
                break
            _, oldest_key, record = oldest
            self._remove_record(oldest_key, record, self.STEAL_SECONDS)

        while sum(len(records) for records in self.active_voices.values()) > self.MAX_TOTAL_VOICES:
            oldest = self._oldest_record()
            if oldest is None:
                break
            _, oldest_key, record = oldest
            self._remove_record(oldest_key, record, self.STEAL_SECONDS)

    def route_to_megaphone_speakers(self, peer_id, pad, adjusted_volume, kit=None):
        gameplay = self.gameplay
        if not (
            gameplay
            and hasattr(gameplay, "megaphone")
            and gameplay.megaphone
            and getattr(gameplay.megaphone, "speaker_data", None)
        ):
            return []
        path = self.pad_defs(kit)[pad][1]
        if path is None:
            return []
        music_bot = getattr(gameplay, "music_bot", None)
        bot_volume = max(0.1, getattr(music_bot, "volume", 50) / 100.0) * 0.5
        # Listener position for distance/occlusion math (same as the megaphone
        # speaker system uses) so drums are shaped by walls and range exactly
        # like voice/music instead of "converging to the middle".
        try:
            pobj = gameplay.camera.focus_object
            player_pos = (float(pobj.x), float(pobj.y), float(pobj.z))
        except Exception:
            player_pos = None
        import math as _m
        sounds = []
        for speaker in gameplay.megaphone.speaker_data:
            position = speaker.get("position")
            if position is None:
                continue
            spk_gain = 1.0
            ref_dist = 15.0
            max_dist = 100.0
            if player_pos is not None:
                d = _m.sqrt((player_pos[0]-position[0])**2 + (player_pos[1]-position[1])**2 + (player_pos[2]-position[2])**2)
                hr = float(speaker.get("hearing_range", 0.0) or 0.0)
                if hr > 0.0:
                    ref_dist = hr * 0.2
                    max_dist = hr
                    if d >= hr:
                        spk_gain = 0.0
                    elif d >= hr * 0.8:
                        fade_start = hr * 0.8
                        spk_gain = 1.0 - (d - fade_start) / (hr - fade_start)
                try:
                    occ = gameplay.megaphone._check_speaker_occlusion(position, player_pos)
                    if occ >= 1.0:
                        spk_gain = 0.0
                    elif occ > 0.0:
                        spk_gain *= (1.0 - occ * 0.85)
                except Exception:
                    pass
            volume = adjusted_volume * speaker.get("base_volume", 0.6) * bot_volume * max(0.0, spk_gain)
            if volume <= 0.0:
                continue
            try:
                sound = self.am.play_unbound(
                    path, position[0], position[1], position[2],
                    volume=volume, cat="miscelaneous",
                    reference_distance=ref_dist,
                    max_distance=max_dist,
                    direct_filter=getattr(gameplay.megaphone, "lowpass_filter", None),
                )
            except Exception:
                continue
            if sound is None:
                continue
            self._tag_sounds(sound, peer_id, pad)
            self.apply_effect_send(sound, 1, getattr(gameplay.megaphone, "eq_slot", None))
            self.apply_effect_send(sound, 2, getattr(gameplay.megaphone, "reverb_slot", None))
            sounds.append(sound)
        return sounds

    def play_hit(self, peer_id, pad, x, y, z, listener_x, listener_y, listener_z,
                 volume=300, occluded=False, via_megaphone=False, kit=None, occlusion=None):
        if not self.is_valid_pad(pad):
            return None
        if kit is None:
            kit = self._active_kit if str(peer_id) == "local" else self.DEFAULT_KIT
        pad_defs = self.pad_defs(kit)
        path = pad_defs[pad][1]
        # Silent pad (e.g. Salamander's Tom 3/4): nothing to play.
        if path is None:
            return None
        peer_id = str(peer_id)
        if pad in self.HAT_CHOKE_PADS:
            self._choke_open_hat(peer_id)

        _, _, volume_scale, _ = pad_defs[pad]
        adjusted_volume = volume * volume_scale
        if occlusion is None:
            full_block = bool(occluded)
            partial_direct = None
        else:
            full_block = occlusion >= 1.0
            partial_direct = (
                self.get_light_occlusion_filter() if 0.0 < occlusion < 1.0 else None
            )
        primary = self.am.play_unbound_stereo_spatial(
            path, x, y, z, listener_x, listener_y, listener_z,
            volume=adjusted_volume,
            cat="miscelaneous",
            max_distance=50.0,
            as_3d_stereo=(peer_id != "local"),
            occluded=(full_block and partial_direct is None),
            direct_filter=partial_direct,
            stereo_provider=self,
            stereo_offset=1.5,
            stereo_reference_distance=8.0,
            stereo_gain_l=1.0,
            stereo_gain_r=1.0,
        )
        if primary is None:
            return None
        self._tag_sounds(primary, peer_id, pad)
        sounds = self._iter_sounds(primary)

        if via_megaphone:
            sounds.extend(self.route_to_megaphone_speakers(peer_id, pad, adjusted_volume, kit=kit))

        key = (peer_id, pad)
        self.active_voices.setdefault(key, deque()).append({
            "created": time.monotonic(),
            "sounds": sounds,
        })
        self._enforce_voice_limits(peer_id, pad)
        return primary

    def _play_remote_hit(self, data):
        gameplay = self.gameplay
        player = getattr(gameplay, "player", None) if gameplay else None
        if player is None:
            return
        try:
            x, y, z = float(data["x"]), float(data["y"]), float(data["z"])
            peer_id = str(data["peer_id"])
            pad = data["pad"]
        except (KeyError, TypeError, ValueError):
            return
        # Occlusion ratio scales with wall thickness: a lone pillar tile
        # partially muffles (~0.33), a long wall fully blocks — see
        # map.wall_occlusion_ratio().
        occlusion = 0.0
        if getattr(gameplay, "map", None):
            with contextlib.suppress(Exception):
                wofn = getattr(gameplay.map, "wall_occlusion_ratio", None)
                if wofn is not None:
                    occlusion = float(wofn((x, y, z), (player.x, player.y, player.z)))
                elif gameplay.map.valid_straight_path(
                    (x, y, z), (player.x, player.y, player.z)
                ) is False:
                    occlusion = 1.0
        raw_volume = data.get("volume", 300)
        if isinstance(raw_volume, bool):
            raw_volume = 300
        try:
            volume = max(0.0, min(300.0, float(raw_volume)))
        except (TypeError, ValueError):
            volume = 300.0
        sound = self.play_hit(
            peer_id, pad, x, y, z,
            player.x, player.y, player.z,
            volume=volume,
            occluded=(occlusion >= 1.0),
            occlusion=occlusion,
            via_megaphone=data.get("via_megaphone") is True,
            kit=data.get("kit"),
        )
        if sound and getattr(gameplay, "map", None):
            reverb = gameplay.map.get_reverb_at(x, y, z)
            if reverb and reverb.reverb:
                self.apply_effect_send(sound, 0, reverb.reverb)

    def _finish_fades(self, now):
        remaining = []
        for fade in self._fades:
            sound = fade["sound"]
            source = getattr(sound, "source", None)
            if source is None:
                continue
            progress = min(1.0, (now - fade["started"]) / fade["duration"])
            try:
                source.gain = fade["gain"] * (1.0 - progress)
                if progress >= 1.0:
                    send_indices = set(range(len(getattr(self.am, "sends", ()))))
                    send_indices.update(getattr(sound, "_drum_effect_sends", ()))
                    for send_index in send_indices:
                        with contextlib.suppress(Exception):
                            self.am.efx.send(source, send_index, None)
                    source.stop()
                else:
                    remaining.append(fade)
            except Exception:
                continue
        self._fades = remaining

    def _prune_finished_voices(self):
        for key, records in list(self.active_voices.items()):
            kept = deque(
                record for record in records
                if any(self._source_is_playing(sound) for sound in record["sounds"])
            )
            if kept:
                self.active_voices[key] = kept
            else:
                self.active_voices.pop(key, None)

    def update(self):
        for _ in range(self.MAX_PENDING_HITS_PER_UPDATE):
            try:
                data = self._pending_hits.get_nowait()
            except queue.Empty:
                break
            self._play_remote_hit(data)
        now = time.monotonic()
        self._finish_fades(now)
        self._prune_finished_voices()

    def remove_peer(self, peer_id):
        peer_id = str(peer_id)
        for key in [key for key in self.active_voices if key[0] == peer_id]:
            for record in list(self.active_voices.get(key, ())):
                self._remove_record(key, record, self.STEAL_SECONDS)

    def reset(self):
        """Detach DrumAudio-owned sends and stop all active drum sources."""
        seen = set()
        owned_groups = [
            record["sounds"]
            for records in list(self.active_voices.values())
            for record in records
        ]
        owned_groups.extend([[fade["sound"]] for fade in self._fades])
        for sounds in owned_groups:
            for sound in sounds:
                if id(sound) in seen:
                    continue
                seen.add(id(sound))
                source = getattr(sound, "source", None)
                if source is None:
                    continue
                send_indices = set(range(len(getattr(self.am, "sends", ()))))
                send_indices.update(getattr(sound, "_drum_effect_sends", ()))
                for send_index in send_indices:
                    with contextlib.suppress(Exception):
                        self.am.efx.send(source, send_index, None)
                with contextlib.suppress(Exception):
                    source.stop()
        self.active_voices.clear()
        self._fades.clear()
        while True:
            try:
                self._pending_hits.get_nowait()
            except queue.Empty:
                break
        # Return the occlusion filter to the pool instead of dropping it for
        # garbage collection (cyal Filter dealloc = crash-prone call).
        if self._occlusion_filter is not None:
            self.am.release_filter(self._occlusion_filter)
        self._occlusion_filter = None
        if self._light_occlusion_filter is not None:
            self.am.release_filter(self._light_occlusion_filter)
        self._light_occlusion_filter = None
        # Release preloaded drum kit buffers so memory does not accumulate across
        # map changes. _preloaded_buffers holds strong refs to OpenAL buffers;
        # without this, every kit preload leaks 17*3 buffers per session.
        for key in [k for k in list(self.am._preloaded_buffers) if k.startswith("drums:")]:
            self.am._preloaded_buffers.pop(key, None)

    def reset_for_map_change(self):
        """Lightweight reset for map transitions.

        Stops live voices, drops queued hits, and releases preloaded kit buffers
        so the next map starts clean. Preserves the gameplay back-reference
        because the same Gameplay instance keeps running on the new map.
        """
        self.reset()
