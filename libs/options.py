import json
import os
import sys

from cryptography.fernet import Fernet
import appdirs

from . import consts

config_dirs = appdirs.AppDirs("Beyond Tournament")
# defaults.
prefs = {
    "beacons": True,
    "buffer_timing": 2,
    "host": consts.DEFAULT_HOST,
    "keyboard_typing_sounds": True,
    "port": consts.DEFAULT_PORT,
    "stream_ambience": True,
    "turning_sensitivity": 1,
}

TURNING_SENSITIVITY_DEFAULT = 1
TURNING_SENSITIVITY_LEVELS = {
    1: ("Slow", 1.0),
    2: ("Moderate", 1.5),
    3: ("Fast", 2.0),
    4: ("Very fast", 3.0),
}
fernet = Fernet(consts.SETTINGS_KEY)


def initialize():
    if not os.path.exists(config_dirs.user_config_dir):
        os.makedirs(config_dirs.user_config_dir)


def load():
    try:
        with open(f"{config_dirs.user_config_dir}/settings.json", "rb") as f:
            global prefs
            loaded_prefs = json.loads(fernet.decrypt(f.read()).decode())
            for key, value in loaded_prefs.items():
                prefs[key] = value
    except FileNotFoundError:
        # settings file not found, create one with the default settings.
        save()


def save():
    with open(f"{config_dirs.user_config_dir}/settings.json", "wb") as f:
        f.write(fernet.encrypt(json.dumps(prefs).encode()))


def get(key, default=None):
    if key == "host" and "local" in sys.argv:
        return "127.0.0.1"
    return prefs.get(key, default)


def set(key, value, autosave=True):
    prefs[key] = value
    if autosave:
        save()


def get_turning_sensitivity():
    """Return a safe persisted turning-sensitivity level from 1 through 4."""
    try:
        level = int(get("turning_sensitivity", TURNING_SENSITIVITY_DEFAULT))
    except (TypeError, ValueError):
        level = TURNING_SENSITIVITY_DEFAULT
    return max(1, min(4, level))


def set_turning_sensitivity(level):
    level = max(1, min(4, int(level)))
    set("turning_sensitivity", level)
    return level


def get_turning_sensitivity_label(level=None):
    if level is None:
        level = get_turning_sensitivity()
    return TURNING_SENSITIVITY_LEVELS[level][0]


def get_turning_step():
    """Return degrees per update while a continuous turn key is held."""
    return TURNING_SENSITIVITY_LEVELS[get_turning_sensitivity()][1]
