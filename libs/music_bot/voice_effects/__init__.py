"""The broadcast voice's processors: one folder, one feature.

These were eight loose modules in `libs/music_bot/`, sharing a directory with
the downloader, the subtitles and the transpose dial but not a subject. They
belong together:

    dsp.py       the processors, the preset tables and `VoiceEffectLine`
    catalogue.py the pedal names and the text Tab reads
    tone.py      the four tone bands any one preset can be played through
    builder.py   Custom Voice, the standalone knob board
    custom.py    Custom Amp, the board inside Amp & Drive
    vocoder.py   Vocoder Lab, the carrier board
    pan.py       Pan Delay, the pedal whose repeat crosses the space
    recipes.py   fifty ready-made Custom Voice layouts
    boards.py    the table every knob board is read through

`dsp` is re-exported from here, so nothing outside had to learn the new
layout: `from libs.music_bot import voice_effects` then
`voice_effects.effect_label(...)` is unchanged, including the internals the
tests read (`_SPECS`, `_EffectProcessor`). New code may import the module it
means -- `from libs.music_bot.voice_effects import catalogue`.

Rules, the chain and every trap: .agents/skills/music_bot_integration/.
"""

from . import dsp
from .dsp import *          # noqa: F401,F403  -- the surface the old module had


def __getattr__(name):
    """Answer for whatever the old `voice_effects` module had.

    `import *` skips the underscore names, and `_SPECS`, `_EffectProcessor`
    and `_board_revision` are exactly what the tests and the catalogue check
    against, so a miss falls through to `dsp`.
    """
    return getattr(dsp, name)
