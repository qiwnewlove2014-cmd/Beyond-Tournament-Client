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

**The DSP is not this module's.** `voice_chat.FastHarmonyLine` is the line
itself -- written here for a guitar, and since shared with the microphone's
press that only wants the copies (`voice_chat._speech_line_for`). One
implementation, two callers. What is this module's is the *scene*: the
Instrument page's switch, the Auto-Tune lock that hides it, and the rule for
when a string may sing at all (`GuitarHarmony._reading`) -- a guitar's ear is
dry and its own switch decides, not a press.

Three rules the shared line keeps:

**Only what leaves is shaped.** `InstrumentInput._stage` hands the player's own
ear its 10 ms chunks the moment they exist, before a 20 ms frame is assembled,
so the ear never sees this line: the guitarist plays at the latency they always
did and everybody else hears the harmony. The note tracker is fed the dry frame
for the same reason -- `pitch.PitchTracker` naming notes from a chord it never
heard is exactly the mis-transcription it does not need.

**25 ms, not 107.** The tuner's line holds a tenth of a second of a voice,
which a player monitoring themselves would feel as a lag. A copy above a string
lands 26.7 ms behind it with `transpose.GuitarTransposer` (the same vocoder on a
21 ms window), and the lead is held back by the same amount so the two voices
are a chord rather than a slap. A BASS copy reads slower than its source and
needs more stretched audio for the same second of playing, so it costs 32-53
ms; the line delays the lead by whatever its slowest singing copy needs, and
not a sample more.

**It opens and closes without a click.** The lead's delay is what makes the
chord; it is also a jump the moment the line opens or closes (26 ms of audio
repeated, or skipped). So the output is a blend of the delayed lead and the
live one, ramped over three frames, and a copy is faded -- never cut -- when it
is switched off. The same rule drops the copies when the Music Bot's intervals
change under a sounding chord.
"""
from .voice_chat import FastHarmonyLine
from .voice_chat import copy_delay as _copy_delay
from .voice_chat import harmony_ids as _harmony_ids

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
    this switch with it -- in the Instrument page and at the frame itself.
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

    The list both harmony lines sing (`voice_chat.harmony_ids` has the rule);
    this name stays because the switch's label and both menus read it.
    """
    return _harmony_ids()


def copy_delay(interval_id, lookahead):
    """How far behind its own source one copy lands, in samples.

    `voice_chat.copy_delay` owns the arithmetic; kept here for the guitar's
    callers and its tests, which ask a string's question.
    """
    return _copy_delay(interval_id, lookahead)


class GuitarHarmony(FastHarmonyLine):
    """The line-in guitar's harmony: the shared fast line, under its switch.

    The microphone builds a line for the press that wants the copies; a guitar
    builds one for the session and asks this subclass, every frame, whether
    its Options switch is on and its family is unlocked. Nothing else differs:
    the copies, the lead delay, the fades and the measurements are the shared
    line's own.
    """

    def _reading(self):
        return enabled() and not gated()
