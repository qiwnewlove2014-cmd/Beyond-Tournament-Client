"""The menu pages that moved out of `libs/music_bot/menus.py`.

One module per group of pages -- `settings.py` (the Settings hub, output
device, equalizer and the Custom EQ slider, song key and speech key),
`autotune.py` (Auto-Tune and Harmony), `subtitles.py`, `voice_effect.py`,
`party.py`, `recording.py` and `library.py` -- each a plain mixin class that
`MusicBotMenus` inherits. `support.py` holds `stand_on`, the one helper both
layers use. The state still lives on `MapMusicBot`: these classes hold the
builders only, and name the bot `self._bot.` on every line.
"""
