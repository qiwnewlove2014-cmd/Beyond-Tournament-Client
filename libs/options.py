import json
import os

from cryptography.fernet import Fernet
import appdirs

from . import consts, server_config

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
        endpoint_options_found = False
        with open(f"{config_dirs.user_config_dir}/settings.json", "rb") as f:
            global prefs
            loaded_prefs = json.loads(fernet.decrypt(f.read()).decode())
            for key, value in loaded_prefs.items():
                if (
                    server_config.is_production_build()
                    and key in server_config.ENDPOINT_OPTION_KEYS
                ):
                    endpoint_options_found = True
                    continue
                prefs[key] = value
        if endpoint_options_found:
            save()
    except FileNotFoundError:
        # settings file not found, create one with the default settings.
        save()


def save():
    saved_prefs = prefs
    if server_config.is_production_build():
        saved_prefs = {
            key: value
            for key, value in prefs.items()
            if key not in server_config.ENDPOINT_OPTION_KEYS
        }
    with open(f"{config_dirs.user_config_dir}/settings.json", "wb") as f:
        f.write(fernet.encrypt(json.dumps(saved_prefs).encode()))


def get(key, default=None):
    if (
        server_config.is_production_build()
        and key in server_config.ENDPOINT_OPTION_KEYS
    ):
        return default
    return prefs.get(key, default)


def set(key, value, autosave=True):
    if (
        server_config.is_production_build()
        and key in server_config.ENDPOINT_OPTION_KEYS
    ):
        return
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
