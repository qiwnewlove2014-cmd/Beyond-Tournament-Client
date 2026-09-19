"""Two machines, one song: where does a live note land in the music?

Every question about jam-note sync ends in the same sentence -- *do the two
machines put the note on the same music?* -- and the answer cannot be heard
from one machine, cannot be read off the code, and cannot be settled by a
single client's log line. So this rig asks it directly: the same song on two
(and three) machines, a note struck by one performer, and the number that
matters for every listener -- the music position the note came out at, against
the position the performer heard when they struck.

Nothing about the timing rule is re-implemented here. Only the OpenAL half is
modelled: how many frames a machine has in hand, how many its ear has had, and
whether its output is playing. Everything above that is the game's own code --
the receiver's fed counter and queue (``pair_frames_fed``,
``buffers_queued``), the measurement ``EventHandeler._active_jukebox_buffer_ms``
makes off them, the payload ``Gameplay._attach_jukebox_sender_lag`` sends, the
hold ``EventHandeler._schedule_remote_note`` places, and the clock that hold is
re-projected on every gameplay frame (``libs/jukebox_clock.py``).

Model of one machine, in the terms the code uses:

    music_ms        the music position this machine's ear has reached
    stage_ms        the music this output is holding ahead of its ear: what
                    ``buffers_queued`` reports, and the only part of its
                    distance the arithmetic counts
    transit_ms      what it is behind the song that NOBODY measures: frames
                    in flight from the server plus the server's own send-ahead
    frame_ms        the transport's frame (relay receives 40 ms, direct
                    decodes 20 ms) -- what the queue is counted in
    tail_ms         what one of this machine's own notes really costs to
                    sound after the play call
    measured_tail   what this machine has measured of itself (0 = never)

A machine's real distance behind the song is therefore ``transit_ms`` plus
the staged music (plus the direct path's own late start, which the code *does*
measure), and the second half of that is the only half it can see.

The two errors the rig exists to separate:

  * the queue is fully accounted for -- the hold is re-projected on it every
    frame, so a queue that sheds or holds moves the *wall instant* and leaves
    the *music* alone (that is what the clock was built for, and this proves
    it);
  * the queued distance is only part of a machine's real distance behind the
    song, and the part that is left is exactly what lands the note off the
    beat: it can be a frame (the transport's resolution), or a whole queue
    window (a performer whose own measurement answered nothing).

Run it: ``python tests/two_machine_jam_sim.py`` (all scenarios, one table) or
``python tests/two_machine_jam_sim.py honest sender_missing`` (named ones).
The numbers are ms of music: positive means the note came out *after* the beat
the performer heard -- the band is behind them.

Resolution, so a number is read for what it is: a note is spawned on a pump
(the gameplay frame, 20 ms here), the pair's own reading of its position moves
one frame at a time (40 ms on relay, 20 ms on direct), and the world ticks at
5 ms. A single note therefore carries up to about one relay frame of grain;
each row is the mean of ``STRIKES`` notes struck at different phases of that
grid, which is what makes the fifth millisecond in the tables mean anything.
"""

import contextlib
import os
import sys
import threading
from types import SimpleNamespace
from unittest import mock

import cyal

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from libs.event_handeler import EventHandeler
from libs.gameplay import Gameplay
from libs.jukebox_relay import JukeboxRelayReceiver
from libs.music_bot.streaming import AudioStreamer

CABINET = "cabinet"
RELAY_FRAME_MS = 40.0          # a relay frame: what one is worth on the wire
DIRECT_FRAME_MS = 20.0         # a decoded direct buffer
GAME_FRAME_MS = 20.0           # the pump cadence: one gameplay frame
TICK_MS = 5.0                  # the world's own resolution
STRIKES = 12                   # notes per scenario, at different grid phases
WARMUP_MS = 4000.0             # the song plays this long before anyone plays
NOTE_TRANSIT_MS = 30.0         # one note's own trip to a listener
RUN_LIMIT_MS = 20000.0         # a run may not go on longer than this


class Clock:
    """The wall clock, moved by the world and by nothing else (in ms).

    The world counts in milliseconds -- the unit every number in this rig is
    read in -- and answers the game's two clocks in the seconds they want.
    """

    def __init__(self, start_ms=5000.0):
        self.now = float(start_ms)

    def tick(self, ms):
        self.now += ms

    def ms(self):
        return self.now

    def monotonic(self):
        return self.now / 1000.0

    def time(self):
        return self.now / 1000.0


@contextlib.contextmanager
def world_clock(clock):
    """Make the game's own clock the fake one, for the whole run.

    ``SpotClock`` (the wait) reads ``libs.jukebox_clock.time.monotonic`` and
    the scheduler reads ``libs.event_handeler.time.time``, so both have to
    move together -- a wait measured on one and stamped on the other would be
    measuring nothing.
    """
    with mock.patch("libs.jukebox_clock.time.monotonic", clock.monotonic):
        with mock.patch("libs.event_handeler.time.time", clock.time):
            yield


class Source:
    """The song as the cabinet plays it: one music timeline, one rate."""

    def __init__(self, start_ms):
        self.start_ms = float(start_ms)

    def ms_at(self, when_ms):
        return max(0.0, when_ms - self.start_ms)


class FakeSource:
    """Everything OpenAL ever tells the clock: frames queued, and playing."""

    def __init__(self):
        self.buffers_queued = 0
        self.state = cyal.SourceState.PLAYING


class Machine:
    """One client: the music it is holding, and the numbers its code reads.

    ``transit_ms`` is deliberately a *separate* knob from the queue: that is
    the whole point of the rig. The queue is what ``buffers_queued`` reports
    and what the game counts; the in-flight delay is the same distance that
    nobody can see. A machine with a slow line has both, and only one of them
    is in the arithmetic.
    """

    def __init__(self, name, *, transport="relay", transit_ms=60.0,
                 stage_ms=200.0, playing=True, tail_ms=45.0,
                 measured_tail_ms=None, late_ms=0, running=True):
        self.name = name
        self.source = None
        self.transport = transport
        self.frame_ms = RELAY_FRAME_MS if transport == "relay" else DIRECT_FRAME_MS
        # What this machine is behind the song and nobody counts: frames in
        # flight from the server plus the server's own send-ahead. The staged
        # queue below is a *different*, smaller number -- which is the whole
        # point of the rig.
        self.transit_ms = float(transit_ms)
        # The music this output is holding ahead of its ear (the queue).
        self.stage_ms = max(self.frame_ms, float(stage_ms))
        # What one of this machine's own notes really costs to sound after the
        # play call, and what this machine has measured of that. ``None`` for
        # the measured figure means "it measured the truth"; a number models a
        # machine that has measured something else (a fast machine, or one
        # that has never measured a note here).
        self.tail_ms = float(tail_ms)
        self.measured_tail_ms = (self.tail_ms if measured_tail_ms is None
                                 else float(measured_tail_ms))
        self.late_ms = float(late_ms)
        self.running = bool(running)
        self.playing = bool(playing)
        self.music_ms = 0.0
        self.hold_ms = 0.0
        self.streamer = None
        self._pair_sources = []
        # Clocks of outputs that were replaced while a note was waiting on
        # them, mirroring ``JukeboxPlayer``: it keeps them
        # (``_keep_waiting_notes``) and pumps them every gameplay frame
        # (``_pump_orphan_clocks``), which is the only reason a note survives
        # its own output going away.
        self.orphan_clocks = []
        self._build()

    # ----------------------------------------------------------- distances

    @property
    def queued_frames(self):
        """What ``buffers_queued`` would report: the staged music in frames."""
        return max(1, int(round(self.stage_ms / self.frame_ms)))

    @property
    def behind_ms(self):
        """How far this machine's ear is behind the song -- measured and not.

        The staged frames are what the client can see; the direct path's late
        start is measured on top of them (``_active_jukebox_buffer_ms`` adds
        it); the in-flight delay is the part nothing counts.
        """
        return (self.transit_ms + self.queued_frames * self.frame_ms
                + self.late_ms)


    # ------------------------------------------------------------- building

    def _build(self):
        """The real streamer object for this transport, hand-built as tests do."""
        if self.transport == "relay":
            streamer = JukeboxRelayReceiver.__new__(JukeboxRelayReceiver)
            streamer.cinema = None
            streamer.running = self.running
            streamer._stopped = False
            streamer._play_started = True
            streamer.paused = False
            streamer.source_l = FakeSource()
            streamer.source_r = FakeSource()
            self._pair_sources = [streamer.source_l, streamer.source_r]
        else:
            streamer = AudioStreamer.__new__(AudioStreamer)
            streamer.cinema = None
            streamer.running = self.running
            streamer.paused = False
            streamer.spatial_pair = (object(), object(), 1.0, 40.0)
            streamer.spatial_src_l = FakeSource()
            streamer.spatial_src_r = FakeSource()
            streamer.source = FakeSource()
            streamer.ready_event = threading.Event()
            streamer.ready_event.set()
            streamer._direct_anchor = True
            streamer.direct_late_s = self.late_ms / 1000.0
            self._pair_sources = [streamer.spatial_src_l, streamer.spatial_src_r]
        streamer.pair_frames_fed = 0
        streamer._pair_frame_ms = self.frame_ms
        self.streamer = streamer

    # ------------------------------------------------------------- the world

    def sync(self, now):
        """Say what this output holds right now, in the numbers the code reads.

        ``pair_frames_fed`` is what has reached the pair, ``buffers_queued`` is
        the staged music still ahead of the ear, and the difference -- the
        frames played -- is the content instant the game measures a live note
        against on every gameplay frame.
        """
        played = int(self.music_ms // self.frame_ms)
        queued = self.queued_frames
        self.streamer.pair_frames_fed = played + queued
        for source in self._pair_sources:
            source.buffers_queued = queued
            source.state = (cyal.SourceState.PLAYING if self.playing
                            else cyal.SourceState.STOPPED)

    def play(self, dt_ms, now):
        """Play ``dt_ms`` of the song, or freeze -- a hold, an underrun."""
        if not self.playing:
            return
        if self.hold_ms > 0.0:
            self.hold_ms = max(0.0, self.hold_ms - dt_ms)
            return
        self.music_ms = min(self.music_ms + dt_ms,
                            self.source.ms_at(now - self.behind_ms))

    def pump(self):
        """One gameplay frame: the pair's own clock fires what it has reached.

        The current output's clock and the ones the player kept when an output
        was replaced, exactly as ``JukeboxPlayer.update`` pumps both.
        """
        clock = getattr(self.streamer, "pair_clock", None)
        if clock is not None:
            clock.pump_waits()
        still = []
        for orphan in self.orphan_clocks:
            orphan.pump_waits()
            if orphan.pending_waits():
                still.append(orphan)
        self.orphan_clocks[:] = still

    # ------------------------------------------------------- what can move

    def shed(self, frames):
        """Drop ``frames`` from the queue: the client skips that much music.

        The ear jumps forward by those frames and the output is left holding
        that much less -- a realign, or a queue the receiver cut back.
        """
        skipped = max(0, int(frames)) * self.frame_ms
        self.stage_ms = max(self.frame_ms, self.stage_ms - skipped)
        self.music_ms += skipped

    def hold(self, ms):
        """The output stops playing for ``ms`` while frames keep arriving.

        The ear does not move, so the machine is left that much deeper behind
        the song -- and holding that much more music -- for good: both sides
        of it then move at the same rate.
        """
        self.hold_ms = max(self.hold_ms, float(ms))
        self.stage_ms += float(ms)

    def switch_to_direct(self):
        """The relay goes away and the direct streamer takes the song over.

        The hold was measured on the *relay's* queue and its wait stays on
        that receiver's clock; a streamer that is no longer running is not a
        clock (``SpotClock`` keeps the wall target instead), so the note fires
        at the instant it was always going to. The music itself is continuous:
        the pair swap keeps the content instant.
        """
        if self.transport == "direct":
            return
        clock = getattr(self.streamer, "pair_clock", None)
        if clock is not None and clock.pending_waits():
            self.orphan_clocks.append(clock)
        self.streamer.running = False
        self.streamer._stopped = True
        self.transport = "direct"
        self.frame_ms = DIRECT_FRAME_MS
        self.stage_ms = max(DIRECT_FRAME_MS, self.stage_ms)
        self._build()


class World:
    """The song, the machines, and the one wall clock they all read."""

    def __init__(self, machines, *, phase_ms=0.0, note_transit_ms=NOTE_TRANSIT_MS,
                 warmup_ms=WARMUP_MS):
        self.clock = Clock(start_ms=5000.0 + phase_ms)
        self.source = Source(self.clock.now)
        self.machines = {machine.name: machine for machine in machines}
        for machine in machines:
            machine.source = self.source
        self.note_transit_ms = float(note_transit_ms)
        self.notes = []
        self._handlers = {}
        self._events = []
        self._timers = []
        self._last_pump = None
        self._warmup_ms = float(warmup_ms)

    # ------------------------------------------------------------- the code

    def handler(self, machine):
        """A real ``EventHandeler`` for one machine, on a world it can measure.

        Only the two room questions are answered for it (this rig is the plain
        cabinet's pair -- a room's clock is the same ``SpotClock``, and the
        room's own behaviour is pinned by ``test_jam_note_room_clock``), and
        the instruments it asks for their measured spawn cost are this
        machine's own.
        """
        handler = getattr(machine, "_handler", None)
        if handler is not None:
            return handler
        entry = {"streamer": machine.streamer}
        handler = EventHandeler.__new__(EventHandeler)
        handler.gameplay = SimpleNamespace(
            jukebox_player=SimpleNamespace(players={CABINET: entry}),
            game=None)
        handler.game = SimpleNamespace(
            audio_mngr=SimpleNamespace(
                piano=SimpleNamespace(note_spawn_ms=lambda: machine.measured_tail_ms)),
            call_after=lambda ms, fn: self._timers.append(
                (self.clock.ms() + float(ms), fn)))
        handler._clock_offset_ms = 0.0
        handler._clock_offset_samples = 10
        handler._last_jam_sync_log = 0.0
        handler._jam_buffer_kind = None
        handler._jam_buffer_detail = None
        handler._jam_streamer = None
        handler._note_song_cabinet = lambda position, peer=None: CABINET
        handler._note_room_bank = lambda cabinet: None
        handler._report_room_note_latency = lambda *args: None
        machine._handler = handler
        return handler

    def _sender_lag(self, performer, packet, *, measure=True):
        """The payload the game sends, built by the game's own code.

        ``measure=False`` is a performer whose own client cannot answer at
        that instant -- a stream mid-rebuild, still pre-buffering, or handing
        its output to a room -- which is the one case that leaves
        ``sender_lag_ms`` out of the packet entirely.
        """
        fake = SimpleNamespace(
            player=SimpleNamespace(name=performer.name, x=1.0, y=2.0, z=0.0),
            game=SimpleNamespace(network=SimpleNamespace(
                event_handeler=self.handler(performer))))
        was = performer.streamer.running
        if not measure:
            performer.streamer.running = False
        try:
            Gameplay._attach_jukebox_sender_lag(fake, packet)
        finally:
            performer.streamer.running = was
        return packet

    # ------------------------------------------------------------- the note

    def strike(self, performer_name, *, listeners=None, measure=True,
               events=()):
        """Strike one note and register it on every listener.

        The beat is the music the *performer* is hearing at this instant --
        what they play against, and the position every other machine has to
        put the note on. ``events`` are ``(offset_ms, callable)`` pairs that
        run that long after a listener's own copy of the note was registered.
        """
        performer = self.machines[performer_name]
        beat_ms = performer.music_ms
        packet = {"server_time": self.clock.ms(), "x": 1.0, "y": 2.0, "z": 0.0,
                  "peer_id": performer_name}
        self._sender_lag(performer, packet, measure=measure)
        names = listeners or [name for name in self.machines
                              if name != performer_name]
        for name in names:
            self._deliver(self.machines[name], dict(packet), beat_ms,
                          performer_name)
        # The scenario's own events belong to the world, not to one listener's
        # copy of the note: one shed is one shed.
        for offset, event in events:
            self._events.append(
                (self.clock.ms() + float(offset),
                 lambda event=event: event(self)))
        return beat_ms

    def _deliver(self, machine, packet, beat_ms, performer_name):
        """The packet arrives: the real scheduler places the hold."""
        note = {"machine": machine.name, "performer": performer_name,
                "beat_ms": beat_ms, "sender_lag_ms": packet.get("sender_lag_ms"),
                "strike_ms": float(packet.get("server_time") or 0.0),
                "fired_ms": None, "heard_ms": None}
        self.notes.append(note)

        def fire():
            note["fired_ms"] = self.clock.ms()
            note["music_at_fire_ms"] = machine.music_ms
            # A held note is handed its sound *now*, not when it was placed:
            # what it sounds at is this machine's own spawn cost.
            self._listen(machine, note)

        handler = self.handler(machine)
        record = handler._report_room_note_latency

        def capture(heard_ms, buffer_ms, sender_lag_ms, delay):
            note["buffer_ms"] = int(buffer_ms)
            note["held_ms"] = int(max(delay, 0.0))
            note["detail"] = handler._jam_buffer_detail
            record(heard_ms, buffer_ms, sender_lag_ms, delay)

        handler._report_room_note_latency = capture
        try:
            handler._schedule_remote_note(packet, fire, instrument="piano")
        finally:
            handler._report_room_note_latency = record
        if note["fired_ms"] is not None:
            self._listen(machine, note)

    def _listen(self, machine, note):
        """A note's sound is its spawn plus what this machine's notes cost."""
        note["heard_due_ms"] = note["fired_ms"] + machine.tail_ms

    # ------------------------------------------------------------- the loop

    def _fire(self, queue, now):
        for due, fn in list(queue):
            if now >= due:
                queue.remove((due, fn))
                fn()

    def step(self):
        """One tick: deliver, pump, play, and record what was heard."""
        now = self.clock.ms()
        self._fire(self._events, now)
        if self._last_pump is None or now - self._last_pump >= GAME_FRAME_MS - 1e-6:
            self._last_pump = now
            for machine in self.machines.values():
                machine.sync(now)
                machine.pump()
            self._fire(self._timers, now)
        for machine in self.machines.values():
            machine.play(TICK_MS, now)
        self.clock.tick(TICK_MS)
        self._probe(self.clock.ms())

    def _probe(self, now):
        """Record the music each note came out at, once its tail has elapsed."""
        for note in self.notes:
            due = note.get("heard_due_ms")
            if due is None or note["heard_ms"] is not None or now < due:
                continue
            machine = self.machines[note["machine"]]
            # The ear plays at 1x, so where it was at ``due`` is where it is
            # now minus the ticks that ran past -- the world's own 5ms grain,
            # not a judgement about the note.
            heard = machine.music_ms - max(0.0, now - due)
            note["heard_ms"] = max(note.get("music_at_fire_ms", heard), heard)
            note["error_ms"] = note["heard_ms"] - note["beat_ms"]

    def run(self, seconds=6.0):
        """Warm the song up, then (the caller strikes) let the note land."""
        self.step_until(self.clock.ms() + self._warmup_ms)
        return self

    def step_until(self, wall_ms, limit_ms=RUN_LIMIT_MS):
        """Step until ``wall_ms``, never past the run's own cap."""
        cap = self.source.start_ms + limit_ms
        while self.clock.ms() < wall_ms and self.clock.ms() < cap:
            self.step()

    def run_out(self, cap_ms=RUN_LIMIT_MS):
        """Let every registered note land, and give up on one that never does.

        A note that has not sounded by ``cap_ms`` after it was struck is not
        "late": nothing is going to play it, and the run must not wait for a
        note that no output is holding.
        """
        end = self.clock.ms() + cap_ms
        while self.clock.ms() < end:
            self.step()
            if self.notes and all(n["heard_ms"] is not None for n in self.notes):
                return self
        return self


class Scenario:
    """One question asked of the machines, and what should come out of it.

    ``events`` are ``(offset_ms_after_a_note_is_registered, callable(world))``
    pairs: the things that really happen to a song -- a shed, a refill hold,
    the relay going away -- while a note is waiting on it.
    """

    def __init__(self, name, machines, *, performer="Ann", measure=True,
                 events=(), title="", strikes=STRIKES, cap_ms=RUN_LIMIT_MS):
        self.name = name
        self.machines = machines
        self.performer = performer
        self.measure = measure
        self.events = events
        self.title = title
        self.strikes = strikes
        self.cap_ms = float(cap_ms)

    def play(self, phase_ms):
        world = World(self.machines(), phase_ms=phase_ms)
        with world_clock(world.clock):
            world.run()
            world.strike(self.performer, measure=self.measure, events=self.events)
            world.run_out(self.cap_ms)
        return world


def _machines(**overrides):
    """The performer and two listeners, with any of them overridden.

    The line-up is the ordinary one, and the numbers are the ordinary ones: a
    relay frame is 40 ms, so four frames is a machine that has been playing a
    while and eight is one whose queue is holding deeper (it rebuilt, realigned
    or came back from a stall a moment ago).

      Ann   plays at the cabinet for the others: 4 frames held (160 ms)
      Bob   listens with a deep queue (8 frames, 320 ms) -- his note is *held*
      Cid   listens with a queue like the performer's (5 frames)

    Cid's case is the other regime, and the rig has to show both: when the
    queue a listener can see is only about as deep as the performer's, the
    note arrives at a beat that has already gone by, and no wait can pull it
    back -- it sounds as soon as it can.
    """
    base = {
        "Ann": dict(transport="relay", transit_ms=40.0, stage_ms=160.0),
        "Bob": dict(transport="relay", transit_ms=40.0, stage_ms=320.0),
        "Cid": dict(transport="relay", transit_ms=25.0, stage_ms=200.0),
    }
    base.update(overrides)
    return lambda: [Machine(name, **config) for name, config in base.items()]


SCENARIOS = {}


def scenario(name, *, title="", **kwargs):
    """Register one scenario: the builder returns a fresh machine factory."""
    def register(builder):
        SCENARIOS[name] = Scenario(name, builder(), title=title, **kwargs)
        return builder
    return register


@scenario("honest", title="every number right, two links of different speed")
def _honest():
    return _machines()


@scenario("sender_missing", title="the performer's own song cannot be measured",
          measure=False)
def _sender_missing():
    return _machines()


@scenario("sender_slow", title="the performer is the one on the slow link")
def _sender_slow():
    return _machines(Ann=dict(transport="relay", transit_ms=120.0, stage_ms=160.0))


@scenario("listener_shed", title="a listener's queue sheds under the wait",
          events=((60.0, lambda world: world.machines["Bob"].shed(2)),))
def _listener_shed():
    return _machines()


@scenario("listener_hold", title="a listener's output freezes and refills",
          events=((60.0, lambda world: world.machines["Bob"].hold(140.0)),))
def _listener_hold():
    return _machines()


@scenario("switch_to_direct", title="the relay goes away mid-wait",
          cap_ms=1500.0,
          events=((60.0, lambda world: world.machines["Bob"].switch_to_direct()),))
def _switch_to_direct():
    return _machines()


@scenario("fast_machine", title="a machine whose note costs less than the constant")
def _fast_machine():
    return _machines(Bob=dict(transport="relay", transit_ms=40.0, stage_ms=320.0,
                              tail_ms=25.0))


@scenario("slow_unmeasured", title="a slow machine that has not measured itself")
def _slow_unmeasured():
    return _machines(Bob=dict(transport="relay", transit_ms=40.0, stage_ms=320.0,
                              tail_ms=95.0, measured_tail_ms=0.0))


@scenario("slow_measured", title="the same slow machine, after one note")
def _slow_measured():
    return _machines(Bob=dict(transport="relay", transit_ms=40.0, stage_ms=320.0,
                              tail_ms=95.0))


def run(scenario_name, strikes=None):
    """Play one scenario ``strikes`` times and summarise what was heard."""
    spec = SCENARIOS[scenario_name]
    count = strikes or spec.strikes
    errors = {}
    details = {}
    walls = {}
    lost = {}
    for index in range(count):
        phase = GAME_FRAME_MS * index / float(count)
        world = spec.play(phase)
        for note in world.notes:
            if note["heard_ms"] is None:
                # Nobody played it: the note is not late, it is gone.
                lost[note["machine"]] = lost.get(note["machine"], 0) + 1
                details.setdefault(note["machine"], note)
                continue
            errors.setdefault(note["machine"], []).append(note["error_ms"])
            walls.setdefault(note["machine"], []).append(
                note["fired_ms"] - note["strike_ms"])
            details.setdefault(note["machine"], note)
    return {"scenario": spec, "errors": errors, "details": details,
            "wall": {machine: sum(values) / len(values)
                     for machine, values in walls.items()},
            "lost": lost, "strikes": count}


def spread(rows):
    """How far apart the machines are -- the sentence the rig exists for."""
    means = [sum(values) / len(values) for values in rows.values() if values]
    if len(means) < 2:
        return 0.0
    return max(means) - min(means)


def describe(name):
    """One line per listener: the mean error, and what held it."""
    if name not in SCENARIOS:
        raise KeyError(name)
    return run(name)


def read_out(names=None, strikes=None):
    """The table, as text -- what the CLI prints and a person reads."""
    header = ("scenario", "listener", "error", "held", "sender_lag", "detail")
    lines = ["  ".join(header)]
    for name in (names or list(SCENARIOS)):
        result = run(name, strikes=strikes)
        for machine in sorted(result["details"]):
            values = result["errors"].get(machine, [])
            detail = result["details"].get(machine) or {}
            lost = result["lost"].get(machine, 0)
            mean = (f"{sum(values) / len(values):+6.1f}ms" if values
                    else ("   lost" if lost else "") )
            if lost and values:
                mean = f"{sum(values) / len(values):+6.1f}ms+{lost}lost"
            lines.append("  ".join((
                name, machine, mean,
                f"{detail.get('held_ms', 0)}ms",
                f"{detail.get('sender_lag_ms', 0)}ms",
                str(detail.get("detail")),
            )))
        lines.append("  ".join((
            "", f"-> {result['scenario'].title}",
            f"spread {spread(result['errors']):.1f}ms", "", "", "")))
    return lines


def main(argv):
    names = [name for name in argv[1:] if not name.startswith("-")]
    text = read_out(names or None)
    sys.stdout.write("\n".join(text) + "\n")
    sys.stdout.write(
        "\nerror = ms of music the note came out after the beat the performer "
        "heard (+ = the band is behind them)\n"
        f"grain: one relay frame {RELAY_FRAME_MS:.0f}ms, one gameplay frame "
        f"{GAME_FRAME_MS:.0f}ms, world tick {TICK_MS:.0f}ms\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
