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
  the overrun (``AudioStreamer.direct_late_s``) and cannot catch up by playing
  on: under ``-re`` pacing its own decode keeps producing at media rate, so
  the whole song stays that far behind the room. The streamer's answer is to
  start the decode somewhere else -- the flight loop drops that flight and
  launches again aimed at the room (``direct_catch_up_aim_s``) -- so a slow
  machine pays silence ONCE and then plays on the room's clock, instead of
  playing wrongly for a whole song;
* ``re_aim=False`` is the machine that gets no re-aim: the old build, a cinema
  room (whose bank cannot be emptied and refilled mid-song), and a machine so
  slow that a second attempt would be late again by what the first one was.
  Those rows still read the whole overrun, which is why the client logs it;
* a machine joining a song already playing seeks past its own projected
  audible start (``AudioStreamer.direct_seek_seconds``), which is exact at any
  offset -- and the one asymmetry it cannot cancel is a machine that anchors
  with a lead-in while the room it joined holds none.

Nothing about the anchor is re-implemented: the two functions, the aim and the
constants above are the shipped ``AudioStreamer``'s.

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
                 by_song=None, re_aim=True, second_startup=None):
        self.name = name
        self.transport = transport
        self.latency = float(latency)
        self.resolve = float(resolve)
        self.startup = float(startup)
        # The one re-aim a late start is allowed: shipped behavior, and what
        # every direct jukebox stream does. False is the old build, a cinema
        # room, or a machine a restart cannot cure.
        self.re_aim = bool(re_aim)
        # What the SECOND launch costs. It skips the resolve (the media is
        # already resolved and the same signed URL is reused) and pays only
        # ffmpeg's own startup -- which is why the aim is normally early and
        # the pre-buffer hold turns the difference into a wait.
        self.second_startup = (float(startup) if second_startup is None
                               else float(second_startup))
        self.catch_up_s = 0.0     # the room instant the re-aim targeted
        self.skipped_s = 0.0      # content the dropped flight had decoded
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
        # A new playback is new flights: whatever the last song's re-aim cost
        # belongs to that song.
        self.catch_up_s = 0.0
        self.skipped_s = 0.0
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
        aim = self.maybe_aim_seconds(startup)
        if aim is not None:
            self._re_aim(received, offset, seek, aim)
        return self

    def maybe_aim_seconds(self, startup):
        """The room instant this machine is re-aimed at, or None.

        The rule itself is not modelled here: ``catch_up_aim_seconds`` is the
        shipping build's own answer, asked with this machine's numbers, so a
        version that stops re-aiming (or re-aims at something else) fails the
        rows below instead of being modelled by them. ``startup`` is *this
        flight's* own cost -- a machine can be quick on one song and slow on
        the next -- which is the number the streamer measures for the flight
        that just failed. ``self.re_aim`` models *this machine*: the old
        build, a cinema room, an unanchored stream, a restart that cured
        nothing, all of which the streamer's own guards refuse.
        """
        if self.transport != "direct" or not self.re_aim:
            return None
        if AudioStreamer.DIRECT_CATCH_UP_MAX < 1:
            return None
        return AudioStreamer.catch_up_aim_seconds(self.late_s, startup)

    def _re_aim(self, received, offset, first_seek, aim):
        """The flight loop's one restart, exactly as the streamer computes it.

        The decision is taken the instant the late flight's pre-buffer is
        complete (a late machine's hold waits nothing), so the aim is that
        flight's own measured startup, and the frames it decoded are dropped
        rather than played. Everything the room is anchored to -- t_zero, the
        offset, its lead-in -- is unchanged: the second flight simply joins
        the room at the position it will have reached by then.
        """
        rebuild_at = self.audible_at
        seek = AudioStreamer.direct_seek_seconds(
            max(0.001, offset), received, rebuild_at, aim_ahead_s=aim,
            lead_in=self.lead_in)
        deadline = AudioStreamer.direct_start_deadline(
            offset, received, seek, lead_in=self.lead_in)
        ready = rebuild_at + self.second_startup
        hold = min(AudioStreamer.DIRECT_MAX_ALIGN_WAIT_S,
                   max(0.0, deadline - ready))
        self.catch_up_s = aim
        self.skipped_s = max(0.0, seek - first_seek)
        self.audible_at = ready + hold
        self.late_s = max(0.0, self.audible_at - deadline)
        self.content_at_audible = seek if seek > 0.5 else 0.0

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
            "aim_s": {machine.name: machine.catch_up_s
                      for machine in self.machines},
            "skip_s": {machine.name: machine.skipped_s
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

# The reported shape, cured: song 1 was always perfect, and the song after it
# put one machine a whole song behind -- until it was re-aimed.
scenario("slow_second_song",
         title="one machine's SECOND song starts slower than the lead-in "
               "(re-aimed: level again)",
         machines=lambda: [_machine("Ann", transport="direct", resolve=1.0,
                                    startup=0.5),
                           _machine("Bob", transport="direct", resolve=1.0,
                                    startup=0.5,
                                    by_song={2: (2.0, 2.4)})])

# The same room on a build that cannot re-aim, and on the shapes the client
# refuses to re-aim: this is what the report was about.
scenario("slow_second_song_no_re_aim",
         title="the same room with no re-aim: song 1 exact, song 2 a whole "
               "overrun behind (the old shape)",
         machines=lambda: [_machine("Ann", transport="direct", resolve=1.0,
                                    startup=0.5),
                           _machine("Bob", transport="direct", resolve=1.0,
                                    startup=0.5, re_aim=False,
                                    by_song={2: (2.0, 2.4)})])

# The same machine, slow on the first song instead: if this row is clean too,
# the song's number is not the cause -- the slow start is.
scenario("slow_first_song",
         title="the same machine, slow on the FIRST song instead "
               "(re-aimed: level again)",
         machines=lambda: [_machine("Ann", transport="direct", resolve=1.0,
                                    startup=0.5),
                           _machine("Bob", transport="direct", resolve=1.0,
                                    startup=0.5,
                                    by_song={1: (2.0, 2.4)})])

scenario("slow_first_song_no_re_aim",
         title="the same machine with no re-aim: it drifts in the song it "
               "was slow in",
         machines=lambda: [_machine("Ann", transport="direct", resolve=1.0,
                                    startup=0.5),
                           _machine("Bob", transport="direct", resolve=1.0,
                                    startup=0.5, re_aim=False,
                                    by_song={1: (2.0, 2.4)})])

scenario("every_song_slow",
         title="a machine that is slow on every song (re-aimed each time)",
         machines=lambda: [_machine("Ann", transport="direct", resolve=1.0,
                                    startup=0.5),
                           _machine("Bob", transport="direct", resolve=2.0,
                                    startup=2.4)])

scenario("every_song_slow_no_re_aim",
         title="a machine that is slow on every song, with no re-aim",
         machines=lambda: [_machine("Ann", transport="direct", resolve=1.0,
                                    startup=0.5),
                           _machine("Bob", transport="direct", resolve=2.0,
                                    startup=2.4, re_aim=False)])

# The refusal that is deliberate: a machine whose own launch is slower than
# the whole alignment slack pays more silence for the restart than the drift
# it removes, so it is left to trail the room and say so.
scenario("too_slow_to_cure",
         title="a machine slower than the alignment slack: the re-aim is "
               "refused, and it trails",
         machines=lambda: [_machine("Ann", transport="direct", resolve=1.0,
                                    startup=0.5),
                           _machine("Bob", transport="direct", resolve=3.0,
                                    startup=12.0)])

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
    lines = ["scenario                    song  machine  drift_ms  late_ms  "
             "aim_s  skip_s"]
    for name in (names or list(SCENARIOS)):
        title, rows = rows_for(name)
        for row in rows:
            for machine in sorted(row["drift_ms"]):
                lines.append(
                    f"{name:26s}  {row['song']}     {machine:5s}  "
                    f"{row['drift_ms'][machine]:+8.1f}  "
                    f"{row['late_ms'][machine]:8.1f}  "
                    f"{row['aim_s'][machine]:6.2f}  "
                    f"{row['skip_s'][machine]:6.1f}")
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
        f"late and stays late\n"
        f"aim_s = the room instant a re-aimed flight targets, skip_s = how much "
        f"song the dropped one cost (the price of the fix, paid once)\n"
        f"rows with re_aim=False are a machine that gets no re-aim: the old "
        f"build, a cinema room, or one a restart cannot cure\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
