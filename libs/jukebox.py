"""Music Jukebox — a map element that plays a shared song queue.

Architecture
------------
The queue lives on the SERVER (per map), so playback keeps going no matter
which player queued the songs or leaves the map. The server broadcasts
``jukebox_play`` / ``jukebox_stop`` / ``jukebox_queue_update`` / ``jukebox_state``
events. New clients receive one server-owned live Opus stream per active
cabinet; decoding, distance attenuation, reverb, and local volume stay on the
client. If the optional server worker is unavailable, playback falls back to
the direct ffmpeg path used by the personal Music Bot.

The jukebox menu deliberately has NO broadcast/megaphone buttons — it supports
searching YouTube, direct YouTube URL input, viewing the queue, removing own
queued songs, skipping songs, adjusting volume, and staff queue clearance.
"""

import contextlib
import struct
import threading
import time
from collections import OrderedDict
from math import trunc

import cyal
import pygame

from . import state
from .speech import speak
from .deferred_log import log_deferred as log_line
from .jukebox_relay import JukeboxRelayReceiver
from .jukebox_media_cache import JukeboxMediaCache
from .audio_diagnostics import probe as audio_probe

# Shared wall occlusion tiers used by every playback site below.
# 0 = clear path · 1 = thin obstacle (a lone pillar tile): light lowpass ·
# 2 = thick wall (>= 3 tiles): full standard occlusion.
OCCLUSION_CLEAR, OCCLUSION_LIGHT, OCCLUSION_FULL = 0, 1, 2


def wall_occlusion_tier(cur_map, src_pos, listener):
    """Classify wall thickness between two points for discrete-filter sites.

    Wraps map.occlusion_tier() (which counts wall tiles along the ray so a
    single pillar only lightly muffles) with a legacy boolean fallback for
    stub/foreign map objects.
    """
    if cur_map is None or src_pos is None or listener is None:
        return OCCLUSION_CLEAR
    tfn = getattr(cur_map, "occlusion_tier", None)
    if tfn is not None:
        try:
            return int(tfn(src_pos, listener))
        except Exception:
            return OCCLUSION_CLEAR
    try:
        if cur_map.valid_straight_path(src_pos, listener) is False:
            return OCCLUSION_FULL
    except Exception:
        pass
    return OCCLUSION_CLEAR


class JukeboxPlayer:
    """Plays one song per jukebox, anchored at the jukebox's 3D position.

    Each jukebox id gets its own OpenAL source + ffmpeg streamer thread.
    """

    def __init__(self, game):
        self.game = game
        self.players = {}  # jukebox_id -> {"source", "secondary_source", "streamer", "title", "url"}
        # Only URL/header metadata survives map changes, never audio sources
        # or a playback position. Eight short-lived entries per player owner.
        self._media_cache = JukeboxMediaCache()
        self.volume = 65
        self._lock = threading.Lock()
        self.relay_routes = {}
        # Frames buffered for relay routes whose jukebox_play event is still
        # sitting on the deferred game queue (see pend_relay_route) — this is
        # what keeps the first fraction of a song (its intro) from being
        # dropped between the network thread and the main loop.
        self._relay_pending = {}
        self._control_serial = 0
        # Jukebox ids awaiting a post-reload play confirmation (see
        # mark_pending_map_change). Confirmed players are kept playing
        # seamlessly; unconfirmed ones are stopped after a short grace period.
        self._pending_map_change = set()
        self._pending_map_change_serial = None
        self._last_recovery_request_at = 0.0
        # Consecutive relay recovery failures per jukebox + sticky
        # direct-playback deadlines (see update() — the cure for "sound dies
        # until a full relogin": map changes keep the same ENet peer, only a
        # fresh connection or local direct playback restores audio).
        self._relay_fail_counts = {}
        self._direct_fallback_until = {}
        self._stall_unsticks = {}
        self.eq_profiles = {}
        self.eq_values = {}
        self.eq_slots = {}
        self.custom_eq_slots = {}
        self.cabinet_volumes = {}
        self._occlusion_filter = None
        self._light_occlusion_filter = None

        self._occlusion_cache = OrderedDict()
        self._occlusion_lock = threading.Lock()
        self._retired_relays = []
        # Direct tails retired at song advance: the streamer keeps playing to
        # its natural EOF while the next song resolves and holds for its own
        # lead-in (see _retire_or_stop).
        self._retiring_direct = []

    def occlusion_tier(self, box_pos, listener, max_distance=40.0):
        """Skip inaudible rays and briefly reuse exact tile-ray results.

        Main relay pump and legacy direct worker may query this; only the
        small cache is locked, never the map scan or any audio operation.
        """
        if listener is None or box_pos is None:
            return OCCLUSION_CLEAR
        if sum((listener[i] - box_pos[i]) ** 2 for i in range(3)) >= (max_distance + 2.5) ** 2:
            return OCCLUSION_CLEAR  # Both stereo channels are out of range.
        gp = getattr(self.game, "gameplay", None)
        cur_map = getattr(gp, "map", None)
        if cur_map is None:
            return OCCLUSION_CLEAR
        tiles = getattr(cur_map, "tile_list", ())
        key = (id(cur_map), id(tiles), len(tiles),
               tuple(trunc(value) for value in box_pos),
               tuple(trunc(value) for value in listener))
        now = time.monotonic()
        with self._occlusion_lock:
            cached = self._occlusion_cache.get(key)
            if cached is not None and now - cached[0] < 0.15:
                self._occlusion_cache.move_to_end(key)
                return cached[1]
        tier = wall_occlusion_tier(cur_map, box_pos, listener)
        with self._occlusion_lock:
            # Retain identity owners until eviction: otherwise a rebuilt tile
            # list can immediately reuse an old id and inherit its wall tier.
            self._occlusion_cache[key] = (now, tier, cur_map, tiles)
            self._occlusion_cache.move_to_end(key)
            while len(self._occlusion_cache) > 64:
                self._occlusion_cache.popitem(last=False)
        return tier

    def get_occlusion_filter(self):
        """Lazy-create and return a shared Lowpass filter for wall occlusion muffling."""
        if self._occlusion_filter is None:
            audio = getattr(self.game, "audio_mngr", None)
            if audio is not None and hasattr(audio, "gen_filter"):
                try:
                    self._occlusion_filter = audio.gen_filter(
                        "LOWPASS",
                        ("GAINHF", 0.05),
                        ("GAIN", 0.22),
                    )
                except Exception:
                    self._occlusion_filter = None
        return self._occlusion_filter

    def get_light_occlusion_filter(self):
        """Lazy-create a gentle Lowpass for PARTIALLY occluded playback.

        A thin obstacle (a lone pillar tile between cabinet and listener)
        should only slightly dull the song — unlike the heavy full-wall
        filter above.
        """
        if self._light_occlusion_filter is None:
            audio = getattr(self.game, "audio_mngr", None)
            if audio is not None and hasattr(audio, "gen_filter"):
                try:
                    self._light_occlusion_filter = audio.gen_filter(
                        "LOWPASS",
                        ("GAINHF", 0.45),
                        ("GAIN", 0.75),
                    )
                except Exception:
                    self._light_occlusion_filter = None
        return self._light_occlusion_filter

    EQ_PRESETS = {
        "bass_boost": (
            ("low_gain", 7.0),
            ("low_cutoff", 260.0),
            ("mid1_gain", 0.9),
            ("high_gain", 1.0),
            ("high_cutoff", 4000.0),
        ),
    }

    @staticmethod
    def _normalize_eq_values(values):
        values = values if isinstance(values, dict) else {}
        normalized = {}
        for band in ("bass", "mid", "treble"):
            try:
                value = int(values.get(band, 50))
            except (TypeError, ValueError):
                value = 50
            normalized[band] = max(0, min(100, value))
        return normalized

    @classmethod
    def _custom_eq_parameters(cls, values):
        """Map accessible 0-100 sliders to safe OpenAL EQUALIZER gains.

        50 is unity/flat. The exponential curve gives equal perceptual room
        above and below unity while staying inside the EFX 0.126-7.943 range.
        Both mid filters move together so the single Mid slider covers vocals
        and instruments instead of only one narrow center frequency.
        """
        values = cls._normalize_eq_values(values)

        def gain(value):
            return 7.0 ** ((value - 50.0) / 50.0)

        mid_gain = gain(values["mid"])
        return (
            ("low_gain", gain(values["bass"])),
            ("low_cutoff", 260.0),
            ("mid1_gain", mid_gain),
            ("mid1_center", 500.0),
            ("mid1_width", 1.0),
            ("mid2_gain", mid_gain),
            ("mid2_center", 3000.0),
            ("mid2_width", 1.0),
            ("high_gain", gain(values["treble"])),
            ("high_cutoff", 4000.0),
        )

    @audio_probe.measured("jukebox.eq")
    def _get_eq_slot(self, profile, jukebox_id=None, eq_values=None):
        """Get or update an OpenAL Hardware Equalizer slot.

        Presets share a cached slot. A custom profile owns one slot per active
        cabinet and mutates its effect in place on every slider tick, avoiding
        EFX slot leaks and keeping changes audible in real time.
        """
        profile = str(profile or "normal").lower()
        if profile != "custom" and profile not in self.EQ_PRESETS:
            return None
        audio = getattr(self.game, "audio_mngr", None)
        if audio is None or getattr(audio, "efx", None) is None or not hasattr(audio, "gen_effect"):
            return None
        if profile == "custom":
            if not jukebox_id:
                return None
            params = self._custom_eq_parameters(eq_values)
            slot = self.custom_eq_slots.get(jukebox_id)
            if slot is None:
                try:
                    slot = audio.gen_effect("EQUALIZER", *params)
                except Exception:
                    slot = None
                self.custom_eq_slots[jukebox_id] = slot
            elif slot is not None:
                effect = getattr(slot, "effect", None)
                if effect is not None:
                    for param in params:
                        try:
                            effect.set(*param)
                        except Exception:
                            pass
                    try:
                        # EFX implementations may snapshot parameters when an
                        # effect is attached; reattach the same object so the
                        # in-place edits become audible without a new slot.
                        slot.effect = effect
                    except Exception:
                        pass
            return slot
        if profile not in self.eq_slots:
            try:
                params = self.EQ_PRESETS[profile]
                self.eq_slots[profile] = audio.gen_effect("EQUALIZER", *params)
            except Exception:
                self.eq_slots[profile] = None
        return self.eq_slots.get(profile)

    def set_eq_profile(self, jukebox_id, profile, eq_values=None):
        """Update EQ profile for a specific jukebox and re-apply EFX sends in real-time."""
        profile = str(profile or "normal").lower()
        if profile not in ("normal", "bass_boost", "custom"):
            profile = "normal"
        previous_profile = self.eq_profiles.get(jukebox_id, "normal")
        normalized_values = self._normalize_eq_values(eq_values)
        self.eq_profiles[jukebox_id] = profile
        if profile == "custom":
            self.eq_values[jukebox_id] = normalized_values
        else:
            self.eq_values.pop(jukebox_id, None)
        audio = getattr(self.game, "audio_mngr", None)
        if audio is None or getattr(audio, "efx", None) is None:
            return
        slot = self._get_eq_slot(profile, jukebox_id, normalized_values)
        with self._lock:
            entry = self.players.get(jukebox_id)
            if entry is not None:
                streamer = entry.get("streamer")
                if streamer is not None:
                    streamer.eq_slot = slot
                filt = None
                gp = getattr(self.game, "gameplay", None)
                cur_map = getattr(gp, "map", None) if gp is not None else None
                listener = getattr(audio, "position", None)
                src = entry.get("source")
                if cur_map is not None and listener is not None and src is not None and hasattr(cur_map, "valid_straight_path"):
                    pos = src.position
                    tier = self.occlusion_tier((pos[0] + 2.5, pos[1], pos[2]), listener)
                    if tier == OCCLUSION_FULL:
                        filt = self.get_occlusion_filter()
                    elif tier == OCCLUSION_LIGHT:
                        filt = self.get_light_occlusion_filter()
                for key in ("source", "secondary_source"):
                    src = entry.get(key)
                    if src is not None:
                        try:
                            audio.efx.send(src, 1, slot, filter=filt)
                        except Exception:
                            pass
        if previous_profile == "custom" and profile != "custom":
            old_slot = self.custom_eq_slots.pop(jukebox_id, None)
            if old_slot is not None and hasattr(audio, "release_effect_slot"):
                # A replaced relay may still be fading on the audio owner.
                # Detach its send before lending this slot to another sound.
                for receiver in self._retired_relays:
                    if receiver.eq_slot is old_slot:
                        receiver.eq_slot = None
                        for source in (receiver.source_l, receiver.source_r):
                            with contextlib.suppress(Exception):
                                audio.efx.send(source, 1, None)
                try:
                    audio.release_effect_slot(old_slot)
                except Exception:
                    pass

    def set_cabinet_volume(self, jukebox_id, volume):
        """Set server-synchronized master volume for a specific jukebox cabinet (0-100)."""
        vol = max(0, min(100, int(volume)))
        self.cabinet_volumes[jukebox_id] = vol
        with self._lock:
            p = self.players.get(jukebox_id)
            if p is not None:
                streamer = p.get("streamer")
                if streamer is not None and hasattr(streamer, "set_cabinet_volume"):
                    streamer.set_cabinet_volume(vol)

    RELAY_STARTUP_TIMEOUT = 7.0
    RELAY_STALL_TIMEOUT = 5.0
    # A stall that survived a warm-up un-stick attempt escalates to a full
    # rebuild (chronic loss of the unreliable relay channel).
    RELAY_HARD_STALL_TIMEOUT = 12.0
    # Frames still arriving but OpenAL making no audible progress for this
    # long -> full rebuild (dead sources / lost context after an audio device
    # switch — the packet-based stall check can never see this state).
    RELAY_AUDIO_STALL_TIMEOUT = 10.0
    # Playback started but the speakers stopped CONSUMING buffers for this
    # long -> the stream is slowly starving (underrunning): frames arrive
    # just often enough to dodge the packet stall check and buffers still
    # queue, yet the listener hears silence or a stutter loop. Only real
    # output consumption proves otherwise.
    RELAY_OUTPUT_STALL_TIMEOUT = 8.0
    # Direct playback has no server relay to announce a mid-song death, and
    # ffmpeg can hang without exiting (frozen CDN read, dead OpenAL sink).
    # When the speakers consume no buffer for this long while the stream
    # thread is alive and playing, rebuild — a silent-but-alive direct
    # stream is the one stall the thread-death check can never see.
    DIRECT_OUTPUT_STALL_TIMEOUT = 8.0
    # Frame-rate starvation: a trickling channel (a few frames every few
    # seconds) passes every liveness check above — packets arrive, buffers
    # queue, speakers consume the tiny bursts — while the listener hears a
    # sped-up stutter that fades to silence. A healthy relay delivers 25
    # frames/second; below half of that over a 10s window the channel is
    # starving and the rebuild ladder (ending in direct TCP playback) must
    # kick in MID-SONG instead of waiting for the next song.
    RELAY_STARVE_WINDOW = 10.0
    RELAY_STARVE_MIN_FPS = 12.5
    # A relay_pending placeholder whose follow-up play event never landed
    # (server worker died mid-retry) asks for a resync after this long.
    RELAY_PENDING_TIMEOUT = 15.0
    RECOVERY_COOLDOWN = 5.0
    # A stall normally recovers via resync + warm-up replay. If this many
    # consecutive un-stick attempts fail to hold, rebuild immediately instead
    # of waiting for the hard-stall timeout (observed after map reloads: the
    # warm-up blip plays but live frames never follow — only a fresh
    # receiver fixes that, so get there in ~10s instead of 15).
    RELAY_UNSTICK_TRIES = 2
    # When relay recovery fails this many times in a row for one jukebox, the
    # client plays that song directly over HTTP/ffmpeg (TCP) instead. Chronic
    # loss of the unreliable relay channel on one player's ENet peer can never
    # be fixed by resyncing — the warm-up frames use the same lost channel —
    # and used to leave that player silent until a full relogin.
    RELAY_DIRECT_FALLBACK_AFTER = 3
    # Direct stays sticky for this long (or until a map change) so later songs
    # don't re-pay the failing-relay window; relay is then tried again.
    DIRECT_FALLBACK_TTL = 10 * 60.0
    # Anchored direct playback runs one AudioStreamer.DIRECT_LEAD_IN_S behind
    # the server's audioStartedAt, so the next song's jukebox_play lands while
    # the old song still has its final seconds queued locally. A retired tail
    # may run at most the lead-in plus this margin before the sweep stops it.
    DIRECT_RETIRE_TAIL_MARGIN_S = 5.0
    # Full parse_map reloads can cross the ordered map channel and the misc
    # response channel.  The observed state reply can arrive just over two
    # seconds later, so do not destroy the healthy receiver prematurely.
    MAP_RELOAD_CONFIRM_TIMEOUT = 4.0
    # A pending relay route that never turns into a real receiver (play was
    # rejected, or the server switched relay identity) is swept after this.
    RELAY_PENDING_TTL = 10.0

    @property
    def control_serial(self):
        with self._lock:
            return self._control_serial

    def set_volume(self, volume):
        """Set local volume level for all jukeboxes (0-100)."""
        self.volume = max(0, min(100, int(volume)))
        with self._lock:
            for p in self.players.values():
                streamer = p.get("streamer")
                if streamer is not None and hasattr(streamer, "set_volume"):
                    streamer.set_volume(self.volume)

    @audio_probe.measured("jukebox.start", trigger=True)
    def play(self, jukebox_id, x, y, z, title, url, duration, volume=None, start_offset=0.0,
             playback_id=None, transport="direct", relay_id=None, stream_epoch=None,
             http_headers=None, room_lead_in_s=None, join_playing_room=False,
             received_at=None, **_kwargs):
        """Start (or seamlessly continue) the song for one jukebox at (x, y, z).

        Stereo-spatial like piano/drums: the STEREO stream is split into two
        MONO sources placed just left/right of the jukebox, so you hear a real
        stereo image up close that collapses to mono and fades with distance.
        """
        if not url and transport == "direct":
            log_line(f"[Jukebox] play({jukebox_id}) skipped: no url")
            return

        # Emergency direct fallback: when this connection chronically loses
        # the unreliable relay channel, play locally over HTTP instead of
        # waiting for relay frames that never arrive. Sticky until the
        # deadline (or a map change) so later songs stay audible too.
        if transport in ("relay", "relay_pending"):
            deadline = self._direct_fallback_until.get(jukebox_id)
            if deadline is not None and time.monotonic() < deadline:
                if relay_id is not None and stream_epoch is not None:
                    try:
                        with self._lock:
                            self._relay_pending.pop((int(relay_id), int(stream_epoch)), None)
                    except (TypeError, ValueError):
                        pass
                transport = "direct"
                # This machine plays direct ONLY because its own relay
                # channel is chronically lost; the rest of the room still
                # hears the server relay. The relay room holds no lead-in,
                # so anchoring with the full DIRECT_LEAD_IN_S hold would
                # leave this machine exactly one lead-in behind the room for
                # the whole song — its jam notes land ~3.5s off the beat
                # with no lag report to fix it (its own anchor says it
                # started on time). Anchor with zero lead-in and JOIN the
                # already-playing room at its current position (seek past
                # the projected audible start) instead of starting at 0.
                room_lead_in_s = 0.0
                join_playing_room = True

        effective_volume = self.volume if volume is None else volume
        playback_key = ("id", int(playback_id)) if playback_id is not None else ("url", url)
        # Kept so the emergency direct fallback can re-start this exact song
        # from its current wall-clock position without another server event.
        play_params = {
            "x": float(x), "y": float(y), "z": float(z),
            "title": title, "url": url, "duration": int(duration or 0),
            "start_offset": float(start_offset or 0.0),
            "http_headers": http_headers,
            "received_at": time.monotonic(),
        }

        with self._lock:
            self._control_serial += 1
            # A play event for this jukebox confirms it is still alive after a
            # map reload — drop the pending-stop mark so playback continues
            # seamlessly instead of being torn down and rebuilt every reload.
            self._pending_map_change.discard(jukebox_id)
            existing = self.players.get(jukebox_id)
            same_relay_identity = True
            if existing is not None and transport == "relay":
                incoming_key = (
                    (int(relay_id), int(stream_epoch))
                    if relay_id is not None and stream_epoch is not None
                    else None
                )
                # If the server restarted the relay worker (new identity, same
                # song/playback), the old receiver must be torn down and the
                # new route registered — otherwise every frame from the new
                # worker is dropped and the song goes silent for clients that
                # were already connected. Fresh clients register the new route
                # on join, which is why only "old" clients lose the audio.
                same_relay_identity = existing.get("relay_key") == incoming_key
            if (
                existing is not None
                and existing.get("playback_key") == playback_key
                and existing.get("transport") == transport
                and same_relay_identity
            ):
                # Idempotent even while yt-dlp/ffmpeg is still resolving. Two map
                # sync routes must never cancel the same in-flight playback.
                src_l = existing.get("source")
                src_r = existing.get("secondary_source")
                current_streamer = existing.get("streamer")
                if getattr(current_streamer, "main_thread_audio", False) is True:
                    current_streamer.box_pos = (float(x), float(y), float(z))
                offset = 2.5
                if src_l is not None and src_r is not None:
                    try:
                        for src, sx in ((src_l, float(x) - offset), (src_r, float(x) + offset)):
                            src.position = (sx, float(y), float(z))
                    except Exception:
                        pass
                log_line(f"[Jukebox] play({jukebox_id}) seamless continuity for {title!r}")
                return

        # Make-before-break: a relay_pending event must not kill a working
        # stream for the same song. Map reloads re-offer the relay
        # ("wait for it") while the listener is happily playing direct —
        # stopping that stream made ~10s of silence until the relay actually
        # became ready. The same applies to a LIVE relay receiver when the
        # server's retry notice races in: the worker may still be streaming
        # (frames keep arriving), and tearing the receiver down would cut the
        # song for no reason. Keep the live stream running; the ready relay
        # event (or the stall watchdogs / direct fallback) switches streams on
        # its own.
        if transport == "relay_pending":
            with self._lock:
                existing_entry = self.players.get(jukebox_id)
                keep_live = False
                if (
                    existing_entry is not None
                    and existing_entry.get("playback_key") == playback_key
                ):
                    existing_streamer = existing_entry.get("streamer")
                    if existing_entry.get("transport") == "direct":
                        keep_live = bool(
                            existing_streamer is not None
                            and hasattr(existing_streamer, "is_alive")
                            and existing_streamer.is_alive()
                        )
                    elif existing_entry.get("transport") == "relay":
                        last_packet = getattr(existing_streamer, "last_packet_at", None)
                        keep_live = bool(
                            existing_streamer is not None
                            and hasattr(existing_streamer, "is_alive")
                            and existing_streamer.is_alive()
                            and last_packet is not None
                            and time.monotonic() - float(last_packet) < 2.0
                        )
            if keep_live:
                log_line(
                    f"[Jukebox] play({jukebox_id}) keeping live "
                    f"{existing_entry.get('transport')} stream for {title!r} "
                    f"while the relay readies"
                )
                return

        self._retire_or_stop(jukebox_id)
        if transport == "relay_pending":
            with self._lock:
                self.players[jukebox_id] = {
                    "source": None, "secondary_source": None, "streamer": None,
                    "title": title, "url": url, "transport": transport,
                    "playback_key": playback_key,
                    "play_params": play_params,
                    # Without a timestamp the update() watchdog computed this
                    # placeholder's age as 0 on every frame, so a placeholder
                    # whose follow-up relay event never landed waited silently
                    # forever (the "must relog after a map reload" bug).
                    "created_at": time.monotonic(),
                }
            log_line(
                f"[Jukebox] waiting for server relay {title!r} "
                f"playback={playback_id!r}"
            )
            return
        audio = getattr(self.game, "audio_mngr", None)
        if audio is None:
            log_line(f"[Jukebox] play({jukebox_id}) skipped: no audio_mngr")
            return
        src_l = src_r = None
        try:
            src_l = audio_probe.call("jukebox.gen_source", audio.context.gen_source)
            src_r = audio_probe.call("jukebox.gen_source", audio.context.gen_source)
        except Exception:
            self._release_sources((src_l, src_r))
            log_line(f"[Jukebox] play({jukebox_id}) failed: gen_source error")
            return
        offset = 2.5   # same stereo offset as piano/drums 3D-stereo sounds
        ref = 8.0      # full volume inside this distance
        maxd = 40.0    # silent at/beyond this distance
        base_gain = max(0.0, min(1.0, effective_volume / 100.0))
        try:
            with audio_probe.span("jukebox.source_setup"):
                for src, sx in ((src_l, float(x) - offset), (src_r, float(x) + offset)):
                    src.position = (sx, float(y), float(z))
                    src.rolloff_factor = 0.0   # linear fade handled per-frame
                    src.reference_distance = maxd
                    src.max_distance = maxd
                    src.spatialize = True
                    src.direct_channels = False
                    src.gain = base_gain
        except Exception:
            pass
        # A song that starts while the listener is underwater inherits the
        # active global water filter (camera.py pushes it onto
        # audio_mngr.filter) so it is muffled from its first frame.
        try:
            active = getattr(audio, "filter", None)
            if active and active[-1] is not None:
                src_l.direct_filter = active[-1]
                src_r.direct_filter = active[-1]
        except Exception:
            pass
        # Initial wall occlusion check:
        filt = None
        try:
            gameplay = getattr(self.game, "gameplay", None)
            cur_map = getattr(gameplay, "map", None) if gameplay is not None else None
            audio = getattr(self.game, "audio_mngr", None)
            listener = getattr(audio, "position", None) if audio is not None else None
            if cur_map is not None and listener is not None and hasattr(cur_map, "valid_straight_path"):
                tier = audio_probe.call("jukebox.initial_wall", self.occlusion_tier,
                                        (float(x), float(y), float(z)), listener, maxd)
                if tier != OCCLUSION_CLEAR:
                    filt = (
                        self.get_light_occlusion_filter() if tier == OCCLUSION_LIGHT
                        else self.get_occlusion_filter()
                    )
                    if filt is not None:
                        src_l.direct_filter = filt
                        src_r.direct_filter = filt
        except Exception:
            pass
        # Room reverb: like piano/drums, the song picks up the reverb zone of
        # the place the jukebox stands in (none if the spot has no reverb).
        reverb = None
        try:
            if gameplay is not None and getattr(gameplay, "map", None) is not None:
                reverb_zone = audio_probe.call("jukebox.reverb_lookup", gameplay.map.get_reverb_at,
                                               float(x), float(y), float(z))
                reverb = reverb_zone.reverb if reverb_zone and hasattr(reverb_zone, "reverb") else None
                if reverb is not None and getattr(audio, "efx", None) is not None:
                    audio_probe.call("jukebox.efx", audio.efx.send, src_l, 0, reverb, filter=filt)
                    audio_probe.call("jukebox.efx", audio.efx.send, src_r, 0, reverb, filter=filt)
        except Exception:
            pass
        # Jukebox Equalizer profile (e.g. Bass Boost / Horn Speaker per-jukebox):
        slot = None
        try:
            eq_profile = str(_kwargs.get("eq_profile") or self.eq_profiles.get(jukebox_id, "normal")).lower()
            eq_values = self._normalize_eq_values(
                _kwargs.get("eq_values") or self.eq_values.get(jukebox_id)
            )
            self.eq_profiles[jukebox_id] = eq_profile
            if eq_profile == "custom":
                self.eq_values[jukebox_id] = eq_values
            else:
                self.eq_values.pop(jukebox_id, None)
            slot = self._get_eq_slot(eq_profile, jukebox_id, eq_values)
            if slot is not None and getattr(audio, "efx", None) is not None:
                audio_probe.call("jukebox.efx", audio.efx.send, src_l, 1, slot, filter=filt)
                audio_probe.call("jukebox.efx", audio.efx.send, src_r, 1, slot, filter=filt)
        except Exception:
            pass
        cab_vol = _kwargs.get("cabinet_volume")
        if cab_vol is None:
            cab_vol = self.cabinet_volumes.get(jukebox_id, 100)
        try:
            cab_vol = int(cab_vol)
        except Exception:
            cab_vol = 100
        self.cabinet_volumes[jukebox_id] = cab_vol

        streamer = None
        try:
            if transport == "relay":
                if relay_id is None or stream_epoch is None:
                    raise ValueError("missing relay identity")
                audio_probe.event("jukebox.relay")
                streamer = audio_probe.call("jukebox.receiver_create", JukeboxRelayReceiver,
                    self.game, src_l, src_r, effective_volume,
                    relay_id, stream_epoch, ref, maxd,
                    box_pos=(float(x), float(y), float(z)), player=self,
                    reverb_slot=reverb, eq_slot=slot,
                    cabinet_volume=cab_vol,
                )
            else:
                from . import music_bot as mb
                audio_probe.event("jukebox.direct")
                streamer = audio_probe.call("jukebox.direct_create", mb.AudioStreamer,
                    self.game, url, src_l, volume=effective_volume, bot=None,
                    channels=2, spatial_pair=(src_l, src_r, ref, maxd),
                    start_offset=start_offset,
                    start_offset_received_at=received_at or time.monotonic(),
                    http_headers=http_headers,
                    timeline_anchor=play_params["duration"] > 0,
                    media_cache=self._media_cache if play_params["duration"] > 0 else None,
                    room_lead_in_s=room_lead_in_s,
                    join_playing_room=join_playing_room,
                )
                streamer.reverb_slot = reverb
                streamer.eq_slot = slot
                streamer.jukebox_player = self
                if hasattr(streamer, "set_cabinet_volume"):
                    streamer.set_cabinet_volume(cab_vol)
            audio_probe.call("jukebox.thread_start", streamer.start)
        except Exception as ex:
            if getattr(streamer, "main_thread_audio", False) is True:
                streamer.stop()
            for src in (src_l, src_r):
                try:
                    src.delete()
                except Exception:
                    pass
            from . import logger
            logger.log_exception(ex, f"JukeboxPlayer.play({jukebox_id}) start streamer")
            log_line(f"[Jukebox] play({jukebox_id}) failed to start streamer: {ex}")
            return
        with self._lock:
            self.players[jukebox_id] = {
                "source": src_l,
                "secondary_source": src_r,
                "streamer": streamer,
                "title": title,
                "url": url,
                "transport": transport,
                "playback_key": playback_key,
                "relay_key": (int(relay_id), int(stream_epoch)) if transport == "relay" else None,
                "created_at": time.monotonic(),
                "play_params": play_params,
            }
            if transport == "relay":
                self.relay_routes[(int(relay_id), int(stream_epoch))] = streamer
                # Take over any frames buffered while this receiver was being
                # created and flush them UNDER THE SAME LOCK: a live frame on
                # the network thread must not slip in first and make the
                # sequence gate reject the buffered intro frames.
                pending = self._relay_pending.pop((int(relay_id), int(stream_epoch)), None)
                if pending is not None:
                    for seq, payload, flags in pending["frames"]:
                        streamer.receive(seq, payload, flags)
        log_line(
            f"[Jukebox] playing {title!r} transport={transport} "
            f"playback={playback_id!r} relay={relay_id!r}/{stream_epoch!r} "
            f"at ({x}, {y}, {z}) offset={start_offset:.1f}s url={url[:60]!r}"
        )

    def _fade_out_sources(self, sources, streamer=None, duration=0.5):
        """Fade active OpenAL sources to 0 gain in a daemon thread and clean them up."""
        valid_sources = [s for s in sources if s is not None]
        if getattr(streamer, "main_thread_audio", False) is True:
            # Relay fades share the main audio pump: no timer thread touches
            # OpenAL, no join, and the old receiver cannot revive a new song.
            self._retired_relays.append(streamer)
            def finish():
                if streamer in self._retired_relays:
                    self._retired_relays.remove(streamer)
                self._release_sources(valid_sources)
            streamer.retire(duration=duration, cleanup_callback=finish)
            return
        if not valid_sources:
            if streamer is not None:
                try:
                    if hasattr(streamer, "stop"):
                        streamer.stop()
                    else:
                        streamer.running = False
                except Exception:
                    pass
            return

        def _fade_worker():
            try:
                start_gains = [float(getattr(s, 'gain', 1.0) or 0.0) for s in valid_sources]
                steps = 10
                step_sleep = duration / steps
                for i in range(steps):
                    fraction = (steps - 1 - i) / steps
                    for idx, s in enumerate(valid_sources):
                        try:
                            s.gain = max(0.0, start_gains[idx] * fraction)
                        except Exception:
                            pass
                    time.sleep(step_sleep)
            except Exception:
                pass
            finally:
                if streamer is not None:
                    try:
                        if hasattr(streamer, "stop"):
                            streamer.stop()
                        else:
                            streamer.running = False
                    except Exception:
                        pass
                audio = getattr(self.game, "audio_mngr", None)
                for s in valid_sources:
                    try:
                        with contextlib.suppress(Exception):
                            del s.direct_filter
                        if audio is not None and getattr(audio, "efx", None) is not None:
                            try:
                                audio.efx.send(s, 0, None)
                                audio.efx.send(s, 1, None)
                            except Exception:
                                pass
                        s.stop()
                        drain_limit = 64
                        while s.buffers_processed > 0 and drain_limit > 0:
                            s.unqueue_buffers()
                            drain_limit -= 1
                        s.delete()
                    except Exception:
                        pass

        import threading
        threading.Thread(target=_fade_worker, daemon=True).start()

    def _release_sources(self, sources):
        """Detach effects, then delete independently of drain failures (owner)."""
        audio = getattr(self.game, "audio_mngr", None)
        for source in sources:
            if source is None:
                continue
            with contextlib.suppress(Exception):
                del source.direct_filter
            if getattr(audio, "efx", None) is not None:
                for index in (0, 1):
                    with contextlib.suppress(Exception):
                        audio.efx.send(source, index, None)
            with contextlib.suppress(Exception):
                source.stop()
                for _ in range(64):
                    if source.buffers_processed <= 0:
                        break
                    source.unqueue_buffers()
            with contextlib.suppress(Exception):
                source.delete()

    @audio_probe.measured("jukebox.stop")
    def stop(self, jukebox_id, playback_id=None, fade=False):
        """Stop the song for one jukebox and free its audio source."""
        with self._lock:
            existing = self.players.get(jukebox_id)
            if existing is not None and playback_id is not None:
                try:
                    expected_key = ("id", int(playback_id))
                except (TypeError, ValueError):
                    return False
                if existing.get("playback_key") != expected_key:
                    return False
            player = self.players.pop(jukebox_id, None)
        if not player:
            return False
        log_line(f"[Jukebox] stop({jukebox_id}) title={player.get('title')!r}")
        streamer = player.get("streamer")
        relay_key = player.get("relay_key")
        if relay_key is not None:
            with self._lock:
                self.relay_routes.pop(relay_key, None)
                self._relay_pending.pop(relay_key, None)
        if fade:
            self._fade_out_sources([player.get("source"), player.get("secondary_source")], streamer=streamer, duration=0.5)
            return True
        if getattr(streamer, "main_thread_audio", False) is True:
            streamer.stop()
            self._release_sources([player.get("source"), player.get("secondary_source")])
            return True
        try:
            if streamer is not None:
                if isinstance(streamer, JukeboxRelayReceiver):
                    streamer.stop()
                else:
                    if hasattr(streamer, "stop"):
                        streamer.stop()
                    else:
                        streamer.running = False
                    if hasattr(streamer, "join") and threading.current_thread() is not streamer:
                        streamer.join(timeout=1.0)
        except Exception:
            pass
        for key in ("source", "secondary_source"):
            source = player.get(key)
            if source is not None:
                try:
                    with contextlib.suppress(Exception):
                        del source.direct_filter
                    audio = getattr(self.game, "audio_mngr", None)
                    if audio is not None and getattr(audio, "efx", None) is not None:
                        try:
                            audio.efx.send(source, 0, None)
                            audio.efx.send(source, 1, None)
                        except Exception:
                            pass
                    source.stop()
                    try:
                        drain_limit = 64
                        while source.buffers_processed > 0 and drain_limit > 0:
                            source.unqueue_buffers()
                            drain_limit -= 1
                    except Exception:
                        pass
                    source.delete()
                except Exception:
                    pass
        return True

    def stop_all(self):
        """Stop every jukebox (map change / disconnect).

        Clears the pending map-change marks too, so any in-flight sweep from an
        earlier reload can never act on a jukebox created after this teardown.
        """
        with self._lock:
            self._pending_map_change = set()
            self._pending_map_change_serial = None
            ids = list(self.players.keys())
        for jukebox_id in ids:
            self.stop(jukebox_id)
        for receiver in list(self._retired_relays):
            receiver.stop()  # Finishes its owner-thread source cleanup too.
        self._stop_retiring_direct()
        with self._occlusion_lock:
            self._occlusion_cache.clear()
        audio = getattr(self.game, "audio_mngr", None)
        slots = list(self.custom_eq_slots.values())
        self.custom_eq_slots.clear()
        self.eq_values.clear()
        if audio is not None and hasattr(audio, "release_effect_slot"):
            for slot in slots:
                if slot is not None:
                    try:
                        audio.release_effect_slot(slot)
                    except Exception:
                        pass

    def _retire_or_stop(self, jukebox_id):
        """Song advance: let a nearly-finished direct song play out its tail.

        Anchored direct playback runs one lead-in behind the server's
        audioStartedAt, so the next jukebox_play lands while the old song
        still has its final seconds queued locally. Stopping on the packet
        (the old behavior) cut every song's ending short; retiring keeps
        the tail audible while the new song resolves and holds for its own
        lead-in. Anything but a short remaining tail falls back to the
        normal faded stop.
        """
        from . import music_bot as mb
        budget = mb.AudioStreamer.DIRECT_LEAD_IN_S + self.DIRECT_RETIRE_TAIL_MARGIN_S
        with self._lock:
            existing = self.players.get(jukebox_id)
            if existing is not None:
                streamer = existing.get("streamer")
                duration = float((existing.get("play_params") or {}).get("duration") or 0)
                position = None
                content_position = getattr(streamer, "content_position", None)
                if duration > 0 and callable(content_position):
                    try:
                        position = float(content_position())
                    except Exception:
                        position = None
                remaining = duration - position if position is not None else None
                if (
                    existing.get("transport") == "direct"
                    and streamer is not None
                    and hasattr(streamer, "is_alive") and streamer.is_alive()
                    and not getattr(streamer, "failure_reason", None)
                    and remaining is not None and 0.0 < remaining <= budget
                ):
                    self.players.pop(jukebox_id, None)
                    streamer.reverb_slot = None
                    self._retiring_direct.append({
                        "id": jukebox_id,
                        "streamer": streamer,
                        "source": existing.get("source"),
                        "secondary_source": existing.get("secondary_source"),
                        "deadline": time.monotonic() + remaining + 2.0,
                    })
                    log_line(
                        f"[Jukebox] retire({jukebox_id}): letting "
                        f"{existing.get('title')!r} finish its last {remaining:.1f}s"
                    )
                    return
        self.stop(jukebox_id, fade=True)

    def _sweep_retiring_direct(self):
        """Release retired direct tails once they drain (or time out)."""
        now = time.monotonic()
        finished = []
        with self._lock:
            still = []
            for entry in self._retiring_direct:
                streamer = entry.get("streamer")
                done = (
                    not hasattr(streamer, "is_alive")
                    or not streamer.is_alive()
                    or now >= float(entry.get("deadline") or now)
                )
                if done:
                    finished.append(entry)
                else:
                    still.append(entry)
            self._retiring_direct[:] = still
        for entry in finished:
            streamer = entry.get("streamer")
            try:
                if streamer is not None and hasattr(streamer, "stop"):
                    streamer.stop()
            except Exception:
                pass
            self._release_sources([entry.get("source"), entry.get("secondary_source")])
            log_line(f"[Jukebox] retire({entry.get('id')}): tail finished")

    def _stop_retiring_direct(self):
        """Hard-stop every retired tail (disconnect / full teardown)."""
        with self._lock:
            entries = list(self._retiring_direct)
            self._retiring_direct.clear()
        for entry in entries:
            streamer = entry.get("streamer")
            try:
                if streamer is not None and hasattr(streamer, "stop"):
                    streamer.stop()
            except Exception:
                pass
            self._release_sources([entry.get("source"), entry.get("secondary_source")])

    def update(self):
        """Recover a jukebox stream that stopped without a stop packet.

        Relay frames are intentionally unreliable.  A temporary loss should
        normally be hidden by the receiver's buffer, but a dead receiver or a
        route that no longer receives frames used to leave an existing client
        permanently silent until reconnect.  The server's ``jukebox_resync``
        reply includes both the authoritative play state and relay warm-up
        frames, so it is the safe recovery authority.

        Recovery is layered so every failure mode ends in a full rebuild
        instead of an infinite silent wait:
        * frames stopped arriving -> resync + warm-up un-stick (5s), escalating
          to a full rebuild if the stall survives to 12s;
        * frames keep arriving but OpenAL makes no audible progress -> full
          rebuild after 10s;
        * a relay_pending placeholder whose follow-up play event never lands
          -> resync after 15s.
        """
        now = time.monotonic()
        self._sweep_retiring_direct()
        rebuilds = []  # [(jukebox_id, reason)]
        stalled_ids = []  # relays relying on a warm-up un-stick this cycle
        needs_resync = False
        with self._lock:
            # Drop pending relay routes that never became receivers (play
            # rejected, or the server switched relay identity mid-flight).
            stale_pending = [
                key for key, entry in self._relay_pending.items()
                if now - entry["at"] > self.RELAY_PENDING_TTL
            ]
            for key in stale_pending:
                del self._relay_pending[key]
            for jukebox_id, entry in list(self.players.items()):
                transport = entry.get("transport")
                streamer = entry.get("streamer")
                created_at = float(entry.get("created_at") or now)
                age = now - created_at
                if transport == "relay" and streamer is not None:
                    alive = streamer.is_alive() if hasattr(streamer, "is_alive") else True
                    last_packet = getattr(streamer, "last_packet_at", None)
                    stall_age = (now - float(last_packet)) if last_packet is not None else None
                    no_packets = (
                        age >= self.RELAY_STARTUP_TIMEOUT and last_packet is None
                    ) or (
                        stall_age is not None and stall_age >= self.RELAY_STALL_TIMEOUT
                    )
                    last_audio = getattr(streamer, "last_audio_activity", None)
                    has_started = bool(getattr(streamer, "_play_started", False) or age >= self.RELAY_STARTUP_TIMEOUT)
                    stuck_audio = bool(
                        alive
                        and last_packet is not None
                        and stall_age is not None and stall_age < 1.0
                        and has_started
                        and last_audio is not None
                        and now - float(last_audio) >= self.RELAY_AUDIO_STALL_TIMEOUT
                    )
                    if (alive and has_started and last_audio is None
                            and getattr(streamer, "failure_reason", None)
                            and age >= self.RELAY_AUDIO_STALL_TIMEOUT):
                        stuck_audio = True
                    # Underrun watchdog: frames may arrive and buffers may
                    # queue, but if the speakers have not consumed anything
                    # for a while the listener is hearing silence/stutter
                    # that every packet-based check calls "healthy".
                    last_output = getattr(streamer, "last_output_at", None)
                    starved_output = bool(
                        alive
                        and has_started
                        and last_output is not None
                        and now - float(last_output) >= self.RELAY_OUTPUT_STALL_TIMEOUT
                    )
                    hard_stall = bool(
                        no_packets and stall_age is not None
                        and stall_age >= self.RELAY_HARD_STALL_TIMEOUT
                    )
                    # Frame-rate starvation: everything above can look green
                    # while the listener hears a sped-up stutter fading to
                    # silence. Only the arrival RATE over a window exposes
                    # the trickling channel (healthy = 25 fps).
                    starved_rate = False
                    if (alive and not no_packets and not stuck_audio
                            and not starved_output
                            and has_started):
                        frames_now = int(getattr(streamer, "received_frames", 0) or 0)
                        check = entry.get("frame_rate_check")
                        if not isinstance(check, list) or len(check) != 2:
                            entry["frame_rate_check"] = [now, frames_now]
                        elif now - float(check[0]) >= self.RELAY_STARVE_WINDOW:
                            window = now - float(check[0])
                            fps = (frames_now - int(check[1])) / window
                            entry["frame_rate_check"] = [now, frames_now]
                            if fps < self.RELAY_STARVE_MIN_FPS:
                                starved_rate = True
                                rebuilds.append(
                                    (jukebox_id, f"frame starvation ({fps:.1f} fps)")
                                )
                    stopped_sources = False
                    src_l = entry.get("source")
                    src_r = entry.get("secondary_source")
                    if src_l is not None and src_r is not None and age >= self.RELAY_STARTUP_TIMEOUT:
                        try:
                            if ((src_l.buffers_queued > 0 or src_r.buffers_queued > 0)
                                    and src_l.state != cyal.SourceState.PLAYING
                                    and src_r.state != cyal.SourceState.PLAYING):
                                stopped_sources = True
                        except Exception:
                            pass
                    if (alive and not no_packets and not stuck_audio
                            and not starved_output and not starved_rate
                            and not stopped_sources
                            and last_audio is not None):
                        # Relay demonstrably working — clear the strike and
                        # un-stick counters.
                        self._relay_fail_counts[jukebox_id] = 0
                        self._stall_unsticks[jukebox_id] = 0
                    if not alive:
                        rebuilds.append((jukebox_id, "receiver thread died"))
                    elif stopped_sources:
                        rebuilds.append((jukebox_id, "sources stopped with queued audio"))
                    elif stuck_audio:
                        rebuilds.append((jukebox_id, "frames arriving but no audio progress"))
                    elif starved_output:
                        rebuilds.append((jukebox_id, "speaker stopped consuming (underrun)"))
                    elif no_packets:
                        if hard_stall:
                            # The stall survived a warm-up un-stick attempt —
                            # stop waiting and rebuild from scratch.
                            rebuilds.append((jukebox_id, f"no frames for {stall_age:.0f}s"))
                        else:
                            stalled_ids.append(jukebox_id)
                        needs_resync = True
                elif transport == "relay_pending":
                    # The server said a relay is coming but the follow-up play
                    # event never landed (worker died mid-retry): ask for the
                    # authoritative state instead of waiting for a relog.
                    if age >= self.RELAY_PENDING_TIMEOUT:
                        needs_resync = True
                elif transport == "direct" and streamer is not None:
                    alive = streamer.is_alive() if hasattr(streamer, "is_alive") else True
                    # Direct fallback has no server relay to signal a mid-song
                    # decoder failure.  Restart only an explicit failure; a
                    # normally completed stream is allowed to await the server's
                    # next-song timer.
                    if (not alive
                            and getattr(streamer, "failure_reason", None)):
                        rebuilds.append((jukebox_id, "direct streamer failed"))
                    else:
                        # Output-stall watchdog: the thread can stay alive
                        # while OpenAL stops consuming (dead audio sink,
                        # ffmpeg hang that never exits). ready_event means
                        # local playback began; last_output_at only advances
                        # when the speakers actually finish a buffer, so a
                        # stream that has made no audible progress for the
                        # timeout is rebuilt even though every packet-style
                        # liveness check would call it healthy. A naturally
                        # finished stream exits its thread within a second of
                        # its last output, so this never fires on a clean end.
                        has_started = bool(
                            getattr(streamer, "ready_event", None)
                            and streamer.ready_event.is_set())
                        last_output = getattr(streamer, "last_output_at", None)
                        if (alive and has_started
                                and getattr(streamer, "running", False)
                                and last_output is not None
                                and now - float(last_output)
                                >= self.DIRECT_OUTPUT_STALL_TIMEOUT):
                            rebuilds.append(
                                (jukebox_id,
                                 "direct output stalled (speaker not consuming)"))
        if rebuilds:
            needs_resync = True
        if needs_resync:
            # Do not remove a dead route unless this call actually sent the
            # recovery request.  Otherwise a cooldown could leave no route
            # behind to trigger the next retry.
            if not self.request_resync("relay recovery"):
                rebuilds = []
            else:
                # The resync just went out: every still-stalled relay that is
                # NOT being rebuilt now owes its recovery to the warm-up
                # replay. Count these un-stick attempts — when they repeat
                # without holding (observed after map reloads: the warm-up
                # blip plays but live frames never follow), rebuild right
                # away instead of waiting out the hard-stall timeout.
                for jukebox_id in stalled_ids:
                    if any(jid == jukebox_id for jid, _ in rebuilds):
                        continue
                    unsticks = self._stall_unsticks.get(jukebox_id, 0) + 1
                    self._stall_unsticks[jukebox_id] = unsticks
                    if unsticks >= self.RELAY_UNSTICK_TRIES:
                        rebuilds.append((jukebox_id, "warm-up un-stick did not hold"))
                # A dead receiver must be removed before the matching
                # ``jukebox_play`` arrives; otherwise idempotent same-identity
                # handling would retain it.  The resync reply rebuilds it with
                # fresh sources, and the buffered pending-route frames plus
                # the server's warm-up replay bridge the gap seamlessly.
                direct_switches = []
                for jukebox_id, reason in rebuilds:
                    log_line(f"[Jukebox] auto-recovery: rebuilding {jukebox_id} ({reason})")
                    params = None
                    with self._lock:
                        entry = self.players.get(jukebox_id)
                        # A strike per EXECUTED recovery attempt (not per scan
                        # frame): three failed rebuild cycles in a row means
                        # resyncing cannot fix this connection.
                        strikes = self._relay_fail_counts.get(jukebox_id, 0) + 1
                        self._relay_fail_counts[jukebox_id] = strikes
                        if entry is not None and strikes >= self.RELAY_DIRECT_FALLBACK_AFTER:
                            # Snapshot before stop() removes the entry.
                            params = dict(entry.get("play_params") or {})
                    self.stop(jukebox_id)
                    if params is not None:
                        direct_switches.append((jukebox_id, params))
                for jukebox_id, params in direct_switches:
                    # Relay keeps failing on this connection: switch this one
                    # jukebox to local direct playback (TCP-based) instead of
                    # looping rebuilds forever on a lost unreliable channel.
                    self._switch_to_direct(jukebox_id, params)

    def _switch_to_direct(self, jukebox_id, params):
        """Emergency: play the current song locally over HTTP when the relay
        channel is chronically unusable on this connection.

        Direct playback does not depend on the unreliable relay channel at
        all, so it survives exactly the cases resyncing cannot fix — chronic
        per-peer loss of the relay channel, where even the warm-up frames
        die and only a full relogin used to restore audio. Other listeners
        keep hearing the server relay unaffected."""
        params = dict(params or {})
        self._direct_fallback_until[jukebox_id] = time.monotonic() + self.DIRECT_FALLBACK_TTL
        if not params.get("url"):
            return
        # Continue from the song's current wall-clock position: the original
        # offset plus everything elapsed since the play event arrived.
        now = time.monotonic()
        offset = float(params.get("start_offset") or 0.0) + max(
            0.0, now - float(params.get("received_at") or now))
        log_line(f"[Jukebox] auto-recovery: {jukebox_id} relay unusable — direct playback")
        speak("Jukebox playing directly.")
        self.play(
            jukebox_id, params["x"], params["y"], params["z"],
            params.get("title", ""), params["url"], params.get("duration") or 0,
            transport="direct", start_offset=offset,
            http_headers=params.get("http_headers"),
            # This machine fell back while the rest of the room still hears
            # the server relay. The relay room holds no lead-in, so the
            # direct anchor must not hold one either — otherwise this
            # machine trails the room by exactly DIRECT_LEAD_IN_S for the
            # whole song and its jam notes land seconds off the beat with no
            # sender-lag report to compensate (its own anchor says it
            # started on time). It must also JOIN the playing room at its
            # current position (seek), never start from 0. Any residual
            # resolve/startup overrun is reported through direct_late_s.
            room_lead_in_s=0.0,
            join_playing_room=True,
        )

    def request_resync(self, reason="manual recovery", *, raise_errors=False):
        """Ask the server for current jukebox routes and relay warm-up frames.

        This is safe after a UI transition: the server remains the playback
        authority, and the cooldown prevents repeated requests from creating
        needless traffic.
        """
        now = time.monotonic()
        with self._lock:
            # A UI close needs no server round-trip when this map has no
            # active jukebox playback known to the client.
            if not self.players:
                return False
            if now - self._last_recovery_request_at < self.RECOVERY_COOLDOWN:
                return False
            self._last_recovery_request_at = now
        try:
            from . import consts
            self.game.network.send(consts.CHANNEL_MISC, "jukebox_resync")
            log_line(f"[Jukebox] requested resync: {reason}")
            return True
        except Exception:
            if raise_errors:
                raise
            return False

    def stop_all_if_serial(self, serial):
        """Ignore a delayed map-cleanup after a newer play control has arrived."""
        with self._lock:
            unchanged = self._control_serial == serial
        if unchanged:
            self.stop_all()
        return unchanged

    def mark_pending_map_change(self, serial):
        """Mark every active player as awaiting a post-reload play event.

        After a map reload the server re-broadcasts jukebox_play for songs
        that are still playing.  Those events clear the mark (via play()) so
        the receiver keeps streaming with zero interruption.  Any player still
        unconfirmed after a short grace period is a song that truly ended or a
        jukebox that no longer exists on this map — stop it so no ghost audio
        lingers.
        """
        import threading as _threading
        with self._lock:
            # This mark is queued from CHANNEL_MAP. A newer jukebox_play may
            # already have reached the game queue through another ENet channel;
            # never let the stale reload mark claim that newly confirmed stream.
            if self._control_serial != serial:
                return False
            pending = set(self.players.keys())
            self._pending_map_change = pending
            self._pending_map_change_serial = serial
            # A fresh map is a fresh chance for the relay channel: give the
            # server relay another try after the emergency direct fallback.
            self._direct_fallback_until.clear()
            self._relay_fail_counts.clear()
            self._stall_unsticks.clear()
            if pending:
                log_line(
                    f"[Jukebox] map reload: {len(pending)} jukebox(es) awaiting "
                    f"play confirmation"
                )
        if not pending:
            return True

        def sweep():
            time.sleep(self.MAP_RELOAD_CONFIRM_TIMEOUT)
            with self._lock:
                if self._pending_map_change_serial != serial:
                    return  # a newer reload's mark superseded this one
                doomed = list(self._pending_map_change)
            if not doomed:
                return

            def do_stop():
                for jid in doomed:
                    with self._lock:
                        # A late play() re-confirmed it — keep playing.
                        if jid not in self._pending_map_change:
                            continue
                        self._pending_map_change.discard(jid)
                    self.stop(jid)

            if self.game is not None:
                self.game.put(do_stop)

        _threading.Thread(target=sweep, daemon=True).start()
        return True

    def pend_relay_route(self, relay_id, stream_epoch):
        """Reserve a relay route BEFORE play() registers the real receiver.

        Relay frames are processed synchronously on the network thread, while
        jukebox_play playback setup is deferred to the main game loop. Without
        a pending buffer every frame in that gap — the song's first few 40 ms
        slices — was dropped, so songs started a fraction of a second in.
        Called from the jukebox_play network handler, so it only touches a dict.
        """
        if relay_id is None or stream_epoch is None:
            return
        try:
            key = (int(relay_id), int(stream_epoch))
        except (TypeError, ValueError):
            return
        with self._lock:
            if key in self.relay_routes:
                return
            if key not in self._relay_pending:
                self._relay_pending[key] = {"frames": [], "at": time.monotonic()}

    def receive_relay_packet(self, data):
        """Parse and enqueue a compact relay frame; safe under the network lock."""
        try:
            packet = bytes(data)
            if len(packet) < 13 or packet[0] != 1:
                return False
            relay_id, stream_epoch, sequence, flags = struct.unpack(">IIHB", packet[1:12])
            payload = packet[12:]
            if len(payload) > 1275 or flags & ~0x03:
                return False
            with self._lock:
                receiver = self.relay_routes.get((relay_id, stream_epoch))
                pending = None
                if receiver is None:
                    # No live route yet: keep the frame for a pending route so
                    # the song intro survives until play() registers the
                    # receiver. Bounded; the oldest frames drop first.
                    pending = self._relay_pending.get((relay_id, stream_epoch))
                    if pending is not None:
                        pending["at"] = time.monotonic()
                        frames = pending["frames"]
                        if len(frames) >= JukeboxRelayReceiver.MAX_PENDING_FRAMES:
                            frames.pop(0)
                        frames.append((sequence, payload, flags))
            if receiver is None:
                return pending is not None
            receiver.receive(sequence, payload, flags)
            return True
        except Exception:
            return False

    def sync_reverb(self):
        """Re-syncs environment Reverb EFX for all active jukebox streams after map reloads."""
        with self._occlusion_lock:
            self._occlusion_cache.clear()
        gameplay = getattr(self.game, "gameplay", None)
        if gameplay is None or getattr(gameplay, "map", None) is None:
            return not bool(self.players)
        audio = getattr(self.game, "audio_mngr", None)
        if audio is None:
            return not bool(self.players)
        healthy = True
        with self._lock:
            for jid, p in self.players.items():
                src_l = p.get("source")
                src_r = p.get("secondary_source")
                if src_l is None or src_r is None:
                    healthy = False
                    continue
                try:
                    pos = src_l.position
                    reverb_zone = gameplay.map.get_reverb_at(pos[0] + 2.5, pos[1], pos[2])
                    reverb = reverb_zone.reverb if reverb_zone and hasattr(reverb_zone, "reverb") else None
                    streamer = p.get("streamer")
                    if streamer is not None:
                        streamer.reverb_slot = reverb
                    filt = None
                    listener = getattr(audio, "position", None)
                    if listener is not None and hasattr(gameplay.map, "valid_straight_path"):
                        tier = self.occlusion_tier((pos[0] + 2.5, pos[1], pos[2]), listener)
                        if tier != OCCLUSION_CLEAR:
                            filt = (
                                self.get_light_occlusion_filter() if tier == OCCLUSION_LIGHT
                                else self.get_occlusion_filter()
                            )
                    audio.efx.send(src_l, 0, reverb, filter=filt)
                    audio.efx.send(src_r, 0, reverb, filter=filt)
                except Exception:
                    healthy = False
        return healthy

    def detach_reverb(self):
        """Detach active and fading streams before map slots return to the pool."""
        audio = getattr(self.game, "audio_mngr", None)
        with self._lock:
            sources = [
                source
                for player in self.players.values()
                for source in (player.get("source"), player.get("secondary_source"))
                if source is not None
            ]
            for player in self.players.values():
                streamer = player.get("streamer")
                if streamer is not None:
                    streamer.reverb_slot = None
        # Retired relay streams remain audible for the crossfade, but must not
        # retain or reattach a slot after the old map returns it to the pool.
        for receiver in tuple(self._retired_relays):
            receiver.reverb_slot = None
            sources.extend(source for source in (receiver.source_l, receiver.source_r)
                           if source is not None)
        # Retired direct tails (song-advance crossfade) follow the same rule.
        for entry in tuple(self._retiring_direct):
            streamer = entry.get("streamer")
            if streamer is not None:
                streamer.reverb_slot = None
            sources.extend(source for source in (entry.get("source"), entry.get("secondary_source"))
                           if source is not None)
        if audio is None or not hasattr(audio, "efx"):
            return
        for source in sources:
            try:
                audio.efx.send(source, 0, None)
            except Exception:
                pass


def _current_state(gp):
    """The last jukebox state payload received from the server."""
    state = getattr(gp, "jukebox_state", None)
    return state if isinstance(state, dict) else {"jukeboxes": {}}


def _closest_jukebox(gp):
    """Pick the jukebox nearest to the player from the cached server state."""
    state = _current_state(gp)
    boxes = state.get("jukeboxes", {})
    if not boxes:
        return None
    px = getattr(getattr(gp, "player", None), "x", 0) or 0
    py = getattr(getattr(gp, "player", None), "y", 0) or 0
    pz = getattr(getattr(gp, "player", None), "z", 0) or 0
    best = None
    best_dist = None
    for jid, box in boxes.items():
        bx = box.get("x", 0)
        by = box.get("y", 0)
        bz = box.get("z", 0)
        d = (px - bx) ** 2 + (py - by) ** 2 + (pz - bz) ** 2
        if best_dist is None or d < best_dist:
            best_dist = d
            best = box
    return best


def open_jukebox_menu(game, gp):
    """Open the jukebox song-queue menu."""
    from . import menu as menu_mod, menus

    jb = _closest_jukebox(gp)
    if jb is None:
        speak("There is no jukebox available here.")
        return
    jukebox_id = jb.get("id")

    def go_search():
        gp.pop_last_substate()
        _open_search_input(game, gp, jukebox_id)

    def go_direct_url():
        gp.pop_last_substate()
        _open_url_input(game, gp, jukebox_id)

    def go_skip():
        gp.pop_last_substate()
        _skip_song(game, gp, jukebox_id)

    def go_stop():
        gp.pop_last_substate()
        _stop_playback(game, gp, jukebox_id)

    def go_pause():
        gp.pop_last_substate()
        _toggle_pause(game, gp, jukebox_id)

    def go_repeat():
        gp.pop_last_substate()
        _toggle_repeat(game, gp, jukebox_id)

    def go_shuffle():
        gp.pop_last_substate()
        _shuffle_queue(game, gp, jukebox_id)

    def go_volume():
        gp.pop_last_substate()
        _open_volume_menu(game, gp, jukebox_id)

    def go_queue():
        gp.pop_last_substate()
        _view_queue(game, gp)

    def go_remove():
        gp.pop_last_substate()
        _remove_my_song(game, gp)

    def go_clear_all():
        gp.pop_last_substate()
        _clear_all(game, gp, jukebox_id)

    def go_eq():
        gp.pop_last_substate()
        _open_eq_menu(game, gp, jukebox_id)

    repeat_names = {"off": "off", "one": "repeat one", "all": "repeat all"}

    def _repeat_label():
        # Callable label: evaluated when the menu speaks, so it always shows
        # the latest repeat mode from the cached server state.
        mode = _get_repeat_mode(gp, jukebox_id)
        return f"Toggle repeat mode (now: {repeat_names.get(mode, 'off')})"

    def _eq_label():
        values = _get_eq_values(gp, jukebox_id)
        return (
            "Sound profile EQ "
            f"(Bass {values['bass']}%, Mid {values['mid']}%, "
            f"Treble {values['treble']}%)"
        )

    def _volume_label():
        vol = _get_jukebox_volume(gp, jukebox_id)
        return f"Adjust jukebox volume (now: {vol}%)"

    menu_items = [
        ("Search YouTube and queue a song", go_search),
        ("Queue by YouTube URL or livestream", go_direct_url),
        (lambda: _pause_menu_label(gp, jukebox_id), go_pause),
        ("Skip current song", go_skip),
        ("Stop playback", go_stop),
        (_repeat_label, go_repeat),
        ("Shuffle queue", go_shuffle),
        (_volume_label, go_volume),
        ("View queue", go_queue),
        ("Remove my queued song", go_remove),
    ]

    is_staff = bool(
        getattr(gp, "is_staff", False)
        or getattr(gp, "is_builder", False)
        or getattr(gp, "is_technician", False)
        or getattr(gp, "can_broadcast_megaphone", False)
    )
    if is_staff:
        menu_items.append((_eq_label, go_eq))
        menu_items.append(("Clear queue and stop (Staff only)", go_clear_all))

    menu_items.append(("Cancel", lambda: gp.pop_last_substate()))

    m = menu_mod.Menu(game, "Music Jukebox", parrent=gp)
    m.add_items(menu_items)
    menus.set_default_sounds(m)
    gp.add_substate(m)


class _JukeboxEqSlider(state.State):
    """Accessible, server-synchronized Bass/Mid/Treble slider state."""

    SEND_INTERVAL = 0.075
    BANDS = (("bass", "Bass"), ("mid", "Mid"), ("treble", "Treble"))

    def __init__(self, game, parent, jukebox_id):
        super().__init__(game, parrent=parent)
        self.jukebox_id = jukebox_id
        self.values = _get_eq_values(parent, jukebox_id)
        self.current_index = 0
        self._last_preview_at = 0.0
        self._closed = False

    def enter(self):
        super().enter()
        speak(
            "Jukebox Sound Profile EQ. Tab switches Bass, Mid, and Treble. "
            "Up and Down adjust. Page Up and Page Down adjust by 10. "
            "Home resets the current band to 50. Enter saves."
        )
        self._announce_current()

    def exit(self):
        super().exit()
        speak("Jukebox Sound Profile EQ closed.")

    def update(self, events):
        super().update(events)
        for event in events:
            if event.type != pygame.KEYDOWN:
                continue
            key = event.key
            if key == pygame.K_TAB:
                direction = -1 if event.mod & pygame.KMOD_SHIFT else 1
                self.current_index = (self.current_index + direction) % len(self.BANDS)
                self._announce_current()
            elif key in (pygame.K_RETURN, pygame.K_KP_ENTER, pygame.K_ESCAPE):
                self._close_and_commit()
                break
            elif key == pygame.K_UP:
                self._adjust(1)
            elif key == pygame.K_DOWN:
                self._adjust(-1)
            elif key == pygame.K_PAGEUP:
                self._adjust(10)
            elif key == pygame.K_PAGEDOWN:
                self._adjust(-10)
            elif key == pygame.K_HOME:
                self._set_current(50)
        return True

    def _announce_current(self):
        band, label = self.BANDS[self.current_index]
        speak(f"{label}. Slider: {self.values[band]}%")

    def _adjust(self, amount):
        band, _ = self.BANDS[self.current_index]
        self._set_current(self.values[band] + amount)

    def _set_current(self, value):
        band, _ = self.BANDS[self.current_index]
        value = max(0, min(100, int(value)))
        if value != self.values[band]:
            self.values[band] = value
            self._apply(preview=True)
        speak(f"{value}%")

    def _apply(self, preview):
        values = dict(self.values)
        boxes = _current_state(self.parrent).setdefault("jukeboxes", {})
        box = boxes.setdefault(self.jukebox_id, {"id": self.jukebox_id})
        box["eq_profile"] = "custom"
        box["eq_values"] = values
        player = getattr(self.parrent, "jukebox_player", None)
        if player is not None:
            player.set_eq_profile(self.jukebox_id, "custom", values)

        now = time.monotonic()
        if preview and now - self._last_preview_at < self.SEND_INTERVAL:
            return
        network = getattr(self.game, "network", None)
        if network is not None:
            from . import consts
            network.send(consts.CHANNEL_MISC, "jukebox_set_eq", {
                "id": self.jukebox_id,
                "eq_values": values,
                "preview": bool(preview),
                "commit": not preview,
            })
            if preview:
                self._last_preview_at = now

    def _close_and_commit(self):
        if self._closed:
            return
        self._closed = True
        self._apply(preview=False)
        self.parrent.pop_last_substate()
        open_jukebox_menu(self.game, self.parrent)


def _open_eq_menu(game, gp, jukebox_id):
    """Open the accessible, real-time Jukebox Equalizer sliders."""
    gp.add_substate(_JukeboxEqSlider(game, gp, jukebox_id))


def _open_search_input(game, gp, jukebox_id):
    gp.add_substate(game.input.run(
        "Enter a song name or livestream to search YouTube:",
        handeler=lambda query: _on_search_submit(game, gp, jukebox_id, query),
    ))


def _on_search_submit(game, gp, jukebox_id, query):
    gp.pop_last_substate()
    if not (query or "").strip():
        speak("Search cancelled.")
        return
    speak("Searching YouTube...")

    def do_search():
        from .music_bot import YouTubeSearcher
        results = YouTubeSearcher.search(query.strip(), count=5)
        game.put(lambda: _show_results_menu(game, gp, jukebox_id, results))

    threading.Thread(target=do_search, daemon=True).start()


def _open_url_input(game, gp, jukebox_id):
    gp.add_substate(game.input.run(
        "Enter YouTube video URL (or paste a livestream link):",
        handeler=lambda url: _on_url_submit(game, gp, jukebox_id, url),
    ))


def _on_url_submit(game, gp, jukebox_id, raw_url):
    gp.pop_last_substate()
    url = (raw_url or "").strip()
    if not url:
        speak("Cancelled.")
        return
    # The jukebox streams through the server's YouTube relay, so only YouTube
    # links are supported. Other yt-dlp sites would resolve here and then be
    # mangled into a bogus youtube.com/watch?v=<foreign-id> queue entry that
    # can never play.
    if url and not url.startswith(("http://", "https://")):
        # Be kind to links pasted without a scheme.
        url = "https://" + url
    try:
        from urllib.parse import urlparse
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        host = ""
    if not url.startswith(("http://", "https://")) or not (
            host == "youtube.com" or host.endswith(".youtube.com") or host == "youtu.be"):
        speak("Only YouTube links are supported on the jukebox.")
        return
    speak("Loading YouTube song info...")

    def do_fetch():
        try:
            import yt_dlp
            from . import logger
            # noplaylist: a shared link with &list=... must resolve to the ONE
            # video, not the whole playlist (otherwise the playlist name, a
            # guessed duration, and the playlist URL all get queued as a song).
            ydl_opts = {
                'format': 'bestaudio/best',
                'quiet': True,
                'no_warnings': True,
                'noplaylist': True,
                'extractor_args': {'youtube': {'player_client': ['android', 'web']}},
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
            # A pure /playlist?list= link still resolves to the whole playlist
            # even with noplaylist — queue its first video instead.
            if info and 'entries' in info:
                entries = [e for e in (info.get('entries') or []) if e]
                if not entries:
                    raise ValueError("empty playlist")
                info = entries[0]
            title = info.get('title', 'YouTube Audio')
            duration = int(info.get('duration') or 300)
            webpage_url = info.get('webpage_url') or ""
            if not _is_canonical_youtube(webpage_url) and info.get('id'):
                webpage_url = f"https://www.youtube.com/watch?v={info['id']}"
            if not webpage_url:
                webpage_url = url

            def do_send():
                _queue_song(game, gp, jukebox_id, title, webpage_url, duration)

            game.put(do_send)
        except Exception as ex:
            from . import logger
            logger.log_exception(ex, "Jukebox direct URL load")
            game.put(lambda: speak("Could not load YouTube URL. Please check the link."))

    threading.Thread(target=do_fetch, daemon=True).start()


def _skip_song(game, gp, jukebox_id):
    from . import consts
    game.network.send(consts.CHANNEL_MISC, "jukebox_skip", {"id": jukebox_id})


def _clear_all(game, gp, jukebox_id):
    from . import consts
    game.network.send(consts.CHANNEL_MISC, "jukebox_clear_all", {"id": jukebox_id})


def _stop_playback(game, gp, jukebox_id):
    from . import consts
    game.network.send(consts.CHANNEL_MISC, "jukebox_stop", {"id": jukebox_id})


def _is_jukebox_paused(gp, jukebox_id):
    """Return the authoritative paused flag cached for one jukebox."""
    box = _current_state(gp).get("jukeboxes", {}).get(jukebox_id) or {}
    return bool(box.get("paused", False))


def _pause_menu_label(gp, jukebox_id):
    """Accessible dynamic label for the shared Pause / Resume action."""
    return "Resume playback" if _is_jukebox_paused(gp, jukebox_id) else "Pause playback"


def _toggle_pause(game, gp, jukebox_id):
    """Ask the server to toggle shared playback without trusting client state."""
    from . import consts
    game.network.send(
        consts.CHANNEL_MISC, "jukebox_toggle_pause", {"id": jukebox_id}
    )


def _get_repeat_mode(gp, jukebox_id):
    """The cached server repeat mode for one jukebox (off / one / all)."""
    box = _current_state(gp).get("jukeboxes", {}).get(jukebox_id) or {}
    mode = box.get("repeat") or "off"
    return mode if mode in ("off", "one", "all") else "off"


def _get_eq_profile(gp, jukebox_id):
    """The cached server EQ sound profile for one jukebox."""
    box = _current_state(gp).get("jukeboxes", {}).get(jukebox_id) or {}
    profile = str(box.get("eq_profile") or "normal").lower()
    return profile if profile in ("normal", "bass_boost", "custom") else "normal"


def _get_eq_values(gp, jukebox_id):
    """Return the three accessible 0-100 EQ values for one cabinet."""
    box = _current_state(gp).get("jukeboxes", {}).get(jukebox_id) or {}
    profile = _get_eq_profile(gp, jukebox_id)
    if profile == "bass_boost":
        return {"bass": 100, "mid": 50, "treble": 50}
    if profile != "custom":
        return {"bass": 50, "mid": 50, "treble": 50}
    return JukeboxPlayer._normalize_eq_values(box.get("eq_values"))


def _get_jukebox_volume(gp, jukebox_id):
    """The cached server volume for one jukebox (0-100)."""
    box = _current_state(gp).get("jukeboxes", {}).get(jukebox_id) or {}
    try:
        vol = int(box.get("volume", 100))
        return max(0, min(100, vol))
    except Exception:
        return 100


def _toggle_repeat(game, gp, jukebox_id):
    from . import consts
    order = ("off", "one", "all")
    current = _get_repeat_mode(gp, jukebox_id)
    nxt = order[(order.index(current) + 1) % len(order)]
    # The server speaks the confirmation and the queue update refreshes the
    # cached mode shown by the menu label.
    game.network.send(consts.CHANNEL_MISC, "jukebox_set_repeat", {
        "id": jukebox_id,
        "repeat": nxt,
    })


def _shuffle_queue(game, gp, jukebox_id):
    from . import consts
    game.network.send(consts.CHANNEL_MISC, "jukebox_shuffle", {"id": jukebox_id})


def _get_or_create_jukebox_player(game, gp):
    player = getattr(gp, "jukebox_player", None)
    if player is None:
        player = gp.jukebox_player = JukeboxPlayer(game)
    return player


class _JukeboxVolumeSlider(state.State):
    """Accessible cabinet-volume slider with immediate local preview."""

    SEND_INTERVAL = 0.075

    def __init__(self, game, parent, jukebox_id):
        super().__init__(game, parrent=parent)
        self.jukebox_id = jukebox_id
        self.value = _get_jukebox_volume(parent, jukebox_id)
        self._last_preview_at = 0.0
        self._closed = False

    def enter(self):
        super().enter()
        speak(
            "Jukebox Volume. Up and Down adjust. Page Up and Page Down "
            "adjust by 10. Home sets 100. End sets 0. Enter saves."
        )
        speak(f"Volume. Slider: {self.value}%")

    def exit(self):
        super().exit()
        speak("Jukebox Volume closed.")

    def update(self, events):
        super().update(events)
        for event in events:
            if event.type != pygame.KEYDOWN:
                continue
            key = event.key
            if key in (pygame.K_RETURN, pygame.K_KP_ENTER, pygame.K_ESCAPE):
                self._close_and_commit()
                break
            if key == pygame.K_UP:
                self._set_value(self.value + 1)
            elif key == pygame.K_DOWN:
                self._set_value(self.value - 1)
            elif key == pygame.K_PAGEUP:
                self._set_value(self.value + 10)
            elif key == pygame.K_PAGEDOWN:
                self._set_value(self.value - 10)
            elif key == pygame.K_HOME:
                self._set_value(100)
            elif key == pygame.K_END:
                self._set_value(0)
        return True

    def _set_value(self, value):
        value = max(0, min(100, int(value)))
        if value != self.value:
            self.value = value
            self._apply(preview=True)
        speak(f"{value}%")

    def _apply(self, preview):
        boxes = _current_state(self.parrent).setdefault("jukeboxes", {})
        box = boxes.setdefault(self.jukebox_id, {"id": self.jukebox_id})
        box["volume"] = self.value
        player = getattr(self.parrent, "jukebox_player", None)
        if player is not None:
            player.set_cabinet_volume(self.jukebox_id, self.value)

        now = time.monotonic()
        if preview and now - self._last_preview_at < self.SEND_INTERVAL:
            return
        network = getattr(self.game, "network", None)
        if network is not None:
            from . import consts
            network.send(consts.CHANNEL_MISC, "jukebox_set_volume", {
                "id": self.jukebox_id,
                "volume": self.value,
                "preview": bool(preview),
                "commit": not preview,
            })
            if preview:
                self._last_preview_at = now

    def _close_and_commit(self):
        if self._closed:
            return
        self._closed = True
        self._apply(preview=False)
        self.parrent.pop_last_substate()
        open_jukebox_menu(self.game, self.parrent)


def _open_volume_menu(game, gp, jukebox_id):
    gp.add_substate(_JukeboxVolumeSlider(game, gp, jukebox_id))


def _show_results_menu(game, gp, jukebox_id, results):
    from . import menu as menu_mod, menus

    if not results:
        speak("No results found.")
        return

    def make_callback(idx):
        return lambda: _pick_song(game, gp, jukebox_id, results[idx])

    m = menu_mod.Menu(game, "Search Results", parrent=gp)
    items = []
    for i, r in enumerate(results):
        # int(): yt-dlp flat search returns durations as float, and the
        # '02d' format only accepts ints — a float here used to crash the
        # whole results menu before it opened.
        dur = int(r.get("duration") or 0)
        is_live = r.get("is_live", False) or r.get("live_status") == "is_live"
        if is_live:
            dur_str = "LIVE"
        elif dur:
            dur_str = f"{dur // 60}:{dur % 60:02d}"
        else:
            dur_str = "?"
        title = r.get("title", "Unknown")
        items.append((f"{title} ({dur_str})", make_callback(i)))
    items.append(("Cancel", lambda: gp.pop_last_substate()))
    m.add_items(items)
    menus.set_default_sounds(m)
    gp.add_substate(m)


def _is_canonical_youtube(url):
    """True when the URL is a stable https youtube.com / youtu.be page URL."""
    try:
        from urllib.parse import urlparse
        host = urlparse(url).hostname or ""
        return (url or "").startswith("https://") and (
            host == "youtube.com" or host.endswith(".youtube.com") or host == "youtu.be"
        )
    except Exception:
        return False


def _queue_song(game, gp, jukebox_id, title, url, duration):
    from . import consts
    game.network.send(consts.CHANNEL_MISC, "jukebox_queue_add", {
        "id": jukebox_id,
        "title": title,
        "url": url,
        "duration": duration,
    })
    speak(f"Queued {title} on the jukebox.")


def _pick_song(game, gp, jukebox_id, result):
    gp.pop_last_substate()
    title = result.get("title", "Unknown Song")
    duration = int(result.get("duration") or 0)
    if duration < 5:
        duration = 300
    # Keep the canonical webpage URL in the server-owned queue.  yt-dlp search
    # results also contain a signed googlevideo URL, but that URL expires and
    # may be bound to the requesting client.  Every listener (including a
    # player returning from a match later) must resolve a fresh stream URL.
    url = result.get("webpage_url") or ""
    if not url and result.get("id"):
        url = f"https://www.youtube.com/watch?v={result['id']}"
    if not url and _is_canonical_youtube(result.get("url", "")):
        url = result.get("url")
    if url:
        _queue_song(game, gp, jukebox_id, title, url, duration)
        return
    # Only a signed googlevideo URL remains.  Resolve its canonical page URL in
    # the background so the queued URL never expires mid-song (403).
    direct_url = result.get("url", "")
    if not direct_url:
        speak("Could not resolve the audio stream for that song.")
        return
    speak("Resolving song link...")

    def do_resolve():
        canonical = direct_url
        try:
            import yt_dlp
            ydl_opts = {
                'format': 'bestaudio/best',
                'quiet': True,
                'no_warnings': True,
                'extractor_args': {'youtube': {'player_client': ['android', 'web']}},
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(direct_url, download=False)
                if info:
                    vid = info.get('id') or ""
                    wp = info.get('webpage_url') or ""
                    if wp and _is_canonical_youtube(wp):
                        canonical = wp
                    elif vid:
                        canonical = f"https://www.youtube.com/watch?v={vid}"
        except Exception:
            pass
        game.put(lambda: _queue_song(game, gp, jukebox_id, title, canonical, duration))

    threading.Thread(target=do_resolve, daemon=True).start()


def _view_queue(game, gp):
    from . import menu as menu_mod, menus

    jb = _closest_jukebox(gp)
    if jb is None:
        speak("There is no jukebox available here.")
        return
    jb_state = _current_state(gp).get("jukeboxes", {}).get(jb.get("id"), {})
    current = jb_state.get("current")
    queue = jb_state.get("queue") or []

    m = menu_mod.Menu(game, "Jukebox Queue", parrent=gp)
    items = []
    repeat_names = {"off": "Off", "one": "Repeat one", "all": "Repeat all"}
    items.append((
        f"Repeat mode: {repeat_names.get(jb_state.get('repeat') or 'off', 'Off')}",
        lambda: None,
    ))
    if current:
        items.append((
            f"Now playing: {current.get('title', 'Unknown')} "
            f"(requested by {current.get('queuedBy', '?')})",
            lambda: None,
        ))
    if not queue:
        items.append(("The queue is empty. Search YouTube to add a song.", lambda: None))
    for i, song in enumerate(queue):
        items.append((
            f"{i + 1}. {song.get('title', 'Unknown')} "
            f"(requested by {song.get('queuedBy', '?')})",
            lambda: None,
        ))
    items.append(("Back", lambda: (gp.pop_last_substate(), open_jukebox_menu(game, gp))))
    m.add_items(items)
    menus.set_default_sounds(m)
    gp.add_substate(m)


def _remove_my_song(game, gp):
    from . import menu as menu_mod, menus

    jb = _closest_jukebox(gp)
    if jb is None:
        speak("There is no jukebox available here.")
        return
    jukebox_id = jb.get("id")
    queue = _current_state(gp).get("jukeboxes", {}).get(jukebox_id, {}).get("queue") or []
    me = getattr(getattr(gp, "player", None), "name", "")
    mine = [(i, s) for i, s in enumerate(queue) if s.get("queuedBy") == me]

    def make_callback(idx):
        return lambda: _do_remove(game, gp, jukebox_id, idx)

    m = menu_mod.Menu(game, "Remove My Song", parrent=gp)
    items = []
    if not mine:
        items.append(("You have no songs in the queue.", lambda: None))
    for i, song in mine:
        items.append((song.get("title", "Unknown"), make_callback(i)))
    items.append(("Back", lambda: (gp.pop_last_substate(), open_jukebox_menu(game, gp))))
    m.add_items(items)
    menus.set_default_sounds(m)
    gp.add_substate(m)


def _do_remove(game, gp, jukebox_id, index):
    gp.pop_last_substate()
    from . import consts
    game.network.send(consts.CHANNEL_MISC, "jukebox_queue_remove", {
        "id": jukebox_id,
        "index": index,
    })
    speak("Your song was removed from the queue.")
