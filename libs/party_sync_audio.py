# Party Sync — map-independent audio sinks for session members.
#
# A Party Sync session follows its members across maps (server libs/party_sync.ts
# addresses every leg to a player, and `voice_channel` is assigned once per
# login). Every receive leg in the client, however, is entity-bound:
# event_handeler.process_music_data / process_voice_data look the sender's
# voice_channel up in `gameplay.voice_channels`, a dict filled from THIS map's
# spawn packets and cleared on every parse_map. A guest who walked to another
# map therefore had no entity to play through, and their frames were dropped
# silently; a host who travelled ended the session server-side.
#
# This module is the missing half: one small sink per session member, created
# from the SERVER's session state (channel ids, never display names), holding
# exactly the four things an entity holds for audio —
#   vc_source / radio_source / music_source + a voice and a music compression —
# kept OUT of `voice_channels` (half the client walks that dict expecting real
# map entities, and the map load clears it), and used only when no local entity
# owns the channel. When the member is standing here after all, the entity
# wins and the sink is released, so one member never plays twice.
#
# Rules kept here (each one is a bug that was paid for elsewhere):
#   * Sources and every OpenAL call happen on the MAIN thread only (see the
#     audio-manager note about cross-thread AL usage). `sync`, `tick` and
#     `release_all` are main-thread; the packet paths only READ this table.
#   * A map load must not release anything — that is the entire point — and
#     nothing here is tied to a map: the sink has no position, no soundgroup
#     and no EFX send. It plays the session's own bytes where the listener
#     stands (direct-to-ear), exactly as it would for a member on this map.
#   * Same source flags as the entity path (`party_sync.set_direct_mode` /
#     `set_voice_direct_mode`): spatialize off, relative, direct_channels on.
#   * The music volume is the listener's own Volume Mixer "music" slider, read
#     every frame, exactly like `Entity._party_sync_direct_volume`.
#   * A room is never involved: a sink has no map, so the cinema routing in
#     `_route_music_to_room` is skipped (it already refuses a direct leg).

import contextlib

from . import voice_chat
from .party_sync import party_member_channels


def _music_slider(audio):
    """The listener's own "music" slider as a 0..1 gain, or None.

    One reader for the whole session's music: the sink plays at this gain
    every frame, and the same number is what an entity needs when it takes a
    sink's place (`take_over_from_entity`), so a member cannot sound louder on
    one side of the seam than on the other.
    """
    try:
        return max(0.0, float(
            audio.volume_categories.get("music", [100])[0]
        ) / 100.0)
    except Exception:
        return None


def state_members(state):
    """Every member of `state` as ``{channel_id: {"name", "host"}}``.

    Built from the server's own state (channel ids from the host/guest
    entries), so the sink table is keyed exactly the way the packet paths are.
    Empty without a session.
    """
    members = {}
    if state is None or not getattr(state, "session_id", ""):
        return members
    try:
        members[int(getattr(state, "host_voice_channel"))] = {
            "name": str(getattr(state, "host_name", "") or ""),
            "host": True,
        }
    except (TypeError, ValueError):
        pass
    for guest in (getattr(state, "guests", None) or []):
        if not isinstance(guest, dict):
            continue
        try:
            members[int(guest.get("voice_channel"))] = {
                "name": str(guest.get("name") or ""),
                "host": False,
            }
        except (TypeError, ValueError):
            continue
    return members


class PartyMemberSink:
    """One session member's private audio leg, alive without a map entity.

    Only ever plays one member's channel; the routing in event_handeler asks
    for this object only after `gameplay.voice_channels` had no entity for
    that channel, so an entity always wins and nothing is ever played twice.
    """

    def __init__(self, game, channel, name="", is_host=False):
        self.game = game
        self.channel = int(channel)
        self.name = name
        self.is_host = is_host
        self.vc_source = None
        self.radio_source = None
        self.music_source = None
        self.music_compression = None
        self.vc_compression = None
        # The two flags every other reader tests, so a member sounds the same
        # here as one standing next to the listener.
        self._party_sync_direct = True
        self._party_sync_voice_direct = True
        self._music_last_recv = 0.0
        self._direct_gain = 1.0

    # ── OpenAL (main thread) ────────────────────────────────────────────

    def ensure_sources(self):
        """Create this member's sources + voice decoder. MAIN THREAD ONLY."""
        if self.music_source is not None:
            return True
        audio = getattr(self.game, "audio_mngr", None)
        context = getattr(audio, "context", None)
        if context is None:
            return False
        try:
            (self.vc_source, self.radio_source,
             self.music_source) = context.gen_sources(3)
        except Exception:
            self.vc_source = None
            self.radio_source = None
            self.music_source = None
            return False
        for source in (self.vc_source, self.music_source):
            for apply_ in (
                lambda s=source: setattr(s, "spatialize", False),
                lambda s=source: setattr(s, "relative", True),
                lambda s=source: setattr(s, "position", (0.0, 0.0, 0.0)),
                lambda s=source: setattr(s, "direct_channels", True),
                lambda s=source: setattr(s, "rolloff_factor", 0.0),
            ):
                with contextlib.suppress(Exception):
                    apply_()
        with contextlib.suppress(Exception):
            self.radio_source.position = (0.0, 0.0, 0.0)
            self.radio_source.relative = True
            self.radio_source.gain = 0.7
        with contextlib.suppress(Exception):
            self.music_source.gain = self.music_gain()
        with contextlib.suppress(Exception):
            self.vc_source.gain = self.voice_gain()
        try:
            if self.vc_compression is None:
                self.vc_compression = voice_chat.voice_chat_compression(self.game)
        except Exception:
            self.vc_compression = None
        return self.music_source is not None

    def apply_gains(self):
        """Re-read the listener's own sliders (main thread, every frame).

        The music leg answers to the "music" category (the same slider the
        entity path uses for a party feed, so the volume the listener set is
        the volume they get) and the voice leg stays flat: a session voice is
        not a world position, so nothing may attenuate it by distance.
        """
        if self.music_source is not None:
            with contextlib.suppress(Exception):
                self.music_source.gain = self.music_gain()
        if self.vc_source is not None:
            with contextlib.suppress(Exception):
                self.vc_source.gain = self.voice_gain()

    def music_gain(self):
        vol = _music_slider(getattr(self.game, "audio_mngr", None))
        if vol is None:
            vol = self._direct_gain
        return vol

    def voice_gain(self):
        """A session voice is heard at conversation distance, always."""
        return 1.0

    def release(self):
        """Stop and delete everything this sink owns. MAIN THREAD ONLY."""
        for compression in (self.music_compression, self.vc_compression):
            if compression is None:
                continue
            with contextlib.suppress(Exception):
                compression.close()
        self.music_compression = None
        self.vc_compression = None
        for name in ("vc_source", "music_source", "radio_source"):
            source = getattr(self, name, None)
            setattr(self, name, None)
            if source is None:
                continue
            with contextlib.suppress(Exception):
                source.stop()
            with contextlib.suppress(Exception):
                source.buffer = None
            for _ in range(64):
                if getattr(source, "buffers_queued", 0) <= 0:
                    break
                with contextlib.suppress(Exception):
                    source.unqueue_buffers()
            with contextlib.suppress(Exception):
                source.delete()

    def __repr__(self):
        return (f"PartyMemberSink(channel={self.channel}, name={self.name!r}, "
                f"host={self.is_host}, live={self.music_source is not None})")


class PartySinkSet:
    """The sinks of the local player's session, keyed by voice channel."""

    def __init__(self, game):
        self.game = game
        self._sinks = {}

    # ── lookups used by the packet paths (any thread, read-only) ────────

    def sink_for(self, channel):
        """The sink for `channel`, or None (never creates anything)."""
        try:
            return self._sinks.get(int(channel))
        except (TypeError, ValueError):
            return None

    def channels(self):
        return set(self._sinks.keys())

    def names(self):
        return {c: s.name for c, s in self._sinks.items()}

    # ── lifecycle (main thread) ─────────────────────────────────────────

    def sync(self, gameplay, state=None):
        """Reconcile the sinks with the session and this map's entities.

        Called on every session change and on every map load. A member needs a
        sink exactly while we are in a session with them and they have no
        entity here; the local player never needs one (a client is never sent
        its own voice, and its own music is local).
        """
        if gameplay is None:
            return
        if state is None:
            state = getattr(gameplay, "party_sync", None)
        members = state_members(state)
        own = _own_channel(gameplay)
        entities = getattr(gameplay, "voice_channels", None) or {}
        wanted = {
            channel: meta for channel, meta in members.items()
            if channel != own and channel not in entities
        }
        for channel in list(self._sinks.keys()):
            if channel in wanted:
                continue
            self.release(channel)
        for channel, meta in wanted.items():
            sink = self._sinks.get(channel)
            if sink is None:
                sink = PartyMemberSink(
                    self.game, channel, meta["name"], meta["host"]
                )
                self._sinks[channel] = sink
            else:
                sink.name = meta["name"]
                sink.is_host = meta["host"]
            sink.ensure_sources()

    def ensure_sink(self, gameplay, channel, name="", is_host=False):
        """The sink for `channel`, created NOW if it does not exist.

        MAIN THREAD ONLY (it creates OpenAL sources). The per-frame reconcile
        only ever runs a frame later, and at a seam that frame is the whole
        difference: a member walking to another map still holds their queue in
        their entity, and it can only be carried into a sink that exists.
        Returns None for this client's own channel (a client never holds a sink
        for itself) or a channel that cannot be read.
        """
        try:
            channel = int(channel)
        except (TypeError, ValueError):
            return None
        if channel == _own_channel(gameplay):
            return None
        sink = self._sinks.get(channel)
        if sink is None:
            sink = PartyMemberSink(self.game, channel, name, is_host)
            self._sinks[channel] = sink
        if name:
            sink.name = name
            sink.is_host = is_host
        sink.ensure_sources()
        return sink

    def release(self, channel):
        """Drop one sink and free everything it owns (main thread)."""
        try:
            channel = int(channel)
        except (TypeError, ValueError):
            return None
        sink = self._sinks.pop(channel, None)
        if sink is not None:
            sink.release()
        return sink

    def keep_across_map_load(self, gameplay, state=None):
        """A map load must not end the session or drop a member's sink.

        The sinks have no position, no soundgroup and no EFX send, so the map
        rebuild cannot invalidate them; this re-runs the reconcile (so a member
        who IS standing on the new map hands over to their entity) and leaves
        every other sink exactly as it is. Called from the map-load path in
        place of the old "sessions are map-scoped, so end it" rule.
        """
        self.sync(gameplay, state)

    def tick(self, gameplay=None):
        """Per-frame reconcile + gain maintenance (main thread).

        Reconciles rather than only tuning gains because a member can change
        sides of the session without any packet: a host who walks to another
        map has their entity removed here (their frames would otherwise have
        nothing to play through until the next session event), and a member
        who walks onto this map gains one and must stop playing through their
        sink. Both hand-overs happen on the next frame.
        """
        if gameplay is not None:
            self.sync(gameplay, getattr(gameplay, "party_sync", None))
        elif not self._sinks:
            return
        for sink in self._sinks.values():
            sink.apply_gains()

    def release_all(self):
        for sink in list(self._sinks.values()):
            sink.release()
        self._sinks.clear()

    def __len__(self):
        return len(self._sinks)


def sinks_for(gameplay, game=None):
    """The sink table belonging to `gameplay`, created on first use.

    Kept on the gameplay object because it is per-session state of that map
    view, and reachable from both the packet paths (which only read it) and
    the per-frame tick.
    """
    if gameplay is None:
        return None
    sinks = getattr(gameplay, "_party_sync_sinks", None)
    if sinks is None:
        if game is None:
            game = getattr(gameplay, "game", None)
        sinks = PartySinkSet(game)
        gameplay._party_sync_sinks = sinks
    return sinks


# ── the two seams (main thread) ────────────────────────────────────────
#
# One member, two possible outputs. Crossing between them must not restart the
# song: the frames keep arriving on the same voice channel whichever leg is
# current, so the only thing that has to travel is what the old leg holds.

def hand_over(old_leg, new_leg, game=None):
    """MAIN THREAD ONLY: continue `old_leg`'s audio on `new_leg`.

    Both legs are read exactly the way `EventHandeler._party_audio_receiver`
    reads them -- `vc_source`, `music_source`, `music_compression` -- so an
    entity and a sink are the same thing here. A voice leg has no continuity
    state to carry (its frames play as they arrive and its jitter estimate is
    keyed by channel in voice_chat), so only its queue moves; the music leg
    also adopts the song's clock, which is what keeps remote jam notes on the
    beat across the seam.

    Returns True when something was carried. Never raises: a seam that cannot
    be carried keeps the old behaviour (the new leg pre-buffers).
    """
    if old_leg is None or new_leg is None or old_leg is new_leg:
        return False
    if game is None:
        game = (getattr(old_leg, "game", None)
                or getattr(new_leg, "game", None))
    carried = False
    try:
        moved, _playing = voice_chat.carry_output(
            getattr(old_leg, "vc_source", None),
            getattr(new_leg, "vc_source", None),
        )
        carried = moved > 0
    except Exception:
        pass
    old_compression = getattr(old_leg, "music_compression", None)
    if old_compression is None:
        return carried      # nothing was playing, so nothing to carry
    compression = getattr(new_leg, "music_compression", None)
    if compression is None:
        try:
            compression = voice_chat.MusicCompression(game)
            new_leg.music_compression = compression
        except Exception:
            return carried
    try:
        carried = compression.carry_over(
            old_compression,
            getattr(old_leg, "music_source", None),
            getattr(new_leg, "music_source", None),
        ) > 0 or carried
    except Exception:
        pass
    return carried


def take_over_from_entity(gameplay, entity, channel, game=None):
    """MAIN THREAD ONLY: a session member's entity is leaving this map.

    Called from the entity-removal path (before the entity's sources are
    destroyed): a member who travels to another map keeps the session -- the
    frames keep arriving, addressed to their voice channel -- so what their
    entity still holds has to cross into the sink that will play it. A channel
    that is not a session member is left alone (their audio simply ends, as it
    always has).
    """
    if gameplay is None or entity is None:
        return False
    try:
        channel = int(channel)
    except (TypeError, ValueError):
        return False
    members = state_members(getattr(gameplay, "party_sync", None))
    if channel not in members:
        return False
    sinks = sinks_for(gameplay, game)
    if sinks is None:
        return False
    sink = sinks.ensure_sink(gameplay, channel, members[channel]["name"],
                             members[channel]["host"])
    if sink is None:
        return False
    return hand_over(entity, sink, game or getattr(gameplay, "game", None))


def hand_back_to_entity(gameplay, entity, channel, game=None):
    """MAIN THREAD ONLY: this entity is a session member's leg from now on.

    Called from the spawn path for every player entity. Two jobs, and the
    FIRST one is not about the sink at all:

    1. Put the entity on the session's legs (`_apply_session_modes`), whether
       or not a sink was ever built. A sink is only one way to arrive here: a
       MAP LOAD or a return from another map replaces every entity in this
       table, and the roster does not change to say the new ones are session
       members -- so an entity left off the legs plays the host's song as a 3D
       boombox standing at their body (nothing past `ENTITY_MUSIC_MAX_DISTANCE`
       50 tiles), and the listener reports a silent party while the host hears
       their own music perfectly. Asking for a sink here would make the legs
       conditional on a race nobody can see: the per-frame reconcile only ever
       builds that sink if it happens to run between the map's clear and this
       spawn.

    2. Carry the sink's queue and the song's clock into the entity, if there
       was a sink, and release it. The sink was a direct-to-ear feed -- no
       position, nothing spatializing it -- and the entity has to take those
       legs over with the audio or the song changes character mid-phrase.
    """
    if gameplay is None or entity is None:
        return False
    try:
        channel = int(channel)
    except (TypeError, ValueError):
        return False
    _apply_session_modes(gameplay, entity, channel,
                         getattr(gameplay, "party_sync", None))
    sinks = sinks_for(gameplay, game)
    if sinks is None:
        return False
    sink = sinks.sink_for(channel)
    if sink is None:
        return False
    carried = hand_over(sink, entity, game or getattr(gameplay, "game", None))
    sinks.release(channel)
    return carried


def _apply_session_modes(gameplay, entity, channel, state):
    """MAIN THREAD ONLY: put ONE entity on the session's direct legs.

    The same two rules `EventHandeler._sync_party_sync_direct_audio` applies to
    every entity at once, asked for the entity that is taking a sink's place.
    That bulk sync runs on a session event or a map load and not on a spawn, so
    without this a member who walks onto the map would keep playing a guest's
    music positionally -- and the output format the packet path derives from
    that flag would flush the very queue just carried across.
    """
    if state is None:
        return
    # The local player's own entity is never on a session leg: nobody sends it
    # audio (a client is never sent its own voice, and its own music is local),
    # and `EventHandeler._sync_party_sync_direct_audio` skips it for exactly
    # this reason. Without the guard a freshly spawned local entity would be
    # flagged and later "restored" by `clear_direct_mode` as if it had ever
    # been positional.
    if getattr(entity, "is_user", False):
        return
    from .party_sync import set_direct_mode, set_voice_direct_mode
    if (getattr(state, "role", None) == "guest"
            and channel == getattr(state, "host_voice_channel", None)):
        vol = _music_slider(getattr(getattr(gameplay, "game", None),
                                    "audio_mngr", None))
        set_direct_mode(entity, 1.0 if vol is None else vol)
    try:
        if channel in party_member_channels(state):
            set_voice_direct_mode(entity)
    except Exception:
        pass


def _own_channel(gameplay):
    """This client's own voice_channel.

    The login snapshot carries it (`own_voice_channel`); when that is missing
    the local entity answers it, because our own entity is in this map's
    channel table like everybody else's (`is_user` is only ever true there).
    A client is never sent its own voice and its own music is local, so it
    must never hold a sink for itself.
    """
    own = getattr(gameplay, "own_voice_channel", None)
    try:
        if own is not None:
            return int(own)
    except (TypeError, ValueError):
        pass
    entities = getattr(gameplay, "voice_channels", None) or {}
    for channel, entity in entities.items():
        if getattr(entity, "is_user", False):
            try:
                return int(channel)
            except (TypeError, ValueError):
                break
    player = getattr(gameplay, "player", None)
    for attr in ("voice_channel", "channel_id"):
        value = getattr(player, attr, None)
        try:
            if value is not None:
                return int(value)
        except (TypeError, ValueError):
            continue
    return None


def member_is_local(gameplay, channel):
    """Whether a session member is standing on this map (has an entity here)."""
    if gameplay is None or channel is None:
        return False
    try:
        channel = int(channel)
    except (TypeError, ValueError):
        return False
    return channel in (getattr(gameplay, "voice_channels", None) or {})
