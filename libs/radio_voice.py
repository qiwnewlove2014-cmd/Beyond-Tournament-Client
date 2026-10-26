"""The radio in your hand: the pedal on the shelf, made of a telephone band.

Why this exists
---------------
A player holding a radio hears a second copy of a speaker's voice, queued into
`radio_source` by `voice_chat._play_voice_frame` (20 ms frames, 48 kHz, mono,
non-spatial, gain 0.7). That copy used to be coloured by OpenAL EFX: a
`DISTORTION` (edge 0.2, gain 0.5, low-pass 8 kHz) chained into an
`EQUALIZER` (below 800 Hz and above 4 kHz both down), built **per player
entity** -- two of the driver's 64 effect slots each, so a twenty-player lobby
spent forty slots on one tone (`AudioManager._init_slot_pool` says what that
did to the rooms that could not borrow one).

The tone is now the Voice Effect list's own **`radio` pedal**
(`_SPECS["radio"]`: 430 Hz high-pass, 2.8 kHz cabinet, 6x tanh with a 0.05
bias, 0.85 out), applied here by the same `_EffectProcessor` every other pedal
and both boards are built by. The radio in your hand is the pedal on the shelf
rather than a second, different telephone -- and the slots it used to hold go
back to the pool. **One block is the radio's own** (`WAH`): a gentle auto-wah
at the end of the chain, because a voice coming out of a speaker that moves is
the sound a player asked for, and because the pedal's 430-2800 Hz band leaves a
narrow sweep that reads as movement rather than as a filter.

Rules this file keeps
---------------------
* **One frame in, one frame out, the same length.** The copy is queued into the
  same OpenAL source as before and a frame that changed length would tear the
  stream.
* **The decode worker, never the main thread.** `frame()` is called from
  `voice_chat_compression.recieve2`, before the frame is handed to the main
  thread's audio inbox; the table is locked because two channels can decode at
  once.
* **Only the radio copy.** The spatial `vc_source` still plays the frame
  untouched. The Voice Effect feature lives on the SEND path (before Opus, in
  `voice_effects.VoiceEffectLine`) and this is the RECEIVE path, so neither can
  move the other -- pinned by `test_radio_voice`.
* **State is per speaker** (`voice_chat._radio_key` names one, from the
  sender's id where the leg carries it and from the receiver's own source where
  it does not). A biquad and the cabinet carry memory between frames: two
  speakers sharing one processor would smear one's tail into the other's
  first syllable. The auto-wah's envelope is per speaker for the same reason --
  one voice's loud syllable must not open another's filter.
* **The tone is the shelf's `Radio` plus one block** (`radio_spec`): the wah is
  added to a copy of the row, never to `_SPECS`, so no pedal in the Music Bot
  grows a wah because the radio did. `MAX_SPEAKERS` bounds the table, and a speaker who
  has not been heard from since 32 others spoke loses filter memory, never the
  feature.
* **Silence is cheap, and a key-up clicks.** Frames quieter than
  `SILENT_PEAK` are counted; after `QUIET_FRAMES` of them the radio is shut,
  hands back silence and does no work, and the first frame of the next run
  opens with a short band-limited click (12 ms, `SQUELCH_LEVEL`). That click is
  the one sound here that is the radio's rather than the pedal's -- set
  `SQUELCH_LEVEL = 0.0` and the radio has no key-up at all.
"""

import collections
import threading

import numpy as np

#: The pedal this file plays: a row of `music_bot.voice_effects._SPECS`, chosen
#: because it is the same telephone the Voice Effect list offers under `Radio`.
PRESET = "radio"

#: The one block the radio has and the shelf does not: a **gentle** auto-wah at
#: the end of the chain (the processor runs its wah block after the cabinet), so
#: the band moves with the voice instead of sitting still. The board's own
#: numbers are a guitar pedal -- 300-1900 Hz at Q 3 with a 10x lift -- and on a
#: telephone band that takes the whole band with it and the words stop being
#: words. These sweep 500-1400 Hz at Q 1.4 and lift 1.8x: a radio that moves,
#: not an effect that replaces the voice.
WAH = (500.0, 1400.0, 1.4, 1.8)

#: The shelf's `out` (0.85), trimmed for the wah's 1.8x lift. Measured, not
#: guessed: the lift adds 3.4 dB to a speech-like frame, and the radio copy is
#: already played at `radio_source.gain` 0.7, so without the trim a radio in the
#: ear would be as loud as the person standing beside you instead of about 3 dB
#: under them -- which is where it was before the colour moved here.
OUT = 0.57

SAMPLE_RATE = 48000

#: How many speakers keep their own filter memory. A radio channel is a handful
#: of people; 32 is every voice one player could plausibly still be hearing.
MAX_SPEAKERS = 32

#: The key-up click: how loud, how long, and after how much silence.
SQUELCH_LEVEL = 0.05
SQUELCH_MS = 12.0

#: Frames quieter than this (peak, full scale) count as silence: 0.004 is -48
#: dBFS, below a room's noise floor and above a digital zero.
SILENT_PEAK = 0.004

#: How many silent frames in a row shut the radio. Six is 120 ms -- longer than
#: a word's gap between syllables and shorter than a breath, so a sentence does
#: not re-click at every comma.
QUIET_FRAMES = 6

_lock = threading.Lock()
_table = collections.OrderedDict()


def radio_spec():
    """The shelf's `radio` row, plus `WAH` and nothing else.

    Its own function so the chain and the test that pins it read the same
    dictionary: the radio is the shelf's pedal, which is what makes it
    defensible, and the wah is the one block it adds, which is what the player
    asked for. A copy, never the table's own row -- mutating `_SPECS` would give
    every `Radio` pedal in the Music Bot a wah.
    """
    from .music_bot import voice_effects
    spec = dict(voice_effects._SPECS[PRESET])
    spec["envelope_wah"] = WAH
    spec["out"] = OUT
    return spec


def _processor():
    """A fresh chain for one speaker, built by the feature that owns the pedal.

    Imported inside the call: the music bot's tables are read when a radio is
    actually heard, not when the client starts.
    """
    from .music_bot import voice_effects
    return voice_effects._EffectProcessor(PRESET, spec=radio_spec())


def _squelch_click():
    """The key-up click, made once: 12 ms of noise between 800 and 3000 Hz.

    Band-limited by keeping the bins of a telephone band and dropping the rest,
    which is why it reads as a radio's own click rather than a burst of white
    noise with a click's pancake-flat spectrum. The envelope is a half-sine, so
    it opens and closes without an edge of its own.
    """
    count = int(round(SAMPLE_RATE * SQUELCH_MS / 1000.0))
    noise = np.random.default_rng(7).standard_normal(count).astype(np.float32)
    spectrum = np.fft.rfft(noise)
    freqs = np.fft.rfftfreq(count, 1.0 / SAMPLE_RATE)
    spectrum[(freqs < 800.0) | (freqs > 3000.0)] = 0.0
    burst = np.fft.irfft(spectrum, count).astype(np.float32)
    peak = float(np.max(np.abs(burst))) or 1.0
    envelope = np.sin(np.linspace(0.0, np.pi, count, dtype=np.float32))
    return (burst / peak * envelope).astype(np.float32)


_click = None


def _click_samples():
    """The click at the radio's own sample rate, built on first use."""
    global _click
    if _click is None:
        _click = _squelch_click()
    return _click


class _Speaker:
    """One voice's radio copy: the chain, and whether it is transmitting."""

    __slots__ = ("processor", "quiet")

    def __init__(self):
        self.processor = _processor()
        # A first transmission opens the squelch: there was no stream before it.
        self.quiet = QUIET_FRAMES


def _speaker(key):
    """The chain for one speaker, the least recently heard dropped when full.

    The table is ordered by use, so the first key is the voice heard from
    longest ago -- and the one to forget when a 33rd speaker starts. A channel
    change is a new key and a clean chain on purpose: a radio's filters must
    not carry one conversation into the next.
    """
    speaker = _table.get(key)
    if speaker is None:
        if len(_table) >= MAX_SPEAKERS:
            _table.popitem(last=False)
        speaker = _Speaker()
    _table[key] = speaker
    _table.move_to_end(key)
    return speaker


def frame(key, pcm):
    """The radio copy of one 20 ms frame, coloured in the pedal's own band.

    ``key`` names the speaker -- ``(channel, sender)`` in `voice_chat`, or the
    receiver's own source when the leg carries no sender id -- so two voices
    never share a filter, and a channel change starts a clean chain.
    """
    data = bytes(pcm)
    size = len(data) // 2
    if size <= 0:
        return data
    samples = np.frombuffer(data, dtype="<i2")
    loud = float(np.max(np.abs(samples))) / 32768.0

    with _lock:
        speaker = _speaker(key)
        if loud < SILENT_PEAK:
            speaker.quiet += 1
            if speaker.quiet >= QUIET_FRAMES:
                # Shut. Between here and the next word nothing is processed:
                # the cabinet's tail is 2.7 ms and was already let out by the
                # frames just inside the pause, so a long pause costs no CPU.
                return bytes(len(data))
            # Still inside the pause: the chain runs, so the tail rings out
            # over real zeros instead of being cut at the first silent frame.
            resumed = False
        else:
            # Keyed up. The click is for a radio that had shut, not for the
            # gap between two syllables (which is under `QUIET_FRAMES`).
            resumed = speaker.quiet >= QUIET_FRAMES
            speaker.quiet = 0

    shaped = np.frombuffer(speaker.processor.process(data), dtype="<i2")
    if resumed:
        shaped = _with_squelch(shaped)
    return shaped.astype("<i2").tobytes()


def _with_squelch(samples):
    """Mix the key-up click into the first `SQUELCH_MS` of a frame."""
    click = _click_samples()
    count = min(click.size, samples.size)
    out = samples.astype(np.float32)
    out[:count] += click[:count] * (SQUELCH_LEVEL * 32768.0)
    return np.clip(out, -32768.0, 32767.0).round().astype("<i2")


def reset():
    """Forget every speaker: a map change, a channel change, or a test."""
    with _lock:
        _table.clear()
