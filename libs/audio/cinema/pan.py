"""Staff panning: sending one player's voice -- and their band -- somewhere else.

A room already answers "which speakers carry this sound": a talker standing in
a cabinet's reach is heard from that room's speakers, a band plays at the room
nearest the performer, and both are shaped by the room's own numbers (level,
distance ramp, aim, wall, trim). What a room cannot express is *a person's*
decision: "put Somchai's voice out of the bar instead", "push that band to the
left of the hall". That is what this module holds.

Two things live here and nothing else:

    * the **destination vocabulary** (which cabinet, and which way the sound
      leans inside it) and the **direction law** that turns a direction into a
      per-speaker multiplier -- pure arithmetic, no game, no engine, so the
      band and the voice can be given identical numbers and a test can pin the
      law without an audio device;
    * the **table** of pans this client believes in, indexed under *both* names
      a player is known by: their name (the instrument path sends ``peer_id``
      as the player's name) and their voice channel (the megaphone path, and
      the Server's ``music_bot_cinema`` relay, key by voice channel).

Design rules this feature is built on:

    * **The Server owns the decision.** A pan is a performance decision, so it
      is made once, by staff, and relayed to the whole map: each listener
      resolves the destination from its *own* map, the same way the room
      routing already works. A pan kept only on the panning client's machine
      would be a feature that only its author can hear.
    * **Staff choose which room, never whether you hear one.** A listener's
      ``cinema_speech`` / ``cinema_live_instruments`` choice is theirs and is
      asked first (``live.note_reaches_a_room``, ``speech.room_target``): a
      panned voice plays through the room for the people who asked for rooms,
      and for somebody who chose the map's PA it stays on the PA -- where it
      still reaches them, so nothing is lost. **One switch per shape of sound**
      and nothing more (``cinema_speakers`` is the jukebox's songs, so it never
      takes a band or a voice with it), because the listening machine is the
      one that decides and a hidden gate there is indistinguishable from a
      room that would not resolve. What a pan *does* outrank is the rule that a
      talker or a band has to be standing inside the destination's own reach
      (``ROOM_RADIUS``): that is what "move somebody's sound to a cabinet" has
      to mean, and it is the one reason a pan exists.
    * **A pan is a route, not a volume.** The destination has to be a room the
      map actually has: a cabinet that was deleted, or one the map set to
      ``off``, resolves to nothing and the voice falls back to the map's PA --
      the shipped behaviour -- rather than to an invented ring of speakers.
    * **It stays until it is cleared**, and it is attached to the *player*
      rather than to a room: the same person keeps their destination while
      they walk around, which is the whole point of panning them.
"""

from math import sqrt

from ...deferred_log import log_deferred as log_line

# Where a panned source can be sent *inside* the room it plays through.
# ``auto`` is the room's own balance: the shipped behaviour, and what clearing
# a pan restores. The rest name a side of the room, in the same vocabulary the
# profiles use, so "left" means the left wall's speakers as the room defines
# them and not a listener-relative guess that flips when someone turns around.
DIRECTIONS = ("auto", "front", "back", "left", "right", "centre")
DEFAULT_DIRECTION = "auto"

# The vocabulary as a player reads it, in the order a reader wants it. One table
# for the picker *and* for anything that says where a sound went: two tables
# would eventually disagree about what "left" is called, and a menu that names a
# direction differently from the sentence confirming it reads like two features.
DIRECTION_LABELS = (
    ("auto", "As the room is mixed"),
    ("front", "Towards the screen"),
    ("back", "Towards the back of the room"),
    ("left", "Towards the left of the room"),
    ("right", "Towards the right of the room"),
    ("centre", "Towards the centre of the room"),
)

# A direction is a *move*, not a death: the far side is cut hard but never to
# nothing, and the near side is lifted, so a pan is heard as the sound going
# somewhere instead of as a speaker being unplugged. The overall loudness is
# then restored by ``apply_direction`` (equal energy), because a pan that also
# changes how loud the band is would be a fader with a side effect.
BOOST = 1.6
CUT = 0.35
CENTRE_FACTOR = 0.7


def normalize_direction(value):
    """The direction a packet meant, or the default when it named no side."""
    if isinstance(value, str):
        text = value.strip().lower()
        if text in DIRECTIONS:
            return text
    return DEFAULT_DIRECTION


def is_supported(value):
    """Whether a string is a direction this module accepts (Server-side gate)."""
    return isinstance(value, str) and value.strip().lower() in DIRECTIONS


def slot_side(slot):
    """``'l'``/``'r'`` for a wall pair, ``'c'`` for anything centred.

    Read off the slot name, which is the room's own vocabulary
    (``layout.SLOT_ORDER``): a ``side_l`` speaker stands on the room's left
    because that is what the map called it, and ``placement`` has already
    overruled any label that disagreed with where the speaker stands.
    """
    name = str(slot)
    if name.endswith("_l"):
        return "l"
    if name.endswith("_r"):
        return "r"
    return "c"


def slot_depth(slot):
    """``'front'``/``'mid'``/``'rear'`` -- which way along the room it stands."""
    name = str(slot)
    if name.startswith("front"):
        return "front"
    if name.startswith("rear"):
        return "rear"
    if name.startswith("side"):
        return "mid"
    return "centre"


def direction_factor(slot, direction, boost=BOOST, cut=CUT):
    """How much a direction leans this speaker, as a multiplier.

    ``auto`` is exactly ``1.0`` for every slot, which is what keeps an unpanned
    room byte-identical to the shipped one: this function is only ever reached
    with a real direction.
    """
    direction = normalize_direction(direction)
    if direction == DEFAULT_DIRECTION:
        return 1.0
    side = slot_side(slot)
    depth = slot_depth(slot)
    if direction == "left":
        return {"l": boost, "r": cut, "c": CENTRE_FACTOR}[side]
    if direction == "right":
        return {"r": boost, "l": cut, "c": CENTRE_FACTOR}[side]
    if direction == "front":
        return {"front": boost, "rear": cut, "mid": CENTRE_FACTOR,
                "centre": boost}[depth]
    if direction == "back":
        return {"rear": boost, "front": cut, "mid": CENTRE_FACTOR,
                "centre": boost}[depth]
    # "centre": everything is pulled towards the middle of the room, and the
    # speaker that already stands there is lifted instead of merely surviving.
    return boost if side == "c" else CENTRE_FACTOR


def apply_direction(terms, direction):
    """The same terms, leaned towards ``direction``, at the same loudness.

    ``terms`` are the room's own ``(slot, spot, gain, ...)`` tuples (the shape
    ``LiveRoomRouter.terms_for_plan`` produces for both a voice and a note), so
    every field after the gain -- a trim, a wall tier, a channel half -- is
    carried through untouched: a pan must not move a speaker's alignment or
    take away the half of a stereo sample it plays.

    The energy of the room is preserved (each gain is scaled so the sum of
    squares matches the unpanned room), which is what makes a pan audible as a
    *place* rather than as a volume change.
    """
    direction = normalize_direction(direction)
    if direction == DEFAULT_DIRECTION or not terms:
        return tuple(terms)
    factors = tuple(direction_factor(term[0], direction) for term in terms)
    if len(set(factors)) == 1:
        # Every speaker is moved by the same amount -- a room with one speaker,
        # or a direction none of this room's slots lean towards. There is
        # nothing to pan, and scaling would only add float drift to gains that
        # are already the ones the room decided on.
        return tuple(terms)
    before = sum(float(term[2]) ** 2 for term in terms)
    after = sum((float(term[2]) * factor) ** 2
                for term, factor in zip(terms, factors))
    scale = sqrt(before / after) if before > 0.0 and after > 0.0 else 1.0
    return tuple((term[0], term[1], float(term[2]) * factor * scale)
                 + tuple(term[3:])
                 for term, factor in zip(terms, factors))


def _channel_key(channel):
    """A voice channel as a table index, or None when it is not a number."""
    if channel is None or isinstance(channel, bool):
        return None
    try:
        return int(channel)
    except (TypeError, ValueError):
        return None


class PanTable:
    """Every staff pan this client knows about, reachable by either name.

    An entry is ``(name, channel, cabinet, direction)``. A player is named by
    their *name* on the instrument path (the Server sends ``peer_id`` as the
    player's name) and by their *voice channel* on the voice path, so both keys
    point at the same entry -- one lookup each, and neither path has to know
    how the other one names people.
    """

    __slots__ = ("_by_name", "_by_channel", "version")

    def __init__(self):
        self._by_name = {}
        self._by_channel = {}
        # Bumped on every accepted change, so a caller that caches a decision
        # about a person (the voice leg follows a room at most once a second)
        # can tell a new pan from the one it already answered.
        self.version = 0

    def set(self, name, channel, cabinet, direction=DEFAULT_DIRECTION):
        """Record a pan. An empty cabinet *clears* it (the Server's protocol)."""
        name = str(name or "").strip()
        cabinet = str(cabinet or "").strip()
        if not name or not cabinet:
            self.clear(name)
            return None
        entry = (name, _channel_key(channel), cabinet,
                 normalize_direction(direction))
        self._by_name[name] = entry
        if entry[1] is not None:
            self._by_channel[entry[1]] = name
        self.version += 1
        return entry

    def clear(self, name):
        """Forget one person's pan (by name). Returns whether there was one."""
        name = str(name or "").strip()
        entry = self._by_name.pop(name, None)
        if entry is None:
            return False
        if entry[1] is not None and self._by_channel.get(entry[1]) == name:
            self._by_channel.pop(entry[1], None)
        self.version += 1
        return True

    def clear_all(self):
        """Forget every pan (a relogin, a map that took them away)."""
        if not self._by_name:
            return False
        self._by_name.clear()
        self._by_channel.clear()
        self.version += 1
        return True

    def for_name(self, name):
        return self._by_name.get(str(name or "").strip())

    def for_channel(self, channel):
        name = self.name_for_channel(channel)
        return None if name is None else self._by_name.get(name)

    def name_for_channel(self, channel):
        return self._by_channel.get(_channel_key(channel))

    def entries(self):
        """Every pan, in name order, so two clients list them the same way."""
        return tuple(self._by_name[name] for name in sorted(self._by_name))

    def __len__(self):
        return len(self._by_name)


def table_for(gameplay, create=True):
    """This map session's pan table, built on first use.

    It lives on the gameplay object so it dies with the map, exactly like the
    things it points at (a cabined id means nothing on the next map), and it is
    created lazily because a client that never gets a pan never owns one.
    """
    if gameplay is None:
        return None
    table = getattr(gameplay, "cinema_pans", None)
    if isinstance(table, PanTable):
        return table
    if not create:
        return None
    table = PanTable()
    try:
        gameplay.cinema_pans = table
    except Exception:
        return None
    return table


def target_for_name(gameplay, name):
    """``(cabinet, direction)`` for a player, or None -- the note path's key."""
    table = table_for(gameplay, create=False)
    if table is None:
        return None
    entry = table.for_name(name)
    return None if entry is None else (entry[2], entry[3])


def target_for_channel(gameplay, channel):
    """``(cabinet, direction)`` for a talker, or None -- the voice path's key."""
    table = table_for(gameplay, create=False)
    if table is None:
        return None
    entry = table.for_channel(channel)
    return None if entry is None else (entry[2], entry[3])


def direction_for_channel(gameplay, channel):
    """The direction a talker was panned to, or the default (``auto``)."""
    target = target_for_channel(gameplay, channel)
    return DEFAULT_DIRECTION if target is None else target[1]


def panned(gameplay, channel):
    """Whether this talker is one a staff member moved."""
    return target_for_channel(gameplay, channel) is not None


def direction_phrase(direction):
    """The words a player hears for a direction, mid-sentence.

    ``"auto"`` -- the room's own mix, and what clearing a pan restores -- is the
    absence of a lean, so it has no phrase: a caller says nothing about
    direction rather than inventing one.
    """
    value = normalize_direction(direction)
    if value == DEFAULT_DIRECTION:
        return ""
    for candidate, label in DIRECTION_LABELS:
        if candidate == value:
            return label[0].lower() + label[1:]
    return ""


# ------------------------------------------------------------------ own sound
# The other half of a pan: the player it moved. Staff own the decision and the
# Server relays it, so what an owner can have is *sight* of it -- where other
# players now hear them -- and never a veto (a pan is a performance decision,
# not a setting on somebody else's machine). These helpers read that half out of
# the same table the routing reads, so the answer a player is told and the
# answer their voice follows cannot disagree.
def own_name(gameplay):
    """This client's own player name, or ``''`` when it is not known yet."""
    return str(getattr(getattr(gameplay, "player", None), "name", "") or "").strip()


def own_channel(gameplay):
    """This client's own voice channel, or None when the Server has not said.

    A player's own name never reaches their own client through a spawn packet
    (``Map.add_player`` tells a joiner about everybody *except* them), so the
    login snapshot carries the channel instead -- ``gameplay.own_voice_channel``
    -- and that is the key a self-pan travels under.
    """
    return _channel_key(getattr(gameplay, "own_voice_channel", None))


def moves_me(gameplay, entry):
    """Whether one entry of the table is *this* client's own sound."""
    if entry is None:
        return False
    name = own_name(gameplay)
    if name and str(entry[0]).strip().lower() == name.lower():
        return True
    channel = own_channel(gameplay)
    return channel is not None and entry[1] == channel


def own_entry(gameplay):
    """The pan that moves this client's own sound, or None.

    Found by name first and then by channel, because a pan reaches the table
    under whichever name its path uses: the instrument path sends the player's
    name, the voice path their channel, and a packet that names nobody (a Server
    resolving the target itself) still has to be recognised by the player whose
    voice it moved. A client that knows neither is told nothing -- the
    alternative is telling the wrong player about somebody else's pan.
    """
    table = table_for(gameplay, create=False)
    if table is None:
        return None
    name = own_name(gameplay)
    if name:
        entry = table.for_name(name)
        if entry is not None:
            return entry
    channel = own_channel(gameplay)
    return None if channel is None else table.for_channel(channel)


def own_label(gameplay):
    """One line answering "where does my sound come out?", for a menu line."""
    entry = own_entry(gameplay)
    if entry is None:
        return "Your sound: where you stand (no staff pan)"
    phrase = direction_phrase(entry[3])
    return (f"Your sound: heard from jukebox {entry[2]}"
            + (f", {phrase}" if phrase else ""))


def _hear_sentence(entry):
    """The one sentence for "this is where other players hear you from".

    Built here, once, because three places read it out: the moment a pan lands,
    the line an owner can check by hand, and anything that later needs to say it
    again. Two copies of it would eventually disagree about the room, the side,
    or what the switches do.
    """
    phrase = direction_phrase(entry[3])
    return (f"Other players now hear your voice from jukebox {entry[2]}"
            + (f", {phrase}" if phrase else "")
            + ". What you hear is up to your own listening switches.")


def own_report(gameplay):
    """Where other players hear this client's voice, or None when nobody moved it."""
    entry = own_entry(gameplay)
    return None if entry is None else _hear_sentence(entry)


def own_notice(gameplay, entry, previous=None):
    """What to say to the player whose own voice just moved (None = nothing).

    Two questions are kept apart on purpose, because running them together is
    how "the switches are off but I can still hear it" happened: **other
    players** now hear this voice from another room -- that is all a pan is --
    while what the owner hears is their own ``Cinema rooms``/``Instruments``/
    ``Speech`` switches, which a pan never touches.

    ``previous`` is the pan this client believed in before the packet arrived,
    and it is what makes an *identical* pan silent: a map hands its pans over
    again to a joiner (and a Server may repeat one), and being read the same
    sentence on every arrival is how a real change stops being noticed.
    """
    if entry == previous:
        return None
    if entry is not None and moves_me(gameplay, entry):
        return _hear_sentence(entry)
    if previous is not None and moves_me(gameplay, previous):
        return ("Your voice is no longer moved by staff: other players hear it "
                "from where you stand.")
    return None


def apply_packet(gameplay, data):
    """Apply one ``staff_pan`` from the Server. Returns the entry it set.

    ``None`` means the packet cleared a pan (or was not usable): the caller
    speaks it either way, so a staff member hears their own command land.

    Every client also *writes down what it will do with it*
    (:func:`_report_landing`), because that is the half a tester cannot see:
    the pan reaches every machine, and each one answers it with its own
    listening switches -- so with two clients open, the pan lands on both and
    can be heard on neither, while a switch that is off, a listener standing
    out of the destination's reach and an old build all look exactly alike.
    """
    if gameplay is None or not isinstance(data, dict):
        return None
    channel = data.get("channel")
    name = str(data.get("name") or "").strip()
    table = table_for(gameplay)
    if table is None:
        return None
    if not name:
        name = str(table.name_for_channel(channel) or "").strip()
    if not name:
        return None
    cabinet = str(data.get("cabinet") or "").strip()
    if not cabinet:
        table.clear(name)
        _report_landing(None, name)
        return None
    entry = table.set(name, channel, cabinet, data.get("direction"))
    _report_landing(entry)
    return entry


def _report_landing(entry, name=None):
    """One line saying what the pan that just arrived means *on this machine*.

    The routing is decided per listener, so the interesting half of a relayed
    pan is this client's own answer: which switches are on here, spelled out.
    A pan is a deliberate act by staff, so a line per pan is not a flood -- and
    it is the only trace a panned voice ever leaves (a note at least reports
    its own reason when a room carries it and this listener cannot hear it).
    """
    from . import plugin as cinema_plugin
    if entry is None:
        where = f"{name} back to their own room"
    else:
        where = f"{entry[0]} -> jukebox {entry[2]} ({entry[3]})"
    try:
        here = cinema_plugin.listening_summary()
    except Exception:
        here = "listening switches unavailable"
    log_line(f"[Cinema] pan: {where} | this client hears: {here}")


def describe(gameplay):
    """One line per pan, for a staff menu and for a log."""
    table = table_for(gameplay, create=False)
    if table is None:
        return []
    return [f"{name} -> jukebox {cabinet}"
            + ("" if direction == DEFAULT_DIRECTION else f" ({direction})")
            for name, _channel, cabinet, direction in table.entries()]
