"""A note waits on the audio that is playing, never on a stopwatch.

Two places hold a remote instrument note until its beat arrives: a cinema
room (``CinemaSpeakerBank``, one speaker at a time) and a plain cabinet's
stereo pair (the streamers -- one stereo image, or one relay queue). Both used
to hold it for a figure measured *once*, when the packet arrived: the queue
depth of whatever the listener is hearing. A queue is exactly the thing that
moves -- it drains as the song plays, a stall sheds frames, a realign refills
it, a refill hold freezes it -- so a note held for a snapshot walks away from
the song whenever the queue changes under it, and it does so *differently on
each machine*: the same note sits on the beat here and trails by most of a
queue there. That is the "some computers hear it straight, some do not"
report, and it is why the hold is not a sleep.

``SpotClock`` is the one implementation of the answer. The owner reports where
its own audio is *now* -- frames played is frames fed minus frames still
queued, and both are numbers OpenAL hands back -- and every wait is
re-projected from that position on every gameplay frame, so a queue that
caught up takes the note with it.

Two bounds keep a wrong reading from being heard as a wrong instant:

* a wait never fires more than ``JAM_WAIT_EARLY_FRAMES`` before the wall-clock
  instant it was always going to fire at. A bigger pull is either a wrong
  reading or a jump that skipped the note's own beat anyway, and in both cases
  a hair late is better than plainly early;
* it never fires later than ``JAM_WAIT_SLACK_MS`` past that instant, because a
  clock that stopped moving must not hold a live player's note forever.

And a source that is not playing is **not a clock at all**: the driver reports
everything a source holds as finished the moment it stops playing it, so an
unplayed queue and a played one are the same number. That reading says
nothing, so the note keeps its wall-clock instant -- it is never dropped for
it, because a note lost to a hiccup is worse than a note on the wall clock.
A torn-down output drops its waits explicitly instead (the bank's ``stop``,
``forget_sources`` and the slots a rebuild removes), which is the only case
where a note is deliberately lost.
"""

import time

# See the module docstring: the two bounds of the correction.
JAM_WAIT_SLACK_MS = 250.0
JAM_WAIT_EARLY_FRAMES = 2

# The one key a plain cabinet's stereo pair is measured on. A room measures one
# wait per speaker it feeds; a pair is one output, so it has one name for it.
PAIR_KEY = "pair"

# What one note's own sounding costs on this machine, in milliseconds: the time
# from "play it" to the point it is audible. One ceiling and one weight for
# every output that measures it -- a room's speakers and a plain cabinet's pair
# alike -- so two paths cannot disagree about how much of a slow machine a wait
# may spend before the beat.
NOTE_SPAWN_CEILING_MS = 150.0
NOTE_SPAWN_WEIGHT = 0.25


class SpawnCost:
    """What sounding one of this output's notes costs here, averaged.

    Reported by whatever actually *places* the note -- a room's
    ``route_to_room``, or an instrument's own remote-note entry -- because that
    is the work a wait has to spend before the beat: a machine that takes 90 ms
    to get a note out sounds the band 90 ms late unless it starts 90 ms early.

    Zero means "never measured", which is not the same as "free": a caller
    keeps its own allowance until a real reading arrives. One reading is one
    game frame that happened to be busy, so readings are averaged, and a NaN or
    an absurd figure is refused rather than latched (every comparison against
    NaN is False, which is what the chained check below relies on).
    """

    def __init__(self, weight=NOTE_SPAWN_WEIGHT,
                 ceiling_ms=NOTE_SPAWN_CEILING_MS):
        self._ms = 0.0
        self._weight = float(weight)
        self._ceiling = float(ceiling_ms)

    def ms(self):
        """What a note of this output's has cost here so far (0 = unmeasured)."""
        return self._ms

    def report(self, ms):
        """Note how long one note took to reach a speaker (ms).

        Averaged, because a single reading is one game frame that happened to
        be busy, and bounded, because the value only ever exists to *raise* a
        machine's allowance -- under-compensating a slow spawn is the failure
        that is heard ("the band is behind the beat on that one computer").
        """
        try:
            value = float(ms)
        except (TypeError, ValueError):
            return
        if not (0.0 <= value <= self._ceiling * 4):
            return
        value = min(value, self._ceiling)
        if self._ms <= 0.0:
            self._ms = value
        else:
            self._ms += (value - self._ms) * self._weight


def mono_ms(pcm, rate=48000):
    """How many milliseconds of audio a MONO16 chunk is.

    The size of one frame, measured from the audio the transport actually
    hands over rather than assumed: the direct streamer decodes 20 ms and the
    relay is sent 40 ms, and a wait counted in frames is only in milliseconds
    once the frame's own length is known.
    """
    try:
        return max(0.0, len(pcm) / 2.0 / float(rate) * 1000.0)
    except Exception:
        return 0.0


class SpotClock:
    """Where a piece of audio is playing right now, and a wait on that.

    The owner supplies five answers, and nothing here touches OpenAL:

    ``keys_of()``
        the keys a wait may be *measured* on -- the outputs that are carrying
        the song. A pair has one key; a room has one per speaker it feeds.
    ``lead_of(key)``
        how far ahead of audible that key is right now, in frames: the count
        a wait is placed on when the owner does not name one, and the reason
        the leading edge is chosen (the same answer ``buffered_ms`` gives).
    ``progress_of(key)``
        the instant that key is playing now, in frames: frames fed minus
        frames still queued.
    ``frame_ms_of()``
        the size of one frame in milliseconds, for turning frames into time.
    ``playing_of(key)``
        whether that key is playing at all (see the docstring's last rule).

    A key that is not playing keeps its wait on the wall clock; a key that
    cannot be resolvable at all is still kept, and fires there too, so an
    output that goes away under a note can never leave the note sounding at
    an instant nobody chose -- ``drop_waits`` is the deliberate way to lose
    one.
    """

    def __init__(self, progress_of, frame_ms_of, playing_of, keys_of, lead_of):
        self._progress_of = progress_of
        self._frame_ms_of = frame_ms_of
        self._playing_of = playing_of
        self._keys_of = keys_of
        self._lead_of = lead_of
        self._waits = []
        # (key -> (progress, when that progress was first seen, when it was
        # last polled)): a content boundary is only ever *observed* at a
        # poll, so half the interval since the last observation is the best
        # estimate of when it happened. The error that leaves is a constant
        # offset for every note measured on that key rather than a per-note
        # one.
        self._epoch = {}

    # ------------------------------------------------------------ choosing

    def keys(self):
        try:
            return list(self._keys_of() or ())
        except Exception:
            return []

    def frame_ms(self):
        try:
            return float(self._frame_ms_of() or 0.0)
        except Exception:
            return 0.0

    def pick(self, key=None):
        """The spot a wait is measured on: the one named, else the leading edge."""
        keys = self.keys()
        if key is not None and key in keys:
            return key
        best, best_lead = None, None
        for candidate in keys:
            try:
                lead = int(self._lead_of(candidate))
            except Exception:
                continue
            if best_lead is None or lead < best_lead:
                best, best_lead = candidate, lead
        return best

    def _playing(self, key):
        try:
            return bool(self._playing_of(key))
        except Exception:
            return False

    def _progress(self, key):
        try:
            return int(self._progress_of(key))
        except Exception:
            return 0

    # -------------------------------------------------------------- waits

    def wait_advance(self, ms, fire, *, key=None, tail_ms=0.0):
        """Run ``fire`` when this output's own clock has advanced ``ms``.

        ``ms`` is the music the note still has to wait for to land on the beat
        the listener hears, and ``tail_ms`` is what the note's own spawn costs
        on this machine, which the wait spends *before* the beat instead of
        after it.

        Returns True when the clock took the wait, and False when there is no
        output to measure at all: the caller then fires the note itself,
        exactly as it did before this existed.
        """
        if not callable(fire):
            return False
        chosen = self.pick(key)
        if chosen is None:
            return False
        advance = max(0.0, float(ms or 0.0))
        tail = max(0.0, float(tail_ms or 0.0))
        now = time.monotonic()
        wait_for = max(0.0, advance - tail)
        self._waits.append({
            "key": chosen,
            "progress0": self._progress(chosen),
            "remaining": wait_for,
            "target": now + wait_for / 1000.0,
            "fire": fire,
        })
        return True

    def pump_waits(self):
        """Fire every note this output's own clock has reached.

        Called once per gameplay frame while the output is playing -- on the
        *game* thread, because firing a note can spawn sources. Each wait is
        re-projected from the output's current position every time, so a queue
        that drained (or grew, or was realigned) moves the note with it
        instead of leaving it where the arrival snapshot put it.
        """
        if not self._waits:
            return 0
        now = time.monotonic()
        fired = 0
        for wait in list(self._waits):
            key = wait["key"]
            due = None
            if self._playing(key):
                progress = self._progress(key)
                seen = self._epoch.get(key)
                if seen is None or seen[0] != progress:
                    interval = (now - seen[2]) if seen else 0.0
                    seen = (progress, now - interval / 2.0, now)
                    self._epoch[key] = seen
                advanced = (progress - wait["progress0"]) * self.frame_ms() / 1000.0
                due = seen[1] + max(0.0, wait["remaining"] / 1000.0 - advanced)
            else:
                # Not a clock (see the module docstring): the stale epoch is
                # forgotten rather than left to be believed when it plays
                # again, and the note keeps the instant it was going to sound
                # at anyway.
                self._epoch.pop(key, None)
            if due is None:
                due = wait["target"]
            due = min(due, wait["target"] + JAM_WAIT_SLACK_MS / 1000.0)
            due = max(due, wait["target"]
                      - JAM_WAIT_EARLY_FRAMES * self.frame_ms() / 1000.0)
            if now < due:
                continue
            self._waits.remove(wait)
            fired += 1
            try:
                wait["fire"]()
            except Exception:
                pass
        return fired

    def pending_waits(self):
        """How many notes are waiting on this output (read-out and tests)."""
        return len(self._waits)

    def drop_waits(self, keys=None):
        """Drop the notes waiting here (a teardown, or outputs that went).

        Dropped rather than fired: a note was played against *this* output's
        song, and firing it into an output that is being taken apart would
        sound it at an instant nobody asked for. One lost note is invisible
        next to that.
        """
        if not self._waits:
            return
        if keys is None:
            self._waits.clear()
            return
        gone = set(keys)
        self._waits = [wait for wait in self._waits if wait["key"] not in gone]
        for key in gone:
            self._epoch.pop(key, None)

    def forget(self, keys=None):
        """Forget the epoch of outputs that changed under the song.

        Called by an owner that rebuilt its output (new sources, a realigned
        queue): the old boundary times describe audio that is no longer
        playing, and a wait measured through them would be measured through
        somebody else's queue.
        """
        if keys is None:
            self._epoch.clear()
            return
        for key in keys:
            self._epoch.pop(key, None)
