"""Harmony on the line-in guitar: the Music Bot's copies, under a string.

The guitar does not need Auto-Tune -- a string is tuned before it is played --
but it can use the second voice. This line is that voice and nothing else: the
Music Bot's Harmony 1 and Harmony 2 intervals, built from the captured frame and
mixed into the SAME 20 ms frame, so what leaves the machine is still one stream
for the voice channel, the PA, a cinema room's speech leg and the bot's own
broadcast. The copies are the microphone's own (`voice_chat.HarmonyVoice`), so
a bass copy is still quieter and low-passed while an upper one is an equal
singer, and each is still placed at its fixed interval from the note as played
(`music_bot/autotune.harmony_plain_offset`): nothing here reads a scale or a
pitch, because nothing here is tuning the lead.

Three rules are this module's own.

**Only what leaves is shaped.** `InstrumentInput._stage` hands the player's own
ear its 10 ms chunks the moment they exist, before a 20 ms frame is assembled,
so the ear never sees this line: the guitarist plays at the latency they always
did and everybody else hears the harmony. The note tracker is fed the dry frame
for the same reason -- `pitch.PitchTracker` naming notes from a chord it never
heard is exactly the mis-transcription it does not need.

**25 ms, not 107.** The microphone's line delays a voice by the tuner's whole
look-ahead (107 ms at the voice's own key), which a player monitoring
themselves would feel as a lag. A copy above a string lands 26.7 ms behind it
with `transpose.GuitarTransposer` (the same vocoder on a 21 ms window), and the
lead is held back by the same amount so the two voices are a chord rather than
a slap. A BASS copy reads slower than its source and needs more stretched audio
for the same second of playing, so it costs 32-53 ms; the line delays the lead
by whatever its slowest singing copy needs, and not a sample more.

**It opens and closes without a click.** The lead's delay is what makes the
chord; it is also a jump the moment the line opens or closes (26 ms of audio
repeated, or skipped). So the output is a blend of the delayed lead and the
live one, ramped over three frames, and a copy is faded -- never cut -- when it
is switched off. The same rule drops the copies when the Music Bot's intervals
change under a sounding chord.
"""
import threading

import numpy as np

#: The Instrument page's switch. Off by default: a guitar is loud on its own,
#: and a player who has not asked for a second voice should not get one.
SWITCH_OPTION = "guitar_harmony"

def switch_label(text="Guitar Harmony"):
    """What the switch says it is doing: 'Guitar Harmony. on (Third + Fifth)'.

    Read by the Instrument page's own row and by the Options line that opens
    that page, so the two can never disagree about which intervals the guitar
    has.  A switch that is on while both Music Bot slots are Off says so
    instead of listing nothing.
    """
    if not enabled():
        return f"{text}. off"
    ids = harmony_ids()
    if not ids:
        return f"{text}. on, but both harmony voices are Off in the Music Bot"
    from .music_bot import autotune
    return (f"{text}. on ({' + '.join(autotune.harmony_label(i) for i in ids)})")


def enabled():
    """Whether the guitar's own harmony line should shape what leaves."""
    from . import options
    return bool(options.get(SWITCH_OPTION, False))


def gated():
    """Whether staff have locked the Music Bot Auto-Tune this switch rides on.

    `auto_tune` is one row in `server/libs/music_bot_gate.ts` and it hides the
    whole Harmony page (both copies' interval lists included) on the Music Bot
    side. The guitar's line sings those same copies, so a locked family takes
    this switch with it -- in the Options list and at the frame itself.
    """
    from .music_bot import gate
    return gate.is_locked("auto_tune")


def menu_item(toggle):
    """The one row the Instrument page adds, or nothing while it is locked."""
    if gated():
        return []
    return [(switch_label, toggle)]


def harmony_ids():
    """The Music Bot's two intervals, in order, without Off and without a twin.

    Two slots holding the same interval are ONE copy on the microphone's line
    (`AutoTuneLine._shape_harmonies` dedupes them, because two identical copies
    are one voice at twice the level). This line follows the same rule, which
    is also why the Options label names two intervals and never three.
    """
    from . import voice_chat
    ids = []
    for interval in voice_chat.autotune_harmony():
        if interval != "off" and interval not in ids:
            ids.append(interval)
    return tuple(ids)


def copy_delay(interval_id, lookahead):
    """How far behind its own source one copy lands, in samples.

    A copy above the string is read faster than it is written, so an opening
    silence of ``lookahead * ratio`` comes out as exactly ``lookahead``
    samples -- the same delay for a third, a fifth and an octave, which is
    what lets one lead delay line them all up. A copy BELOW the string reads
    slower and cannot open with less than the vocoder's own window
    (`WINDOW + HOP`), so it takes longer in real time: that is where the
    bass copies' extra milliseconds come from.
    """
    from .music_bot import autotune
    from .music_bot.transpose import GuitarTransposer
    step = autotune.harmony_semitones(interval_id)
    if step == 0:
        return 0
    nominal = 2.0 ** (step / 12.0)
    opening = max(float(GuitarTransposer.WINDOW + GuitarTransposer.HOP),
                  float(lookahead) * nominal)
    return int(round(opening / nominal))


class GuitarHarmony:
    """One line-in guitar's harmony: the copies, mixed into its own frames.

    Fed one 20 ms mono frame per call from the capture thread
    (`InstrumentInput._emit_frame`) and expected to answer the same number of
    frames back, exactly like the microphone's lines. The lock is the capture
    thread's against the release (`reset` on a device switch or `stop`).
    """

    #: The copies' opening silence, in stretched samples: 1280 is
    #: `GuitarTransposer`'s window + hop, 26.7 ms at 48 kHz.
    LOOKAHEAD = 1280
    #: The frame the capture thread hands over: 20 ms at 48 kHz.
    FRAME_SAMPLES = 960
    #: How long a copy takes to join or leave the mix. A cut is a click.
    FADE_FRAMES = 3
    #: The longest lead delay this line can need: a bass octave's copy reads at
    #: half speed, so the window that is 1280 samples of stretched audio is
    #: 2560 samples of playing time.
    MAX_LEAD = 2560

    def __init__(self):
        self._lock = threading.Lock()
        #: The last seconds of dry audio, so a lead delay that grows does not
        #: have to start from silence and a rebuild repeats no more than the
        #: delay it just took.
        self._dry = np.zeros((0,), dtype=np.float32)
        self._voices = []
        self._target = 0
        self._previous = 0
        #: 1.0 while the lead is passed through untouched, 0.0 while it is
        #: held back for the copies; ramped, never stepped.
        self._bypass = 1.0

    # ------------------------------------------------------------- the frame
    def shape(self, pcm):
        """One 20 ms mono frame in, one out -- unchanged unless it is singing."""
        reading = enabled() and not gated()
        if not reading and not self._voices:
            with self._lock:
                self._dry = np.zeros((0,), dtype=np.float32)
                self._bypass = 1.0
                self._target = self._previous = 0
            return pcm
        samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
        frames = samples.size
        if frames <= 0:
            return pcm
        with self._lock:
            self._remember(samples)
            self._aim(harmony_ids() if reading else ())
            voices = [voice for voice in self._voices
                      if voice.wanted or voice.gain > 0.0]
            if not voices and self._bypass >= 1.0:
                self._voices = []
                return pcm
            lead = self._lead(frames, self._target)
            if self._previous != self._target:
                # The slowest copy changed, so the lead is read from a new
                # place in its own history. Both places are blended ACROSS the
                # frame, sample by sample: a per-frame step in the blend is a
                # step in the audio (measured 0.10 -- twelve times the wave's
                # own slope -- which is the click it sounds like).
                old = self._lead(frames, self._previous)
                blend = np.linspace(0.0, 1.0, frames, endpoint=False,
                                    dtype=np.float32)
                lead = old * (1.0 - blend) + lead * blend
                self._previous = self._target
            goal = 0.0 if voices else 1.0
            was = self._bypass
            self._bypass = self._ramp(was, goal)
            if was != self._bypass:
                # The same rule on the way in and out: the delayed lead and
                # the live string are 26 ms apart, so the two are crossfaded
                # per sample rather than per frame.
                blend = np.linspace(was, self._bypass, frames, endpoint=False,
                                    dtype=np.float32)
                lead = lead * (1.0 - blend) + samples * blend
            elif self._bypass > 0.0:
                lead = lead * (1.0 - self._bypass) + samples * self._bypass
            if not voices:
                # Everything has faded: the lead is through again (or on its
                # way), so this frame is the plain lead and nothing else.
                self._voices = []
                return _to_pcm(lead)
            return _to_pcm(self._mix(lead, pcm, frames, voices))

    def _mix(self, lead, pcm, frames, voices):
        """Sum the lead and its copies into ONE frame, in their own shares.

        The fades are ramped SAMPLE BY SAMPLE, not once per frame. A voice that
        leaves in three 1/3 steps of a 20 ms frame is three steps in the audio --
        measured 0.097 of full scale on a 0.3 tone, nine times its own slope,
        which is the click it sounds like. Carrying `voice.gain` from frame to
        frame and spreading each step over the frame leaves no step at the
        boundary, and the lead's own duck follows the same ramp (`total`), so
        the mix stays inside full scale through the whole fade.
        """
        from .music_bot import autotune
        weights = []
        total = np.ones(frames, dtype=np.float32)
        for voice in voices:
            start = voice.gain
            if (start <= 0.0 and voice.wanted
                    and voice.fed < voice.lead_in):
                # The copy's vocoder opens with `lead_in` frames of silence. A
                # fade that has already started when its first real sample
                # arrives puts a waveform in at whatever phase it happens to
                # begin on -- a step (measured 0.063, six times the wave's
                # slope). So the fade waits for the audio it is fading.
                end = 0.0
            else:
                end = voice.advance_gain()
            voice.fed += 1
            ramp = np.linspace(start, end, frames,
                               endpoint=False, dtype=np.float32)
            weight = ramp * autotune.harmony_share(voice.interval)
            weights.append((voice, weight))
            total = total + weight
        mix = lead / total
        for voice, weight in weights:
            mix = mix + voice.process(pcm) * (weight / total)
            if not voice.wanted and voice.gain <= 0.0 and voice in self._voices:
                self._voices.remove(voice)
        return mix

    # ------------------------------------------------------------- the copies
    def _aim(self, wanted):
        """Keep one copy per wanted interval, on the timeline they need.

        A copy below the string needs a longer lead delay than one above it,
        and every copy is aligned to that same delay -- so a change of that
        delay is a rebuild for all of them (the old ones fade out on their own
        timeline rather than being cut).
        """
        delays = [copy_delay(interval, self.LOOKAHEAD) for interval in wanted]
        target = max([self.LOOKAHEAD] + delays)
        self._previous, self._target = self._target, target
        for interval in wanted:
            if not any(voice.interval == interval and voice.lead == target
                       for voice in self._voices):
                self._voices.append(self._build(interval, target))
        for voice in self._voices:
            voice.wanted = (voice.interval in wanted and voice.lead == target)

    def _build(self, interval, target):
        """One copy, aimed at its plain interval, on the line's timeline.

        `HarmonyVoice` builds the shifter and the limits but never chooses a
        ratio -- the microphone's line steers it from its tuner every frame
        (`AutoTuneLine.shape`). A guitar has nothing to steer with, so the
        interval is set once, here, at the catalogue's own size
        (`autotune.harmony_plain_offset`: a major third is four semitones down
        to the last one), and it stays there for the copy's life. Skipping this
        leaves the copy at ratio 1.0 -- a unison doubling rather than a
        harmony, and one that arrives at a different time than the lead.
        """
        from .voice_chat import HarmonyVoice
        from .music_bot import autotune
        from .music_bot.transpose import GuitarTransposer
        voice = HarmonyVoice(interval, transposer_cls=GuitarTransposer,
                             lookahead=self.LOOKAHEAD, lead_delay=target)
        offset = autotune.harmony_plain_offset(interval)
        if offset is not None:
            voice.transposer.set_pitch_offset(offset)
        #: Which timeline this copy was built for (`_aim` rebuilds on a change).
        voice.lead = target
        #: Frames this copy has been fed, and the frames of silence its own
        #: line opens with -- the fade waits for both (`_mix`). The silence is
        #: the LINE's delay, not the copy's own: a treble copy is held back to
        #: a bass copy's timeline (`_align`), so its first real sample arrives
        #: `target` samples after it is built, whatever its interval is.
        voice.fed = 0
        voice.lead_in = int(np.ceil(float(target)
                                    / float(self.FRAME_SAMPLES))) + 1
        return voice

    # ------------------------------------------------------------- the lead
    def _remember(self, samples):
        """Keep the dry audio the lead can be read from, and no more."""
        keep = self.MAX_LEAD + 4 * samples.size
        self._dry = np.concatenate((self._dry, samples))
        if self._dry.size > keep:
            self._dry = self._dry[-keep:]

    def _lead(self, frames, delay):
        """The frame the copies are aligned to: this audio, `delay` samples ago."""
        needed = frames + delay
        dry = self._dry
        if dry.size < needed:
            dry = np.concatenate((np.zeros(needed - dry.size, np.float32), dry))
        if delay <= 0:
            return dry[-frames:]
        return dry[-needed:-delay]

    def _ramp(self, value, goal):
        step = 1.0 / float(self.FADE_FRAMES)
        if value < goal:
            return min(goal, value + step)
        if value > goal:
            return max(goal, value - step)
        return value

    # ------------------------------------------------------------- lifecycle
    def latency_samples(self):
        """How far the lead is held back right now (0 when nothing sings)."""
        with self._lock:
            return 0 if not self._voices else self._target

    def reset(self):
        """Forget the session: the next frame is a fresh string."""
        with self._lock:
            self._dry = np.zeros((0,), dtype=np.float32)
            self._voices = []
            self._target = self._previous = 0
            self._bypass = 1.0


def _to_pcm(samples):
    return (np.clip(samples, -1.0, 1.0) * 32767.0).round().astype("<i2").tobytes()
