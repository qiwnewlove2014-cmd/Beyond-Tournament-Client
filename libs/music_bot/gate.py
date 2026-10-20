"""The Server's Music Bot feature gate, as this Client sees it.

One catalogue, one answer
------------------------
The feature list is not written here. `server/libs/music_bot_gate.ts` declares
every lockable part of the Music Bot and stores which of them a Developer has
locked; the Server pushes the locked ids to every client inside the
`staff_permissions` snapshot, under `music_bot_locked` (id -> the label to say).
This module only remembers that answer and answers two questions:

    gate.is_locked("seek")      locked, silently
    gate.blocked("seek")        locked, and says the refusal out loud (True)
    gate.allow("seek")          may be used (False when locked, silent)

so the guard at a use site is one line::

    if gate.blocked("seek"):
        return

**A lock is global.** It hides the feature from every player, the Developer who
set it included: it is a kill switch for a broken or abusable feature, not a
per-player permission (that is `can_use_music_bot` and the rest of the
snapshot, and this sits on top of it without ever widening it).

**A missing key is not an unlock.** A mid-session `staff_permissions` refresh
from a Server built before this gate carries no `music_bot_locked`; the last
known answer is kept then, exactly as the cinema keys are, so an unrelated rank
change cannot quietly switch a locked feature back on. The login snapshot is
the other way round -- a fresh session with no key means that Server locks
nothing -- because there is no last answer worth keeping across a login.

**Adding a feature needs no edit here either.** The catalogue row is the
Server's; the Client learns the id from the snapshot and the only thing to add
is the one guard where the feature is actually used, which is the same line
every other feature already has.

Pins: client/tests/test_music_bot_feature_gate.py.
Rules: .agents/skills/music_bot_integration/.
"""

try:  # pragma: no cover - speech is present in every real client
    from ..speech import speak
except Exception:  # pragma: no cover
    def speak(*_args, **_kwargs):
        return None


#: Locked feature id -> the label the Server said to use for it.
_locked: "dict[str, str]" = {}

#: What a feature is called when a Server sends an id with no label.
_DEFAULT_LABEL = "That Music Bot feature"


def apply_locked(value):
    """Replace the locked set with the Server's `music_bot_locked` map.

    Returns the ids that are locked now, so a caller can notice a feature that
    went away inside the session (a running stream, an open menu).
    """
    labels = {}
    if isinstance(value, dict):
        for key, label in value.items():
            if isinstance(key, str) and key:
                labels[key] = label if isinstance(label, str) and label else key
    _locked.clear()
    _locked.update(labels)
    return frozenset(labels)


def clear():
    """Forget every lock (login snapshot without the key, or a test)."""
    _locked.clear()


def locked_ids():
    """Every locked feature id, as the Server last said."""
    return frozenset(_locked)


def is_locked(feature_id):
    """Whether `feature_id` is locked right now."""
    return feature_id in _locked


def label(feature_id):
    """The name to say for `feature_id`, in the Server's words."""
    return _locked.get(feature_id) or _DEFAULT_LABEL


def allow(feature_id):
    """True when `feature_id` may be used. Never speaks."""
    return feature_id not in _locked


def blocked(feature_id):
    """True when `feature_id` is locked -- and says the refusal.

    The speaking half of `allow`: a key that answers with silence reads as a
    broken key, so a locked feature always says whose decision it was.
    """
    if feature_id not in _locked:
        return False
    speak(f"{label(feature_id)} is locked by staff right now.")
    return True


def filter_items(items):
    """Menu lines minus the ones whose feature is locked.

    Each entry is ``(feature_id, label, callback)`` -- the feature id sits
    beside the line it guards, which is the whole of a menu's part in this:
    a locked feature's line does not exist, so it cannot be chosen. A line that
    belongs to no single feature (Back, the Settings hub, Help) names ``None``
    and is never hidden. Returns the ordinary ``(label, callback)`` pairs a
    Menu is built from.
    """
    return [(item[1], item[2]) for item in items if item[0] not in _locked]
