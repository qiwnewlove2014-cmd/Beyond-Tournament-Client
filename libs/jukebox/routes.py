"""The route bookkeeping: what to do with frames that arrive before play() does.

Relay frames are decoded on the network thread, but a jukebox_play setup event is deferred to
the main loop. Between the two, a cabinet has an identity and no receiver, and without a
buffer the song's first slices are dropped and songs start mid-word. That buffer is
`JukeboxPlayer._relay_pending`, and this module is everything that keeps it honest:

* `pend_relay_route` reserves the route at the control packet, before the play event.
* `receive_relay_packet` routes a frame to the live receiver, or into the buffer when there
  is none yet -- bounded, oldest first, because an intro is worth keeping and a backlog is not.
* `_relay_pending_keeps_a_live_stream` and `_relay_pending_placeholder` are what a
  relay_pending offer means: leave a working stream alone (make-before-break), or leave an
  entry that knows where the song is and when it started waiting.

The buffer is pruned by `_drop_stale_relay_routes` in `recovery.py`, which is the watchdog
that runs the sweep; the TTL it reads is that module's too.

It is a base class of `JukeboxPlayer` and not a helper, for the same reason the watchdog is:
these methods read `self.relay_routes`, `self._relay_pending`, `self.players` and `self._lock`
exactly as they did inside the class. A test that patches a name this code READS must patch it
here, in the module that owns it (`libs.jukebox.routes.log_line`, ...), or the patch reaches
nothing.
"""

import struct
import time

from ..deferred_log import log_deferred as log_line
from .relay import JukeboxRelayReceiver


class RelayRoutes:
    """The route a cabinet's frames take: live receiver first, buffer until play()."""

    def _relay_pending_keeps_a_live_stream(self, jukebox_id, playback_key, transport, title):
        """True when a pending relay offer must leave the working stream alone.

        Make-before-break: map reloads re-offer the relay while the listener is
        playing direct happily -- stopping that stream made ~10 s of silence
        until the relay was ready -- and a live receiver whose retry notice races
        in may still be streaming. The ready event or a watchdog switches it over.
        """
        # Make-before-break: a relay_pending event must not kill a working stream for the same song.
        # Map reloads re-offer the relay while the listener is playing direct happily - stopping that
        # stream made ~10 s of silence until the relay was ready - and a live receiver whose retry
        # notice races in may still be streaming. The ready event or the watchdogs switch it over.
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
                return True
        return False

    def _relay_pending_placeholder(self, jukebox_id, transport, title, url, playback_key,
                                   play_params, start_offset, anchor_at, playback_id):
        """True when a pending relay offer leaves a placeholder entry behind.

        The entry has no streamer yet and still has to know where the song is the
        moment a note is played along to it, which is what ``_song_position_keys``
        is for. Without ``created_at`` the watchdog computed this placeholder's
        age as 0 on every frame, so one whose follow-up relay event never landed
        waited silently forever (the "must relog after a map reload" bug).
        """
        if transport == "relay_pending":
            with self._lock:
                self.players[jukebox_id] = {
                    "source": None, "secondary_source": None, "streamer": None,
                    "title": title, "url": url, "transport": transport,
                    "playback_key": playback_key,
                    "play_params": play_params,
                    # The song's own position (see _song_position_keys): a relay_pending entry has no
                    # streamer yet, and it still has to know where the song is the moment a note is played
                    # along to it.
                    **self._song_position_keys(start_offset, anchor_at),
                    # Without a timestamp the update() watchdog computed this placeholder's age as 0 on
                    # every frame, so a placeholder whose follow-up relay event never landed waited
                    # silently forever (the "must relog after a map reload" bug).
                    "created_at": time.monotonic(),
                }
            log_line(
                f"[Jukebox] waiting for server relay {title!r} "
                f"playback={playback_id!r}"
            )
            return True
        return False

    def pend_relay_route(self, relay_id, stream_epoch):
        # Reserve a relay route BEFORE play() registers the real receiver: relay frames are processed
        # on the network thread while jukebox_play setup is deferred to the main loop, so without a
        # pending buffer the song's first few 40 ms slices were dropped and songs started mid-way.
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
