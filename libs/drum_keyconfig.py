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
    DrumBinding("drum_snare_2", "Snare 2", 2, pygame.K_n),
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
    DrumBinding("drum_rim", "Rim", 17, pygame.K_m),
)

RESERVED_DRUM_KEYS = frozenset((
    pygame.K_ESCAPE,
    pygame.K_RETURN,
    pygame.K_KP_ENTER,
))


def binding_key(keyconfig, binding):
    return keyconfig.get(binding.function, binding.default_key)


def alternate_key(keyconfig, binding):
    return keyconfig.get(binding.alternate_function, None)


def assigned_slots(keyconfig):
    """Yield function, accessible label, key, and pad for each assigned slot."""
    for binding in DRUM_BINDINGS:
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


def key_to_pad(keyconfig):
    """Resolve primary and alternate keys to stable pad IDs."""
    result = {}
    for _function, _label, key_code, pad in assigned_slots(keyconfig):
        # Hand-edited duplicate files remain deterministic and cannot crash.
        result.setdefault(key_code, pad)
    return result


def validate_key(keyconfig, function, key_code):
    """Return an accessible error message or ``None`` when a bind is valid."""
    key_name = friendly_key_name(key_code)
    if key_code in RESERVED_DRUM_KEYS:
        return f"{key_name} is reserved for leaving drum mode. Choose another key."

    for assigned_function, label, assigned_key, _pad in assigned_slots(keyconfig):
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


def clear_all(keyconfig):
    """Explicitly unassign every drum slot so the player can rebind freely.

    A missing primary means "use its default", so clearing custom values was
    not enough: default keys stayed reserved and blocked every new assignment.
    Store ``None`` for primaries to distinguish an intentional clear from a
    first-run/default configuration.  Restore defaults replaces these markers.
    """
    for binding in DRUM_BINDINGS:
        keyconfig.set(None, binding.function, autosave=False)
        keyconfig.unset(binding.alternate_function, autosave=False)
    keyconfig.save()


def restore_defaults(keyconfig):
    """Restore primary defaults and clear every alternate with one write."""
    for binding in DRUM_BINDINGS:
        keyconfig.set(binding.default_key, binding.function, autosave=False)
        clear_alternate(keyconfig, binding, autosave=False)
    keyconfig.save()
