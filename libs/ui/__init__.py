"""The parts of this client a player can *see*.

Nothing here owns game state. A module in this package reads what the game is
already doing -- the open menu and its focused line, for now -- and draws it on
the pygame window the client has always opened (`beyond_tournament.py`), which
until now only ever showed the loading text.

The rule that keeps it safe: a visual module may not be required by the audio
game. Every one of these is optional, silent, and no-ops when it has no screen
(a test, a headless run), so the client that blind players use keeps working
byte for byte if this package is removed.

`menu_view` is the first one; see its docstring for why one renderer covers
every menu in the game.
"""
