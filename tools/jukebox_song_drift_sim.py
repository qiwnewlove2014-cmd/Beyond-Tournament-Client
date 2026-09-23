"""Song 1, then song 2: is every machine still on the same bar?

The report this exists for: the first song after a login jams exactly in sync,
and the songs after it drift -- "the music runs ahead of the drums" -- until
the player leaves and comes back. Two people hearing one cabinet are only in
sync if their two streams are at the same *position in the song*, so that is
the question asked here, and it is a question about the ANCHOR, not about note
scheduling: a note is aimed at a position in the song, and no timing rule can
make up for two machines whose music is seconds apart.

What is modelled, and what the game really computes:

* a RELAY machine is audible at the broadcast instant at the position the
  Server named -- the relay room holds no lead-in;
* a DIRECT machine resolves and starts its own ffmpeg, then holds its
  prebuffered head until ``AudioStreamer.direct_start_deadline``. A machine
  whose resolve+startup outruns that deadline becomes audible LATE by exactly
  the overrun (``AudioStreamer.direct_late_s``) and never catches up: under
  ``-re`` pacing its own decode keeps producing at media rate, so the whole
  song stays that far behind the room;
* a machine joining a song already playing seeks past its own projected
  audible start (``AudioStreamer.direct_seek_seconds``), which is exact at any
  offset -- and the one asymmetry it cannot cancel is a machine that anchors
  with a lead-in while the room it joined holds none.

Nothing about the anchor is re-implemented: the two functions and the constants
above are the shipped ``AudioStreamer``'s.

Run it: ``python tools/jukebox_song_drift_sim.py`` (every shape) or
``python tools/jukebox_song_drift_sim.py slow_start all_direct`` (named ones).
The number is milliseconds of music: positive means this machine's music is
AHEAD of the reference machine's, so a note the reference played is heard
*behind* this listener's song.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from libs.music_bot.streaming import AudioStreamer

# How long into a song the comparison is taken. Long enough that a lead-in or a
# late start has finished showing itself, short enough to be inside one song.
AT_S = 30.0
# The gap between one song ending and the next starting: long enough for the
# queue advance to be a new playback on every machine.
GAP_S = 1.0
FRESH = 0.0                     # a fresh broadcast: the song's position is 0


class Machine:
    """One listener's stream: when its music starts, and at which position.

    ``resolve`` and ``startup`` are this machine's own cost -- yt-dlp looking
    up a link and ffmpeg opening a pipe -- and they are the only reason two
    direct machines can end up anywhere but level with each other.
    """

    def __init__(self, name, *, transport="relay", latency=0.05,
                 resolve=1.0, startup=0.5, lead_in=None, join=False,
                 by_song=None):
        self.name = name
        self.transport = transport
        self.latency = float(latency)
        self.resolve = float(resolve)
        self.startup = float(startup)
        # A machine that is quick on one song and slow on another -- what the
        # queue advance looks like when its own yt-dlp/ffmpeg is warm for the
        # song it has been playing and cold for the one after it.
        # ``{song_index: (resolve, startup)}``.
        self.by_song = dict(by_song or {})
        # None is the room's own lead-in; 0.0 is the per-listener direct
        # fallback, which must not hold one while the relay room holds none.
        self.lead_in = (AudioStreamer.DIRECT_LEAD_IN_S if lead_in is None
                        else float(lead_in))
        self.join = bool(join)
        self.audible_at = None
        self.content_at_audible = 0.0
        self.late_s = 0.0

    def receive(self, sent_at, offset=FRESH, *, song=1):
        """The Server's play event arrives: anchor this machine's music."""
        resolve, startup = self.by_song.get(song, (self.resolve, self.startup))
        received = sent_at + self.latency
        if self.transport == "relay":
            self.audible_at = received
            self.content_at_audible = float(offset)
            self.late_s = 0.0
            return self
        spawn = received + resolve
        if self.join:
            seek = AudioStreamer.direct_seek_seconds(
                max(0.001, offset), received, spawn)
        elif offset > AudioStreamer.DIRECT_FRESH_MAX_S:
            seek = AudioStreamer.direct_seek_seconds(offset, received, spawn)
        else:
            seek = float(offset)
        deadline = AudioStreamer.direct_start_deadline(
            offset, received, seek, lead_in=self.lead_in)
        ready = spawn + startup
        hold = min(AudioStreamer.DIRECT_MAX_ALIGN_WAIT_S,
                   max(0.0, deadline - ready))
        self.audible_at = ready + hold
        self.late_s = max(0.0, self.audible_at - deadline)
        self.content_at_audible = seek if seek > 0.5 else 0.0
        return self

    def position_at(self, when):
        """The position in the song this machine's ear has reached."""
        if self.audible_at is None:
            return None
        if when < self.audible_at:
            return self.content_at_audible - (self.audible_at - when)
        return self.content_at_audible + (when - self.audible_at)


class Song:
    """One playback: the instant the Server sent the play event, from 0."""

    def __init__(self, index, sent_at, offset=FRESH):
        self.index = index
        self.sent_at = float(sent_at)
        self.offset = float(offset)


class Run:
    """A room, two songs, and where every machine's music is in each of them."""

    def __init__(self, machines, *, at_s=AT_S, gap_s=GAP_S):
        self.machines = list(machines)
        self.songs = [Song(1, 0.0, FRESH), Song(2, at_s + gap_s, FRESH)]
        self.at_s = float(at_s)

    def play(self):
        """Anchor every machine on song 1, then on song 2. Returns the rows."""
        rows = []
        for song in self.songs:
            for machine in self.machines:
                machine.receive(song.sent_at, song.offset, song=song.index)
            rows.append(self._row(song))
        return rows

    def _row(self, song):
        """Where every machine's music is ``at_s`` into this song, in ms.

        The reference is the machine that started on time -- the room as
        everybody else hears it -- so a machine that started late reads as a
        negative drift: its music is behind, and a note the room played is
        heard *ahead* of this listener's song rather than on its beat.
        """
        when = song.sent_at + self.at_s
        positions = {machine.name: machine.position_at(when)
                     for machine in self.machines}
        reference_name = min(
            self.machines, key=lambda m: (m.late_s, m.name)).name
        reference = positions[reference_name] or 0.0
        return {
            "song": song.index,
            "reference": reference_name,
            "drift_ms": {name: ((position or 0.0) - reference) * 1000.0
                         for name, position in positions.items()},
            "late_ms": {machine.name: machine.late_s * 1000.0
                        for machine in self.machines},
        }


def _machine(name, **kwargs):
    return Machine(name, **kwargs)


SCENARIOS = {}


def scenario(name, *, title, machines, **kwargs):
    SCENARIOS[name] = (title, machines, kwargs)


# The normal case, and the control: nothing to disagree about.
scenario("all_direct",
         title="a room where everyone plays direct (the normal case)",
         machines=lambda: [_machine("Ann", transport="direct", resolve=1.0,
                                    startup=0.5),
                           _machine("Bob", transport="direct", resolve=2.0,
                                    startup=0.9)])

scenario("all_relay",
         title="a room the server relays to everyone",
         machines=lambda: [_machine("Ann", transport="relay", latency=0.03),
                           _machine("Bob", transport="relay", latency=0.11)])

# The reported shape: song 1 perfect, the songs after it off.
scenario("slow_second_song",
         title="one machine's SECOND song starts slower than the lead-in",
         machines=lambda: [_machine("Ann", transport="direct", resolve=1.0,
                                    startup=0.5),
                           _machine("Bob", transport="direct", resolve=1.0,
                                    startup=0.5,
                                    by_song={2: (2.0, 2.4)})])

# The same machine, slow on the first song instead: if this row is clean too,
# the song's number is not the cause -- the slow start is.
scenario("slow_first_song",
         title="the same machine, slow on the FIRST song instead",
         machines=lambda: [_machine("Ann", transport="direct", resolve=1.0,
                                    startup=0.5),
                           _machine("Bob", transport="direct", resolve=1.0,
                                    startup=0.5,
                                    by_song={1: (2.0, 2.4)})])

scenario("every_song_slow",
         title="a machine that is slow on every song",
         machines=lambda: [_machine("Ann", transport="direct", resolve=1.0,
                                    startup=0.5),
                           _machine("Bob", transport="direct", resolve=2.0,
                                    startup=2.4)])

scenario("fallback_in_relay_room",
         title="one machine plays direct while the room hears the relay",
         machines=lambda: [_machine("Ann", transport="relay", latency=0.03),
                           _machine("Bob", transport="direct", resolve=1.0,
                                    startup=0.5, lead_in=0.0, join=True)])

scenario("fallback_holding_the_lead_in",
         title="the same machine, holding a lead-in the relay room never holds",
         machines=lambda: [_machine("Ann", transport="relay", latency=0.03),
                           _machine("Bob", transport="direct", resolve=1.0,
                                    startup=0.5, lead_in=None, join=False)])


def rows_for(name):
    title, builder, kwargs = SCENARIOS[name]
    run = Run(builder(), **kwargs)
    return title, run.play()


def read_out(names=None):
    """The table, as text -- what the CLI prints and a person reads."""
    lines = ["scenario                    song  machine  drift_ms  late_ms"]
    for name in (names or list(SCENARIOS)):
        title, rows = rows_for(name)
        for row in rows:
            for machine in sorted(row["drift_ms"]):
                lines.append(
                    f"{name:26s}  {row['song']}     {machine:5s}  "
                    f"{row['drift_ms'][machine]:+8.1f}  "
                    f"{row['late_ms'][machine]:8.1f}")
        lines.append(f"  -> {title}")
    return lines


def main(argv):
    names = [name for name in argv[1:] if not name.startswith("-")]
    lines = read_out(names or None)
    sys.stdout.write("\n".join(lines) + "\n")
    sys.stdout.write(
        f"\ndrift = ms of music between this machine and the one that started "
        f"on time, {AT_S:.0f}s into the song\n"
        f"a fresh song holds DIRECT_LEAD_IN_S={AudioStreamer.DIRECT_LEAD_IN_S}s "
        f"before it becomes audible; resolve+startup past that is played "
        f"late and stays late\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
