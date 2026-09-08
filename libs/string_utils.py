import pygame

def direction(num):
    '''return's a string representation of {direction}'''
    val=int((num/22.5)+.5)
    arr=["North","NorthNorthEast","NorthEast","EastNorthEast","East","EastSouthEast", "SouthEast", "SouthSouthEast","South","SouthSouthWest","SouthWest","WestSouthWest","West","WestNorthWest","NorthWest","NorthNorthWest"]
    return arr[(val % 16)]


def clock_direction(num):
    '''return's a clock-face string for {num} degrees, nearest hour.

    0 degrees (North) is 12 o'clock, 90 (East) is 3 o'clock, 180 (South) is
    6 o'clock, 270 (West) is 9 o'clock. Blind players who navigate by clock
    positions ("the door is at 3 o'clock") can use this for turning and
    direction checks instead of raw degrees or 16-way compass names.
    '''
    hour = int((num / 30.0) + 0.5) % 12
    if hour == 0:
        hour = 12
    return f"{hour} o'clock"


KEY_NAME_OVERRIDES = {
    "return": "enter",
    "kp_enter": "enter",
}


def friendly_key_name(key_code):
    """Pygame-style key name with friendlier labels.

    Pygame reports the Enter key as "return" (and "kp_enter" for the numpad
    Enter); players know it as Enter, so we display "enter" instead. Returns a
    lowercase name; callers can upper() it when needed.
    """
    name = pygame.key.name(key_code).lower()
    return KEY_NAME_OVERRIDES.get(name, name)
