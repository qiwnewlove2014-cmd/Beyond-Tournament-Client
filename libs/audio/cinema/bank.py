"""OpenAL side of the Cinema Speaker System: one source per speaker.

The renderer decides *what* each speaker should sound like; this bank owns
the OpenAL objects that play it. Keeping every native call here means the
jukebox transports only ever ask two questions -- "queue this frame for the
room" and "refresh the room" -- so their existing two-source code paths stay
exactly as they were and never grow a cinema branch of their own.

Every method that touches OpenAL runs on the audio owner thread, the same
thread that already owns the relay pump and the direct streamer's queueing.
Nothing here is called from the network or decoder threads.

Output gain mirrors the plain jukebox exactly, slot by slot:

    gain = (volume/100) * jukebox_mixer * cabinet * fade * distance_ramp

where the distance ramp is the same linear fade from reference to max
distance the two-source playback uses, so a cinema room at the cabinet is
neither louder nor quieter than the jukebox it replaces. What the bank adds
is that the ramp, the wall occlusion and the reverb zone are evaluated for
each speaker's own position instead of for the cabinet as a whole: a wall
behind the rear speakers must not muffle the screen wall.
"""

import contextlib
import time

import cyal

# One pool and one queue allowance per speaker. The radio between them keeps
# the same shape as the plain jukebox (32 buffers, 10 queued): enough head-
# room for the startup pre-buffer plus network jitter, and never enough to
# let a fast decoder run ahead of the room.
BUFFERS_PER_SLOT = 12
PREBUFFER_FRAMES = 4
RESUME_FRAMES = 3
MAX_QUEUED_FRAMES = 6

SAMPLERATE = 48000


class CinemaSpeakerBank:
    """One positioned OpenAL source (and its buffer pool) per cinema slot."""

    main_thread_audio = True

    def __init__(self, game, renderer, *, volume=100, cabinet_volume=100,
                 reference_distance=8.0, max_distance=40.0,
                 occlusion_provider=None, reverb_slot=None, eq_slot=None,
                 buffers_per_slot=None, clock=None):
        self.game = game
        self.renderer = renderer
        self.volume = max(0, min(100, int(volume)))
        self.cabinet_volume = max(0.0, min(1.0, float(cabinet_volume) / 100.0))
        self.reference_distance = float(reference_distance)
        self.max_distance = float(max_distance)
        self.occlusion_provider = occlusion_provider
        self.reverb_slot = reverb_slot
        self.eq_slot = eq_slot
        self.buffers_per_slot = int(buffers_per_slot or BUFFERS_PER_SLOT)
        self._clock = clock or time.monotonic
        self._external_fade = 1.0
        self._fade_started = None
        self._fade_duration = 0.0
        self._environment_dirty = True
        self._slot_tier = {}
        self._occlusion_filters = {}
        self._stopped = False
        self.slot_sources = {}
        self._pools = {}
        self.last_output_at = None
        self.failure_reason = None
        self.frames_queued = 0
        self._plays_started = False
        self._build()

    # ------------------------------------------------------------- lifetime

    @property
    def anchor(self):
        return self.renderer.layout.anchor

    @property
    def sources(self):
        """Every speaker source, in room order (front wall first)."""
        return tuple(self.slot_sources[slot] for slot in self.renderer.slots
                     if slot in self.slot_sources)

    @property
    def primary_source(self):
        sources = self.sources
        return sources[0] if sources else None

    @property
    def secondary_source(self):
        sources = self.sources
        return sources[1] if len(sources) > 1 else None

    def _build(self):
        audio = getattr(self.game, "audio_mngr", None)
        if audio is None:
            raise RuntimeError("cinema speakers need an audio manager")
        context = audio.context
        for slot in self.renderer.slots:
            source = context.gen_source()
            position = self.renderer.layout.position(slot)
            if position is not None:
                source.position = position
            # Same spatial contract as the plain jukebox pair: OpenAL's own
            # rolloff is off and the frame-by-frame gain ramp does the fading.
            source.rolloff_factor = 0.0
            source.reference_distance = self.max_distance
            source.max_distance = self.max_distance
            source.spatialize = True
            source.direct_channels = False
            source.gain = 0.0
            self.slot_sources[slot] = source
            # A song that starts mid-dive inherits the active water muffle
            # from its first frame (see camera._music_water_sources).
            active = getattr(audio, "filter", None)
            if active and active[-1] is not None:
                with contextlib.suppress(Exception):
                    source.direct_filter = active[-1]
            pool = []
            for _ in range(self.buffers_per_slot):
                try:
                    pool.append(context.gen_buffer())
                except Exception:
                    break
            if not pool:
                raise RuntimeError("cinema speakers could not allocate buffers")
            self._pools[slot] = pool

    def stop(self):
        """Silence and drain every speaker; the player deletes the sources."""
        if self._stopped:
            return
        self._stopped = True
        self._reset_output(stamp=False)
        self.release_buffers()

    def release_buffers(self):
        """Drop buffer references so they are collected like any other pool."""
        for pool in self._pools.values():
            pool.clear()
        self._pools.clear()

    def forget_sources(self):
        """Called once the owning player has deleted the OpenAL sources."""
        self.slot_sources.clear()
        self._stopped = True

    # ---------------------------------------------------------------- output

    def queue_frame(self, left, right):
        """Render one stereo frame and queue it on every speaker it feeds.

        Buffers are reserved for every speaker *before* anything is uploaded,
        so a frame can never land on half the room: a partial frame is torn
        down rather than left to drift out of step with the other speakers.
        """
        if self._stopped or not self.slot_sources:
            return False
        feeds = self.renderer.render(left, right)
        if not feeds:
            return False
        claimed = []
        for slot, pcm in feeds:
            pool = self._pools.get(slot)
            if not pool:
                return False
            claimed.append((slot, pool.pop(), pcm))
        queued = 0
        try:
            for slot, buffer, pcm in claimed:
                buffer.set_data(pcm, sample_rate=SAMPLERATE,
                                format=cyal.BufferFormat.MONO16)
                self.slot_sources[slot].queue_buffers(buffer)
                queued += 1
            self.frames_queued += 1
            return True
        except Exception:
            # Return whatever was not queued, then reset the whole room so
            # the next frame starts from a consistent queue on every speaker.
            self.failure_reason = "cinema speaker queue failed"
            for slot, buffer, _pcm in claimed[queued:]:
                pool = self._pools.get(slot)
                if pool is not None:
                    pool.append(buffer)
            self._reset_output()
            return False

    def reclaim(self):
        """Return finished buffers to their speaker's pool; True if any moved."""
        reclaimed = False
        for slot, source in list(self.slot_sources.items()):
            pool = self._pools.get(slot)
            if pool is None:
                continue
            try:
                while source.buffers_processed > 0:
                    result = source.unqueue_buffers()
                    if result is None:
                        break
                    try:
                        pool.extend(result)
                    except TypeError:
                        # cyal returns a single Buffer, not a list.
                        pool.append(result)
                    reclaimed = True
            except Exception:
                continue
        if reclaimed:
            self.last_output_at = self._clock()
        return reclaimed

    def queued_frames(self):
        """Frames currently queued on the slowest speaker (min, not sum)."""
        counts = []
        for source in self.slot_sources.values():
            try:
                counts.append(source.buffers_queued)
            except Exception:
                return 0
        return min(counts) if counts else 0

    def playing(self):
        """True when every speaker is playing."""
        for source in self.slot_sources.values():
            try:
                if source.state != cyal.SourceState.PLAYING:
                    return False
            except Exception:
                return False
        return True

    def wanted_for_start(self):
        return RESUME_FRAMES if self._plays_started else PREBUFFER_FRAMES

    def start_playback(self):
        """Start the room once enough frames are queued on every speaker."""
        if not self.playing():
            for source in self.slot_sources.values():
                with contextlib.suppress(Exception):
                    source.play()
            self._plays_started = True
        return True

    def _reset_output(self, *, stamp=False):
        for source in self.slot_sources.values():
            with contextlib.suppress(Exception):
                source.stop()
        try:
            self.reclaim()
        except Exception:
            pass
        self._plays_started = False

    # ------------------------------------------------------------------ gain

    def configure(self, volume=None, cabinet_volume=None, fade=None):
        """Set output trims without refreshing, for callers that will refresh.

        The relay receiver recomputes its own retire ramp every pump, so it
        sets all three here and then calls update_output() once.
        """
        if volume is not None:
            self.volume = max(0, min(100, int(volume)))
        if cabinet_volume is not None:
            self.cabinet_volume = max(0.0, min(1.0, float(cabinet_volume) / 100.0))
        if fade is not None:
            self._external_fade = max(0.0, min(1.0, float(fade)))

    def set_volume(self, volume):
        self.configure(volume=volume)
        self.update_output()

    def set_cabinet_volume(self, volume):
        self.configure(cabinet_volume=volume)
        self.update_output()

    def set_fade(self, gain):
        """External fade (the relay receiver owns its own retire ramp)."""
        self.configure(fade=gain)

    def retire(self, duration=0.5):
        """Fade the room out over ``duration`` on the owner thread."""
        if self._fade_started is not None:
            return
        self._fade_started = self._clock()
        self._fade_duration = max(0.0, float(duration))

    def _fade_gain(self):
        if self._fade_started is None:
            return 1.0
        if self._fade_duration <= 0:
            return 0.0
        return max(0.0, 1.0 - (self._clock() - self._fade_started) / self._fade_duration)

    def current_fade(self):
        return self._external_fade * self._fade_gain()

    def distance_gain(self, slot, listener):
        """The plain jukebox's linear fade, evaluated at this speaker."""
        if listener is None:
            return 1.0
        position = self.slot_sources[slot].position
        distance = sum((float(listener[i]) - position[i]) ** 2 for i in range(3)) ** 0.5
        if distance <= self.reference_distance:
            return 1.0
        if distance >= self.max_distance:
            return 0.0
        span = max(0.0001, self.max_distance - self.reference_distance)
        return max(0.0, 1.0 - (distance - self.reference_distance) / span)

    def update_output(self):
        """Refresh per-speaker gain, occlusion and environment sends.

        Called every frame by whichever transport is feeding the room, so it
        stays cheap: one distance check per speaker, and the occlusion ray is
        only cast when a speaker's wall tier actually changes.
        """
        if self._stopped or not self.slot_sources:
            return
        audio = getattr(self.game, "audio_mngr", None)
        if audio is None:
            return
        listener = getattr(audio, "position", None)
        category = audio.volume_categories.get("jukebox", [100])[0] / 100.0
        local = (self.volume / 100.0) * category * self.cabinet_volume * self.current_fade()
        reverb_slot = self.reverb_slot
        eq_slot = self.eq_slot
        efx = getattr(audio, "efx", None)
        for slot, source in self.slot_sources.items():
            with contextlib.suppress(Exception):
                source.gain = local * self.distance_gain(slot, listener)
            if listener is None:
                continue
            tier = self._wall_tier(slot, listener)
            if tier == self._slot_tier.get(slot) and not self._environment_dirty:
                continue
            self._slot_tier[slot] = tier
            filt = None
            if tier == 2:
                filt = self._occlusion_filter(audio, heavy=True)
            elif tier == 1:
                filt = self._occlusion_filter(audio, heavy=False)
            with contextlib.suppress(Exception):
                if filt is not None:
                    source.direct_filter = filt
                else:
                    # Clearing a wall must not strip the underwater muffle;
                    # restore the active global filter instead of deleting.
                    active = getattr(audio, "filter", None)
                    if active and active[-1] is not None:
                        source.direct_filter = active[-1]
                    else:
                        del source.direct_filter
            if efx is not None:
                with contextlib.suppress(Exception):
                    if reverb_slot is not None:
                        efx.send(source, 0, reverb_slot, filter=filt)
                    if eq_slot is not None:
                        efx.send(source, 1, eq_slot, filter=filt)
        self._environment_dirty = False

    def _wall_tier(self, slot, listener):
        provider = self.occlusion_provider
        if not callable(provider):
            return 0
        position = self.renderer.layout.position(slot)
        if position is None:
            return 0
        try:
            return int(provider(position, listener, self.max_distance))
        except Exception:
            return 0

    def _occlusion_filter(self, audio, *, heavy):
        key = "heavy" if heavy else "light"
        cached = self._occlusion_filters.get(key)
        if cached is not None:
            return cached
        if not hasattr(audio, "gen_filter"):
            return None
        params = (("GAINHF", 0.05), ("GAIN", 0.22)) if heavy else (("GAINHF", 0.45), ("GAIN", 0.75))
        try:
            cached = audio.gen_filter("LOWPASS", *params)
        except Exception:
            cached = None
        self._occlusion_filters[key] = cached
        return cached

    # -------------------------------------------------------------- settings

    def set_reverb(self, slot):
        if slot is not self.reverb_slot:
            self.reverb_slot = slot
            self._environment_dirty = True

    def set_eq_slot(self, slot):
        if slot is not self.eq_slot:
            self.eq_slot = slot
            self._environment_dirty = True

    def set_profile(self, profile):
        """Swap the room's profile live and re-aim the room's speakers."""
        self.renderer.set_profile(profile)
        self._slot_tier.clear()
        self._environment_dirty = True

    def touch_environment(self):
        """Force the next refresh to re-send occlusion and environment."""
        self._slot_tier.clear()
        self._environment_dirty = True

    def __repr__(self):
        return (f"CinemaSpeakerBank({self.renderer.profile.name!r}, "
                f"slots={list(self.slot_sources)}, queued={self.queued_frames()})")
