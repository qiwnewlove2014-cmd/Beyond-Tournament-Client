import pygame

def direction(num):
    '''return's a string representation of {direction}'''
    val=int((num/22.5)+.5)
    arr=["North","NorthNorthEast","NorthEast","EastNorthEast","East","EastSouthEast", "SouthEast", "SouthSouthEast","South","SouthSouthWest","SouthWest","WestSouthWest","West","WestNorthWest","NorthWest","NorthNorthWest"]
    return arr[(val % 16)]


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
