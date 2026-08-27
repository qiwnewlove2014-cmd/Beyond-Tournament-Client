"""Spoken target bearings relative to the player's horizontal facing.

Only wording lives here: movement, target selection and beacon audio are unchanged.
The game faces +Y at zero degrees and turns toward +X as the angle increases.
"""

import math


def describe_tracking_direction(dx, dy, dz, facing):
    """Describe a displacement using mirrored, deterministic angular bands."""
    if math.hypot(dx, dy) <= 1e-7:
        if dz > 0:
            return "directly above you"
        if dz < 0:
            return "directly below you"
        return "right here"

    relative = (math.degrees(math.atan2(dx, dy)) - facing + 180) % 360 - 180
    # Remove floating-point noise, not meaningful off-axis angles. In particular,
    # even a one-degree offset must not be called straight in front/behind.
    angle = round(abs(relative), 7)
    side = "right" if relative > 0 else "left"
    in_front = angle < 90
    off_axis = angle if in_front else 180 - angle
    position = "in front" if in_front else "behind"

    if angle == 90:
        direction = f"straight off to the {side}"
    elif off_axis == 0:
        direction = f"straight {position}"
    elif off_axis < 15:
        if in_front:
            direction = f"in front and slightly off to the {side}"
        else:
            direction = f"behind and slightly to the {side}"
    elif off_axis < 30:
        if in_front:
            direction = f"in front a little ways off to the {side}"
        else:
            direction = f"behind and a little ways off to the {side}"
    elif off_axis < 60:
        direction = f"{position} and a fair distance off to the {side}"
    else:
        direction = f"slightly {position} and a fair distance off to the {side}"

    # Retain the existing height threshold for targets that are also offset
    # horizontally. A directly vertical target has no horizontal bearing.
    if dz > 2:
        direction += " and above you"
    elif dz < -2:
        direction += " and below you"
    return direction
