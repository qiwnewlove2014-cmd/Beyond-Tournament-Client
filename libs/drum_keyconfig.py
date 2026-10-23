"""Shared keyboard-binding registry for the playable drumset."""

from dataclasses import dataclass

import pygame

from .string_utils import friendly_key_name


@dataclass(frozen=True, slots=True)
class DrumBinding:
    function: str
    label: str
    pad: int
    default_key: int

    @property
    def alternate_function(self):
        return f"{self.function}_alt"


DRUM_BINDINGS = (
    DrumBinding("drum_kick", "Kick", 0, pygame.K_SPACE),
    DrumBinding("drum_snare", "Snare", 1, pygame.K_f),
    DrumBinding("drum_snare_2", "Snare 2", 2, pygame.K_g),
    DrumBinding("drum_closed_hihat", "Closed Hi-Hat", 3, pygame.K_j),
    DrumBinding("drum_open_hihat", "Open Hi-Hat", 4, pygame.K_k),
    DrumBinding("drum_foot_hihat", "Foot Hi-Hat", 5, pygame.K_l),
    DrumBinding("drum_tom_1", "Tom 1", 6, pygame.K_q),
    DrumBinding("drum_tom_2", "Tom 2", 7, pygame.K_w),
    DrumBinding("drum_tom_3", "Tom 3", 8, pygame.K_e),
    DrumBinding("drum_tom_4", "Tom 4", 9, pygame.K_r),
    DrumBinding("drum_crash_left", "Crash Left", 10, pygame.K_z),
    DrumBinding("drum_crash_right", "Crash Right", 11, pygame.K_x),
    DrumBinding("drum_china", "China", 12, pygame.K_c),
    DrumBinding("drum_splash", "Splash", 13, pygame.K_v),
    DrumBinding("drum_ride", "Ride", 14, pygame.K_u),
    DrumBinding("drum_ride_bell", "Ride Bell", 15, pygame.K_i),
    DrumBinding("drum_cowbell", "Cowbell", 16, pygame.K_o),
    DrumBinding("drum_rim", "Rim", 17, pygame.K_d),
)

RESERVED_DRUM_KEYS = frozenset((
    pygame.K_ESCAPE,
    pygame.K_RETURN,
    pygame.K_KP_ENTER,
))

# Kits that bring their own keys. A kit with no entry here plays the drum kit's
# own 18 bindings, which is what every kit did before this table existed.
#
# The `per` kit is one hand drum's voices, not a drum kit, and it has four
# *keys* rather than eighteen: 1 for the treble stroke (the left-hand voice), 2
# for the slap in the middle, 3 for the bass stroke (the right-hand voice) and 4
# for the bell beside it -- the number row reads left to right in the same order
# the drum sits in the stereo field, with the bell last. They are stored under
# their own function names (`drum_per_lo`, ...) so rebinding one instrument never
# rebinds the other -- a player who moves the kick off Space has not moved the
# drum's bass stroke, and the conflict check is scoped to the kit that is being
# edited for the same reason.
KIT_BINDINGS = {
    "per": (
        DrumBinding("drum_per_lo", "Per lo", 0, pygame.K_3),
        DrumBinding("drum_per_hi", "Per Hi", 1, pygame.K_1),
        DrumBinding("drum_per_p", "Per P", 17, pygame.K_2),
        DrumBinding("drum_per_cabell", "Cabell", 16, pygame.K_4),
    ),
    # The tuned per drum: the five notes of A minor pentatonic on 1-5, with the
    # untuned bass stroke and slap kept on 6 and 7 as rhythm pieces. The notes are
    # baked files (tools/percussion_tune_bake.py), so a key is a *note* here, not a
    # transposition: A3 C4 D4 E4 G4 reads left to right on the number row.
    "per_tuned": (
        DrumBinding("drum_per_tuned_a3", "A3", 0, pygame.K_1),
        DrumBinding("drum_per_tuned_c4", "C4", 1, pygame.K_2),
        DrumBinding("drum_per_tuned_d4", "D4", 2, pygame.K_3),
        DrumBinding("drum_per_tuned_e4", "E4", 3, pygame.K_4),
        DrumBinding("drum_per_tuned_g4", "G4", 4, pygame.K_5),
        DrumBinding("drum_per_tuned_lo", "Per lo", 5, pygame.K_6),
        DrumBinding("drum_per_tuned_p", "Per slap", 6, pygame.K_7),
    ),
}

# The kit's name where the menu reads it, so the menu does not import the audio
# module just to say a word. The Server names the same ids in chat and in the
# Builder (`DRUM_KIT_LABELS`); the two tables must agree on every id.
KIT_LABELS = {"per": "Per", "per_tuned": "Per tuned"}


def bindings(kit=None):
    """The key table one kit plays: its own if it has one, else the drum kit's."""
    return KIT_BINDINGS.get(kit, DRUM_BINDINGS)


def binding_groups():
    """Yield ``(kit, label, bindings)`` for the menu, the drum kit first.

    A label of ``None`` means "the drum kit's own keys" -- those rows keep the
    wording they always had, so nothing a player already knows is renamed.
    """
    yield None, None, DRUM_BINDINGS
    for kit, kit_bindings in KIT_BINDINGS.items():
        yield kit, KIT_LABELS.get(kit, kit), kit_bindings


def binding_key(keyconfig, binding):
    return keyconfig.get(binding.function, binding.default_key)


def alternate_key(keyconfig, binding):
    return keyconfig.get(binding.alternate_function, None)


def assigned_slots(keyconfig, kit=None):
    """Yield function, accessible label, key, and pad for each assigned slot."""
    for binding in bindings(kit):
        primary = binding_key(keyconfig, binding)
        if primary is not None:
            yield (
                binding.function,
                f"{binding.label} primary",
                primary,
                binding.pad,
            )
        alternate = alternate_key(keyconfig, binding)
        if alternate is not None:
            yield (
                binding.alternate_function,
                f"{binding.label} alternate",
                alternate,
                binding.pad,
            )


def key_to_pad(keyconfig, kit=None):
    """Resolve primary and alternate keys to stable pad IDs.

    ``kit`` is the kit the local performer is playing: a kit that brought its
    own keys resolves those, and every other kit keeps the drum kit's table.
    """
    result = {}
    for _function, _label, key_code, pad in assigned_slots(keyconfig, kit):
        # Hand-edited duplicate files remain deterministic and cannot crash.
        result.setdefault(key_code, pad)
    return result


def validate_key(keyconfig, function, key_code, kit=None):
    """Return an accessible error message or ``None`` when a bind is valid.

    Only the edited kit's own slots are checked: the drum kit and the ``per``
    kit are never active together, and their defaults deliberately share keys
    (Space is the kick and the drum's bass stroke), so checking across kits
    would refuse the very default the player is allowed to keep.
    """
    key_name = friendly_key_name(key_code)
    if key_code in RESERVED_DRUM_KEYS:
        return f"{key_name} is reserved for leaving drum mode. Choose another key."

    for assigned_function, label, assigned_key, _pad in assigned_slots(keyconfig, kit):
        if assigned_function == function:
            continue
        if assigned_key == key_code:
            return (
                f"{key_name} is already assigned to {label}. "
                "Choose another key."
            )
    return None


def clear_alternate(keyconfig, binding, autosave=True):
    keyconfig.unset(binding.alternate_function, autosave=autosave)


def clear_all(keyconfig, kit=None):
    """Explicitly unassign drum slots so the player can rebind freely.

    A missing primary means "use its default", so clearing custom values was
    not enough: default keys stayed reserved and blocked every new assignment.
    Store ``None`` for primaries to distinguish an intentional clear from a
    first-run/default configuration.  Restore defaults replaces these markers.

    With no ``kit`` every table is cleared (the menu's "Clear drum keys" means
    all of them) in one file write; with a ``kit`` only that kit's slots go.
    """
    if kit is None:
        groups = [binding for _kit, _label, table in binding_groups()
                  for binding in table]
    else:
        groups = bindings(kit)
    for binding in groups:
        keyconfig.set(None, binding.function, autosave=False)
        keyconfig.unset(binding.alternate_function, autosave=False)
    keyconfig.save()


def restore_defaults(keyconfig, kit=None):
    """Restore primary defaults and clear every alternate with one write."""
    if kit is None:
        groups = [binding for _kit, _label, table in binding_groups()
                  for binding in table]
    else:
        groups = bindings(kit)
    for binding in groups:
        keyconfig.set(binding.default_key, binding.function, autosave=False)
        clear_alternate(keyconfig, binding, autosave=False)
    keyconfig.save()
