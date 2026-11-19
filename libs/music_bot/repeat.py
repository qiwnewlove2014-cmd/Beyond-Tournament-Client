"""Repeat modes for the Music Bot's song advancement.

One small vocabulary shared by every place a song can move: the end of a
song, Next and Previous, the play-next queue, a playlist or local batch, and
the Personal Music Feed. Keeping the words here means the menu, the crossfade
pre-roll and the tests cannot describe three different repeat behaviours.
"""

REPEAT_OFF = "off"
REPEAT_ALL = "all"
REPEAT_ONE = "one"

#: The order the menu's one line cycles through. Off first: a bot nobody has
#: touched plays each song once, exactly as it always did.
REPEAT_MODES = (REPEAT_OFF, REPEAT_ALL, REPEAT_ONE)

_LABELS = {
    REPEAT_OFF: "Off",
    REPEAT_ALL: "All songs",
    REPEAT_ONE: "This song",
}

_SENTENCES = {
    REPEAT_OFF: "Repeat off. Each song plays once.",
    REPEAT_ALL: ("Repeat all songs. The queue, playlist, local batch and "
                 "feed start again at the end."),
    REPEAT_ONE: ("Repeat this song. It plays again when it ends, and Next "
                 "or Previous restart it."),
}


def normalize_mode(value):
    """A stored repeat mode, or Off when it is not one of ours."""
    return value if value in REPEAT_MODES else REPEAT_OFF


def next_mode(value):
    """The mode one press of the line moves to."""
    mode = normalize_mode(value)
    return REPEAT_MODES[(REPEAT_MODES.index(mode) + 1) % len(REPEAT_MODES)]


def label(value):
    """The picker's own words for a mode."""
    return _LABELS[normalize_mode(value)]


def sentence(value):
    """What the player hears after one press."""
    return _SENTENCES[normalize_mode(value)]
