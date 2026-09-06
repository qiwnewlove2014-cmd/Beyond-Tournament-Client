"""Queue-advance jam-sync simulation.

Models the exact scenario the user reports: song 1 plays in sync, the queue
advances to song 2, and then everyone's instrument notes land several
seconds behind the beat until the queue is reset.

Drives the REAL production math:
  * AudioStreamer.direct_start_deadline / direct_seek_seconds (direct mode)
  * EventHandeler._schedule_remote_note + _active_jukebox_buffer_ms (relay
    and anchored-direct branches), via the real classes with fake OpenAL.

Run:  python tools/jukebox_queue_advance_sim.py
"""

import os
import sys
import threading
import time
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from libs.event_handeler import EventHandeler
from libs.jukebox import JukeboxRelayReceiver
from libs.music_bot import AudioStreamer


class FakeGame:
    def __init__(self):
        self.after_calls = []

    def call_after(self, ms, callback):
        self.after_calls.append((ms, callback))


class FakeSource:
    def __init__(self):
        self.buffers_queued = 0


class Machine:
    """One listener/performer machine in the room.

    transport: "relay" or "direct". For relay the audible start is the
    worker-ready broadcast instant plus jitter; the OpenAL queue depth sits
    at a per-machine steady value. For direct, the real anchor math decides
    the audible start (hold to the shared deadline; late_s when the resolve
    overruns it).
    """

    def __init__(self, name, latency=0.05, resolve=2.0, startup=0.8,
                 relay=False, queue_frames=10, lead_in=None, join=False):
        self.name = name
        self.latency = latency
        self.resolve = resolve
        self.startup = startup
        self.relay = relay
        self.queue_frames = queue_frames
        # Lead-in this machine's direct anchor holds (None = room default).
        self.lead_in = lead_in
        # True = per-listener direct fallback into an already-playing room:
        # the stream must seek (join) instead of starting from position 0.
        self.join = join
        self.audible_at = None          # wall time song position 0 is heard
        self.seek_to = 0.0
        self.late_s = 0.0
        self.play_started = False

    # ---- event handling ----

    def receive_play(self, server_now, start_offset):
        received_at = server_now + self.latency
        if self.relay:
            # Relay room: the machine hears the first frame almost
            # immediately after the ready broadcast.
            self.audible_at = received_at + 0.05
            self.seek_to = 0.0
            self.late_s = 0.0
            self.play_started = True
            return
        # Anchored direct playback: real alignment math.
        spawn_at = received_at + self.resolve
        if self.join:
            seek = AudioStreamer.direct_seek_seconds(
                max(0.001, start_offset), received_at, spawn_at)
            self.seek_to = seek if seek > 0.5 else 0.0
        elif start_offset > AudioStreamer.DIRECT_FRESH_MAX_S:
            seek = AudioStreamer.direct_seek_seconds(
                start_offset, received_at, spawn_at)
            self.seek_to = seek if seek > 0.5 else 0.0
        else:
            self.seek_to = 0.0
        ready_at = spawn_at + self.startup
        deadline = AudioStreamer.direct_start_deadline(
            start_offset, received_at, self.seek_to, lead_in=self.lead_in)
        hold = min(AudioStreamer.DIRECT_MAX_ALIGN_WAIT_S,
                   max(0.0, deadline - ready_at))
        self.audible_at = ready_at + hold
        self.late_s = max(0.0, self.audible_at - deadline)
        self.play_started = True

    def hear_time(self, content_position):
        return self.audible_at + (content_position - self.seek_to)

    # ---- measurement (== _active_jukebox_buffer_ms on this machine) ----

    def buffer_ms(self):
        if not self.play_started:
            return None
        if self.relay:
            return max(self.queue_frames, 1) * 40
        return max(5, 1) * 20 + int(max(0.0, self.late_s) * 1000)


def run_note(performer, listener, beat, use_sender_lag=True):
    """Song-position error (seconds) of a remote note at the listener.

    Performer strikes the beat they HEAR; the server re-stamps it after the
    performer's latency; the listener schedules it through the real
    _schedule_remote_note with the real measurement formula. Returns heard
    position minus intended beat (positive = note lands late).
    """
    t_press = performer.hear_time(beat)
    t_stamp = t_press + performer.latency
    t_arrive = t_stamp + listener.latency

    handler = EventHandeler.__new__(EventHandeler)
    handler.gameplay = SimpleNamespace(jukebox_player=SimpleNamespace(
        players={"box": {"transport": "relay" if listener.relay else "direct",
                         "streamer": _fake_streamer(listener)}}))
    game = FakeGame()
    handler.game = game
    handler._clock_offset_ms = 0.0
    handler._clock_offset_samples = 10
    handler._last_jam_sync_log = 0.0

    enqueue = []
    packet = {"server_time": t_stamp * 1000.0}
    if use_sender_lag:
        lag = performer.buffer_ms()
        if lag is not None and lag > 0:
            packet["sender_lag_ms"] = int(lag)

    real_time = time.time
    with _patch_time(t_arrive):
        import unittest.mock as mock
        with mock.patch.object(EventHandeler, "_active_jukebox_buffer_ms",
                               return_value=listener.buffer_ms()), \
                mock.patch("libs.event_handeler.time.time",
                           return_value=t_arrive), \
                mock.patch("libs.logger.log"):
            handler._schedule_remote_note(packet, lambda: enqueue.append(1))

    if enqueue or not game.after_calls:
        play_at = t_arrive
    else:
        play_at = t_arrive + game.after_calls[0][0] / 1000.0
    return play_at - listener.hear_time(beat)


def _fake_streamer(machine):
    ready = threading.Event()
    if machine.play_started:
        ready.set()
    if machine.relay:
        cls = type("FakeReceiver", (JukeboxRelayReceiver,), {})
        streamer = cls.__new__(cls)
        streamer.running = True
        streamer._play_started = machine.play_started
        streamer.source_l = SimpleNamespace(
            buffers_queued=machine.queue_frames)
        return streamer
    return SimpleNamespace(
        _direct_anchor=True,
        running=True,
        ready_event=ready,
        direct_late_s=machine.late_s,
        spatial_src_l=SimpleNamespace(buffers_queued=5),
        source=SimpleNamespace(buffers_queued=5),
    )


class _patch_time:
    def __init__(self, fixed):
        self.fixed = fixed

    def __enter__(self):
        self.orig = time.time
        time.time = lambda: self.fixed
        return self

    def __exit__(self, *exc):
        time.time = self.orig
        return False


def sim_room(label, machines, beats, use_sender_lag=True):
    print(f"\n=== {label} ===")
    server_now = 1000.0
    for machine in machines:
        machine.receive_play(server_now, 0.0)
    performer = machines[0]
    worst = 0.0
    for beat in beats:
        for listener in machines[1:]:
            err = run_note(performer, listener, beat, use_sender_lag)
            worst = max(worst, abs(err))
            print(f"  beat {beat:>6.1f}s -> {listener.name:>10s}: "
                  f"note error {err:+7.3f}s")
    print(f"  worst |error| = {worst:.3f}s")
    return worst


def main():
    beats = [5.0, 60.0, 120.0]

    # --- Song 1: all-relay room (server relay enabled) ---
    room1 = [
        Machine("host", latency=0.03, relay=True),
        Machine("guestA", latency=0.06, relay=True),
        Machine("guestB", latency=0.10, relay=True),
    ]
    sim_room("SONG 1 - relay room (works)", room1, beats)

    # --- Song 2: queue advance, same relay room, fresh worker ---
    # The advance re-broadcasts jukebox_play at EOF; the new worker becomes
    # ready ~2s later and every machine starts the new receiver together.
    room2 = [
        Machine("host", latency=0.03, relay=True),
        Machine("guestA", latency=0.06, relay=True),
        Machine("guestB", latency=0.10, relay=True),
    ]
    sim_room("SONG 2 - relay room after queue advance", room2, beats)

    # --- Song 2 with one machine on client-side DIRECT FALLBACK ---
    # The watchdog switched the guest to direct playback (sticky 10 min)
    # while the host and the other guest still hear the server relay.
    room3 = [
        Machine("host", latency=0.03, relay=True),
        Machine("guestA(fb)", latency=0.06, relay=False,
                resolve=2.0, startup=0.8),
        Machine("guestB", latency=0.10, relay=True),
    ]
    sim_room("SONG 2 - MIXED room: one client fell back to direct (OLD: lead-in hold)",
             room3, beats)

    # Same mixed room with the FIX: the fallback machine anchors with
    # room_lead_in_s=0.0 AND joins the playing room via a seek (even a
    # fresh-song event seeks past the projected audible start).
    room3b = [
        Machine("host", latency=0.03, relay=True),
        Machine("guestA(fb)", latency=0.06, relay=False,
                resolve=2.0, startup=0.8, lead_in=0.0, join=True),
        Machine("guestB", latency=0.10, relay=True),
    ]
    sim_room("SONG 2 - MIXED room: fallback anchored lead_in=0 + join (FIXED)",
             room3b, beats)

    # Slow fallback machine whose startup outruns the 4.5s estimate: its
    # own overrun is measured by direct_late_s and reported as sender lag
    # (the residual is the machine's own trail — physics, not a bug).
    room3c = [
        Machine("host", latency=0.03, relay=True),
        Machine("guestA(fb)", latency=0.06, relay=False,
                resolve=2.0, startup=6.5, lead_in=0.0, join=True),
        Machine("guestB", latency=0.10, relay=True),
    ]
    sim_room("SONG 2 - MIXED room: SLOW fallback lead_in=0 + join (FIXED)",
             room3c, beats)

    # --- Song 2, direct-transport room (relay disabled server-side) ---
    room4 = [
        Machine("host", latency=0.03, relay=False, resolve=1.6, startup=0.6),
        Machine("guestA", latency=0.06, relay=False, resolve=2.2, startup=0.9),
        Machine("guestB", latency=0.10, relay=False, resolve=6.5, startup=1.0),
    ]
    sim_room("SONG 2 - pure direct room (server relay disabled)",
             room4, beats)

    # --- Song 2, direct room, but the sender's lag report is missing ---
    # (mixed-version client / measurement returned None at strike time)
    room5 = [
        Machine("host", latency=0.03, relay=False, resolve=6.5, startup=1.0),
        Machine("guestA", latency=0.06, relay=False, resolve=1.6, startup=0.6),
    ]
    sim_room("SONG 2 - direct room, sender WITHOUT lag report",
             room5, beats, use_sender_lag=False)

    # --- The "reset queue and play again" case: fresh song, fresh broadcast,
    #     all machines re-anchor from the same start_offset=0 ---
    room6 = [
        Machine("host", latency=0.03, relay=True),
        Machine("guestA", latency=0.06, relay=True),
        Machine("guestB", latency=0.10, relay=True),
    ]
    sim_room("RESET - fresh song after queue reset", room6, beats)


if __name__ == "__main__":
    main()