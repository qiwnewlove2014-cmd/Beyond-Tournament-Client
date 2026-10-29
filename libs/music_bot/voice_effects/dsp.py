"""The broadcast voice through a guitar amp -- for a player with no guitar.

What this is
------------
A player who cannot play a guitar can still want the *sound*: a voice pushed
through a distorted amp, a clean amp with a chorus, a fuzz pedal, a small radio
or a bitcrusher. Nothing here becomes a string or a note -- a voice through a
guitar amp is a voice through a guitar amp, and that is the joke. The presets are
the pedals on the floor; the voice is still the player's own.

**It is a line, like every other voice effect.** The broadcast voice is shaped on
the talker's machine, in one 20 ms frame at a time, before Opus: a frame in is
the same frame out, so the PA's sequence numbers, a cinema room's playout, the
jam clock and a listener's decoder count exactly what they counted before. The
line WRAPS whatever line the press already had (`AutoTuneLine`, the fast harmony
line or `SpeechKeyLine`), so Auto-Tune, Harmony and the speech key keep working
and the effect is the last thing measured before the voice leaves. With nothing
else on, the wrapper holds no other line and the effect is the only work.

Only the broadcast leg is wrapped -- `VoiceChatRecord._speech_line_for` is what
decides that, exactly as it does for Auto-Tune. Proximity voice stays raw.

The chain, in order
-------------------
1. A second-order high-pass at the preset's own corner (95 Hz for the amp, 430
   for the radio), so a desk rumble is not driven into the clipper. It is a
   biquad and not an FIR deliberately: a 129-tap windowed-sinc at 95 Hz still
   passes only 0.76 of a 220 Hz tone, so the "high-pass" was taking 12 dB out
   of the voice's own fundamental. Two poles put the corner where it is named,
   and cost a couple of samples of delay instead of a look-ahead.
2. A noise gate. High gain on an open microphone is a feedback loop waiting for
   a room; the gate closes over about eight frames when the frame's RMS falls
   below the preset's threshold and opens in half a frame, always with a ramp
   across the frame rather than a step (a step in gain is a click).
3. The shaper: `tanh` with a little bias for the amp and radio (a valve's
   asymmetric soft clip), a hard, more biased clip for the fuzz, and nothing for
   the clean amp or the crusher.
4. The crusher, when the preset has one: sample-and-hold at 8 kHz and 6 bits.
5. The octave, when the preset wants one: the rectified voice mixed back with
   the dry one. Rectification is the oldest octave trick there is -- it adds a
   copy an octave up without a pitch shifter, and it is dirty on purpose. It
   runs BEFORE the DC block because a rectified waveform has a large offset.
6. The cabinet: a linear-phase low-pass at 4.2 kHz (3.4 for the fuzz, 6.5 for
   the clean amp), then a 20 Hz high-pass that removes the DC the asymmetric
   shaper leaves behind.
7. The chorus, on the clean preset: a 12 ms delay swept 3 ms either way at
   0.45 Hz, mixed a third in.
8. The pedals that are one block each, off unless the preset names them: a
   tremolo (5.2 Hz, depth 0.85), an auto-wah (a resonant band swept 360-1800 Hz
   at 1.25 Hz), a two-stage phaser (all-passes swept 320-1500 Hz at 0.4 Hz), a
   slapback echo (145 ms, 0.35 feedback) and a ring modulator (55 Hz).
9. The modern pedals (2026-10-25): an exciter (Air), a raised formant
   (Talkbox), a channel vocoder, an upward-and-downward compressor (Hyperpop),
   a stepped three-band filter, a beat duck (Pump), four drifting taps (Cloud),
   a backwards ghost (Swell), a held slice (Freeze) and a varispeed that slows
   to a stop (Tape Stop). Two rules keep them out of trouble: **a gain that
   moves once per frame is a 50 Hz buzz**, so Hyperpop's follower runs per
   sample and the step filter crossfades the frame its band changes in; and **a
   varispeed is a delay that grows faster than real time** (rate `1 -
   d(delay)/dt`), so every gain is zero where a head jumps.
10. The valve sag, on `Sag` and on the Custom Amp's own chain: the supply's
   gain rather than the output's. It dips under a loud passage and recovers
   over about six frames, interpolated across the frame so the recovery cannot
   step -- the same rule as the follower above, for the same reason.11. **The player's own channel strip** (`_ToneStack`, `tone.py`), after the
    preset has made its sound: a low shelf, two bells and a high shelf, saved
    per preset and flat until somebody moves one. Around them sit the mixer's
    controls -- a low cut, a de-esser, a high cut, a mix, a compressor and a
    level. The de-esser is an event, not a filter: it reads the frame's own top
    (a 4.8 kHz high-pass a vowel barely crosses and a sibilant fills) and
    ducks the frame while that top is over its threshold -- a band split was
    built first and measured able to take only 2.4 dB off an 8 kHz `s`, so it
    was not kept. Flat builds no filter at all, so the hundred and forty-four
    presets tuned by ear here are byte-for-byte what they were. Only
    `VoiceEffectLine` asks for it: the in-game radio is this same class on the
    receive path and must not hear a tone saved for the `Radio` pedal.
12. Output trim and a soft ceiling. Opus's `voip` mode is speech-tuned and will
    thin the top of any distortion it is handed; the cabinet is deliberately
    below the range where that hurts, and the ceiling keeps the encoder from
    being handed a clipped frame. A **board** -- and a fixed preset whose tone
    has been moved -- also gets a loudness trim here (`VOICE_LOUDNESS`): a fixed
    preset was tuned by ear against a PA, while ten slots can stack a shaper, a
    crusher, a sub octave and `Output: Loud` into a square wave whose average is
    sixteen decibels above a voice's -- the level a room's bass shelf, speech
    compressor and receive-side limiter were never built for.

The cabinet is FIR (windowed sinc, 257 taps), so its delay is a known constant:
128 samples after the shaper, 2.7 ms, well inside the 20 ms frame. `_Fir` carries
its tail from frame to frame, so the stream is continuous across frame
boundaries. The high-passes are biquads (see above), whose delay is a few
samples and not worth a look-ahead.

Switching a preset mid-sentence
-------------------------------
The preset is read per frame, like Auto-Tune's switch. A change is crossfaded
from the previous frame's output into the new one, sample by sample, because a
hard switch is a step the size of the waveform -- and that is heard as a click.
The first frame of a press has nothing to crossfade from (the microphone was
silent), so it starts clean.

The shelves and the boards
--------------------------
The Amp & Drive family holds the original six pedals, the twenty amps added on
2026-10-26 (`Crunch` to `Twelve String` -- each one a different corner, band,
cabinet and bias on this same chain), the twenty more of 2026-10-27 (from
`Bright Stack` to `Amp Tremolo`, each with a high-pass/cabinet pair no other
row in the family has) and `Custom Amp`, which is not a preset at all: `custom.py` holds a table of knobs, and its spec is built from
the board the player set. The Vocoder Lab family ends the same way: its twenty
machines are fixed carriers, and `vocoder.py` is the board that
exposes the carrier -- its mode, note, scale degree, waveform, band shift,
breath and mix. A knob change on either board bumps that board's revision, and
the line rebuilds its processor on the next frame and crossfades into it -- the
knobs land on the next 20 ms rather than at the next pedal change.

The vocoder board is Developer/Contributor-only: the Server owns the flag
(`canUseVocoderLab`), and `set_vocoder_lab_allowed` below is written from that
snapshot so `effect_id()` can refuse a board saved on an account that no longer
has it. The capture thread cannot call into gameplay, and the permission must
be a plain bool it can read.

The Developer's lock
--------------------
`voice_fx` is a row in `server/libs/music_bot_gate.ts`. While it is locked the
preset reads as Off, so a saved effect stops applying and the voice is dry; the
Settings line is hidden by the same gate.

Pinned by `client/tests/test_voice_effects.py`.
"""

import collections
import threading

import numpy as np

from ... import options
from .. import gate
from . import tone
from .catalogue import (BUILDER_EFFECT, CUSTOM_EFFECT,
                                     DEFAULT_EFFECT, PAN_EFFECT,
                                     STANDALONE_EFFECTS, VOICE_EFFECTS,
                                     VOICE_EFFECT_GROUPS, VOX_LAB_EFFECT,
                                     catalogue_problems, clamp_effect,
                                     effect_label, effect_note, group_label,
                                     group_members, group_note, group_of)

#: One microphone frame: 20 ms of 48 kHz mono16, the unit every voice line is
#: fed (`libs/voice_chat.VOICE_FRAME_BYTES`).
FRAME_BYTES = 1920
SAMPLE_RATE = 48000


#: The two saved option keys live here, beside the catalogue and the only reader.
OPTION = "music_bot_voice_effect"

#: What each preset does, in pieces the processor can build. ``hp``/``lp`` are
#: the cabinet's corners in Hz, ``drive`` the gain into the shaper, ``bias`` the
#: asymmetry, ``gate`` the RMS floor and ``out`` the trim after the cabinet.
_SPECS = {
    "amp": dict(hp=95.0, lp=4200.0, drive=9.0, shape="tanh", bias=0.10,
                gate=0.008, out=0.72, chorus=0.0, crush=None),
    "clean": dict(hp=100.0, lp=6500.0, drive=1.0, shape=None, bias=0.0,
                  gate=0.004, out=0.92, chorus=0.35, crush=None),
    "fuzz": dict(hp=110.0, lp=3400.0, drive=22.0, shape="hard", bias=0.18,
                 gate=0.010, out=0.55, chorus=0.0, crush=None),
    "radio": dict(hp=430.0, lp=2800.0, drive=6.0, shape="tanh", bias=0.05,
                  gate=0.008, out=0.85, chorus=0.0, crush=None),
    "crush": dict(hp=80.0, lp=5000.0, drive=1.0, shape=None, bias=0.0,
                  gate=0.006, out=0.80, chorus=0.0, crush=(6, 8000.0)),
    # The pedals below reuse the same chain and add one block each; every
    # setting is read with `.get` so an older preset carries no dead keys.
    "octave": dict(hp=110.0, lp=5500.0, drive=1.0, shape=None, bias=0.0,
                   gate=0.006, out=1.55, octave=0.65),
    "tremolo": dict(hp=100.0, lp=6200.0, drive=1.0, shape=None, bias=0.0,
                    gate=0.004, out=0.94, tremolo=(5.2, 0.85)),
    "wah": dict(hp=120.0, lp=5000.0, drive=1.0, shape=None, bias=0.0,
                gate=0.005, out=0.95, wah=(360.0, 1800.0, 1.25, 3.2, 4.6)),
    "phaser": dict(hp=100.0, lp=6500.0, drive=1.0, shape=None, bias=0.0,
                   gate=0.004, out=1.0, phaser=(0.4, 320.0, 1500.0)),
    "echo": dict(hp=100.0, lp=5200.0, drive=1.0, shape=None, bias=0.0,
                 gate=0.005, out=0.94, echo=(0.145, 0.35, 0.45)),
    "robot": dict(hp=120.0, lp=6000.0, drive=1.0, shape=None, bias=0.0,
                  gate=0.004, out=0.90, robot=(55.0, 0.78)),
    "flanger": dict(hp=100.0, lp=6200.0, drive=1.0, shape=None, bias=0.0,
                    gate=0.004, out=0.95,
                    flanger=(1.5, 4.0, 0.25, 0.45, 0.55)),
    "vibrato": dict(hp=100.0, lp=6500.0, drive=1.0, shape=None, bias=0.0,
                    gate=0.004, out=0.95, vibrato=(6.0, 2.5, 5.5)),
    "stutter": dict(hp=100.0, lp=6000.0, drive=1.0, shape=None, bias=0.0,
                    gate=0.004, out=0.95, stutter=(5, 10)),
    "cave": dict(hp=110.0, lp=4800.0, drive=1.0, shape=None, bias=0.0,
                 gate=0.005, out=1.5,
                 cave=((31.0, 47.0, 61.0, 83.0), 0.45, 0.55)),
    "alien": dict(hp=120.0, lp=6000.0, drive=1.0, shape=None, bias=0.0,
                  gate=0.004, out=0.92, alien=(300.0, 0.7, 0.72)),
    "sub_octave": dict(hp=90.0, lp=5000.0, drive=1.0, shape=None, bias=0.0,
                       gate=0.005, out=0.95, sub_octave=0.5),
    "envelope_wah": dict(hp=110.0, lp=5200.0, drive=1.0, shape=None, bias=0.0,
                         gate=0.004, out=0.90,
                         envelope_wah=(300.0, 1900.0, 3.0, 10.0)),
    "gate": dict(hp=100.0, lp=6000.0, drive=1.0, shape=None, bias=0.0,
                 gate=0.004, out=0.95, chop=(8.0, 0.06)),
    "reverse": dict(hp=100.0, lp=6000.0, drive=1.0, shape=None, bias=0.0,
                    gate=0.004, out=0.95, reverse=(5, 10)),
    "chorus": dict(hp=100.0, lp=6800.0, drive=1.0, shape=None, bias=0.0,
                   gate=0.004, out=0.92,
                   chorus3=((7.0, 9.0, 12.0), 0.35, 1.6, 0.5)),
    "vinyl": dict(hp=80.0, lp=5500.0, drive=1.0, shape=None, bias=0.0,
                  gate=0.005, out=0.98, vinyl=(0.6, 0.45, 0.30)),
    "body": dict(hp=90.0, lp=5200.0, drive=1.0, shape=None, bias=0.0,
                 gate=0.005, out=1.0, body=(110.0, 0.72, 0.55)),
    "shimmer": dict(hp=100.0, lp=6000.0, drive=1.0, shape=None, bias=0.0,
                    gate=0.004, out=0.95, shimmer=(14.0, 0.62)),
    "whisper": dict(hp=900.0, lp=7000.0, drive=1.0, shape=None, bias=0.0,
                    gate=0.003, out=1.30, whisper=(0.45, 0.50)),
    # The hiss pair (2026-10-26): the whisper pedal with its voice taken out, so
    # what is left is the breath alone -- a band of noise riding the syllable's
    # own envelope, bright and dry. It is deliberately NOT a whisper: it has no
    # formants and no words, which is why it reads as a brush on a snare rather
    # than as somebody speaking. That is the sound it is here for.
    "hiss": dict(hp=1100.0, lp=7000.0, drive=1.0, shape=None, bias=0.0,
                 gate=0.002, out=1.15, whisper=(0.95, 0.0)),
    "hiss_room": dict(hp=1100.0, lp=6000.0, drive=1.0, shape=None, bias=0.0,
                      gate=0.002, out=1.20,
                      cave=((29.0, 43.0, 67.0, 97.0), 0.40, 0.40),
                      whisper=(0.95, 0.0)),
    "growl": dict(hp=80.0, lp=6000.0, drive=2.2, shape="tanh", bias=0.04,
                  gate=0.005, out=0.95, growl=(450.0, 0.80)),
    "doubler": dict(hp=100.0, lp=6500.0, drive=1.0, shape=None, bias=0.0,
                    gate=0.004, out=0.95,
                    doubler=(22.0, 1.5, 0.33, 0.55)),
    # The modern pedals. A talkbox is a formant pushed up (Growl is the same
    # trick down), a vocoder replaces the excitation and keeps the envelopes,
    # and the rest are a production gesture each.
    "vocoder": dict(hp=90.0, lp=7500.0, drive=1.0, shape=None, bias=0.0,
                    gate=0.004, out=1.3, vocoder=(2.6, 0.9)),
    "talkbox": dict(hp=300.0, lp=7200.0, drive=3.4, shape="tanh", bias=0.06,
                    gate=0.006, out=0.72, formant=(2300.0, 1.1, 0.55, 1.9)),
    "hyperpop": dict(hp=80.0, lp=9000.0, drive=1.0, shape=None, bias=0.0,
                     gate=0.004, out=1.5, ott=(0.9, 0.35)),
    "tapestop": dict(hp=120.0, lp=6000.0, drive=1.0, shape=None, bias=0.0,
                     gate=0.004, out=0.95, tapestop=(0.55, 0.85)),
    "cloud": dict(hp=120.0, lp=5200.0, drive=1.0, shape=None, bias=0.0,
                  gate=0.005, out=1.3,
                  cloud=((95.0, 160.0, 240.0, 330.0), 0.11, 16.0, 0.62)),
    "air": dict(hp=150.0, lp=12000.0, drive=1.0, shape=None, bias=0.0,
                gate=0.004, out=0.95, air=(4200.0, 2.6, 0.45)),
    "pump": dict(hp=100.0, lp=6500.0, drive=1.0, shape=None, bias=0.0,
                 gate=0.004, out=0.95, pump=(128.0, 0.8, 0.22)),
    "freeze": dict(hp=110.0, lp=5200.0, drive=1.0, shape=None, bias=0.0,
                   gate=0.004, out=0.95, freeze=(8, 60)),
    "step": dict(hp=120.0, lp=6500.0, drive=1.0, shape=None, bias=0.0,
                 gate=0.004, out=1.35,
                 step=(7.0, (250.0, 900.0, 3000.0), 1.4)),
    "swell": dict(hp=110.0, lp=5500.0, drive=1.0, shape=None, bias=0.0,
                  gate=0.004, out=0.95, swell=(0.06, 1.10, 0.55, 0.9, 0.5)),
    # The studio ten: a record's vocal chain, one piece at a time.
    "tube": dict(hp=90.0, lp=9000.0, drive=1.9, shape="tanh", bias=0.15,
                 gate=0.005, out=0.92),
    "presence": dict(hp=140.0, lp=13000.0, drive=1.0, shape=None, bias=0.0,
                     gate=0.004, out=1.15,
                     formant=(3200.0, 1.15, 0.45, 1.7),
                     air=(9000.0, 1.8, 0.18)),
    "plate": dict(hp=220.0, lp=7500.0, drive=1.0, shape=None, bias=0.0,
                  gate=0.004, out=0.95,
                  cluster=((18.0, 27.0, 41.0, 58.0, 79.0, 97.0),
                           (0.8, 0.7, 0.6, 0.5, 0.42, 0.35), 0.22, 0.42)),
    "gated": dict(hp=180.0, lp=7000.0, drive=1.0, shape=None, bias=0.0,
                  gate=0.004, out=0.95,
                  gated=((14.0, 23.0, 34.0, 47.0, 63.0, 81.0),
                         (0.8, 0.72, 0.62, 0.54, 0.46, 0.4), 0.22, 0.55,
                         0.22, 0.02)),
    "throw": dict(hp=120.0, lp=6000.0, drive=1.0, shape=None, bias=0.0,
                  gate=0.004, out=0.95,
                  dark_echo=(0.375, 0.45, 0.5, 2400.0)),
    "dub": dict(hp=100.0, lp=5000.0, drive=1.0, shape=None, bias=0.0,
                gate=0.004, out=0.95,
                dark_echo=(0.24, 0.68, 0.6, 1100.0)),
    "wash": dict(hp=160.0, lp=6000.0, drive=1.0, shape=None, bias=0.0,
                 gate=0.004, out=0.95,
                 cluster=((90.0, 150.0, 220.0, 300.0),
                          (0.7, 0.6, 0.5, 0.4), 0.34, 0.8)),
    "choir": dict(hp=110.0, lp=7500.0, drive=1.0, shape=None, bias=0.0,
                  gate=0.004, out=0.95, octave=0.30,
                  chorus3=((9.0, 14.0, 19.0), 0.24, 1.2, 0.45)),
    "riser": dict(hp=110.0, lp=6500.0, drive=1.0, shape=None, bias=0.0,
                  gate=0.004, out=0.95, riser=(0.42, 0.55, 1.3, 0.55)),
    "scream": dict(hp=150.0, lp=5200.0, drive=16.0, shape="hard", bias=0.2,
                   gate=0.012, out=0.50,
                   formant=(1700.0, 1.3, 0.35, 1.5)),
    # The twenty amps (2026-10-26): the same chain, one voicing each. The
    # shelves are built from the four things that make an amp an amp -- where
    # the bass is cut, which band is lifted, where the cabinet stops and how
    # the clip is biased -- so no two of them answer the same way even when
    # they share a drive.
    "crunch": dict(hp=110.0, lp=4600.0, drive=4.6, shape="tanh", bias=0.08,
                   gate=0.006, out=0.82, formant=(800.0, 1.1, 0.35, 1.6)),
    "overdrive": dict(hp=140.0, lp=3900.0, drive=6.8, shape="tanh",
                      bias=0.06, gate=0.006, out=0.80,
                      formant=(700.0, 1.2, 0.45, 1.7)),
    "boost": dict(hp=100.0, lp=5200.0, drive=2.0, shape="tanh", bias=0.03,
                  gate=0.004, out=1.05),
    "lead": dict(hp=130.0, lp=4800.0, drive=13.0, shape="tanh", bias=0.10,
                 gate=0.010, out=0.60, formant=(1100.0, 1.1, 0.40, 1.6)),
    "modern": dict(hp=150.0, lp=4200.0, drive=19.0, shape="hard", bias=0.10,
                   gate=0.010, out=0.52),
    "tweed": dict(hp=130.0, lp=3100.0, drive=5.8, shape="tanh", bias=0.14,
                  gate=0.008, out=0.80, formant=(850.0, 1.3, 0.40, 1.6)),
    "brown": dict(hp=120.0, lp=3600.0, drive=8.5, shape="tanh", bias=0.12,
                  gate=0.008, out=0.72, formant=(650.0, 1.2, 0.40, 1.6)),
    "hotrod": dict(hp=150.0, lp=3300.0, drive=11.0, shape="tanh", bias=0.12,
                   gate=0.009, out=0.66, air=(6500.0, 2.0, 0.22)),
    "chime": dict(hp=110.0, lp=7000.0, drive=3.2, shape="tanh", bias=0.05,
                  gate=0.005, out=0.92, air=(7500.0, 2.2, 0.30)),
    "sag": dict(hp=100.0, lp=4400.0, drive=5.4, shape="tanh", bias=0.09,
                gate=0.006, out=0.78, sag=(0.45, 0.16)),
    "direct": dict(hp=70.0, lp=11000.0, drive=1.0, shape=None, bias=0.0,
                   gate=0.004, out=0.98),
    "acoustic": dict(hp=60.0, lp=10500.0, drive=1.0, shape=None, bias=0.0,
                     gate=0.003, out=0.98,
                     formant=(2700.0, 1.4, 0.30, 1.7),
                     air=(8000.0, 1.8, 0.20)),
    "jazz": dict(hp=90.0, lp=2800.0, drive=1.0, shape=None, bias=0.0,
                 gate=0.004, out=0.95, formant=(520.0, 1.2, 0.45, 1.5)),
    "room_cab": dict(hp=120.0, lp=4300.0, drive=7.0, shape="tanh",
                     bias=0.08, gate=0.008, out=0.70,
                     cluster=((9.0, 14.0, 19.0, 26.0),
                              (0.5, 0.42, 0.34, 0.28), 0.25, 0.22)),
    "doom": dict(hp=60.0, lp=2300.0, drive=24.0, shape="tanh", bias=0.16,
                 gate=0.012, out=0.48, sub_octave=0.45),
    "stoner": dict(hp=100.0, lp=3200.0, drive=15.0, shape="hard", bias=0.12,
                   gate=0.010, out=0.55,
                   wah=(300.0, 1200.0, 0.20, 2.6, 2.4)),
    "velcro": dict(hp=140.0, lp=3000.0, drive=21.0, shape="hard", bias=0.22,
                   gate=0.015, out=0.55, chop=(13.0, 0.10)),
    "octavia": dict(hp=120.0, lp=4800.0, drive=11.0, shape="hard",
                    bias=0.16, gate=0.008, out=0.70, octave=0.60),
    "fuzzsub": dict(hp=70.0, lp=2600.0, drive=18.0, shape="hard", bias=0.20,
                    gate=0.010, out=0.52, sub_octave=0.50),
    "twelve": dict(hp=90.0, lp=8200.0, drive=1.0, shape=None, bias=0.0,
                   gate=0.004, out=1.05, octave=0.26),
    # The third shelf (2026-10-27): twenty more voicings on the same chain.
    # The rule between them is a corner of their own: no two of the twenty,
    # and none of the rows above, share a high-pass and a cabinet corner --
    # the low reach and the top of the cabinet are the two numbers a voice
    # cannot help sounding, so they are what keeps these apart. The band and
    # the exciter are the second pair: a lift at a new centre or an air band
    # at a new corner, never both of an existing row's.
    "plexi": dict(hp=150.0, lp=5800.0, drive=8.0, shape="tanh", bias=0.09,
                  gate=0.007, out=0.78,
                  formant=(950.0, 1.15, 0.35, 1.6)),
    "scoop": dict(hp=85.0, lp=5200.0, drive=18.0, shape="hard", bias=0.10,
                  gate=0.010, out=0.55, air=(7000.0, 2.0, 0.22)),
    "treble_boost": dict(hp=330.0, lp=7200.0, drive=3.4, shape="tanh",
                         bias=0.03, gate=0.005, out=1.05,
                         air=(9000.0, 2.2, 0.34)),
    "bass_rig": dict(hp=40.0, lp=3500.0, drive=2.6, shape="tanh", bias=0.05,
                     gate=0.005, out=1.35,
                     formant=(600.0, 1.1, 0.25, 1.4)),
    "cocked": dict(hp=180.0, lp=4600.0, drive=5.5, shape="tanh", bias=0.07,
                   gate=0.006, out=0.85,
                   formant=(1250.0, 3.2, 0.75, 2.0)),
    "wool": dict(hp=95.0, lp=2900.0, drive=9.5, shape="hard", bias=0.26,
                 gate=0.008, out=0.70),
    "starved": dict(hp=215.0, lp=3000.0, drive=14.0, shape="hard", bias=0.30,
                    gate=0.026, out=0.95),
    "boxy": dict(hp=240.0, lp=2700.0, drive=6.5, shape="tanh", bias=0.12,
                 gate=0.007, out=0.95,
                 formant=(720.0, 1.6, 0.55, 1.8)),
    "studio_pre": dict(hp=75.0, lp=4600.0, drive=2.2, shape="tanh", bias=0.04,
                       gate=0.004, out=1.15,
                       formant=(1600.0, 1.0, 0.20, 1.3)),
    "tape": dict(hp=65.0, lp=4500.0, drive=2.0, shape="tanh", bias=0.02,
                 gate=0.004, out=1.10),
    "organ": dict(hp=300.0, lp=6000.0, drive=4.0, shape="tanh", bias=0.06,
                  gate=0.005, out=1.20,
                  formant=(2400.0, 1.2, 0.45, 1.5)),
    "air_amp": dict(hp=110.0, lp=9000.0, drive=1.4, shape="tanh", bias=0.03,
                    gate=0.004, out=1.05, air=(11000.0, 1.6, 0.28)),
    "horn": dict(hp=190.0, lp=3200.0, drive=9.0, shape="tanh", bias=0.15,
                 gate=0.008, out=0.80,
                 formant=(1500.0, 1.8, 0.60, 1.9)),
    "grind": dict(hp=120.0, lp=4000.0, drive=21.0, shape="hard", bias=0.12,
                  gate=0.011, out=0.55),
    "substack": dict(hp=55.0, lp=2200.0, drive=12.0, shape="tanh", bias=0.18,
                     gate=0.010, out=0.60, sub_octave=0.35),
    "sizzle": dict(hp=160.0, lp=8000.0, drive=6.0, shape="tanh", bias=0.07,
                   gate=0.006, out=0.85,
                   formant=(3200.0, 1.3, 0.40, 1.7)),
    "lowgrowl": dict(hp=105.0, lp=6800.0, drive=12.0, shape="tanh",
                     bias=0.20, gate=0.008, out=0.62,
                     formant=(420.0, 1.2, 0.50, 1.7)),
    "wall": dict(hp=50.0, lp=4300.0, drive=26.0, shape="hard", bias=0.18,
                 gate=0.012, out=0.50),
    "spring": dict(hp=130.0, lp=6400.0, drive=3.0, shape="tanh", bias=0.05,
                   gate=0.005, out=0.95,
                   cluster=((11.0, 17.0, 23.0, 31.0),
                            (0.50, 0.42, 0.34, 0.27), 0.20, 0.40)),
    "trem": dict(hp=145.0, lp=5000.0, drive=4.4, shape="tanh", bias=0.08,
                 gate=0.006, out=0.88, tremolo=(4.6, 0.65)),
    # The vocoder family (2026-10-26): twenty ways to hear the words spoken by a
    # machine. Every one of them is the same six-band bank; what changes is the
    # carrier -- its note (`hz`), its waveform, where its bands sit (`shift`,
    # below 1 for a bigger machine, above for a smaller one) and how much noise
    # rides on it -- plus at most one block from the rest of the file, so no two
    # of the twenty share a fingerprint. The classic `vocoder` row in Machine
    # stays as it was: `(q, mix)` reads exactly as it always did.
    "vox_classic": dict(hp=90.0, lp=7500.0, drive=1.0, shape=None, bias=0.0,
                        gate=0.004, out=1.25,
                        vocoder=(2.6, 0.90, 110.0, "saw", 1.0, 0.0)),
    "vox_deep": dict(hp=60.0, lp=6000.0, drive=1.0, shape=None, bias=0.0,
                     gate=0.004, out=1.30,
                     vocoder=(2.4, 0.92, 55.0, "saw", 0.80, 0.0)),
    "vox_chip": dict(hp=220.0, lp=6500.0, drive=1.0, shape=None, bias=0.0,
                     gate=0.004, out=1.50,
                     vocoder=(2.8, 0.86, 220.0, "saw", 1.25, 0.0)),
    "vox_giant": dict(hp=50.0, lp=5000.0, drive=1.0, shape=None, bias=0.0,
                      gate=0.004, out=1.35,
                      vocoder=(2.2, 0.90, 41.0, "saw", 0.70, 0.0)),
    "vox_gnome": dict(hp=300.0, lp=7000.0, drive=1.0, shape=None, bias=0.0,
                      gate=0.004, out=1.90,
                      vocoder=(3.0, 0.84, 330.0, "saw", 1.45, 0.0)),
    "vox_square": dict(hp=100.0, lp=6000.0, drive=1.0, shape=None, bias=0.0,
                       gate=0.004, out=1.25,
                       vocoder=(2.6, 0.88, 130.0, "square", 1.0, 0.0)),
    "vox_pulse": dict(hp=90.0, lp=5500.0, drive=1.0, shape=None, bias=0.0,
                      gate=0.004, out=1.25,
                      vocoder=(2.6, 0.90, 98.0, "pulse", 1.0, 0.0)),
    "vox_stack": dict(hp=90.0, lp=8000.0, drive=1.0, shape=None, bias=0.0,
                      gate=0.004, out=1.25,
                      vocoder=(2.6, 0.88, 110.0, "stack", 1.0, 0.0)),
    "vox_noise": dict(hp=200.0, lp=7000.0, drive=1.0, shape=None, bias=0.0,
                      gate=0.003, out=1.10,
                      vocoder=(2.2, 0.95, 110.0, "noise", 1.0, 0.0)),
    "vox_breath": dict(hp=100.0, lp=7000.0, drive=1.0, shape=None, bias=0.0,
                       gate=0.004, out=1.20,
                       vocoder=(2.6, 0.90, 110.0, "saw", 1.0, 0.45)),
    "vox_choir": dict(hp=110.0, lp=7500.0, drive=1.0, shape=None, bias=0.0,
                      gate=0.004, out=1.70,
                      vocoder=(2.6, 0.82, 110.0, "saw", 1.0, 0.0),
                      chorus3=((9.0, 13.0, 17.0), 0.22, 1.2, 0.40)),
    "vox_hall": dict(hp=120.0, lp=6000.0, drive=1.0, shape=None, bias=0.0,
                     gate=0.004, out=1.55,
                     vocoder=(2.6, 0.78, 120.0, "saw", 1.0, 0.0),
                     cave=((31.0, 47.0, 61.0, 83.0), 0.35, 0.35)),
    "vox_radio": dict(hp=420.0, lp=2900.0, drive=3.0, shape="tanh", bias=0.05,
                      gate=0.006, out=1.05,
                      vocoder=(2.8, 0.85, 150.0, "square", 1.0, 0.0)),
    "vox_bit": dict(hp=80.0, lp=6500.0, drive=1.0, shape=None, bias=0.0,
                    gate=0.005, out=1.15,
                    vocoder=(2.6, 0.85, 110.0, "saw", 1.0, 0.0),
                    crush=(5, 9000.0)),
    "vox_alien": dict(hp=120.0, lp=6500.0, drive=1.0, shape=None, bias=0.0,
                      gate=0.004, out=1.70,
                      vocoder=(2.6, 0.80, 110.0, "saw", 1.0, 0.0),
                      alien=(300.0, 0.70, 0.55)),
    "vox_shimmer": dict(hp=110.0, lp=7500.0, drive=1.0, shape=None, bias=0.0,
                        gate=0.004, out=1.50,
                        vocoder=(2.6, 0.80, 165.0, "saw", 1.0, 0.0),
                        shimmer=(12.0, 0.50)),
    "vox_iron": dict(hp=70.0, lp=5200.0, drive=3.0, shape="tanh", bias=0.06,
                     gate=0.006, out=0.85,
                     vocoder=(2.4, 0.85, 70.0, "saw", 0.90, 0.0),
                     formant=(420.0, 1.1, 0.40, 1.6)),
    "vox_air": dict(hp=150.0, lp=9000.0, drive=1.0, shape=None, bias=0.0,
                    gate=0.004, out=1.15,
                    vocoder=(2.8, 0.85, 165.0, "saw", 1.0, 0.0),
                    air=(6000.0, 2.4, 0.35)),
    "vox_formant": dict(hp=120.0, lp=7200.0, drive=1.0, shape=None, bias=0.0,
                        gate=0.004, out=1.90,
                        vocoder=(2.6, 0.82, 120.0, "saw", 1.0, 0.0),
                        formant=(2400.0, 1.2, 0.50, 1.8)),
    "vox_octave": dict(hp=110.0, lp=6000.0, drive=1.0, shape=None, bias=0.0,
                       gate=0.005, out=2.60,
                       vocoder=(2.6, 0.85, 110.0, "saw", 1.0, 0.0),
                       octave=0.40),
    # The radio family (2026-10-26): twenty channels, from a four-bit toy to a
    # cockpit headset. What makes a voice a *transmission* is four things and
    # they are each a dial here: the channel's corners (hp/lp), the codec
    # (`crush`, absent at the top), the noise floor (`static`, added after the
    # gate so it never follows the words) and how hard the channel is levelled
    # (`ott`). The ten clean room and console chains that used to top this
    # ladder are their own families now (`PA & Venue`, `Studio Console`).
    "rad_8bit": dict(hp=380.0, lp=3200.0, drive=1.0, shape=None, bias=0.0,
                     gate=0.006, out=2.20, crush=(4, 6000.0),
                     static=(0.016, 380.0, 3200.0)),
    "rad_broken": dict(hp=420.0, lp=2600.0, drive=5.0, shape="tanh", bias=0.12,
                       gate=0.008, out=0.76, crush=(4, 8000.0),
                       static=(0.022, 420.0, 2600.0)),
    "rad_toy": dict(hp=520.0, lp=3000.0, drive=1.0, shape=None, bias=0.0,
                    gate=0.006, out=2.24, crush=(5, 7000.0),
                    static=(0.014, 520.0, 3000.0)),
    "rad_cardboard": dict(hp=450.0, lp=2400.0, drive=3.0, shape="hard",
                          bias=0.10, gate=0.008, out=1.01,
                          crush=(6, 9000.0),
                          static=(0.010, 450.0, 2400.0)),
    "rad_intercom": dict(hp=350.0, lp=2800.0, drive=6.0, shape="tanh",
                         bias=0.05, gate=0.007, out=0.68,
                         crush=(6, 10000.0),
                         static=(0.008, 350.0, 2800.0)),
    "rad_cb": dict(hp=400.0, lp=3000.0, drive=2.0, shape="tanh", bias=0.04,
                   gate=0.006, out=2.10, ott=(0.35, 0.18),
                   static=(0.020, 400.0, 3000.0)),
    "rad_walkie": dict(hp=500.0, lp=3200.0, drive=1.0, shape=None, bias=0.0,
                       gate=0.006, out=2.67, crush=(7, 11000.0),
                       static=(0.018, 500.0, 3200.0)),
    "rad_drivethru": dict(hp=380.0, lp=3400.0, drive=1.8, shape="tanh",
                          bias=0.03, gate=0.005, out=1.90,
                          formant=(1800.0, 1.2, 0.35, 1.5),
                          static=(0.012, 380.0, 3400.0)),
    "rad_am": dict(hp=300.0, lp=4000.0, drive=1.0, shape=None, bias=0.0,
                   gate=0.005, out=3.34, robot=(55.0, 0.18),
                   ott=(0.30, 0.15), static=(0.022, 300.0, 4000.0)),
    "rad_shortwave": dict(hp=300.0, lp=3800.0, drive=1.0, shape=None,
                          bias=0.0, gate=0.005, out=2.69,
                          alien=(90.0, 0.35, 0.25),
                          static=(0.030, 300.0, 3800.0)),
    "rad_numbers": dict(hp=280.0, lp=3200.0, drive=1.0, shape=None, bias=0.0,
                        gate=0.005, out=2.51, robot=(60.0, 0.20),
                        static=(0.025, 280.0, 3200.0)),
    "rad_fieldphone": dict(hp=350.0, lp=2600.0, drive=2.4, shape="tanh",
                           bias=0.05, gate=0.006, out=1.17,
                           crush=(7, 9000.0),
                           static=(0.010, 350.0, 2600.0)),
    "rad_military": dict(hp=320.0, lp=3000.0, drive=3.0, shape="tanh",
                         bias=0.06, gate=0.006, out=1.60,
                         ott=(0.45, 0.22),
                         static=(0.015, 320.0, 3000.0)),
    "rad_police": dict(hp=350.0, lp=3200.0, drive=2.6, shape="tanh",
                       bias=0.05, gate=0.006, out=1.97,
                       ott=(0.50, 0.25),
                       static=(0.010, 350.0, 3200.0)),
    "rad_dispatch": dict(hp=320.0, lp=3400.0, drive=1.0, shape=None, bias=0.0,
                         gate=0.005, out=3.89, ott=(0.60, 0.30),
                         static=(0.008, 320.0, 3400.0)),
    "rad_marine": dict(hp=300.0, lp=3600.0, drive=1.0, shape=None, bias=0.0,
                       gate=0.005, out=3.78, ott=(0.60, 0.30),
                       static=(0.012, 300.0, 3600.0)),
    "rad_ham": dict(hp=280.0, lp=3800.0, drive=1.6, shape="tanh", bias=0.03,
                    gate=0.005, out=2.41, ott=(0.50, 0.25),
                    static=(0.020, 280.0, 3800.0)),
    "rad_airtraffic": dict(hp=250.0, lp=4500.0, drive=1.0, shape=None,
                           bias=0.0, gate=0.005, out=3.90,
                           ott=(0.70, 0.35),
                           static=(0.006, 250.0, 4500.0)),
    "rad_tower": dict(hp=220.0, lp=4800.0, drive=1.0, shape=None, bias=0.0,
                      gate=0.004, out=4.10, ott=(0.80, 0.40),
                      static=(0.004, 220.0, 4800.0)),
    "rad_cockpit": dict(hp=200.0, lp=5200.0, drive=1.0, shape=None, bias=0.0,
                        gate=0.004, out=4.55,
                        formant=(3000.0, 1.15, 0.30, 1.4),
                        ott=(0.70, 0.35),
                        static=(0.003, 200.0, 5200.0)),
    "rad_terminal": dict(hp=160.0, lp=6000.0, drive=1.0, shape=None, bias=0.0,
                         gate=0.004, out=3.88, ott=(0.60, 0.30),
                         static=(0.002, 160.0, 6000.0),
                         cluster=((17.0, 25.0, 37.0, 53.0),
                                  (0.5, 0.42, 0.34, 0.27), 0.18, 0.22)),
    "rad_station": dict(hp=140.0, lp=6000.0, drive=1.0, shape=None, bias=0.0,
                        gate=0.004, out=3.12, ott=(0.50, 0.25),
                        static=(0.002, 140.0, 6000.0),
                        cluster=((23.0, 34.0, 49.0, 71.0),
                                 (0.5, 0.42, 0.34, 0.27), 0.22, 0.30)),
    "rad_stadium": dict(hp=180.0, lp=5600.0, drive=1.0, shape=None, bias=0.0,
                        gate=0.004, out=6.80, ott=(1.00, 0.50),
                        air=(5500.0, 2.2, 0.25),
                        cluster=((41.0, 59.0, 83.0, 113.0),
                                 (0.45, 0.38, 0.30, 0.24), 0.26, 0.30)),
    "rad_church": dict(hp=150.0, lp=5000.0, drive=1.0, shape=None, bias=0.0,
                       gate=0.004, out=3.20, ott=(0.50, 0.25),
                       cave=((41.0, 59.0, 79.0, 113.0), 0.32, 0.30)),
    "rad_cinema": dict(hp=90.0, lp=7500.0, drive=1.0, shape=None, bias=0.0,
                       gate=0.004, out=3.92, ott=(1.10, 0.55),
                       air=(7000.0, 2.0, 0.25)),
    "rad_trailer": dict(hp=80.0, lp=8000.0, drive=2.0, shape="tanh",
                        bias=0.10, gate=0.005, out=4.29,
                        formant=(700.0, 1.1, 0.40, 1.6),
                        ott=(1.30, 0.65)),
    "rad_fm": dict(hp=90.0, lp=9500.0, drive=1.0, shape=None, bias=0.0,
                   gate=0.004, out=5.45, ott=(1.40, 0.70),
                   air=(8000.0, 2.2, 0.20)),
    "rad_studio": dict(hp=70.0, lp=12000.0, drive=1.0, shape=None, bias=0.0,
                       gate=0.004, out=4.20, ott=(1.20, 0.60),
                       air=(9000.0, 2.0, 0.18)),
    "rad_news": dict(hp=120.0, lp=9000.0, drive=1.0, shape=None, bias=0.0,
                     gate=0.004, out=7.15,
                     formant=(2800.0, 0.9, 0.30, 1.5),
                     ott=(1.30, 0.65)),
    "rad_mission": dict(hp=110.0, lp=8000.0, drive=1.0, shape=None, bias=0.0,
                        gate=0.004, out=5.77, ott=(1.20, 0.60),
                        air=(7500.0, 2.0, 0.20),
                        cluster=((13.0, 19.0, 29.0, 41.0),
                                 (0.40, 0.34, 0.28, 0.22), 0.16, 0.20)),

}


def spec_for(effect_id):
    """The filter settings for one pedal: a fixed row, or a player's board.

    Every preset's numbers live in `_SPECS`; the three boards' do not exist
    until a player sets them, so they are built from their tables instead. All
    of the answers go to the same processor, which is what keeps a board a
    preset rather than a second feature.
    """
    if effect_id == CUSTOM_EFFECT:
        from . import custom
        return custom.spec()
    if effect_id == BUILDER_EFFECT:
        from . import builder
        return builder.spec()
    if effect_id == VOX_LAB_EFFECT:
        from . import vocoder
        return vocoder.spec()
    if effect_id == PAN_EFFECT:
        from . import pan
        return pan.spec()
    return _SPECS[effect_id]

#: The loudest a pedal may hand over, in full scale. The PA and a cinema cabinet
#: both put the voice through effect sends *on top of* the dry voice -- the PA two
#: (its cabinet EQ and its compressor), a cinema room up to four (the room's
#: reverb, the cabinet's EQ, the same voice EQ and the compressor). A preamp
#: pushing the frame to 1.0 left that chain no headroom at all: the mixer clipped
#: on every syllable, heard as a buzz under the words and as a compressor that
#: worked so hard a tremolo arrived flattened. 0.7 is 3 dB of room for the chain
#: and still a loud pedal -- unity below the knee, so a quiet voice is untouched.
PEAK_CEILING = 0.70

#: The AVERAGE level a BOARD may hand over, in full scale. A peak ceiling alone
#: does not protect a room: a fuzz at `PEAK_CEILING` is a square wave, so its
#: RMS is 0.5 where a spoken voice's is 0.08 -- the same peak, sixteen decibels
#: hotter on average. A PA and a cinema room are built for the second: their EQ
#: lifts the low end (+3.5 dB below 250 Hz), their compressor is calibrated for
#: speech, and the PA's own receive-side limiter only engages above 0.85, which
#: a clipped pedal never reaches. A dense effect therefore arrived unattenuated
#: and broke up in the room -- a buzz or a torn syllable out of a PA speaker or
#: a cinema cabinet (2026-10-26).
#:
#: It is the two BOARDS that get this, not the fixed pedals: every one of the
#: sixty-odd presets was tuned by ear against a PA (its drive, its cabinet and
#: its `out`), while a board is combinatorics -- eleven slots can stack a shaper, a
#: crusher, a sub octave and `Output: Loud`, a voice no preset ever hands over.
#: 0.22 is a voice's own 0.08 plus about 9 dB: still a loud board, and no longer
#: a square wave pretending to be one.
VOICE_LOUDNESS = 0.22

#: How much of the way to the trim the gain moves in one frame, down and up.
#: Down quickly, because a syllable must not arrive 16 dB hot; up slowly, so a
#: quiet word is not pumped; interpolated across the frame either way, because
#: a gain that moves once per frame is the 50 Hz buzz this file warns about.
LOUDNESS_ATTACK = 0.5
LOUDNESS_RELEASE = 0.06

#: The vocoder's six bands, in hertz. A channel vocoder is a bank of band-pass
#: filters on both sides: the voice's bands supply the envelopes, a fixed saw
#: supplies the excitation, and the result keeps the words and loses the voice.
#: Six is the narrowest bank that still separates vowels; every extra band is
#: another per-sample filter on the talker's own thread.
VOCODER_BANDS = (180.0, 360.0, 700.0, 1300.0, 2400.0, 4200.0)

#: The saw the vocoder speaks through, and each band's own level on it --
#: measured once per process, because the carrier is a fixed waveform and its
#: band levels are constants. Dividing the voice's envelope by them is what
#: keeps a band with a quiet harmonic as loud as one with a strong harmonic.
VOCODER_CARRIER_HZ = 110.0

#: The carriers a vocoder may be spoken through -- one family of twenty, added
#: 2026-10-26, is built out of these. A saw is the classic: every harmonic, so
#: every band has something to shape. Square is hollow and buzzy (odd harmonics
#: only, and loud ones), pulse is thinner, stack is a saw with its octave under
#: it (richer and brighter than one saw), and noise is the odd one -- no pitch
#: at all, so the words come out as a loud whisper that keeps its formants.
#:
#: A sine or a triangle is deliberately NOT in this list, and the reason is the
#: one rule a vocoder has to keep: the carrier must reach the top band (4.2 kHz)
#: or the top of the words is simply missing. A sine has one partial at all, and
#: a triangle's harmonics fall as 1/n^2 -- measured on a 140 Hz saw, the sine
#: carrier put 100% of the output's energy on its own fundamental and the
#: triangle 93% below 120 Hz: a tone with the words taken out, which is the same
#: failure as the Whispered family's snare hit. Both were dropped rather than
#: shipped as presets.
VOCODER_WAVES = ("saw", "square", "pulse", "stack", "noise")

_vocoder_carrier_levels = None


def _carrier_frame(kind, phase, count, rng, breath=0.0):
    """One frame of a vocoder's carrier, built from its phase in 0..1.

    The phase is handed in rather than kept here so a frame joins the last one
    at the sample it left off, whatever the carrier's frequency is doing; the
    noise carrier is the exception, because noise has no phase to join.
    """
    if kind == "square":
        base = np.where(phase < 0.5, 1.0, -1.0)
    elif kind == "pulse":
        base = np.where(phase < 0.22, 1.0, -1.0)
    elif kind == "stack":
        # A saw and the same saw an octave up, half as loud: richer than one
        # saw and brighter, while every band still has a harmonic in it.
        lower = 2.0 * phase - 1.0
        upper = 2.0 * ((phase * 2.0) % 1.0) - 1.0
        base = lower + 0.5 * upper
    elif kind == "noise":
        base = rng.standard_normal(count) * 0.6
    else:
        base = 2.0 * phase - 1.0
    base = base.astype(np.float32)
    if breath > 0.0 and kind != "noise":
        # A breath of noise on the carrier, so the machine sounds like it has
        # air in it. Scaled like the noise carrier, which is 0.6 RMS.
        base = (base * (1.0 - float(breath))
                + rng.standard_normal(count).astype(np.float32) * 0.6
                * float(breath))
    return base

#: The step filter's pattern: which of the three bands is heard on each
#: sixteenth. Eight steps is a bar's worth of movement, not a melody.
STEP_PATTERN = (0, 0, 1, 2, 1, 0, 2, 1)


def _carrier_levels(q, hz, shift, kind, breath):
    """A carrier's RMS in each vocoder band, measured once per carrier.

    Every band's gain is the voice's envelope divided by the carrier's own level
    in that band, which is what keeps a band the carrier barely reaches as loud
    as one it fills -- so this has to be measured with the *same* carrier the
    block will use: its note, its waveform, where its formants sit and whether
    it has noise on it. Hence the key, and hence one cache entry per carrier
    rather than one per Q.

    The noise carrier is measured on a fixed seed while the block draws from
    its own: filtered white noise has the same mean square frame after frame, so
    the level is a property of the carrier and not of the draw.
    """
    global _vocoder_carrier_levels
    key = (float(q), float(hz), float(shift), kind, float(breath))
    if _vocoder_carrier_levels is not None and _vocoder_carrier_levels[0] == key:
        return _vocoder_carrier_levels[1]
    frames = 3
    samples = frames * 960
    rng = np.random.default_rng(0x5EED)
    phase = (np.arange(samples) * (float(hz) / SAMPLE_RATE)) % 1.0
    carrier = _carrier_frame(kind, phase, samples, rng, breath)
    levels = []
    for band_hz in VOCODER_BANDS:
        band = _Biquad.bandpass(band_hz * float(shift), q)
        shaped = np.concatenate([
            band.process(carrier[start:start + 960])
            for start in range(0, samples, 960)])
        # The last frame only: the filters have settled by then.
        levels.append(max(1e-3, float(np.sqrt(np.mean(
            np.square(shaped[-960:]))))))
    _vocoder_carrier_levels = (key, tuple(levels))
    return _vocoder_carrier_levels[1]

#: The gate's two speeds, per frame. Opening is quick (a word must not lose its
#: first consonant); closing is slow enough that a breath holds the gate open
#: rather than chopping the tail of a sentence into pieces.
GATE_OPEN = 0.5
GATE_CLOSE = 0.12

#: The sag's two speeds, per frame. A valve rectifier gives its current up at
#: once (the supply dips under a loud passage) and recovers slowly -- that slow
#: recovery is the swell everyone recognises as sag. Both are interpolated
#: across the frame, so the recovery cannot step (a step is the 50 Hz buzz the
#: modern pedals were fixed for).
SAG_FALL = 0.55
SAG_RISE = 0.12


#: Whether the Server says this account may use the Vocoder Lab board. The
#: capture thread reads `effect_id()` and must not call into gameplay, so the
#: answer is a plain bool, written on the main thread when a login or a staff
#: refresh arrives (libs/event_handeler.py). False until a snapshot says so: a
#: board saved on another account must not apply in the meantime.
_vocoder_lab_allowed = False


def set_vocoder_lab_allowed(allowed):
    """Record the Server's answer for the vocoder board (main thread only)."""
    global _vocoder_lab_allowed
    _vocoder_lab_allowed = bool(allowed)


def vocoder_lab_allowed():
    """Whether the vocoder board may apply -- the last Server answer."""
    return _vocoder_lab_allowed


def selected():
    """The saved preset, without asking the Developer's gate."""
    return clamp_effect(options.get(OPTION, DEFAULT_EFFECT))


def effect_id():
    """The preset to apply right now; Off when staff have locked the family.

    Reading the option is not enough: a lock must silence the effect for a
    player who already saved one, not merely hide the line that chose it.
    """
    if gate.is_locked("voice_fx"):
        return DEFAULT_EFFECT
    wanted = selected()
    if wanted == VOX_LAB_EFFECT and not _vocoder_lab_allowed:
        return DEFAULT_EFFECT
    return wanted


def enabled():
    """Whether a preset is chosen and allowed."""
    return effect_id() != DEFAULT_EFFECT


#: The one pedal a recording may wear on its own, without changing the voice
#: other players hear. It is a fixed shelf row, not a board: the recorder
#: asks for exactly this chain and gets a fresh processor for each take.
RECORDING_STUDIO_EFFECT = "rad_studio"


def recording_studio_available():
    """Whether the Staff's Voice Effects lock leaves the recording board open."""
    return not gate.is_locked("voice_fx")


def recording_studio_processor():
    """A standalone Studio Condenser for the recorder thread, or None when locked.

    The recorder is not the broadcast path: it gets its own processor so a
    take cannot inherit a sentence's look-ahead or a live preset change, and
    the capture thread never pays for it.
    """
    if not recording_studio_available():
        return None
    return _EffectProcessor(RECORDING_STUDIO_EFFECT)


# ------------------------------------------------------------------ the filters


def _lowpass(hz, taps):
    """A windowed-sinc low-pass, normalised to unity at DC."""
    offset = np.arange(taps) - (taps - 1) / 2.0
    cutoff = float(hz) / SAMPLE_RATE
    kernel = (2.0 * cutoff * np.sinc(2.0 * cutoff * offset)
              * np.hamming(taps))
    total = kernel.sum()
    if abs(total) < 1e-12:
        return np.zeros(taps, dtype=np.float32)
    return (kernel / total).astype(np.float32)


def _tick(taps):
    """A short one-sided click: what a record's scratch sounds like in a frame."""
    window = np.hanning(taps)
    total = float(window.sum())
    return (window / total if total else window).astype(np.float32)


def _hilbert(taps):
    """A windowed Hilbert kernel: the quadrature branch of a 90-degree shift.

    Odd length, antisymmetric, zero at the centre; a Hamming window keeps the
    stop band from ringing into the voice's top octave. The branch it produces
    lags by 90 degrees at every frequency the kernel covers, which is what makes
    a frequency shifter shift every partial by the same number of hertz instead
    of multiplying the spectrum the way a ring modulator does.
    """
    centre = (taps - 1) // 2
    index = np.arange(taps) - centre
    kernel = np.zeros(taps, dtype=np.float32)
    odd = (np.arange(taps) - centre) % 2 != 0
    kernel[odd] = 2.0 / (np.pi * index[odd])
    kernel *= np.hamming(taps)
    return kernel.astype(np.float32)


class _Biquad:
    """A second-order high-pass that carries its state across frames.

    Why not an FIR like the cabinet: at 95 Hz a 129-tap windowed-sinc filter has
    a transition band more than a kilohertz wide, so it passes 0.76 of a 220 Hz
    tone -- a 12 dB cut to the voice's own fundamental, heard as a thin
    telephone. A biquad is where the corner is named, and its delay (two or
    three samples) is small enough to ignore.
    """

    __slots__ = ("b", "a", "_z1", "_z2")

    def __init__(self, b, a):
        self.b = np.asarray(b, dtype=np.float64)
        self.a = np.asarray(a, dtype=np.float64)
        self._z1 = 0.0
        self._z2 = 0.0

    @staticmethod
    def _bandpass_coefficients(hz, q):
        """RBJ's band-pass, normalised so the peak gain is exactly 1."""
        w0 = 2.0 * np.pi * float(hz) / SAMPLE_RATE
        cos_w0 = float(np.cos(w0))
        alpha = float(np.sin(w0)) / (2.0 * float(q))
        a0 = 1.0 + alpha
        return ((alpha / a0, 0.0, -alpha / a0),
                (1.0, -2.0 * cos_w0 / a0, (1.0 - alpha) / a0))

    @classmethod
    def bandpass(cls, hz, q=1.0):
        b, a = cls._bandpass_coefficients(hz, q)
        return cls(b, a)

    def retune_bandpass(self, hz, q=1.0):
        """Move a running band-pass, keeping its state (a sweep, not a reset)."""
        self.b, self.a = self._bandpass_coefficients(hz, q)

    def retune(self, b, a):
        """A new response on the same filter, keeping the samples it holds.

        The tone stage moves a band a few frames at a time (`_ToneStack`), and
        a filter that reset its state on every one of those frames would drop
        the tail it is still ringing with -- a tick at each 20 ms boundary.
        """
        self.b = np.asarray(b, dtype=np.float64)
        self.a = np.asarray(a, dtype=np.float64)

    def reset(self):
        """Forget the samples this filter is holding (a band let go of)."""
        self._z1 = 0.0
        self._z2 = 0.0

    def state(self):
        """The samples this filter is holding, so they can be rewound.

        A response that changes between frames has to be auditioned twice on
        the same input (`_ToneStack`): once with the response that was playing
        and once with the new one, crossfaded across the frame. The second run
        starts from the first one's state, which is what makes the seam
        continuous -- the alternative is a step the size of whatever the old
        response was holding.
        """
        return self._z1, self._z2

    def restore(self, state):
        """Put back a state `state()` saved, before a second run."""
        self._z1, self._z2 = state

    @staticmethod
    def _lowpass_coefficients(hz, q=0.7071067811865476):
        """RBJ's cookbook low-pass, at a Butterworth's Q (the delay's darkening)."""
        w0 = 2.0 * np.pi * float(hz) / SAMPLE_RATE
        cos_w0 = float(np.cos(w0))
        alpha = float(np.sin(w0)) / (2.0 * q)
        b0 = (1.0 - cos_w0) / 2.0
        b1 = 1.0 - cos_w0
        b2 = (1.0 - cos_w0) / 2.0
        a0 = 1.0 + alpha
        a1 = -2.0 * cos_w0
        a2 = 1.0 - alpha
        return ((b0 / a0, b1 / a0, b2 / a0),
                (1.0, a1 / a0, a2 / a0))

    @classmethod
    def lowpass(cls, hz, q=0.7071067811865476):
        b, a = cls._lowpass_coefficients(hz, q)
        return cls(b, a)

    def retune_lowpass(self, hz, q=0.7071067811865476):
        """Move a running low-pass, keeping the samples it is holding.

        The same reason as the high-pass's (`retune_highpass`): a stop on the
        high cut's ladder is a new response, and a filter that reset its state
        on the press would drop the tail it is still ringing with.
        """
        self.b, self.a = self._lowpass_coefficients(hz, q)

    @staticmethod
    def _highpass_coefficients(hz, q=0.7071067811865476):
        """RBJ's cookbook high-pass, at a Butterworth's Q."""
        w0 = 2.0 * np.pi * float(hz) / SAMPLE_RATE
        cos_w0 = float(np.cos(w0))
        alpha = float(np.sin(w0)) / (2.0 * q)
        b0 = (1.0 + cos_w0) / 2.0
        b1 = -(1.0 + cos_w0)
        b2 = (1.0 + cos_w0) / 2.0
        a0 = 1.0 + alpha
        a1 = -2.0 * cos_w0
        a2 = 1.0 - alpha
        return ((b0 / a0, b1 / a0, b2 / a0),
                (1.0, a1 / a0, a2 / a0))

    @classmethod
    def highpass(cls, hz, q=0.7071067811865476):
        b, a = cls._highpass_coefficients(hz, q)
        return cls(b, a)

    def retune_highpass(self, hz, q=0.7071067811865476):
        """Move a running high-pass, keeping the samples it is holding.

        The cut ladder changes on a key press, and a filter that reset its
        state on that press would drop the tail it is still ringing with -- a
        tick at the frame boundary. `_ToneStack` crossfades the frame as well;
        this keeps the state the new response starts from.
        """
        self.b, self.a = self._highpass_coefficients(hz, q)

    def process(self, block):
        b0, b1, b2 = (float(value) for value in self.b)
        _unity, a1, a2 = (float(value) for value in self.a)
        z1, z2 = self._z1, self._z2
        # A list of Python floats, not the numpy array: a per-sample loop over
        # numpy scalars is several times slower than one over floats, and this
        # loop runs 50 times a second on the talker's capture thread.
        samples = block.tolist()
        out = [0.0] * len(samples)
        for index, x in enumerate(samples):
            y = b0 * x + z1
            z1 = b1 * x - a1 * y + z2
            z2 = b2 * x - a2 * y
            out[index] = y
        self._z1, self._z2 = z1, z2
        return np.asarray(out, dtype=np.float32)


#: A filter that changes nothing, for a tone band that is back at flat.
_IDENTITY = (1.0, 0.0, 0.0)


def _shelf_coefficients(hz, decibels, high=False):
    """RBJ's shelf, at S = 1: it reaches its whole gain by the corner's octave.

    A low shelf is what "less bottom" means and a high shelf is what "less
    top" means: neither leaves a hole in the middle of the voice the way a
    bell at 120 Hz would.
    """
    amplitude = 10.0 ** (float(decibels) / 40.0)
    w0 = 2.0 * np.pi * float(hz) / SAMPLE_RATE
    cos_w0 = float(np.cos(w0))
    alpha = float(np.sin(w0)) / 2.0 * np.sqrt(2.0)
    root_alpha = 2.0 * np.sqrt(amplitude) * alpha
    if high:
        b0 = amplitude * ((amplitude + 1.0) + (amplitude - 1.0) * cos_w0
                          + root_alpha)
        b1 = -2.0 * amplitude * ((amplitude - 1.0)
                                 + (amplitude + 1.0) * cos_w0)
        b2 = amplitude * ((amplitude + 1.0) + (amplitude - 1.0) * cos_w0
                          - root_alpha)
        a0 = ((amplitude + 1.0) - (amplitude - 1.0) * cos_w0 + root_alpha)
        a1 = 2.0 * ((amplitude - 1.0) - (amplitude + 1.0) * cos_w0)
        a2 = ((amplitude + 1.0) - (amplitude - 1.0) * cos_w0 - root_alpha)
    else:
        b0 = amplitude * ((amplitude + 1.0) - (amplitude - 1.0) * cos_w0
                          + root_alpha)
        b1 = 2.0 * amplitude * ((amplitude - 1.0)
                                - (amplitude + 1.0) * cos_w0)
        b2 = amplitude * ((amplitude + 1.0) - (amplitude - 1.0) * cos_w0
                          - root_alpha)
        a0 = ((amplitude + 1.0) + (amplitude - 1.0) * cos_w0 + root_alpha)
        a1 = -2.0 * ((amplitude - 1.0) + (amplitude + 1.0) * cos_w0)
        a2 = ((amplitude + 1.0) + (amplitude - 1.0) * cos_w0 - root_alpha)
    return ((b0 / a0, b1 / a0, b2 / a0), (1.0, a1 / a0, a2 / a0))


def _bell_coefficients(hz, q, decibels):
    """RBJ's peaking band: only the band around its centre moves.

    This is the one that takes the mud out from under the words without
    thinning the chest -- a shelf cannot do it from either end.
    """
    amplitude = 10.0 ** (float(decibels) / 40.0)
    w0 = 2.0 * np.pi * float(hz) / SAMPLE_RATE
    cos_w0 = float(np.cos(w0))
    alpha = float(np.sin(w0)) / (2.0 * float(q))
    a0 = 1.0 + alpha / amplitude
    return ((1.0 + alpha * amplitude) / a0,
            -2.0 * cos_w0 / a0,
            (1.0 - alpha * amplitude) / a0), (
        1.0, -2.0 * cos_w0 / a0, (1.0 - alpha / amplitude) / a0)


def _tone_coefficients(index, decibels):
    """The coefficients for one tone band, from the table in `tone.BANDS`."""
    _band_id, _label, hz, kind, q, _note = tone.BANDS[index]
    if kind == "bell":
        return _bell_coefficients(hz, q, decibels)
    return _shelf_coefficients(hz, decibels, high=(kind == "highshelf"))


class _Fir:
    """One FIR filter that carries the samples it needs across frames."""

    __slots__ = ("kernel", "tail")

    def __init__(self, kernel):
        self.kernel = kernel
        self.tail = np.zeros(len(kernel) - 1, dtype=np.float32)

    def process(self, block):
        data = np.concatenate((self.tail, block))
        out = np.convolve(data, self.kernel, mode="valid")
        self.tail = data[-(len(self.kernel) - 1):].copy()
        return out.astype(np.float32, copy=False)


class _Compressor:
    """The voice strip's level rider: the quiet and the loud arrive together.

    **Why a compressor, when the chain already ends in a ceiling and a trim.**
    The peak ceiling catches a shout after it has happened, and the loudness
    trim pulls a dense effect back to the average a voice arrives at -- both
    are safety, and neither can make a whisper and a shout *the same size*.
    That is the one job of a compressor, and it is the job a listener hears as
    a professional voice: the sentence stays at the same distance from the
    room whether it is spoken across the desk or into the microphone.

    **Feed-forward, with the detector high-passed at 120 Hz.** The gain is
    computed from the level of the frame arriving, so it cannot overshoot into
    feedback the way a feedback design can, and it needs no look-ahead. The
    level is read through its own high-pass, though: a `p` or a knock on the
    desk is a large low-frequency spike that carries no words, and a full-band
    detector would duck the whole sentence under every one of them. That is
    the voice compressor's most common fault -- the words pumping whenever the
    speaker moves.

    **A soft knee of 6 dB.** A hard knee switches on at one decibel and off at
    the one below it, which on a voice is heard as a lisp around the
    threshold; the curve is bent across six decibels instead, so a level that
    crosses it is leaned on rather than caught.

    **Attack and release are the detector's.** The gain is the static curve of
    a level that has already been through them, which is what makes a release
    time sound like a breath rather than like a delay. A threshold or a ratio
    moved on the page is ramped across the frame -- the same rule as the level
    fader, because a gain that steps is a click.

    **There is no make-up gain of its own.** A compressor that lifts itself
    back up is guessing at how much of the signal was above its threshold; the
    Level row sits immediately after this block, and the meter reports the
    reduction so a player can add exactly that. It is also what makes the
    block usable as a bus compressor later: it does what it is told and leaves
    the level where it was put.
    """

    #: The width of the knee, in decibels: three either side of the threshold.
    KNEE_DB = 6.0

    #: Where the detector's high-pass sits: under the fundamental of a deep
    #: voice, over the thump of a desk and the plosive of a `p`.
    DETECTOR_HZ = 120.0

    #: The threshold that is "no threshold": no frame can cross it, so the
    #: static curve is flat while the release finishes what it was holding.
    OFF = 200.0

    def __init__(self):
        self._sidechain = _Biquad.highpass(self.DETECTOR_HZ)
        self._env = 0.0
        self._attack_ms = None
        self._release_ms = None
        self._attack = 0.0
        self._release = 0.0
        self._threshold = self.OFF
        self._ratio = 1.0
        self._reduction = 0.0

    def reduction(self):
        """How far the last frame was pulled down, in decibels."""
        return self._reduction

    def rearm(self):
        """Start a fresh engagement with nothing left from the last one.

        The stage is skipped while the threshold is off, so the detector's
        filter state and its envelope would otherwise sit where the previous
        engagement left them. Switching the compressor back on ten seconds
        later would then begin with a second of gain reduction that belongs to
        audio nobody is playing -- measured as the quiet sentence after a
        re-engage being pulled down by the press that had already ended.
        """
        self._sidechain.reset()
        self._env = 0.0

    def active(self):
        """Whether the compressor is holding the voice down at all.

        The stage is skipped entirely while this is false and the saved
        threshold is off, which is what keeps a preset nobody has compressed
        byte-for-byte the pedal it was.
        """
        return self._reduction > 0.05

    def _retune(self, attack_ms, release_ms):
        """The detector's two coefficients, when the page has moved them."""
        if (attack_ms, release_ms) == (self._attack_ms, self._release_ms):
            return
        self._attack_ms = attack_ms
        self._release_ms = release_ms
        self._attack = float(np.exp(-1.0 / (max(1.0, attack_ms) * 0.001
                                           * SAMPLE_RATE)))
        self._release = float(np.exp(-1.0 / (max(1.0, release_ms) * 0.001
                                             * SAMPLE_RATE)))

    @staticmethod
    def _ramp(before, after, count):
        """A parameter across the frame, so a press is not a step."""
        if abs(after - before) < 1e-6:
            return np.full(count, after, dtype=np.float64)
        return np.linspace(before, after, count, dtype=np.float64)

    def process(self, block, threshold_db, ratio, attack_ms, release_ms):
        """Pull one frame down by the static curve of its own level."""
        count = block.size
        if count <= 0:
            return block
        self._retune(attack_ms, release_ms)
        # `threshold_db` is 0 for off; the internal "off" is a threshold no
        # frame can cross, so the release still finishes what it was holding
        # instead of the gain snapping back to one in a single sample.
        target = float(threshold_db) if threshold_db else self.OFF
        thresholds = self._ramp(self._threshold, target, count)
        self._threshold = target
        slopes = 1.0 - 1.0 / self._ramp(self._ratio, float(ratio), count)
        self._ratio = float(ratio)
        detector = self._sidechain.process(block).tolist()
        env = self._env
        attack = self._attack
        release = self._release
        levels = [0.0] * count
        for index, sample in enumerate(detector):
            square = sample * sample
            coefficient = attack if square > env else release
            env = square + coefficient * (env - square)
            levels[index] = env
        self._env = env
        # The mean square is a power, so 10*log10 of it is the same decibel a
        # 20*log10 of an amplitude would be -- without a square root per sample.
        over = (10.0 * np.log10(np.maximum(np.asarray(levels, dtype=np.float64),
                                           1e-12)) - thresholds)
        half = self.KNEE_DB / 2.0
        reduction = np.zeros(count, dtype=np.float64)
        above = over >= half
        knee = (over > -half) & (over < half)
        reduction[above] = over[above] * slopes[above]
        reduction[knee] = (slopes[knee] * (over[knee] + half) ** 2
                           / (2.0 * self.KNEE_DB))
        self._reduction = float(reduction.max())
        if self._reduction <= 0.0:
            return block
        gain = np.power(10.0, -reduction / 20.0).astype(np.float32)
        return block * gain


class _DeEsser:
    """The strip's sibilance rider: it takes the `s` while it is happening.

    **Why a rider and not a filter.** A high shelf turned down takes the breath
    off every syllable, and a high cut takes the top off the voice for good. A
    `s` is neither: it is an event, loud for eighty milliseconds, and the tool
    that leaves the rest of the sentence alone is a gain that moves with it.
    The gain is read from the frame's own top -- a high-pass at 4.8 kHz, which
    a vowel barely crosses and a sibilant fills -- and applied to the frame.

    **Why the whole frame, and not a split band.** A band de-esser is the
    textbook shape and it was built first: split at 4.8 kHz, pull the top half
    down, add the bottom half back. Measured, it could take only 2.4 decibels
    off an 8 kHz tone set to `12` -- the 12 dB/octave split leaves a third of
    the `s` in the low half, out of the gain's reach -- and cascading the split
    made it worse, because the two halves are not in phase: an 8 kHz tone came
    out *louder* with the row at 12 than with it off. A full-band duck does
    exactly what the row says (-12.0 dB measured at `12`) and there is no
    vowel under a `s` for it to darken. The row is a duck, not an EQ.

    **The threshold is on the band, not on the frame.** A loud vowel carries
    more energy than a quiet `s`, so a detector on the whole frame would duck
    the `s` in a whisper and miss it in a shout. The level read is the top
    band's own RMS: a vowel carries almost nothing up there, a sibilant carries
    most of its energy there, and the one setting works at every volume the
    strip is handed.

    **One knob, because a second is a second mistake.** The threshold, attack
    and release are properties of speech and are fixed; the row is the most the
    frame may lose. That is the decision a player makes when they hear a spit
    on the `s`.

    **Off is no filter at all.** The first stop is 0 dB and the stage is
    skipped entirely there, so a preset nobody has de-essed costs nothing and
    is byte-for-byte the pedal it was.
    """

    #: Where the band it watches begins: over the words' own clarity and under
    #: where the `s`, the `sh` and a cymbal sit.
    SPLIT_HZ = 4800.0

    #: The band's own RMS the reduction starts from, as an amplitude. A vowel
    #: carries a thousandth of its energy here; a `s` carries a tenth of a
    #: frame, and this is the level between them.
    THRESHOLD = 0.02

    #: The knee, in decibels: the same reason the compressor has one at all.
    KNEE_DB = 6.0

    #: The envelope's times. A `s` is over in tens of milliseconds, so the
    #: attack has to catch it, and the release must not chop the next word.
    ATTACK_MS = 0.5
    RELEASE_MS = 70.0

    def __init__(self):
        self._split = _Biquad.highpass(self.SPLIT_HZ)
        self._env = 0.0
        self._attack = float(np.exp(-1.0 / (self.ATTACK_MS * 0.001
                                            * SAMPLE_RATE)))
        self._release = float(np.exp(-1.0 / (self.RELEASE_MS * 0.001
                                             * SAMPLE_RATE)))
        self._reduction = 0.0
        self._amount = 0.0

    def reduction(self):
        """How far the last frame was pulled down, in decibels."""
        return self._reduction

    def active(self):
        """Whether the stage is doing anything -- the row on, or letting go."""
        return self._amount > 0.0 or self._reduction > 0.0

    def rearm(self):
        """Start a fresh engagement with nothing held from the last one."""
        self._split.reset()
        self._env = 0.0
        self._reduction = 0.0
        self._amount = 0.0

    def process(self, block, amount_db):
        """Duck the frame for the length of a sibilant, by at most the row.

        The row is ramped across the frame rather than read at its new value,
        exactly like the compressor's threshold and for the same reason: a
        player who turns the row down while a `s` is still sounding would
        otherwise be handed a gain that steps from a quarter to one in a
        single sample, which is a click. Turning it up ramps the other way, so
        the first `s` after the press is ducked in rather than dropped on.
        """
        count = block.size
        if count <= 0:
            return block
        before = self._amount
        self._amount = float(amount_db)
        ceiling = np.linspace(before, self._amount, count, dtype=np.float64)
        high = self._split.process(block)
        attack = self._attack
        release = self._release
        env = self._env
        levels = [0.0] * count
        for index, sample in enumerate(high.tolist()):
            square = sample * sample
            coefficient = attack if square > env else release
            env = square + coefficient * (env - square)
            levels[index] = env
        self._env = env
        # The mean square is a power, so 10*log10 of it is the amplitude in
        # decibels without a square root per sample -- the same arithmetic the
        # compressor's own detector uses.
        over = (10.0 * np.log10(np.maximum(
            np.asarray(levels, dtype=np.float64), 1e-12))
            - 10.0 * np.log10(self.THRESHOLD * self.THRESHOLD))
        half = self.KNEE_DB / 2.0
        reduction = np.zeros(count, dtype=np.float64)
        above = over >= half
        knee = (over > -half) & (over < half)
        # An infinite ratio -- the band is brought back to the threshold -- and
        # the row is the ceiling on how far that may go. A `s` that is 20 dB
        # over the threshold at `De-ess 6` loses 6, not 20.
        reduction[above] = np.minimum(over[above], ceiling[above])
        reduction[knee] = np.minimum(
            (over[knee] + half) ** 2 / (2.0 * self.KNEE_DB), ceiling[knee])
        self._reduction = float(reduction.max())
        if self._reduction <= 0.0:
            return block
        gain = np.power(10.0, -reduction / 20.0).astype(np.float32)
        return block * gain


class _ToneStack:
    """The cuts, bands, de-esser, compressor and fader for one preset.

    `tone.py` owns the table, the saved setting and the words; this is only the
    filters and the gain. **Flat is not a filter**: a preset whose bands are
    all zero, whose cuts are off, whose de-esser is off, whose compressor is
    off and whose level is 0 dB is handed straight back, so the hundred and
    forty-four presets that nobody has touched cost exactly what they cost
    before this class existed.

    A band is GLIDED to its saved value rather than jumped to, because a filter
    whose coefficients change between one sample and the next is a step the
    size of whatever the old response was holding -- heard as a click. 3 dB a
    frame puts a slider press on the next ~80 ms, and the band's state (the
    samples the biquad is still ringing with) is kept across every one of
    those frames, which is what makes the glide continuous.
    """

    #: How far a band may move in one frame, in decibels.
    GLIDE_DB = 3.0

    def __init__(self, preset):
        self.preset = preset
        self._filters = [_Biquad(_IDENTITY, _IDENTITY)
                         for _band in tone.BANDS]
        self._applied = [0.0] * len(tone.BANDS)
        # The low cut and the preset's own fader, moved by the same page as
        # the bands. Both start at what a preset opens with: no cut, 0 dB.
        self._cut = _Biquad(_IDENTITY, _IDENTITY)
        self._cut_hz = 0.0
        # The other end of the strip, and the band rider between them. Both
        # start off, which is no filter and no per-sample loop at all.
        self._highcut = _Biquad(_IDENTITY, _IDENTITY)
        self._highcut_hz = 0.0
        self._deess = _DeEsser()
        self._deess_on = False
        # The compressor, between the bands and the fader. Off is a threshold
        # of 0 and no frame of DSP: `_comp_on` holds it for the frames a
        # release still needs after the player turns it off.
        self._comp = _Compressor()
        self._comp_on = False
        self._comp_live = False
        self._level_gain = 1.0
        self._level_db = 0.0
        self._engaged = False

    def active(self):
        """Whether the strip is doing anything -- the loudness trim asks this.

        A fixed preset is normally left alone by `_loudness_trim` because it
        was tuned against a PA by ear. A preset whose tone has been moved is
        no longer that measurement, so from the first decibel it is levelled
        like a board; flat returns it to exactly as it was made.

        **The latch does not clear at 0 dB on the way past.** A band gliding
        from +12 to -12 crosses 0, and a trim that read the applied gain alone
        would release for that one frame -- which measured as a 2x burst in the
        middle of a 20 ms glide, heard as a thump under the words. It clears
        only when the saved value is flat too.
        """
        return self._engaged

    def compression(self):
        """How far the last frame was pulled down, in decibels (0 when off)."""
        return self._comp.reduction()

    def process(self, block):
        (targets, cut_hz, high_cut_hz, deess_db, _mix, compressor,
         level_db) = tone.strip(self.preset)
        threshold, ratio, attack, release = compressor
        idle = (not any(targets) and not cut_hz and not high_cut_hz
                and not deess_db and not level_db and not threshold)
        if idle and not self._engaged:
            return block
        if cut_hz != self._cut_hz:
            block = self._retune_cut(cut_hz, block)
            self._engaged = True
        if self._cut_hz:
            block = self._cut.process(block)
        for index, target in enumerate(targets):
            applied = self._applied[index]
            if applied != target:
                step = max(-self.GLIDE_DB,
                           min(self.GLIDE_DB, float(target) - applied))
                applied += step
                self._applied[index] = applied
                block = self._change(index, applied, block)
                self._engaged = True
            elif applied:
                block = self._filters[index].process(block)
        # The two ends and the sibilance, in the order they hear the voice: the
        # de-esser has to see the band a `s` lives in before the high cut takes
        # the top off, and both keep their state across the frame they change
        # on (a filter whose state was built for another response ticks).
        if deess_db or self._deess.active():
            if not self._deess_on:
                self._deess.rearm()
            block = self._deess.process(block, deess_db)
            self._deess_on = self._deess.active()
            self._engaged = True
        else:
            self._deess_on = False
        if high_cut_hz != self._highcut_hz:
            block = self._retune_highcut(high_cut_hz, block)
            self._engaged = True
        if self._highcut_hz:
            block = self._highcut.process(block)
        # The compressor sits between the bands and the fader, and it is the
        # only block here whose own state (a release still letting go) keeps
        # it running after its threshold is put back to off. Turning it back
        # on starts from a clean detector rather than from the press before.
        if threshold and not self._comp_live:
            self._comp.rearm()
        self._comp_live = bool(threshold)
        if threshold or self._comp_on:
            block = self._comp.process(block, threshold, ratio, attack,
                                       release)
            self._comp_on = self._comp.active()
            if self._comp_on:
                self._engaged = True
        block = self._level_block(level_db, block)
        if (idle and not any(self._applied) and not self._cut_hz
                and not self._highcut_hz and not self._level_db
                and not self._comp_on):
            self._engaged = False
        return block

    def _retune_cut(self, hz, block):
        """Move the low cut, crossfading the frame it moves on."""
        self._cut_hz = hz
        return self._retune_edge(self._cut, hz, block,
                                 self._cut.retune_highpass)

    def _retune_highcut(self, hz, block):
        """Move the high cut, the same way round as the low one."""
        self._highcut_hz = hz
        return self._retune_edge(self._highcut, hz, block,
                                 self._highcut.retune_lowpass)

    def _retune_edge(self, edge, hz, block, retune):
        """Run the frame through the old corner and the new one, and blend.

        The same reason as a band's change: a biquad's state belongs to the
        response that filled it, so the first samples after a new corner are a
        step the old one was about to make. The frame is run twice from the
        same state and the two are blended across the 20 ms -- a ladder press
        is a change of corner, not a glide along one.

        Off is a response like any other, the identity: both halves of it.
        `retune` takes the b triple and the a triple; the one value that used to
        be splatted on the low cut raised instead of easing the bottom back in
        (a live press, 2026-10-29).
        """
        previous = edge.state()
        before = edge.process(block)
        edge.restore(previous)
        if hz:
            retune(hz)
        else:
            edge.retune(_IDENTITY, _IDENTITY)
        after = edge.process(block)
        blend = np.linspace(0.0, 1.0, block.size, endpoint=False,
                            dtype=np.float32)
        return before * (1.0 - blend) + after * blend

    def _level_block(self, decibels, block):
        """The preset's own fader, ramped across the frame it changes on.

        A fader is a gain, not a filter, so there is no response to crossfade;
        a linear ramp from the gain the last frame ended on is enough to keep
        a press from being a step. At 0 dB the frame is handed back without a
        multiply at all, which is what keeps an untouched preset byte-exact.
        """
        target = 10.0 ** (float(decibels) / 20.0)
        if abs(target - self._level_gain) < 1e-6:
            if abs(self._level_gain - 1.0) < 1e-9:
                return block
            return block * self._level_gain
        ramp = np.linspace(self._level_gain, target, block.size,
                           endpoint=False, dtype=np.float32)
        self._level_gain = target
        self._level_db = float(decibels)
        return block * ramp

    def _change(self, index, applied, block):
        """Run the same frame through the old response and the new one, blended.

        Changing a biquad's coefficients under a running stream is only half a
        fix: the filter's state was computed for the response that was playing, so
        the first samples after a change are a step the old response was about
        to make. Measured on the 0 dB crossing of a +12 to -12 glide, that step
        was 0.067 of full scale -- a click under the words, from the one frame
        the stage spent at "no change".

        So the frame is shaped twice: once with the filter as it was (its state
        carried on), then again from the same starting state with the new
        response, and the two are crossfaded across the 20 ms. The second run's
        end state is the one kept, so the next frame starts where this one
        ended. It costs one extra filter run only on frames where a band moves.
        """
        band_filter = self._filters[index]
        state = band_filter.state()
        before = band_filter.process(block)
        band_filter.restore(state)
        band_filter.retune(*_tone_coefficients(index, applied))
        after = band_filter.process(block)
        blend = np.linspace(0.0, 1.0, block.size, endpoint=False,
                            dtype=np.float32)
        return before * (1.0 - blend) + after * blend


class _EffectProcessor:
    """One preset's chain, with the state a stream needs between frames."""

    #: The cabinet is designed at a fixed length so every preset has the same
    #: delay: 128 samples after the shaper, 2.7 ms. Every preset also carries the
    #: same two biquads (input high-pass and DC block), so the presets stay in
    #: step with one another as well as with the frames.
    TAPS = 257

    def __init__(self, preset, spec=None, with_tone=False):
        """`spec` overrides the table for a chain that is a preset *plus*
        something -- the in-game radio's gentle auto-wah is the one caller
        (`libs/radio_voice.py`); everything else is the preset's own row.

        `with_tone` is the player's own channel strip -- the cut, the four
        bands, the mix and the level -- and it is off by
        default on purpose: the in-game radio is built by this same class on
        the RECEIVE path, and a tone saved for the `Radio` pedal must not
        colour every radio a player hears. `VoiceEffectLine` is the only
        caller that asks for it.
        """
        spec = spec if spec is not None else spec_for(preset)
        self.preset = preset
        self.spec = spec
        self.pre = _Biquad.highpass(spec["hp"])
        self.dc = _Biquad.highpass(20.0)
        self.post = _Fir(_lowpass(spec["lp"], self.TAPS))
        self._gate_gain = 0.0
        self._gate_threshold = float(spec["gate"])
        self._drive = float(spec["drive"])
        self._bias = float(spec["bias"])
        self._shape = spec["shape"]
        self._out = float(spec["out"])
        # The loudness trim, for the two boards only (see VOICE_LOUDNESS): the
        # fixed presets were tuned against a PA and are left exactly as they
        # were. `_loudness_gain` is the gain the last frame ended on, so the
        # next frame starts there rather than stepping.
        self._levelled = preset in (CUSTOM_EFFECT, BUILDER_EFFECT)
        self._loudness_gain = 1.0
        # The player's four tone bands, for the broadcast line only (see
        # `with_tone`). Flat hands every frame straight back untouched.
        self._tone = _ToneStack(preset) if with_tone else None
        # How much of the effect is heard (the mix fader), 0..1. A processor
        # is built fully wet so a pedal chosen mid-press does not start
        # silent; `_blend` ramps to the saved value over a few frames.
        self._mix = 1.0
        # A chorus is a modulated delay; the ring is long enough for the base
        # delay plus its whole sweep.
        self._chorus = float(spec.get("chorus", 0.0))
        self._ring = None
        self._ring_pos = 0
        self._phase = 0.0
        if self._chorus:
            self._ring = np.zeros(4096, dtype=np.float32)
        # A bitcrusher holds one sample for several and rounds what it holds.
        self._crush = spec.get("crush")
        self._crush_value = 0.0
        self._crush_phase = 0.0
        # The pedals below are one block each, off unless their spec names
        # them, so a preset pays only for what it uses. One LFO is shared by
        # whichever sweep pedal a preset has (only one preset runs at a time).
        self._octave = float(spec.get("octave", 0.0))
        self._tremolo = spec.get("tremolo")
        self._wah = spec.get("wah")
        self._wah_filter = (_Biquad.bandpass(self._wah[0], self._wah[3])
                            if self._wah else None)
        self._phaser = spec.get("phaser")
        self._phaser_state = [[0.0, 0.0], [0.0, 0.0]]
        self._echo = spec.get("echo")
        if self._echo:
            seconds = float(self._echo[0])
            self._echo_ring = np.zeros(
                int(round(seconds * SAMPLE_RATE)) + 4096, dtype=np.float32)
        else:
            self._echo_ring = None
        self._echo_pos = 0
        self._robot = spec.get("robot")
        self._robot_phase = 0.0
        self._lfo = 0.0
        self._flanger = spec.get("flanger")
        self._flanger_ring = (np.zeros(4096, dtype=np.float32)
                              if self._flanger else None)
        self._flanger_pos = 0
        self._vibrato = spec.get("vibrato")
        self._vibrato_ring = (np.zeros(4096, dtype=np.float32)
                              if self._vibrato else None)
        self._vibrato_pos = 0
        self._stutter = spec.get("stutter")
        self._stutter_history = collections.deque(
            maxlen=int(self._stutter[0]) if self._stutter else 0)
        self._stutter_loop = None
        self._stutter_pos = 0
        self._cave = spec.get("cave")
        if self._cave:
            longest = max(float(tap) for tap in self._cave[0])
            self._cave_ring = np.zeros(
                int(round(longest * SAMPLE_RATE / 1000.0)) + 4096,
                dtype=np.float32)
        else:
            self._cave_ring = None
        self._cave_pos = 0
        self._alien = spec.get("alien")
        self._alien_phase = 0.0
        self._sub_octave = spec.get("sub_octave")
        self._divider_filter = (_Fir(_lowpass(700.0, 129))
                                if self._sub_octave else None)
        self._divider_state = 0
        self._divider_negative = False
        self._divider_level = 0.0
        self._envelope_wah = spec.get("envelope_wah")
        self._env_filter = (_Biquad.bandpass(self._envelope_wah[0],
                                            self._envelope_wah[2])
                            if self._envelope_wah else None)
        self._env = 0.0
        self._gate_pedal = spec.get("chop")
        self._gate_fir = (_Fir(np.ones(48, dtype=np.float32) / 48.0)
                          if self._gate_pedal else None)
        # The chop keeps its own phase: the Custom Voice board may run a Chop
        # in the Rhythm slot and a Tremolo in Movement at once, and two blocks
        # advancing one oscillator would make each run at the sum of the two
        # rates. Every other block here already owns its phase.
        self._chop_phase = 0.0
        self._reverse = spec.get("reverse")
        self._reverse_history = collections.deque(
            maxlen=int(self._reverse[0]) if self._reverse else 0)
        self._reverse_loop = None
        self._reverse_pos = 0
        self._chorus3 = spec.get("chorus3")
        self._chorus_ring = (np.zeros(4096, dtype=np.float32)
                            if self._chorus3 else None)
        self._chorus_pos = 0
        # Vinyl: a short delay whose length drifts (a tape's wow) plus the odd
        # click. Doubler: one copy at 22 ms, almost still.
        self._vinyl = spec.get("vinyl")
        self._vinyl_ring = (np.zeros(4096, dtype=np.float32)
                            if self._vinyl else None)
        self._vinyl_pos = 0
        self._vinyl_pop = (_Fir(_tick(7)) if self._vinyl else None)
        self._vinyl_level = 0.0
        self._rng = np.random.default_rng(0x5EED)
        self._doubler = spec.get("doubler")
        self._doubler_ring = (np.zeros(4096, dtype=np.float32)
                              if self._doubler else None)
        self._doubler_pos = 0
        # Wood Body: one resonant comb, the air of a guitar's box.
        self._body = spec.get("body")
        self._body_ring = (np.zeros(8192, dtype=np.float32)
                           if self._body else None)
        self._body_pos = 0
        # Shimmer: a single-sideband shifter. The Hilbert branch is a short FIR
        # and the real branch is delayed to meet it, so the sideband that is
        # cancelled stays cancelled at the top of the voice.
        self._shimmer = spec.get("shimmer")
        self._shimmer_phase = 0.0
        self._shimmer_delay = (np.zeros(4096, dtype=np.float32)
                               if self._shimmer else None)
        self._shimmer_pos = 0
        self._shimmer_fir = (_Fir(_hilbert(127)) if self._shimmer else None)
        # Radio: a noise floor that never stops. It is the one thing a
        # band-limited voice needs to read as a transmission rather than as a
        # telephone -- and it is added *after* the gate (see `_static_block`),
        # because a hiss that closes with the words is a whisper, not a radio.
        self._static = spec.get("static")
        self._static_high = (_Biquad.highpass(self._static[1], 0.9)
                             if self._static else None)
        self._static_low = (_Biquad.lowpass(self._static[2], 0.9)
                            if self._static else None)
        # Whisper: filtered noise shaped by the voice's own envelope.
        self._whisper = spec.get("whisper")
        self._whisper_noise = (_Biquad.bandpass(2500.0, 0.8)
                               if self._whisper else None)
        self._whisper_level = 0.0
        # Growl: two formants moved down, so the voice sits in its chest.
        self._growl = spec.get("growl")
        self._growl_filter = (_Fir(_lowpass(self._growl[0], 129))
                              if self._growl else None)
        # The modern pedals: a vocoder bank, a raised formant, an exciter, an
        # up-and-down compressor, a varispeed delay, a cloud, a beat duck, a
        # held slice, a stepped band-bank and a backwards ghost.
        # A vocoder spec is `(q, mix)` for the classic and `(q, mix, hz, wave,
        # shift, breath)` for the family of twenty: the note it speaks on, its
        # waveform, where its band bank sits (below 1 is a bigger machine, above
        # 1 a smaller one) and how much noise rides on it. The short form is
        # kept so every preset written before the family reads the same.
        self._vocoder = spec.get("vocoder")
        if self._vocoder:
            _q, _mix = float(self._vocoder[0]), float(self._vocoder[1])
            _extra = tuple(self._vocoder[2:])
            _hz = float(_extra[0]) if len(_extra) > 0 else VOCODER_CARRIER_HZ
            _wave = _extra[1] if len(_extra) > 1 else "saw"
            _shift = float(_extra[2]) if len(_extra) > 2 else 1.0
            _breath = float(_extra[3]) if len(_extra) > 3 else 0.0
            self._vocoder_voice = (_q, _mix, _hz, _wave, _shift, _breath)
            self._vocoder_filters = [_Biquad.bandpass(hz * _shift, _q)
                                     for hz in VOCODER_BANDS]
            self._vocoder_levels = _carrier_levels(_q, _hz, _shift, _wave,
                                                   _breath)
        else:
            self._vocoder_voice = None
            self._vocoder_filters = None
            self._vocoder_levels = None
        self._vocoder_envelopes = [0.0] * len(VOCODER_BANDS)
        self._vocoder_gains = [0.0] * len(VOCODER_BANDS)
        self._vocoder_cycle = 0.0
        # Pan Delay: a delay (the talker's) and a position (the listener's).
        # The ring holds `taps` repeats of `seconds` each; `_pan_clock` is the
        # movement's own clock, advanced once per frame and read by `motion()`
        # so the three bytes that travel describe the frame just shaped.
        self._pan_delay = spec.get("pan_delay")
        self._pan_motion = spec.get("pan_motion", "stereo")
        if self._pan_delay:
            seconds = float(self._pan_delay[0])
            taps = max(1, int(self._pan_delay[1]))
            self._pan_ring = np.zeros(int(round(seconds * taps * SAMPLE_RATE))
                                      + 4800, dtype=np.float32)
            self._pan_pos = 0
            self._pan_weight = 0.55 / taps
        else:
            self._pan_ring = None
            self._pan_pos = 0
            self._pan_weight = 0.0
        self._pan_clock = 0.0
        self._pan_last = 0.0
        self._formant = spec.get("formant")
        self._formant_filter = (_Biquad.bandpass(self._formant[0],
                                                 self._formant[1])
                                if self._formant else None)
        self._ott = spec.get("ott")
        self._ott_env = 0.0
        # Sag: the amp's supply, not its output -- see `_sag_block`.
        self._sag = spec.get("sag")
        self._sag_gain = 1.0
        self._tapestop = spec.get("tapestop")
        self._tapestop_ring = (np.zeros(32768, dtype=np.float32)
                               if self._tapestop else None)
        self._tapestop_pos = 0
        self._tapestop_clock = 0.0
        self._cloud = spec.get("cloud")
        self._cloud_ring = (np.zeros(32768, dtype=np.float32)
                            if self._cloud else None)
        self._cloud_pos = 0
        self._cloud_phase = 0.0
        self._air = spec.get("air")
        self._air_filter = (_Biquad.highpass(self._air[0], 1.1)
                            if self._air else None)
        self._pump = spec.get("pump")
        self._pump_phase = 0.0
        self._freeze = spec.get("freeze")
        self._freeze_history = collections.deque(
            maxlen=int(self._freeze[0]) if self._freeze else 0)
        self._freeze_loop = None
        self._freeze_pos = 0
        self._freeze_counter = 0
        self._freeze_seam = None
        self._step = spec.get("step")
        self._step_filters = ([_Biquad.bandpass(hz, self._step[2])
                               for hz in self._step[1]]
                              if self._step else None)
        self._step_index = 0
        self._step_counter = 0
        self._step_band = 0
        self._swell = spec.get("swell")
        self._swell_ring = (np.zeros(65536, dtype=np.float32)
                            if self._swell else None)
        self._swell_pos = 0
        self._swell_clock = 0.0
        # The studio pedals: a generic tap cluster (a plate, a room, a wash),
        # a dark feedback echo, a gated reverb and a riser.
        self._cluster = spec.get("cluster")
        if self._cluster:
            longest = max(float(tap) for tap in self._cluster[0])
            self._cluster_ring = np.zeros(
                int(round(longest * SAMPLE_RATE / 1000.0)) + 4096,
                dtype=np.float32)
        else:
            self._cluster_ring = None
        self._cluster_pos = 0
        self._dark_echo = spec.get("dark_echo")
        if self._dark_echo:
            self._dark_ring = np.zeros(
                int(round(float(self._dark_echo[0]) * SAMPLE_RATE)) + 4096,
                dtype=np.float32)
        else:
            self._dark_ring = None
        self._dark_pos = 0
        self._dark_filter = (_Biquad.lowpass(self._dark_echo[3])
                             if self._dark_echo else None)
        self._gated = spec.get("gated")
        if self._gated:
            longest = max(float(tap) for tap in self._gated[0])
            self._gated_ring = np.zeros(
                int(round(longest * SAMPLE_RATE / 1000.0)) + 4096,
                dtype=np.float32)
        else:
            self._gated_ring = None
        self._gated_pos = 0
        self._gated_gain = 0.0
        self._gated_left = 0.0
        self._riser = spec.get("riser")
        self._riser_ring = (np.zeros(32768, dtype=np.float32)
                            if self._riser else None)
        self._riser_pos = 0
        self._riser_clock = 0.0

    # ------------------------------------------------------------------ the gate
    def _gate(self, block):
        """Open or close this frame, then ramp the gain ACROSS it.

        The ramp is what keeps a gate from clicking: the gain arrives at the
        frame boundary at exactly the value the next frame starts from.
        """
        level = float(np.sqrt(np.mean(np.square(block)))) if block.size else 0.0
        before = self._gate_gain
        if level >= self._gate_threshold:
            self._gate_gain = min(1.0, before + GATE_OPEN)
        else:
            self._gate_gain = max(0.0, before - GATE_CLOSE)
        if before == self._gate_gain:
            return block * self._gate_gain
        ramp = np.linspace(before, self._gate_gain, block.size,
                           endpoint=False, dtype=np.float32)
        return block * ramp

    # ---------------------------------------------------------------- the shaper
    def _shape_block(self, block):
        if self._shape is None:
            return block
        driven = block * self._drive
        if self._shape == "tanh":
            offset = np.tanh(self._bias)
            scaled = (np.tanh(driven + self._bias) - offset) / (1.0 - offset)
            return scaled
        # A fuzz's hard clip, biased so the two halves do not match: that
        # asymmetry is the octave-ish edge a fuzz has and a distortion does not.
        floor = -1.0 + 0.0
        clipped = np.clip(driven + self._bias, floor, 1.0)
        scaled = (clipped - np.clip(self._bias, floor, 1.0)) / (1.0 - abs(self._bias))
        return scaled

    # --------------------------------------------------------------- the crusher
    def _crush_block(self, block):
        if self._crush is None:
            return block
        bits, rate = self._crush
        step = SAMPLE_RATE / float(rate)
        held = np.empty_like(block)
        value = self._crush_value
        phase = self._crush_phase
        for index in range(block.size):
            if phase <= 0.0:
                value = block[index]
                phase += step
            held[index] = value
            phase -= 1.0
        self._crush_value = float(value)
        self._crush_phase = phase
        levels = float(2 ** (int(bits) - 1))
        return np.round(held * levels) / levels

    # ---------------------------------------------------------------- the chorus
    def _chorus_block(self, block):
        if not self._chorus:
            return block
        limits = self._ring.size
        start = self._ring_pos
        positions = np.arange(block.size)
        write_at = (start + positions) % limits
        self._ring[write_at] = block
        # 12 ms base, 3 ms sweep, 0.45 Hz: the width is what is heard, and the
        # base is why this preset carries the only real delay in the set.
        sweep = (0.012 + 0.003 * np.sin(
            2.0 * np.pi * 0.45 * positions / SAMPLE_RATE + self._phase))
        delay = sweep * SAMPLE_RATE
        read_at = (start + positions - delay) % limits
        lower = np.floor(read_at).astype(np.int64)
        frac = (read_at - lower).astype(np.float32)
        delayed = (self._ring[lower % limits] * (1.0 - frac)
                   + self._ring[(lower + 1) % limits] * frac)
        self._ring_pos = (start + block.size) % limits
        self._phase = (self._phase
                       + 2.0 * np.pi * 0.45 * block.size / SAMPLE_RATE) % (
                           2.0 * np.pi)
        return block * (1.0 - self._chorus) + delayed * self._chorus

    # ------------------------------------------------- the one-block pedals
    def _octave_block(self, block):
        """The rectified copy that adds the octave, before the DC block."""
        if not self._octave:
            return block
        return block * (1.0 - self._octave) + np.abs(block) * self._octave

    def _tremolo_block(self, block):
        if not self._tremolo:
            return block
        rate, depth = self._tremolo
        positions = np.arange(block.size)
        phase = self._lfo + 2.0 * np.pi * rate * positions / SAMPLE_RATE
        # Depth 0.85 reaches 0.15 at the bottom: a pulse, not a mute.
        gain = (1.0 - depth) + depth * (0.5 + 0.5 * np.sin(phase))
        self._lfo = ((self._lfo
                      + 2.0 * np.pi * rate * block.size / SAMPLE_RATE)
                     % (2.0 * np.pi))
        return block * gain

    def _wah_block(self, block):
        if not self._wah:
            return block
        low, high, rate, q, boost = self._wah
        sweep = 0.5 - 0.5 * np.cos(self._lfo)
        self._wah_filter.retune_bandpass(low + (high - low) * sweep, q)
        self._lfo = ((self._lfo
                      + 2.0 * np.pi * rate * block.size / SAMPLE_RATE)
                     % (2.0 * np.pi))
        return self._wah_filter.process(block) * boost

    def _phaser_block(self, block):
        """Two swept all-pass stages mixed with the dry voice.

        Two, not four: each stage is a sample loop in Python, and two already
        put a pair of moving notches in the voice -- four cost twice the frame
        time for a shade more depth, which is the wrong trade on a talker's own
        machine.
        """
        if not self._phaser:
            return block
        rate, low, high = self._phaser
        sweep = 0.5 - 0.5 * np.cos(self._lfo)
        centre = low + (high - low) * sweep
        self._lfo = ((self._lfo
                      + 2.0 * np.pi * rate * block.size / SAMPLE_RATE)
                     % (2.0 * np.pi))
        coefficients = []
        for multiplier in (1.0, 1.6):
            hz = min(centre * multiplier, SAMPLE_RATE * 0.45)
            tangent = float(np.tan(np.pi * hz / SAMPLE_RATE))
            coefficients.append((1.0 - tangent) / (1.0 + tangent))
        state = self._phaser_state
        samples = block.tolist()
        out = [0.0] * len(samples)
        for index, dry in enumerate(samples):
            sample = dry
            for stage in range(2):
                a = coefficients[stage]
                stage_state = state[stage]
                y = a * sample + stage_state[0] - a * stage_state[1]
                stage_state[0] = sample
                stage_state[1] = y
                sample = y
            out[index] = 0.5 * dry + 0.5 * sample
        return np.asarray(out, dtype=np.float32)

    def _echo_block(self, block):
        """A single tap with feedback; the delay is longer than a frame."""
        if not self._echo:
            return block
        seconds, feedback, mix = self._echo
        delay = int(round(float(seconds) * SAMPLE_RATE))
        ring, size = self._echo_ring, self._echo_ring.size
        positions = np.arange(block.size)
        read = (self._echo_pos + positions - delay) % size
        write = (self._echo_pos + positions) % size
        delayed = ring[read].copy()
        ring[write] = block + float(feedback) * delayed
        self._echo_pos = int((self._echo_pos + block.size) % size)
        return block * (1.0 - mix) + delayed * mix

    def _pan_delay_block(self, block):
        """The Pan Delay's repeats: `taps` copies, `seconds` apart, no feedback.

        No feedback on purpose. A repeat that feeds itself would smear the
        movement: three copies at known distances are what a listener hears
        crossing, and a tail past the third is a wash rather than a ping-pong.
        The gains fall with each repeat, so the third is a hint of the first.
        """
        if not self._pan_delay:
            return block
        seconds, taps, _metres = self._pan_delay
        taps = max(1, int(taps))
        delay = int(round(float(seconds) * SAMPLE_RATE))
        ring, size = self._pan_ring, self._pan_ring.size
        positions = np.arange(block.size)
        write = (self._pan_pos + positions) % size
        wet = np.zeros(block.size, dtype=np.float32)
        for tap in range(1, taps + 1):
            read = (self._pan_pos + positions - delay * tap) % size
            wet += ring[read] * (self._pan_weight * (taps - tap + 1) / taps)
        ring[write] = block
        self._pan_pos = int((self._pan_pos + block.size) % size)
        period = 2.0 * float(seconds)
        self._pan_clock = (self._pan_clock + block.size / SAMPLE_RATE) % period
        return block + wet

    def motion(self):
        """The frame's place in the listener's space: (right, forward), -1..1.

        Read after `process()`: the clock moved with the frame that was just
        shaped, so the three bytes a listener receives describe the sound they
        are about to hear. A preset with no pan stays at the centre -- the
        answer is (0, 0) for every other pedal in the catalogue.
        """
        if not self._pan_delay or self._pan_motion == "off":
            return 0.0, 0.0
        seconds = float(self._pan_delay[0])
        pan = float(np.cos(np.pi * self._pan_clock / seconds))
        if self._pan_motion == "front_back":
            return 0.0, pan
        return pan, 0.0

    def _robot_block(self, block):
        if not self._robot:
            return block
        hz, mix = self._robot
        phase = (self._robot_phase
                 + 2.0 * np.pi * hz * np.arange(block.size) / SAMPLE_RATE)
        carrier = np.sin(phase)
        self._robot_phase = ((self._robot_phase
                              + 2.0 * np.pi * hz * block.size / SAMPLE_RATE)
                             % (2.0 * np.pi))
        return block * (1.0 - mix) + block * carrier * mix

    def _flanger_block(self, block):
        """A short swept delay that feeds back -- the jet sound.

        One sample at a time, unlike the pure delays: the ring is fed the
        output, and the delay (1.5-5.5 ms) is shorter than a frame, so the
        sample being read can have been written by this very block. Reading a
        whole block first and writing it afterwards -- what this used to do --
        means every read past the delay lands on the *previous* wrap of the
        ring, 4096 samples old: a step at every 20 ms boundary, heard as a
        50 Hz buzz under the jet.
        """
        if not self._flanger:
            return block
        base, sweep, rate, feedback, mix = self._flanger
        ring, size = self._flanger_ring, self._flanger_ring.size
        start = self._flanger_pos
        samples = block.tolist()
        out = [0.0] * len(samples)
        step = 2.0 * np.pi * rate / SAMPLE_RATE
        angle = self._lfo
        gain = 1.0 - float(mix)
        for index, dry in enumerate(samples):
            delay = ((base + sweep * (0.5 + 0.5 * float(np.sin(angle))))
                     * SAMPLE_RATE / 1000.0)
            read = start + index - delay
            lower = int(np.floor(read))
            fraction = read - lower
            delayed = (ring[lower % size] * (1.0 - fraction)
                       + ring[(lower + 1) % size] * fraction)
            ring[(start + index) % size] = dry + float(feedback) * delayed
            out[index] = dry * gain + delayed * float(mix)
            angle += step
        self._flanger_pos = int((start + block.size) % size)
        self._lfo = float(angle % (2.0 * np.pi))
        return np.asarray(out, dtype=np.float32)

    def _delay_frame(self, block, ring, start, delay_samples):
        """Write the frame, then read the delay -- the correct order.

        A delay shorter than a frame (every wobbling pedal here) has to see the
        samples this frame has just written: read-before-write makes the read
        fall on the previous wrap of the ring for every sample past the delay, a
        step at the frame boundary. Writing first is exact and still
        vectorised; the ring must be longer than the frame plus the longest
        delay, which is why they are powers of two far above both.
        """
        size = ring.size
        positions = np.arange(block.size)
        ring[(start + positions) % size] = block
        read = (start + positions - delay_samples) % size
        lower = np.floor(read).astype(np.int64)
        fraction = (read - lower).astype(np.float32)
        delayed = (ring[lower % size] * (1.0 - fraction)
                   + ring[(lower + 1) % size] * fraction)
        return delayed, int((start + block.size) % size)

    def _vibrato_block(self, block):
        """A delay that only wobbles, fully wet: pitch, not an echo."""
        if not self._vibrato:
            return block
        base, sweep, rate = self._vibrato
        positions = np.arange(block.size)
        phase = self._lfo + 2.0 * np.pi * rate * positions / SAMPLE_RATE
        delay = (base + sweep * np.sin(phase)) * SAMPLE_RATE / 1000.0
        delayed, self._vibrato_pos = self._delay_frame(
            block, self._vibrato_ring, self._vibrato_pos, delay)
        self._lfo = ((self._lfo
                      + 2.0 * np.pi * rate * block.size / SAMPLE_RATE)
                     % (2.0 * np.pi))
        return delayed

    def _stutter_block(self, block):
        """Repeat the last tenth of a second, five times a second.

        Frames are the unit because the effect is a rhythm, not a sample
        operation: every period the last `slice` frames are captured and played
        back, then the voice carries on. The first slice waits until there is
        something to repeat, so a press never opens with silence.
        """
        if not self._stutter:
            return block
        slice_frames, period_frames = self._stutter
        history = self._stutter_history
        if len(history) < slice_frames:
            history.append(block.copy())
            return block
        position = self._stutter_pos
        if position == 0:
            self._stutter_loop = list(history)
        out = block
        looped = (self._stutter_loop[position]
                  if position < slice_frames and self._stutter_loop else None)
        if looped is not None and looped.size == block.size:
            out = looped
        history.append(block.copy())
        self._stutter_pos = (position + 1) % period_frames
        return out

    @staticmethod
    def _multi_tap(block, taps, gains, feedback, mix, ring, position):
        """A bank of taps feeding one another; returns the frame and the next
        write position. Delays are longer than a frame, which is what makes
        read-before-write safe and the whole thing vectorised."""
        size = ring.size
        positions = np.arange(block.size)
        wet = np.zeros(block.size, dtype=np.float32)
        for tap_ms, gain in zip(taps, gains):
            delay = int(round(float(tap_ms) * SAMPLE_RATE / 1000.0))
            read = (position + positions - delay) % size
            wet += ring[read] * gain
        ring[(position + positions) % size] = (block
                                              + float(feedback) * wet)
        return (block * (1.0 - mix) + wet * mix,
                int((position + block.size) % size))

    def _cave_block(self, block):
        """Four taps that feed one another: a room rather than one repeat."""
        if not self._cave:
            return block
        taps, feedback, mix = self._cave
        out, self._cave_pos = self._multi_tap(
            block, taps, (0.7, 0.55, 0.4, 0.3), feedback, mix,
            self._cave_ring, self._cave_pos)
        return out

    def _sub_octave_block(self, block):
        """An octave down with a divider, not a pitch shifter.

        A square wave toggles at every positive-going zero crossing of the
        (low-passed) voice, so its period is two of the voice's cycles: an
        octave below. This is the oldest sub-octave pedal there is, and it is
        cheap and has no latency -- at the cost of a buzzy, square copy. The
        700 Hz filter before the crossing detector keeps a formant from adding
        crossings of its own and turning the note into noise.
        """
        if not self._sub_octave:
            return block
        mix = float(self._sub_octave)
        smooth = self._divider_filter.process(block)
        negative = smooth < 0.0
        rise = np.empty(block.size, dtype=bool)
        rise[0] = self._divider_negative and not bool(negative[0])
        rise[1:] = negative[:-1] & ~negative[1:]
        toggles = np.cumsum(rise) % 2
        levels = ((toggles ^ self._divider_state).astype(np.float32)
                  * 2.0 - 1.0)
        self._divider_state = int(self._divider_state
                                  ^ int(toggles[-1]))
        self._divider_negative = bool(negative[-1])
        # A square's own DC must not make silence audible: the divider fades
        # with the voice, and a frame that was never voiced stays exactly zero.
        level = (float(np.sqrt(np.mean(np.square(smooth))))
                 if smooth.size else 0.0)
        self._divider_level += (min(1.0, level / 0.02)
                                - self._divider_level) * 0.5
        return block * (1.0 - mix) + levels * (mix * self._divider_level)

    def _envelope_wah_block(self, block):
        """A band that follows the voice's level, not a clock."""
        if not self._envelope_wah:
            return block
        low, high, q, boost = self._envelope_wah
        level = (float(np.sqrt(np.mean(np.square(block))))
                 if block.size else 0.0)
        target = min(1.0, level / 0.12)
        # Opening is quick (a word pushes the filter up); closing is slow, so a
        # gap between words does not slam it shut and open on the next one.
        speed = 0.45 if target > self._env else 0.08
        self._env += (target - self._env) * speed
        self._env_filter.retune_bandpass(low + (high - low) * self._env, q)
        return self._env_filter.process(block) * boost

    def _gate_pedal_block(self, block):
        """A hard 8 Hz chop, smoothed over a millisecond so it is not a click."""
        if not self._gate_pedal:
            return block
        rate, depth = self._gate_pedal
        positions = np.arange(block.size)
        phase = self._chop_phase + 2.0 * np.pi * rate * positions / SAMPLE_RATE
        raw = np.where(np.sin(phase) >= 0.0, 1.0, depth).astype(np.float32)
        gain = self._gate_fir.process(raw)
        self._chop_phase = ((self._chop_phase
                             + 2.0 * np.pi * rate * block.size / SAMPLE_RATE)
                            % (2.0 * np.pi))
        return block * gain

    def _reverse_block(self, block):
        """Play the last slice backwards, then let the voice carry on."""
        if not self._reverse:
            return block
        slice_frames, period_frames = self._reverse
        history = self._reverse_history
        if len(history) < slice_frames:
            history.append(block.copy())
            return block
        position = self._reverse_pos
        if position == 0:
            self._reverse_loop = [np.ascontiguousarray(frame[::-1])
                                  for frame in reversed(list(history))]
        out = block
        looped = (self._reverse_loop[position]
                  if position < slice_frames and self._reverse_loop else None)
        if looped is not None and looped.size == block.size:
            out = looped
        history.append(block.copy())
        self._reverse_pos = (position + 1) % period_frames
        return out

    def _vinyl_block(self, block):
        """A record: a delay that drifts a few cents, and the odd click.

        The drift is a delay of 2.5 ms whose length breathes by a third of a
        millisecond at 0.6 Hz -- about 3 cents of wow, the amount a turntable's
        motor gives a sustained note. The crackle is sparse by construction
        (four sigma of white noise) and smoothed to a millisecond so a click is
        a tick rather than a sample-wide snap.
        """
        if not self._vinyl:
            return block
        rate, depth_ms, crackle = self._vinyl
        positions = np.arange(block.size)
        phase = self._lfo + 2.0 * np.pi * rate * positions / SAMPLE_RATE
        delay = ((2.5 + 0.5 * depth_ms * (1.0 + np.sin(phase)))
                 * SAMPLE_RATE / 1000.0)
        delayed, self._vinyl_pos = self._delay_frame(
            block, self._vinyl_ring, self._vinyl_pos, delay)
        self._lfo = ((self._lfo
                      + 2.0 * np.pi * rate * block.size / SAMPLE_RATE)
                     % (2.0 * np.pi))
        noise = self._rng.standard_normal(block.size).astype(np.float32)
        pops = (np.abs(noise) > 3.0).astype(np.float32) * np.sign(noise)
        # The crackle rides the voice: an empty frame stays exactly empty, and
        # silence never turns into a hiss of its own.
        level = (float(np.sqrt(np.mean(np.square(block))))
                 if block.size else 0.0)
        self._vinyl_level += (min(1.0, level / 0.03)
                              - self._vinyl_level) * 0.5
        return delayed + (self._vinyl_pop.process(pops) * float(crackle)
                          * self._vinyl_level)

    def _body_block(self, block):
        """A voice inside a guitar's box: one resonant comb.

        `y[n] = x[n] + g*y[n-D]` with D the period of 110 Hz is the shortest
        honest model of a wooden body -- it puts a resonance on a low A and its
        harmonics, so the voice gains a hollow knock rather than a reverb tail.
        """
        if not self._body:
            return block
        hz, feedback, mix = self._body
        ring, size = self._body_ring, self._body_ring.size
        delay = max(1, int(round(SAMPLE_RATE / float(hz))))
        start = self._body_pos
        samples = block.tolist()
        out = [0.0] * len(samples)
        gain = 1.0 - float(mix)
        for index, dry in enumerate(samples):
            wet = dry + float(feedback) * ring[(start + index - delay) % size]
            ring[(start + index) % size] = wet
            out[index] = dry * gain + wet * float(mix)
        self._body_pos = int((start + block.size) % size)
        return np.asarray(out, dtype=np.float32)

    def _shimmer_block(self, block):
        """A single-sideband frequency shifter: every partial moves by 14 Hz.

        Unlike the ring modulator (which mirrors the spectrum around a carrier
        and so sounds robotic), shifting *up* every partial by the same number
        of hertz keeps the voice's own shape and makes it glide -- the sound a
        ring-mod with a moving carrier only imitates. The quadrature branch is
        a 31-tap Hilbert FIR; the real branch waits the FIR's own delay so the
        two still cancel the lower sideband.
        """
        if not self._shimmer:
            return block
        hz, mix = self._shimmer
        quadrature = self._shimmer_fir.process(block)
        # `_Fir` correlates with its kernel, and a Hilbert kernel is
        # antisymmetric: what comes back is minus the 90-degree copy, aligned
        # with the frame. Pairing it with the voice delayed by half the kernel
        # (its own group delay) is what makes the pair add up to the *upper*
        # sideband only; the sign here is the difference between a shimmer and
        # a plain ring modulator.
        delay = (self._shimmer_fir.kernel.size - 1) // 2
        in_phase, self._shimmer_pos = self._delay_frame(
            block, self._shimmer_delay, self._shimmer_pos, delay)
        positions = np.arange(block.size)
        phase = (self._shimmer_phase
                 + 2.0 * np.pi * hz * positions / SAMPLE_RATE)
        shifted = (in_phase * np.cos(phase)
                   - quadrature * np.sin(phase)).astype(np.float32)
        self._shimmer_phase = ((self._shimmer_phase
                                + 2.0 * np.pi * hz * block.size / SAMPLE_RATE)
                               % (2.0 * np.pi))
        return block * (1.0 - float(mix)) + shifted * float(mix)

    def _whisper_block(self, block):
        """A hush: the voice thinned, with breath riding its own envelope.

        A whisper has no pitch, only noise shaped by the mouth -- so the noise
        here is filtered noise multiplied by the frame's own level, added under
        a voice that has already lost everything below 900 Hz. The envelope is
        smoothed over two frames, because a per-frame gain would chop the noise
        into 20 ms steps.
        """
        if not self._whisper:
            return block
        noise_mix, voice_mix = self._whisper
        level = (float(np.sqrt(np.mean(np.square(block))))
                 if block.size else 0.0)
        self._whisper_level += (level - self._whisper_level) * 0.5
        noise = self._whisper_noise.process(
            self._rng.standard_normal(block.size).astype(np.float32))
        breath = noise * min(1.0, self._whisper_level / 0.12)
        return block * float(voice_mix) + breath * float(noise_mix)

    def _growl_block(self, block):
        """Everything below 450 Hz lifted into a driven chest.

        A band-pass at two fixed formants made a voice quieter and hollow; what
        makes a growl is weight, not a notch. The low half of the voice is mixed
        back 1.6 times over the dry one, and the pre-gain in the chain has
        already made it dirty, so the result is a bigger body rather than a
        filter.
        """
        if not self._growl:
            return block
        _hz, mix = self._growl
        dark = self._growl_filter.process(block)
        return block * (1.0 - float(mix)) + dark * float(mix) * 1.6

    def _doubler_block(self, block):
        """One copy, 22 ms behind and barely moving: a double-tracked voice."""
        if not self._doubler:
            return block
        base, drift, rate, mix = self._doubler
        positions = np.arange(block.size)
        phase = self._lfo + 2.0 * np.pi * rate * positions / SAMPLE_RATE
        delay = (base + drift * 0.5 * (1.0 + np.sin(phase))) \
            * SAMPLE_RATE / 1000.0
        doubled, self._doubler_pos = self._delay_frame(
            block, self._doubler_ring, self._doubler_pos, delay)
        self._lfo = ((self._lfo
                      + 2.0 * np.pi * rate * block.size / SAMPLE_RATE)
                     % (2.0 * np.pi))
        return block * (1.0 - float(mix)) + doubled * float(mix)

    def _chorus3_block(self, block):
        """Three drifting copies, one LFO at three phase offsets."""
        if not self._chorus3:
            return block
        bases, rate, sweep, mix = self._chorus3
        ring, size = self._chorus_ring, self._chorus_ring.size
        positions = np.arange(block.size)
        start = self._chorus_pos
        ring[(start + positions) % size] = block
        wet = np.zeros(block.size, dtype=np.float32)
        for index, base in enumerate(bases):
            phase = (self._lfo + index * 2.1
                     + 2.0 * np.pi * rate * positions / SAMPLE_RATE)
            delay = (base + sweep * np.sin(phase)) * SAMPLE_RATE / 1000.0
            read = (start + positions - delay) % size
            lower = np.floor(read).astype(np.int64)
            fraction = (read - lower).astype(np.float32)
            wet += (ring[lower % size] * (1.0 - fraction)
                    + ring[(lower + 1) % size] * fraction)
        self._chorus_pos = int((start + block.size) % size)
        self._lfo = ((self._lfo
                      + 2.0 * np.pi * rate * block.size / SAMPLE_RATE)
                     % (2.0 * np.pi))
        return block * (1.0 - mix) + (wet / 3.0) * mix

    def _alien_block(self, block):
        """Ring modulation with a carrier that slides, not a fixed pitch."""
        if not self._alien:
            return block
        hz, rate, mix = self._alien
        centre = hz * (1.0 + 0.5 * np.sin(self._lfo))
        phase = (self._alien_phase
                 + 2.0 * np.pi * centre * np.arange(block.size) / SAMPLE_RATE)
        carrier = np.sin(phase)
        self._alien_phase = ((self._alien_phase
                              + 2.0 * np.pi * centre * block.size
                              / SAMPLE_RATE) % (2.0 * np.pi))
        self._lfo = ((self._lfo
                      + 2.0 * np.pi * rate * block.size / SAMPLE_RATE)
                     % (2.0 * np.pi))
        return block * (1.0 - mix) + block * carrier * mix

    # -------------------------------------------------------------- the modern
    def _air_block(self, block):
        """An exciter: the top filtered out, driven, and added back.

        A voice recorded close has nothing above four kilohertz to work with,
        and an EQ can only take away from what is there. Driving the top makes
        new harmonics above it, which is why this opens a dull voice instead of
        merely brightening it.
        """
        if not self._air:
            return block
        _hz, drive, mix = self._air
        top = self._air_filter.process(block)
        return block + np.tanh(top * float(drive)) * float(mix)

    def _formant_block(self, block):
        """One band lifted and driven: a talkbox's tube, a chest's weight."""
        if not self._formant:
            return block
        _hz, _q, mix, gain = self._formant
        band = self._formant_filter.process(block)
        return block * (1.0 - float(mix)) + band * (float(mix) * float(gain))

    def _vocoder_block(self, block):
        """The voice's six band envelopes, spoken by a saw instead of by you.

        A channel vocoder does not shift pitch: it keeps *what* the mouth did
        and throws away *what* made the sound, then rebuilds it from a fixed
        carrier through the same six bands. That is why the words survive and
        the voice does not -- and why the family of twenty can sound so
        different while doing the same thing: the carrier is the whole
        instrument (its note, its waveform, where its band bank sits, whether
        it has noise on it). The envelopes are read as one spectrum per frame (six
        more filters on the voice would be six more per-sample loops), and each
        band's gain ramps across the frame, so a syllable's edge is not a step
        at a frame boundary.
        """
        if not self._vocoder_voice:
            return block
        _q, mix, hz, kind, _shift, breath = self._vocoder_voice
        count = block.size
        positions = np.arange(count)
        phase = ((self._vocoder_cycle + hz * positions / SAMPLE_RATE) % 1.0)
        carrier = _carrier_frame(kind, phase, count, self._rng, breath)
        self._vocoder_cycle = ((self._vocoder_cycle
                                + hz * count / SAMPLE_RATE) % 1.0)
        spectrum = np.abs(np.fft.rfft(block)) ** 2
        bin_hz = SAMPLE_RATE / float(count)
        levels = self._vocoder_levels
        half = 2.0 ** 0.35
        wet = np.zeros(count, dtype=np.float32)
        for index, hz in enumerate(VOCODER_BANDS):
            first = max(1, int(hz / half / bin_hz))
            last = max(first + 1, int(hz * half / bin_hz))
            energy = float(spectrum[first:last].sum()) * 2.0 / (count * count)
            wanted = float(np.sqrt(energy))
            envelope = self._vocoder_envelopes[index]
            # Up fast, down slowly: a consonant is an envelope step and a vowel
            # is a steady band, so only the release may be slow.
            envelope = (wanted if wanted > envelope
                        else envelope + (wanted - envelope) * 0.45)
            self._vocoder_envelopes[index] = envelope
            gain = min(6.0, envelope / levels[index])
            band = self._vocoder_filters[index].process(carrier)
            wet += band * np.linspace(self._vocoder_gains[index], gain, count,
                                      endpoint=False)
            self._vocoder_gains[index] = gain
        return block * (1.0 - float(mix)) + wet * float(mix)

    def _static_block(self, block):
        """The channel's own noise floor: a band of noise under the voice.

        A radio is not a telephone because of its bandwidth -- a telephone is
        band-limited too. It is a radio because something is always hissing
        under the words, and because that hiss sits at a level of its own
        rather than following the speaker. So this is added to the frame the
        gate has already closed, and it draws from the processor's RNG rather
        than from the voice: silence in a channel is not silence in the room.

        The noise is band-limited to the same corners the preset gives the
        voice, so a narrow channel hisses narrowly -- which is what makes an
        eight-bit walkie sound thin rather than merely noisy. It joins the
        frame at the output stage -- before the preset's trim and the ceiling
        -- so a louder channel hisses louder and the hiss can never push the
        frame past `PEAK_CEILING`.
        """
        if not self._static:
            return block
        level = float(self._static[0])
        noise = (self._rng.standard_normal(block.size)
                 .astype(np.float32) * level)
        noise = self._static_high.process(noise)
        noise = self._static_low.process(noise)
        return block + noise

    def _ott_block(self, block):
        """A pop vocal's loudness: quiet lifted, loud held, no dials.

        Per sample, not per frame: a gain that moves at frame rate is a 50 Hz
        amplitude modulation under the words, the same buzz the wobbling delays
        had. The follower's attack is instant and its release is about forty
        milliseconds, which is what makes a compressor sound like a compressor.
        """
        if not self._ott:
            return block
        upward, downward = self._ott
        target = 0.05
        samples = block.tolist()
        out = [0.0] * len(samples)
        env = self._ott_env
        for index, sample in enumerate(samples):
            level = sample if sample >= 0.0 else -sample
            env = level if level > env else env + (level - env) * 0.0018
            if env > 1e-4:
                if env > target:
                    gain = (target / env) ** downward
                else:
                    gain = (target / env) ** upward
                if gain > 4.0:
                    gain = 4.0
            else:
                gain = 1.0
            out[index] = sample * gain
        self._ott_env = env
        return np.asarray(out, dtype=np.float32)

    def _sag_block(self, block):
        """A valve rectifier's sag: the supply dips, then comes back slowly.

        Not a compressor on the output but the amp's own behaviour: a loud
        passage draws more current than the rectifier can supply, so the whole
        amp gets quieter and only recovers once the passage ends. That is why
        it is *felt* on a chord rather than heard as an effect -- and why the
        recovery is much slower than the dip. The gain is ramped across the
        frame; a gain that jumps once per frame is the 50 Hz buzz the modern
        pedals were fixed for.
        """
        if not self._sag:
            return block
        depth, reference = self._sag
        level = (float(np.sqrt(np.mean(np.square(block))))
                 if block.size else 0.0)
        wanted = 1.0 / (1.0 + float(depth) * (level / float(reference)))
        before = self._sag_gain
        rate = SAG_FALL if wanted < before else SAG_RISE
        after = before + (wanted - before) * rate
        self._sag_gain = after
        if before == after:
            return block * after
        return block * np.linspace(before, after, block.size,
                                   endpoint=False, dtype=np.float32)

    def _pump_block(self, block):
        """A four-to-the-floor duck: the voice steps back into every beat."""
        if not self._pump:
            return block
        bpm, depth, release = self._pump
        beats = float(bpm) / 60.0
        positions = np.arange(block.size)
        phase = (self._pump_phase + beats * positions / SAMPLE_RATE) % 1.0
        gain = 1.0 - float(depth) * np.exp(-phase / float(release))
        self._pump_phase = float((self._pump_phase
                                  + beats * block.size / SAMPLE_RATE) % 1.0)
        return block * gain.astype(np.float32)

    def _step_block(self, block):
        """Three bands, one heard at a time, the choice on sixteenths."""
        if not self._step:
            return block
        rate, _bands, _q = self._step
        step_frames = max(1, int(round(1.0 / (float(rate) * 0.020))))
        bands = [filt.process(block) for filt in self._step_filters]
        self._step_counter += 1
        if self._step_counter >= step_frames:
            self._step_counter = 0
            self._step_index += 1
        wanted = STEP_PATTERN[self._step_index % len(STEP_PATTERN)]
        previous = self._step_band
        if wanted == previous:
            return bands[previous]
        # The band changes between frames, so the frame it changes in
        # crossfades from the old band into the new one: a hard switch between
        # two filters is a step, and a step is a click.
        ramp = np.linspace(0.0, 1.0, block.size, endpoint=False,
                           dtype=np.float32)
        self._step_band = wanted
        return bands[previous] * (1.0 - ramp) + bands[wanted] * ramp

    def _cloud_block(self, block):
        """Four drifting copies of the last third of a second."""
        if not self._cloud:
            return block
        taps, rate, depth, mix = self._cloud
        ring, size = self._cloud_ring, self._cloud_ring.size
        positions = np.arange(block.size)
        start = self._cloud_pos
        ring[(start + positions) % size] = block
        wet = np.zeros(block.size, dtype=np.float32)
        for index, tap in enumerate(taps):
            phase = (self._cloud_phase + index * 1.7
                     + 2.0 * np.pi * rate * positions / SAMPLE_RATE)
            delay = ((float(tap) + float(depth) * np.sin(phase))
                     * SAMPLE_RATE / 1000.0)
            read = (start + positions - delay) % size
            lower = np.floor(read).astype(np.int64)
            fraction = (read - lower).astype(np.float32)
            copy = (ring[lower % size] * (1.0 - fraction)
                    + ring[(lower + 1) % size] * fraction)
            # Each tap swells and fades at its own speed, so the haze moves
            # instead of being four fixed echoes.
            wet += copy * (0.55 + 0.45 * np.sin(phase * 0.5))
        self._cloud_pos = int((start + block.size) % size)
        self._cloud_phase = ((self._cloud_phase
                              + 2.0 * np.pi * rate * block.size / SAMPLE_RATE)
                             % (2.0 * np.pi))
        return block * (1.0 - float(mix)) + (wet / 4.0) * float(mix)

    def _tapestop_block(self, block):
        """The head slows to a stop, then the tape starts again.

        A varispeed: the read head runs at a rate that falls to zero, so the
        pitch falls with it. The gain is zero while the head resets to the live
        sample, which is what keeps the jump back inaudible; the ring is
        written all the while, so the voice is never lost.
        """
        if not self._tapestop:
            return block
        hold, fall = self._tapestop
        count = block.size
        positions = np.arange(count)
        cycle = float(hold) + float(fall)
        phase = np.mod(self._tapestop_clock + positions / SAMPLE_RATE, cycle)
        progress = np.clip((phase - float(hold)) / float(fall), 0.0, 1.0)
        # A 20 ms delay through the hold, then the delay grows as p-squared, so
        # the playback rate (1 - d(delay)/dt) reaches exactly zero at its end.
        delay = (0.020 + (float(fall) * 0.5) * progress * progress) * SAMPLE_RATE
        fade_in = np.clip(phase / 0.12, 0.0, 1.0)
        fade_out = np.clip((1.0 - progress) / 0.20, 0.0, 1.0)
        self._tapestop_clock = float((self._tapestop_clock
                                      + count / SAMPLE_RATE) % cycle)
        slowed, self._tapestop_pos = self._delay_frame(
            block, self._tapestop_ring, self._tapestop_pos, delay)
        return slowed * np.minimum(fade_in, fade_out)

    def _swell_block(self, block):
        """A backwards ghost of the last word, growing under this one.

        The read head walks backwards (the delay grows faster than real time),
        so what is heard under the voice is a slice of the recent past played
        in reverse, with an envelope that grows into the cut. It is the reverse
        reverb of a trailer, made without the look-ahead a real one would need.
        """
        if not self._swell:
            return block
        near, far, active, cycle, mix = self._swell
        count = block.size
        positions = np.arange(count)
        clock = np.mod(self._swell_clock + positions / SAMPLE_RATE, float(cycle))
        progress = np.clip(clock / float(active), 0.0, 1.0)
        delay = (float(near) + (float(far) - float(near)) * progress) \
            * SAMPLE_RATE
        gain = np.where(clock < float(active), progress * progress, 0.0)
        self._swell_clock = float((self._swell_clock
                                   + count / SAMPLE_RATE) % float(cycle))
        ghost, self._swell_pos = self._delay_frame(
            block, self._swell_ring, self._swell_pos, delay)
        return block + ghost * (gain.astype(np.float32) * float(mix))

    def _freeze_block(self, block):
        """The last sixth of a second held, with a crossfade at its seam."""
        if not self._freeze:
            return block
        length, refresh = self._freeze
        history = self._freeze_history
        history.append(block.copy())
        if len(history) < length:
            return block
        self._freeze_counter += 1
        if self._freeze_loop is None:
            self._freeze_loop = list(history)
            self._freeze_pos = 0
        elif self._freeze_counter % int(refresh) == 0:
            # The drone follows the newest words without cutting the loop: the
            # position and the seam both survive the capture.
            self._freeze_loop = list(history)
        loop = self._freeze_loop
        position = self._freeze_pos
        out = loop[position]
        seam = self._freeze_seam
        if seam is not None and seam.shape == out.shape:
            # Two frames of blend: the slice ends on one phoneme and starts on
            # another, and that join is the one place this drone can click.
            if position == 0:
                out = loop[0] * 0.7 + seam * 0.3
            elif position == 1:
                out = loop[1] * 0.9 + seam * 0.1
        if position == length - 1:
            self._freeze_seam = loop[-1]
        self._freeze_pos = (position + 1) % length
        return out

    # ------------------------------------------------------ the studio pedals
    def _cluster_block(self, block):
        """A bank of taps feeding one another: a plate, a room, a wash."""
        if not self._cluster:
            return block
        taps, gains, feedback, mix = self._cluster
        out, self._cluster_pos = self._multi_tap(
            block, taps, gains, feedback, mix, self._cluster_ring,
            self._cluster_pos)
        return out

    def _dark_echo_block(self, block):
        """An echo whose every repeat is darker than the one before it.

        The low-pass sits *inside* the feedback path, so a repeat passes it
        once per round trip: the second is darker than the first, the third
        darker again, and a dub tail falls away instead of piling up.
        """
        if not self._dark_echo:
            return block
        seconds, feedback, mix, _corner = self._dark_echo
        delay = int(round(float(seconds) * SAMPLE_RATE))
        ring, size = self._dark_ring, self._dark_ring.size
        positions = np.arange(block.size)
        read = (self._dark_pos + positions - delay) % size
        write = (self._dark_pos + positions) % size
        delayed = ring[read].copy()
        ring[write] = block + float(feedback) * self._dark_filter.process(
            delayed)
        self._dark_pos = int((self._dark_pos + block.size) % size)
        return block * (1.0 - float(mix)) + delayed * float(mix)

    def _gated_block(self, block):
        """A reverb with a timer on its tail: the gated sound.

        The tail opens with the voice and is cut a fixed time later whatever
        the voice does -- that is what separates a gated room from a plate. The
        gain is ramped across each frame, because a gate that steps is a click.
        """
        if not self._gated:
            return block
        taps, gains, feedback, mix, hold, threshold = self._gated
        wet, self._gated_pos = self._multi_tap(
            block, taps, gains, feedback, 1.0, self._gated_ring,
            self._gated_pos)
        level = (float(np.sqrt(np.mean(np.square(block))))
                 if block.size else 0.0)
        if level >= float(threshold):
            self._gated_left = float(hold)
        before = self._gated_gain
        self._gated_gain = min(1.0, max(0.0, self._gated_left / float(hold)))
        self._gated_left = max(0.0, self._gated_left
                               - block.size / SAMPLE_RATE)
        ramp = np.linspace(before, self._gated_gain, block.size,
                           endpoint=False)
        return block * (1.0 - float(mix)) + wet * (float(mix) * ramp)

    def _riser_block(self, block):
        """The pitch climbs, and the head catches the live sample again.

        The delay falls, so the read head runs faster than real time -- rate
        `1 - d(delay)/dt` is above one and the pitch climbs with it. The wet is
        silent where the head resets to the top of the ramp, so the jump back
        is not heard; the dry voice carries the words throughout.
        """
        if not self._riser:
            return block
        start_delay, rise, cycle, mix = self._riser
        count = block.size
        positions = np.arange(count)
        clock = np.mod(self._riser_clock + positions / SAMPLE_RATE,
                       float(cycle))
        progress = np.clip(clock / float(rise), 0.0, 1.0)
        # The delay holds, then falls as p-squared: the playback rate is
        # 1 + 2*start*p/rise, so the note starts *in tune* and climbs -- a fall
        # in the delay from the first sample would arrive already sharp.
        delay = ((float(start_delay) * (1.0 - progress ** 2) + 0.02)
                 * SAMPLE_RATE)
        gain = (np.clip((1.0 - progress) / 0.12, 0.0, 1.0)
                * np.clip(clock / 0.08, 0.0, 1.0))
        self._riser_clock = float((self._riser_clock
                                   + count / SAMPLE_RATE) % float(cycle))
        climbed, self._riser_pos = self._delay_frame(
            block, self._riser_ring, self._riser_pos, delay)
        return block * (1.0 - float(mix)) + climbed * (
            gain.astype(np.float32) * float(mix))

    def _loudness_trim(self, block):
        """Pull a dense effect back to the average a voice arrives at.

        The peak ceiling is not enough on its own. A fuzz, a crusher or a board
        that stacks a shaper with a sub octave hands over a nearly square wave:
        the same 0.70 peak as a thin pedal, but an RMS four to six times a
        spoken voice's. The room's chain -- a bass shelf, a speech compressor, a
        PA safety limiter that does not engage below 0.85 -- was built for the
        voice, so the square arrives unattenuated and the speaker breaks up.

        So the trim is on the RMS, not the peak: attenuating only (a quiet voice
        and a quiet board pass byte for byte), aimed at `VOICE_LOUDNESS`, fast
        on the way down and slow on the way up, and interpolated across the
        frame so the gain cannot step. A fixed pedal is not trimmed -- it was
        tuned for a room when it was made, and `self._levelled` says so --
        unless the player has moved its tone, which is no longer that
        measurement; a tone put back to flat releases the trim (slowly, over
        the same ramp) rather than dropping the gain on the frame it lands.
        """
        levelled = self._levelled or (self._tone is not None
                                      and self._tone.active())
        if not levelled and self._loudness_gain > 0.9995:
            # A fixed preset that has never been moved: byte for byte, its own
            # measured sound, and no multiply by 1.0 on the way out.
            self._loudness_gain = 1.0
            return block
        level = (float(np.sqrt(np.mean(np.square(block))))
                 if block.size else 0.0)
        # A preset whose tone has been put back to flat releases the trim
        # instead of dropping it: the gain it was holding is still on the last
        # frame, and returning the block untrimmed would be a step the size of
        # that gain (the same click the latch fixes on the way past 0 dB).
        target = ((VOICE_LOUDNESS / level)
                  if levelled and level > VOICE_LOUDNESS else 1.0)
        rate = (LOUDNESS_ATTACK if target < self._loudness_gain
                else LOUDNESS_RELEASE)
        goal = self._loudness_gain + (target - self._loudness_gain) * rate
        if target >= 1.0 and goal > 0.9995:
            # Nothing to trim and nothing to slide: hand the frame back exactly
            # as the chain made it, rather than multiplying it by 1.0 (which
            # would round every sample for no reason).
            self._loudness_gain = 1.0
            return block
        gains = np.linspace(self._loudness_gain, goal, block.size,
                            dtype=np.float32)
        self._loudness_gain = float(goal)
        return block * gains

    #: How much of the mix one frame may move: the whole range in ten frames,
    #: so a press from fully dry to fully wet is a 200 ms fade, not a step.
    MIX_RATE = 0.1

    def _blend(self, dry, wet):
        """The mix fader: the pedal and the voice it was put on, blended.

        A mix of 100 is the pedal alone and is handed back exactly as the chain
        made it -- byte for byte, like every other untouched path here. A mix of
        0 is the voice alone. Between the two the ear hears the effect *beside*
        the voice instead of over it, which is what makes a fuzz or a radio
        usable at speaking volume, and it is the one control a preset cannot
        have been designed with: it depends on the voice it is put on.

        The fade is per frame rather than per press because the setting is read
        on the capture thread: a slider that moved 40 percent between two
        frames would otherwise be a step the size of the difference.
        """
        target = tone.mix(self.preset) / 100.0
        if abs(target - self._mix) < 1e-6:
            if self._mix >= 1.0 - 1e-6:
                return wet
            if self._mix <= 1e-6:
                return dry
            return dry * (1.0 - self._mix) + wet * self._mix
        step = max(-self.MIX_RATE, min(self.MIX_RATE, target - self._mix))
        goal = self._mix + step
        ramp = np.linspace(self._mix, goal, wet.size, endpoint=False,
                           dtype=np.float32)
        self._mix = goal
        return dry * (1.0 - ramp) + wet * ramp

    # ------------------------------------------------------------------ the frame
    def process(self, pcm):
        """Shape one frame; the same number of bytes comes back."""
        block = np.frombuffer(bytes(pcm), dtype="<i2").astype(np.float32) / 32768.0
        if block.size <= 0:
            return bytes(pcm)
        # The frame as it arrived: the dry half of the mix fader, kept before
        # the preset's own high-pass so that "dry" is the voice the rest of
        # the path would have sent, not the preset with its corner removed.
        dry = block
        block = self.pre.process(block)
        block = self._gate(block)
        block = self._shape_block(block)
        block = self._crush_block(block)
        block = self._octave_block(block)
        block = self.dc.process(block)
        block = self.post.process(block)
        block = self._air_block(block)
        block = self._formant_block(block)
        block = self._sag_block(block)
        block = self._vocoder_block(block)
        block = self._ott_block(block)
        block = self._chorus_block(block)
        block = self._tremolo_block(block)
        block = self._wah_block(block)
        block = self._phaser_block(block)
        block = self._echo_block(block)
        block = self._pan_delay_block(block)
        block = self._dark_echo_block(block)
        block = self._gated_block(block)
        block = self._cluster_block(block)
        block = self._robot_block(block)
        block = self._flanger_block(block)
        block = self._vibrato_block(block)
        block = self._stutter_block(block)
        block = self._cave_block(block)
        block = self._alien_block(block)
        block = self._sub_octave_block(block)
        block = self._envelope_wah_block(block)
        block = self._gate_pedal_block(block)
        block = self._reverse_block(block)
        block = self._chorus3_block(block)
        block = self._vinyl_block(block)
        block = self._body_block(block)
        block = self._shimmer_block(block)
        block = self._whisper_block(block)
        block = self._growl_block(block)
        block = self._doubler_block(block)
        block = self._step_block(block)
        block = self._pump_block(block)
        block = self._cloud_block(block)
        block = self._swell_block(block)
        block = self._freeze_block(block)
        block = self._riser_block(block)
        block = self._tapestop_block(block)
        # The radio family's noise floor comes last of the blocks: a channel's
        # hiss is not something the compressor or the gate acts on.
        block = self._static_block(block)
        # The player's own four tone bands, after the preset has made its sound
        # and before the trim and the ceiling -- so a band pushed up is caught
        # by the ceiling like any other loud pedal, and a preset nobody has
        # equalised takes no new code path at all.
        if self._tone is not None:
            block = self._blend(dry, block)
            block = self._tone.process(block)
        # Unity below the knee and a hard asymptotic ceiling at PEAK_CEILING:
        # the encoder is handed a frame with room left for the effect sends the
        # PA and a cinema room put on top of it (see PEAK_CEILING).
        shaped = PEAK_CEILING * np.tanh(block * self._out / PEAK_CEILING)
        # ... and then the average, which is what a room actually takes (see
        # VOICE_LOUDNESS). The peak ceiling above stays the hard one.
        shaped = self._loudness_trim(shaped)
        if self._tone is not None:
            # The meter reads what actually leaves -- after the ceiling and
            # the trim -- because that is the frame a listener gets, and a
            # fader is set against what leaves, not against the chain.
            level = (float(np.sqrt(np.mean(np.square(shaped))))
                     if shaped.size else 0.0)
            peak = float(np.max(np.abs(shaped))) if shaped.size else 0.0
            tone.report(self.preset, level, peak,
                        reduction=self._tone.compression())
        return (np.clip(shaped, -1.0, 1.0) * 32767.0).round().astype(
            "<i2").tobytes()


def _board_revision(effect_id):
    """A board's change count, or 0 for a fixed pedal.

    Imported inside the call: a board's module reads the options file, and a
    voice path that never chooses it should not carry it at import time.
    """
    if effect_id == CUSTOM_EFFECT:
        from . import custom
        return custom.revision()
    if effect_id == BUILDER_EFFECT:
        from . import builder, pan
        count = builder.revision()
        # The board's Pan Delay slot has a `Custom` choice that plays whatever
        # the Pan Delay page holds, so while the slot is ON it, that page's
        # changes have to rebuild the board -- a typed delay must land on the
        # next frame there too. With the slot on Off or a named setting the two
        # counts are independent again.
        if builder.uses_pan_page():
            count += pan.revision()
        return count
    if effect_id == VOX_LAB_EFFECT:
        from . import vocoder
        return vocoder.revision()
    if effect_id == PAN_EFFECT:
        from . import pan
        return pan.revision()
    return 0


def _crossfade(previous, fresh):
    """Blend the previous frame's output into this one, sample by sample.

    A preset change lands between two frames, and the two sides of it are not
    the same waveform: switching hard is a step, and a step is a click. The
    blend is one frame long (20 ms), which is short enough to be heard as a
    change of sound rather than as a slide.
    """
    if not previous or len(previous) != len(fresh):
        return fresh
    old = np.frombuffer(previous, dtype="<i2").astype(np.float32)
    new = np.frombuffer(fresh, dtype="<i2").astype(np.float32)
    blend = np.linspace(0.0, 1.0, old.size, endpoint=False, dtype=np.float32)
    mixed = old * (1.0 - blend) + new * blend
    return np.clip(mixed, -32768.0, 32767.0).round().astype("<i2").tobytes()


class VoiceEffectLine:
    """The effect on top of whatever line the press already had.

    Wrapping the line -- instead of another line beside it -- is what keeps the
    promises the voice path already makes: one frame in is one frame out, the
    release still hands the inner line's look-ahead back, and Auto-Tune, Harmony
    and the speech key do not have to know this exists. `inner` is None for a
    press that has no other line; the effect is then the whole line.
    """

    def __init__(self, inner=None):
        self.inner = inner
        self._lock = threading.Lock()
        self._processor = None
        self._preset = DEFAULT_EFFECT
        self._revision = 0
        self._last = b""

    @property
    def semitones(self):
        return self.inner.semitones if self.inner is not None else 0

    def motion(self):
        """The frame just shaped, as a place in the listener's space.

        `(right, forward)` in the range -1..1, or `(0, 0)` for every preset that
        has no movement. The voice path reads this beside the shaped frame and
        sends it on `CHANNEL_VOICE_MOTION`; nothing else in this module needs
        it, and a line with no processor answers the centre.
        """
        with self._lock:
            processor = self._processor
            if processor is None:
                return 0.0, 0.0
            return processor.motion()

    def set_semitones(self, semitones):
        if self.inner is not None:
            return self.inner.set_semitones(semitones)
        return semitones

    def shape(self, pcm):
        """Shape one frame through the inner line, then through the preset."""
        with self._lock:
            data = self.inner.shape(pcm) if self.inner is not None else bytes(pcm)
            wanted = effect_id()
            if wanted == DEFAULT_EFFECT:
                if self._processor is None:
                    self._last = data
                    return data
                # Fade the processed voice back into the dry one rather than
                # cutting the pedal: the last processed frame is the other half
                # of the crossfade.
                out = _crossfade(self._last, data)
                self._processor = None
                self._preset = DEFAULT_EFFECT
                self._revision = 0
                self._last = out
                return out
            # The Custom Amp's knobs are read when its processor is built, so a
            # board that moved needs a fresh one -- crossfaded in, exactly like
            # a preset change, because two gains of the same waveform are still
            # a step at the frame boundary.
            revision = _board_revision(wanted)
            if (self._processor is None or wanted != self._preset
                    or revision != self._revision):
                switching = self._processor is not None
                self._processor = _EffectProcessor(wanted, with_tone=True)
                out = self._processor.process(data)
                if switching:
                    out = _crossfade(self._last, out)
                self._preset = wanted
                self._revision = revision
                self._last = out
                return out
            out = self._processor.process(data)
            self._last = out
            return out

    def tail_frames(self, frame_bytes=FRAME_BYTES):
        """The inner line's look-ahead, with the effect already on it."""
        frames = (list(self.inner.tail_frames(frame_bytes))
                  if self.inner is not None else [])
        if not frames:
            return []
        with self._lock:
            wanted = effect_id()
            if wanted == DEFAULT_EFFECT:
                return frames
            processor = self._processor
            revision = _board_revision(wanted)
            if (processor is None or wanted != self._preset
                    or revision != self._revision):
                processor = _EffectProcessor(wanted, with_tone=True)
            return [processor.process(frame) for frame in frames]
