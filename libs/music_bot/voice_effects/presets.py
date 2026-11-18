"""The fixed voice-effect presets and the frame constants they share.

Data only: one row per pedal in `_SPECS`, read by `dsp.spec_for` (the three
boards build their own rows in custom.py / builder.py / vocoder.py). Kept apart
from the DSP so a new preset is a table edit, not a change to the processor.
"""

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

#: The step filter's pattern: which of the three bands is heard on each
#: sixteenth. Eight steps is a bar's worth of movement, not a melody.
STEP_PATTERN = (0, 0, 1, 2, 1, 0, 2, 1)

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

#: The one pedal a recording may wear on its own, without changing the voice
#: other players hear. It is a fixed shelf row, not a board: the recorder
#: asks for exactly this chain and gets a fresh processor for each take.
RECORDING_STUDIO_EFFECT = "rad_studio"
