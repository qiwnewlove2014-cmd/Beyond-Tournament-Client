import cyal, cyal.efx, cyal.hrtf, cyal.exceptions
import contextlib
import gc
import os
import queue
import weakref
import math
import threading
import time
import pyogg
import requests
from .audio.soundgroup import SoundGroup
from .audio.sound import Sound
from .piano import PianoAudio
from .drums import DrumAudio
from . import options
from . import path_utils
from . import consts

class AudioManager():
    @staticmethod
    def _open_context(cyal_device):
        return cyal.Context(
            cyal_device,
            make_current=True,
            mono_sources=1024,
            stereo_sources=1024,
            max_auxiliary_sends=64,
        )

    def __init__(self):
        device = options.get("audio_device", cyal.util.get_default_all_device_specifier())
        if device == "system default": device = cyal.util.get_default_all_device_specifier()

        try:
            cyal_device = cyal.Device(name=device)
            self.context = self._open_context(cyal_device)
        except cyal.exceptions.AlcError as ex:
            # A saved device that has since disappeared (USB unplugged, BT
            # headset off, port renamed) can fail either at Device open OR at
            # Context creation (InvalidDeviceError), not only with
            # DeviceNotFoundError. Without this recovery the game dies at
            # startup and the player can never reach the settings menu.
            print(f"Warning: Audio device '{device}' unusable ({ex.__class__.__name__}). Falling back to system default.")
            try:
                from .speech import speak
                speak("Saved audio device could not be opened. Using system default.", True)
            except Exception:
                pass
            options.set("audio_device", "system default")
            cyal_device = cyal.Device(name=cyal.util.get_default_all_device_specifier())
            self.context = self._open_context(cyal_device)
        self.silent_buf = bytearray(96*options.get("jitter_buffer", 60))
        self.hrtf = cyal.hrtf.HrtfExtension(self.context.device)
        self.hrtf.use(options.get("hrtf_model", "oalsoft_hrtf_48000"))
        self.muted=False
        self.max_distance = 59
        self.efx = cyal.efx.EfxExtension(self.context)
        
        self.listener = self.context.listener
        self.listener.position=(0,0,0)
        self.listener.orientation=[0, 1, 0, 0, 0, 1] # right-handed co-ordenate system. X is left to right, Y is backward to forward, Z is down to up. 
        self.soundgroups = weakref.WeakSet()
        self.filter = []
        self.sends = [
            None,
            None,
            None,
            None
        ]
        self.volume_categories = {
            "master": [options.get("volume_master", 100), weakref.WeakSet()],
            "self": [options.get("volume_self", 100), weakref.WeakSet()],
            "players": [options.get("volume_players", 100), weakref.WeakSet()],
            "zombies": [options.get("volume_zombies", 100), weakref.WeakSet()],
            "weapons": [options.get("volume_weapons", 100), weakref.WeakSet()],
            "ui": [options.get("volume_ui", 100), weakref.WeakSet()],
            "music": [options.get("volume_music", 100), weakref.WeakSet()],
            # Music jukebox songs get their OWN mixer slider (separate from the
            # personal music bot / map music, which stay under "music").
            "jukebox": [options.get("volume_jukebox", 100), weakref.WeakSet()],
            "ambience": [options.get("volume_ambience", 100), weakref.WeakSet()],
            "sound_source": [options.get("volume_sound_source", 100), weakref.WeakSet()],
            "miscelaneous": [options.get("volume_miscelaneous", 100), weakref.WeakSet()]
        }
        self.unbound_sources = []
        self.buffers = weakref.WeakValueDictionary()
        self._preloaded_buffers = {}  # Strong references for preloaded sounds to prevent GC
        # Audio Inbox: worker threads (voice chat, megaphone playout, music
        # bot) hand their OpenAL work here instead of touching OpenAL
        # themselves. AudioManager.loop() drains it on the MAIN thread inside
        # the frame batch, so every AL call in the process happens on one
        # thread — the OpenAL context is only current there, and concurrent
        # cross-thread AL usage (especially mismatched context.batch()
        # nesting, a per-context GLOBAL flag) corrupted native memory and
        # crashed the game (0xC0000005) under load.
        self._audio_inbox = queue.SimpleQueue()
        self._unbound_occlusion_filter = None
        self._light_unbound_occlusion_filter = None
        self.piano = PianoAudio(self)
        self.drums = DrumAudio(self)
        
        # Initialize volumes
        for cat, val in self.volume_categories.items():
            self.set_volume(cat, val[0])
        
        # === EFX Auxiliary Effect Slot Pool ===
        # Pre-allocate a fixed pool of aux effect slots at startup.
        # This is the industry-standard approach (FMOD/Wwise/Unreal pattern):
        # slots are NEVER created or destroyed during gameplay, only borrowed/returned.
        # OpenAL typically limits aux effect slots to 4-16, so we pre-allocate
        # as many as the driver allows and reuse them forever.
        self._slot_pool = []      # Available slots
        self._slot_in_use = []    # Currently borrowed slots
        self._slot_pool_size = 0
        # Recycled EFX filters. cyal's Filter has no explicit delete(): the
        # AL resource is only released by __dealloc__, which calls through a
        # function pointer stored on the EfxExtension instance — a call that
        # hard-crashes the game (0xC0000005 into python311.dll's .data) when
        # that pointer slot is corrupted. Pooling filter wrappers means they
        # are NEVER garbage collected, so that code path never runs at all.
        self._filter_pool = []
        self._init_slot_pool()
    
    # Sets the orientation, taking (horizontal angle, pitch, lean)
    # if anyone goes anywhere near this function with a 10foot pole, you'll find yourself without a left testical
    def make_orientation(self, angle: float = 0.0, pitch: float = 0.0, lean: float = 0.0):
        # converts to radians for for use in math.sin and math.cos
        angle_rad = math.radians(angle)
        pitch_rad = math.radians(pitch)
        lean_rad = math.radians(lean)
        
        # forward x, y, and z indicate which way the listener is pointing
        forward_x = math.sin(angle_rad) # The x component of the forward vector only deppends on the horizontal angle
        forward_y = math.cos(angle_rad) # the Y component of the forward vector only deppends on the horizontal angle
        forward_z = math.sin(pitch_rad) * math.cos(lean_rad) # multiplies the sine of the pitch by the cosine of the lean in order to create the correct direction when leaning/pitching. when lean is 0, the pitch is multiplied by 1 so no querky behavia
        
        # the up x, y and z indicate which way is up from the listener's perspective
        up_x = -math.sin(pitch_rad) * math.sin(angle_rad) + math.sin(lean_rad) # multiplies the negative sine of pitch by the sine of angle so that when facing forward, x is 0. Adds the sine of lean so that when leaning away from 0 degrees, the necesary lean offset is added. The negative sine of pitch is used so the correct perpedicular angle between the forward and up vectors are kept. 
        up_y = -math.sin(pitch_rad) * math.cos(angle_rad) + math.sin(lean_rad) # multiplies the negative sine of pitch by the cos of angle so that when facing forward, y is 0. Adds the sine of lean so that when leaning away from 0 degrees, the necesary lean offset is added. The negative sine of pitch is used so the correct perpedicular angle between the forward and up vectors are kept. 
        up_z = math.cos(pitch_rad) * math.cos(lean_rad) # same as forward z, except uses to cosine pitch in order to maintain a perpendicular angle

        return (forward_x, forward_y, forward_z, up_x, up_y, up_z)
    @property
    def orientation(self):
        return self.listener.orientation
    
    @orientation.setter
    def orientation(self, value: tuple):
        self.listener.orientation = self.make_orientation(*value)

    @property
    def position(self):
        return self.listener.position
    
    @position.setter
    def position(self, value: tuple):
        self.listener.position = value

    def get_unbound_occlusion_filter(self):
        """Get or lazily create a persistent lowpass filter for occluded unbound 3D sounds (e.g. doors behind walls)."""
        if getattr(self, "_unbound_occlusion_filter", None) is None:
            flt = self.gen_filter("LOWPASS")
            if flt is not None:
                try:
                    flt.set("GAINHF", 0.05)
                    flt.set("GAIN", 0.22)
                    self._unbound_occlusion_filter = flt
                except Exception:
                    self._unbound_occlusion_filter = None
        return self._unbound_occlusion_filter

    def get_light_unbound_occlusion_filter(self):
        """Lowpass for PARTIALLY occluded unbound 3D sounds.

        A thin obstacle (a single pillar tile between source and listener)
        should only slightly dull the sound — much gentler than the heavy
        full-wall filter from get_unbound_occlusion_filter().
        """
        if getattr(self, "_light_unbound_occlusion_filter", None) is None:
            flt = self.gen_filter("LOWPASS")
            if flt is not None:
                try:
                    flt.set("GAINHF", 0.45)
                    flt.set("GAIN", 0.75)
                    self._light_unbound_occlusion_filter = flt
                except Exception:
                    self._light_unbound_occlusion_filter = None
        return self._light_unbound_occlusion_filter

    def preload_ui_sounds(self):
        """Pre-load critical UI sounds at startup to prevent first-play silence."""
        ui_sounds = [
            "ui/warn.ogg", "ui/kick.ogg", "ui/broadcast.ogg",
            "ui/online.ogg", "ui/offline.ogg", "ui/chat.ogg",
            "ui/pm.ogg", "ui/kill.ogg", "ui/notify1.ogg", "ui/notify2.ogg",
        ]
        for snd in ui_sounds:
            snd_path = os.path.join(consts.SOUNDPREPEND, snd)
            try:
                rel_snd = os.path.relpath(snd_path)
            except ValueError:
                rel_snd = os.path.normpath(snd_path)
                
            self._preloaded_buffers[rel_snd] = None  # Mark for strong ref
            buf = self.load_buffer(snd)
            if buf:
                self._preloaded_buffers[rel_snd] = buf

    def load_buffer(self, path: str, as_mono: bool = False) -> cyal.Buffer | None:
        if path.split(":")[0] == "server_sounds":
            path = path.split(":")[1]
            if not os.path.exists(path):
                if path.startswith("server_sounds/") and not os.path.exists(os.path.join(consts.SOUNDPREPEND, "server_sounds")):
                    os.mkdir(os.path.join(consts.SOUNDPREPEND, "server_sounds"))
                data = requests.get(f"{consts.SERVER_SOUNDS_URL}{path}")
                if data.ok:
                    try:
                        with open(os.path.join(consts.SOUNDPREPEND, path), 'wb+') as f:
                            f.write(data.content)
                    except Exception as e:
                        print(e)
        if not os.path.isabs(path) and not path.startswith(consts.SOUNDPREPEND): path = os.path.join(consts.SOUNDPREPEND, path)
        if not path.endswith(".ogg"): path = path_utils.get_next_cycle_item(path)
        # Presence sounds are cached under the user's profile, which may be on
        # a different Windows drive than the game. relpath() raises ValueError
        # across drives, so absolute cache paths must remain absolute.
        try:
            path = os.path.normpath(path) if os.path.isabs(path) else os.path.relpath(path)
        except ValueError:
            # Different drive letters (e.g. sound cached on C: while the game
            # runs on D:).  Keep the absolute path as-is; OpenAL/cyal accepts it.
            path = os.path.normpath(path)
        cache_key = f"{path}_mono" if as_mono else path
        if cache_key in self.buffers.keys():
            return self.buffers[cache_key]
        try:
            # Safe chunked decoder — pyogg 0.7's VorbisFile can write past
            # its destination buffer on files whose PCM exceeds the
            # granulepos estimate, silently corrupting the CPython heap
            # (root cause of the hard zombie-round crashes). See
            # libs/safe_vorbis.py.
            from .safe_vorbis import load_vorbis_pcm
            file = load_vorbis_pcm(path)
            try: buffer = self.context.gen_buffer()
            except cyal.exceptions.InvalidOperationError as e:
                print(e)
                buffer = self.context.gen_buffer()
        
            format = None
            audio_data = bytes(file.buffer)
            if as_mono and file.channels == 2:
                import array
                stereo_samples = array.array('h', audio_data)
                mono_samples = array.array('h', ((l + r) // 2 for l, r in zip(stereo_samples[0::2], stereo_samples[1::2])))
                audio_data = mono_samples.tobytes()
                format = cyal.BufferFormat.MONO16
            else:
                match file.channels:
                    case 1: format = cyal.BufferFormat.MONO16
                    case 2: format = cyal.BufferFormat.STEREO16
                    case _: raise(RuntimeError("file is neither mono or stereo 16 bit"))
            buffer.set_data(
                audio_data,
                sample_rate=file.frequency,
                format = format
            )
            gc.disable()
            try:
                self.buffers[cache_key] = buffer
            finally:
                gc.enable()
            # Keep strong reference if this path was preloaded
            if path in self._preloaded_buffers:
                self._preloaded_buffers[path] = buffer
            return buffer
        except Exception as e:
            print(f"unable to load file: {path} — {e}")
            return None

    def set_volume(self, cat, volume):
        with contextlib.suppress(RuntimeError, AttributeError):
            with self.context.batch():
                if cat not in self.volume_categories.keys(): cat="master"
                self.volume_categories[cat][0] = volume
                options.set(f"volume_{cat}", volume)
                if cat == "master":
                    # Listener gain maps master volume 1:1 to [0,1].
                    # Do NOT divide by <100 here — a listener gain above 1.0
                    # pre-amplifies the summed output and causes digital clipping
                    # whenever multiple sources (e.g. megaphone talkover) overlap.
                    self.listener.gain = self.volume_categories["master"][0] / 100
                    return
                
                # Snapshot the WeakSet: sounds can be added concurrently
                # (music-synced queues, worker callbacks), and mutating a
                # WeakSet during iteration raises RuntimeError.
                for source in list(self.volume_categories[cat][1]):
                    if source.source is None:
                        continue
                    gain = (self.volume_categories[cat][0] / 100) * (source.volume / 100)
                    if not source.muted: source.source.gain = gain

    def play_unbound(self, path, x, y, z, looping=False, cat="miscelaneous", direct=False, cone_inner_angle=360, cone_outer_angle=360, cone_outer_gain=0.4, cone_outer_gainhf=0.4, direction=(0,0,0), velocity=(0,0,0), volume=100, pitch=1.0, reference_distance=15.0, rolloff=1.0, max_distance=100.0, direct_filter=None):
        if self.muted and not looping: return
        direction=self.make_orientation(*direction)
        buffer = self.load_buffer(path)
        if not buffer: return
        if cat not in self.volume_categories:
            cat = "miscelaneous"
        if not self.volume_categories[cat] or cat == "master": return
        try:
            source = self.context.gen_source(
                looping=looping,
                gain =
                (volume / 100) *
                (self.volume_categories[cat][0]/100),
                direction=direction,
                position=(x,y,z),
                velocity=velocity,
                pitch=pitch
            )
            if direct:
                source.direct_channels=True
                source.spatialize = False
            else:
                source.direct_channels = False
                source.spatialize=True
                source.reference_distance = reference_distance
                source.rolloff_factor = rolloff
                source.max_distance = max_distance
                source.cone_inner_angle = cone_inner_angle
                source.cone_outer_angle = cone_outer_angle
                source.cone_outer_gain = cone_outer_gain
                source.set("cone_outer_gainhf", cone_outer_gainhf)


            source.buffer = buffer
        except Exception:
            # gen_source, source property setters, and buffer assignment are all
            # OpenAL calls. If any of them fault, bail out rather than letting the
            # error escalate into a native crash (unclean_exit).
            return None
        snd = Sound(source, volume, False, cat=cat)
        self.unbound_sources.append(snd)
        try:
            if direct_filter is not None:
                source.direct_filter = direct_filter
            elif len(self.filter) > 0 and self.filter[-1] is not None:
                source.direct_filter = self.filter[-1]
            for i in self.sends:
                try: self.efx.send(source, self.sends.index(i), i, filter=self.filter[-1] if len(self.filter) > 0 else None)
                except cyal.exceptions.InvalidOperationError as e: print(e)
            source.play()
        except Exception:
            with contextlib.suppress(Exception):
                source.stop()
        gc.disable()
        try:
            self.volume_categories["master"][1].add(snd)
            self.volume_categories[cat][1].add(snd)
        finally:
            gc.enable()
        return snd



    def play_unbound_stereo_spatial(self, path, x, y, z, listener_x, listener_y, listener_z, volume=200, cat="miscelaneous", max_distance=25.0, facing_angle=0.0, as_mono=False, as_3d_stereo=False, occluded=False, direct_filter=None, stereo_provider=None, stereo_offset=2.5, stereo_reference_distance=6.0, stereo_rolloff=0.6, stereo_gain_l=1.15, stereo_gain_r=1.0):
        if self.muted:
            return None
        if cat not in self.volume_categories:
            cat = "miscelaneous"
        if not self.volume_categories[cat] or cat == "master":
            return None

        ui_cat_vol = self.volume_categories.get(cat, [100])[0] / 100
        gain = (volume / 100) * ui_cat_vol

        if as_3d_stereo:
            # Linear distance fade for playable instruments.
            #
            # cyal/OpenAL only exposes the INVERSE_DISTANCE_CLAMPED model, whose gain
            # asymptotically approaches (but never reaches) zero. With that model a
            # hard cutoff at max_distance feels like the sound is abruptly switched
            # off, because the gain just before the cutoff is still audible. To make
            # the fade-out natural AND reach true silence at the edge, we bypass the
            # OpenAL rolloff entirely (rolloff_factor=0) and apply our own linear
            # gain ramp: full gain inside reference_distance, then linearly down to
            # 0 at max_distance. The remaining hard cutoff at max_distance is then
            # inaudible (gain is already ~0).
            ddx = x - listener_x
            ddy = y - listener_y
            ddz = z - listener_z
            dist_sq = ddx * ddx + ddy * ddy + ddz * ddz
            if dist_sq > (max_distance * max_distance):
                return None
            dist = dist_sq ** 0.5
            if dist <= stereo_reference_distance:
                dist_gain = 1.0
            else:
                span = max(0.0001, max_distance - stereo_reference_distance)
                dist_gain = max(0.0, 1.0 - (dist - stereo_reference_distance) / span)
            gain *= dist_gain
            stereo_provider = stereo_provider or self.piano
            buf_l, buf_r = stereo_provider.load_stereo_split_buffers(path)
            if not buf_l or not buf_r:
                return None
            try:
                gain_l = gain * stereo_gain_l
                gain_r = gain * stereo_gain_r

                src_l = self.context.gen_source(position=(x - stereo_offset, y, z), velocity=(0,0,0), pitch=1.0, gain=gain_l)
                src_l.relative = False
                src_l.direct_channels = False
                src_l.spatialize = True
                src_l.reference_distance = max_distance
                src_l.rolloff_factor = 0.0
                src_l.max_distance = max_distance
                src_l.buffer = buf_l

                src_r = self.context.gen_source(position=(x + stereo_offset, y, z), velocity=(0,0,0), pitch=1.0, gain=gain_r)
                src_r.relative = False
                src_r.direct_channels = False
                src_r.spatialize = True
                src_r.reference_distance = max_distance
                src_r.rolloff_factor = 0.0
                src_r.max_distance = max_distance
                src_r.buffer = buf_r
            except Exception as e:
                print(f"Error generating 3D split stereo sources: {e}")
                return None

            if occluded:
                filter_obj = stereo_provider.get_occlusion_filter()
                if filter_obj:
                    with contextlib.suppress(Exception):
                        src_l.direct_filter = filter_obj
                        src_r.direct_filter = filter_obj

            snd_l = Sound(src_l, volume, False, cat=cat)
            snd_r = Sound(src_r, volume, False, cat=cat)
            self.unbound_sources.append(snd_l)
            self.unbound_sources.append(snd_r)
            try:
                if len(self.filter) > 0 and self.filter[-1] is not None:
                    src_l.direct_filter = self.filter[-1]
                    src_r.direct_filter = self.filter[-1]
                if direct_filter is not None:
                    # Piano pedal filters are attached before playback so the note
                    # attack never leaks through at full brightness.
                    src_l.direct_filter = direct_filter
                    src_r.direct_filter = direct_filter
                for i in self.sends:
                    with contextlib.suppress(Exception):
                        self.efx.send(src_l, self.sends.index(i), i, filter=self.filter[-1] if len(self.filter) > 0 else None)
                        self.efx.send(src_r, self.sends.index(i), i, filter=self.filter[-1] if len(self.filter) > 0 else None)
                src_l.play()
                src_r.play()
            except Exception:
                # OpenAL calls here (direct_filter setter, efx.send, play) are
                # not strictly necessary for the sound object to exist; if they
                # fail we still return the Sounds so cleanup tracks them, but we
                # avoid letting an AL fault propagate as a native crash.
                with contextlib.suppress(Exception):
                    src_l.stop()
                with contextlib.suppress(Exception):
                    src_r.stop()
            gc.disable()
            try:
                self.volume_categories["master"][1].add(snd_l)
                self.volume_categories["master"][1].add(snd_r)
                self.volume_categories[cat][1].add(snd_l)
                self.volume_categories[cat][1].add(snd_r)
            finally:
                gc.enable()
            return (snd_l, snd_r)

        buffer = self.load_buffer(path, as_mono=as_mono)
        if not buffer:
            return None

        try:
            source = self.context.gen_source(
                position=(x, y, z),
                velocity=(0, 0, 0),
                pitch=1.0,
                gain=gain
            )
        except Exception as e:
            print(f"Error generating source for stereo spatial: {e}")
            return None

        dx = x - listener_x
        dy = y - listener_y
        dz = z - listener_z
        dist = math.sqrt(dx * dx + dy * dy + dz * dz)

        try:
            if as_mono:
                # Full 3D spatial positioning for 1-channel mono sound (remote players / map pianos)
                source.position = (x, y, z)
                source.relative = False
                source.direct_channels = False
                source.spatialize = True
                source.reference_distance = 3.0
                source.rolloff_factor = 1.0
                source.max_distance = max_distance
            elif dist <= 2.5:
                # Inside inner radius: 2D direct wide stereo for full head-filling richness (local player)
                source.position = (0, 0, 0)
                source.relative = True
                source.direct_channels = True
                source.spatialize = False
            else:
                # Outside inner radius: 3D spatial with wide 180° stereo field (-90° to +90°) and smooth rolloff
                source.position = (x, y, z)
                source.relative = False
                source.direct_channels = False
                source.spatialize = True
                source.reference_distance = 3.5
                source.rolloff_factor = 0.7
                source.max_distance = max_distance

                # OpenAL Soft AL_STEREO_ANGLES extension for wide 180-degree stereo spatial panning
                with contextlib.suppress(Exception):
                    source.set(0x1030, (math.radians(-90), math.radians(90)))

            source.buffer = buffer
        except Exception:
            # OpenAL property/buffer assignment fault: stop the freshly-generated
            # source and bail out rather than letting an AL error escalate to a
            # native crash (unclean_exit).
            with contextlib.suppress(Exception):
                source.stop()
            return None

        snd = Sound(source, volume, False, cat=cat)
        self.unbound_sources.append(snd)
        try:
            if len(self.filter) > 0 and self.filter[-1] is not None:
                source.direct_filter = self.filter[-1]
            if direct_filter is not None:
                source.direct_filter = direct_filter
            for i in self.sends:
                with contextlib.suppress(Exception):
                    self.efx.send(source, self.sends.index(i), i, filter=self.filter[-1] if len(self.filter) > 0 else None)
            source.play()
        except Exception:
            with contextlib.suppress(Exception):
                source.stop()
        gc.disable()
        try:
            self.volume_categories["master"][1].add(snd)
            self.volume_categories[cat][1].add(snd)
        finally:
            gc.enable()
        return snd



    def defer_audio(self, fn):
        """Schedule an OpenAL-touching callable to run on the main thread.

        Safe to call from ANY thread (SimpleQueue, no locks, never blocks,
        never raises). The callable runs inside AudioManager.loop()'s frame
        batch on the main thread. Use this for every AL operation that used
        to run on voice/music worker threads.
        """
        self._audio_inbox.put(fn)

    def _drain_audio_inbox(self, limit=500):
        drained = 0
        while drained < limit:
            try:
                fn = self._audio_inbox.get_nowait()
            except queue.Empty:
                return
            drained += 1
            try:
                fn()
            except Exception as e:
                print(f"[AUDIO INBOX] deferred call failed: {e}")

    def loop(self):
        with contextlib.suppress(RuntimeError):
            with self.context.batch():
                # Deferred worker-thread audio runs FIRST, inside the same
                # single-thread batch window as everything else below.
                self._drain_audio_inbox()
                self.piano.update()
                self.drums.update()
                # Drain ALL finished unbound sources every frame (the old
                # one-per-frame break let sources accumulate without bound
                # under mass-spawn load). Snapshot first: destroy() mutates
                # the list.
                for snd in list(self.unbound_sources):
                    if snd.source is None or snd.source.state == cyal.SourceState.STOPPED:
                        self.unbound_sources.remove(snd)
                        snd.destroy()
                for soundgroup in self.soundgroups:
                    soundgroup.loop()
    
    def create_soundgroup(self, direct=False, radius=0.5, filterable=False):
        sg = SoundGroup(self.context, self, direct, radius=radius, filterable=filterable)
        for i in self.filter: sg.apply_filter(i)
        for i in self.sends:
            sg.apply_effect(i, self.sends.index(i))
        
        self.soundgroups.add(sg)
        return sg
    


    def apply_effect(self, slot, sendnum=0, filter=None):
        self.sends[sendnum] = slot
        for source in self.unbound_sources:
            self.efx.send(source.source, sendnum, slot, filter=self.filter[-1] if len(self.filter) > 0 else None)
        for sg in self.soundgroups:
            sg.apply_effect(slot, sendnum, filter=filter)
        
    def apply_filter(self, filter, exclude=[], replace=True, clear=False):
        if clear: self.filter.clear()
        if filter is not None: 
            if replace and len(self.filter) > 0: self.filter.pop()
            self.filter .append(filter)
        elif len(self.filter) > 0: self.filter.pop()

        for source in self.unbound_sources:
            if filter is not None: source.source.direct_filter = filter
            else: 
                del source.source.direct_filter
                if len(self.filter) > 0 and self.filter[-1] is not None: source.source.direct_filter = self.filter[-1]

        for sg in self.soundgroups:
            if sg not in exclude: sg.apply_filter(filter, replace=replace, clear=clear)
    
    def _armor_filter(self, filter_obj, site, label="filter"):
        """Permanently INCREF an EFX wrapper so its refcount can never hit 0.

        cyal EFX wrappers (Filter/Effect/AuxiliaryEffectSlot) delete their AL
        resource in __dealloc__ through stored function pointers — crash
        dumps show this firing on a Filter whose memory was ALREADY reused
        (its efx field pointed at a static type object), i.e. a
        use-after-free of the wrapper itself. Leaking one reference makes
        that dealloc unreachable no matter what reference-counting bug
        occurs elsewhere. The finalize callback only fires if the armor
        somehow fails, and then names the creation site.
        """
        try:
            import ctypes
            import weakref
            ctypes.pythonapi.Py_IncRef.argtypes = [ctypes.py_object]
            ctypes.pythonapi.Py_IncRef(ctypes.py_object(filter_obj))

            def _armor_broken(f_id=id(filter_obj), s=site, lbl=label):
                try:
                    from .logger import log
                    log(f"[EFX ARMOR] WARNING: {lbl} {f_id} (created at {s}) was garbage collected despite INCREF armor!")
                except Exception:
                    pass

            weakref.finalize(filter_obj, _armor_broken)
        except Exception:
            pass

    def gen_filter(self, type, *args):
        """Borrow an EFX filter, serving from the pool when possible.

        Wrappers are INCREF-armored (see _armor_filter) and pooled, so their
        crash-prone __dealloc__ is unreachable. Return filters with
        release_filter() instead of dropping them. Returns ``None`` when the
        filter type is unsupported — callers must check.
        """
        import sys as _sys
        try:
            frame = _sys._getframe(1)
            site = f"{frame.f_code.co_filename}:{frame.f_lineno}"
        except Exception:
            site = "unknown"
        if self._filter_pool:
            filter_obj = self._filter_pool.pop()
            try:
                filter_obj.type = type  # reconfigure the recycled filter
            except Exception as e:
                print(f"[AudioManager] Unable to retype pooled filter '{type}': {e}")
                # Keep the wrapper pooled (never GC-freed) even on failure.
                self._filter_pool.append(filter_obj)
                return None
        else:
            try:
                filter_obj = self.efx.gen_filter(type=type)
            except cyal.exceptions.InvalidOperationError as e:
                # Log the failure and return ``None`` so the caller can handle it.
                print(f"[AudioManager] Unable to create filter '{type}': {e}")
                return None
            self._armor_filter(filter_obj, site)

        # Apply any additional parameters safely.
        for param in args:
            try:
                filter_obj.set(*param)
            except cyal.exceptions.InvalidAlEnumError as e:
                print(f"{e} in audio_manager.gen_filter with parameters {param}")
        return filter_obj

    def release_filter(self, filter_obj):
        """Return a borrowed filter to the pool (call on the main thread).

        The wrapper is deliberately kept alive forever — deleting a cyal
        Filter runs through __dealloc__'s crash-prone indirect call. Peak
        live filter count is bounded by concurrent use, never above the
        previous (GC-based) peak.
        """
        if filter_obj is None:
            return
        try:
            filter_obj.type = "null"  # neutral state for the next borrower
        except Exception:
            pass
        self._filter_pool.append(filter_obj)
    
    # === Effect Slot Pool Methods ===

    def _init_slot_pool(self):
        """Pre-allocate auxiliary effect slots at startup.
        These slots are NEVER deleted — they are reused for the lifetime of the app.
        This prevents the OpenAL resource exhaustion that causes reverb to die."""
        max_slots = 32  # Try to allocate up to 32 (driver will cap at its limit)
        for i in range(max_slots):
            try:
                slot = self.efx.gen_auxiliary_effect_slot()
                self._armor_filter(slot, "slot_pool_init", label="slot")
                self._slot_pool.append(slot)
                self._slot_pool_size += 1
            except (MemoryError, cyal.exceptions.InvalidOperationError):
                break  # Hit the driver's limit
        print(f"[AudioManager] Effect Slot Pool: {self._slot_pool_size} slots pre-allocated")

    def acquire_effect_slot(self):
        """Borrow an auxiliary effect slot from the pool.
        Returns None if pool is exhausted (graceful degradation)."""
        if self._slot_pool:
            slot = self._slot_pool.pop()
            self._slot_in_use.append(slot)
            return slot
        print(f"[AudioManager] WARNING: Effect slot pool exhausted! "
              f"({self._slot_pool_size} slots all in use)")
        return None

    def release_effect_slot(self, slot):
        """Return a slot to the pool. Detaches any effect but does NOT delete the slot."""
        if slot is None:
            return
        try:
            slot.unload()  # Detach effect from slot before it is reused
        except Exception:
            pass
        if slot in self._slot_in_use:
            self._slot_in_use.remove(slot)
        if slot not in self._slot_pool:
            self._slot_pool.append(slot)

    def create_effect(self, type, *args):
        """Create an EFX effect object only (no slot). Used with the pool system.
        The caller must acquire a slot separately via acquire_effect_slot()."""
        try:
            efx = self.efx.gen_effect(type=type)
            self._armor_filter(efx, f"create_effect:{type}", label="effect")
            for param in args:
                try:
                    efx.set(*param)
                except cyal.exceptions.InvalidAlEnumError as e:
                    print(f"{e} in audio_manager.create_effect on param {param}")
            return efx
        except (MemoryError, cyal.exceptions.InvalidOperationError) as e:
            print(f"[AudioManager] Could not create effect '{type}': {e}")
            return None

    def gen_effect(self, type, *args):
        """Create an effect + acquire a slot from pool. Pool-aware version.
        Returns the slot with the effect attached, or None."""
        efx = self.create_effect(type, *args)
        if efx is None:
            return None
        slot = self.acquire_effect_slot()
        if slot is None:
            # Can't get a slot — effect is useless without one
            return None
        slot.effect = efx
        return slot
