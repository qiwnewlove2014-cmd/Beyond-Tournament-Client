import contextlib
import os
import json
from pygame import key
from .speech import speak


class Keyconfig:
    def __init__(self, file="keyconfig.json"):
        self.file = file
        self.keys = {}
        self.load()

    def load(self):
        with contextlib.suppress(FileNotFoundError):
            path = self.file if os.path.exists(self.file) else "default_keyconfig.json"
            with open(path, "rb") as f:
                data = json.loads(f.read())
                # Format 2 stores function -> key, so more than one function
                # can deliberately share the same physical key. Older files
                # stored key -> function and are still accepted below.
                if isinstance(data, dict) and data.get("format") == 2:
                    bindings = data.get("bindings", {})
                    for func, key_name in bindings.items():
                        try:
                            self.keys[func.strip()] = key.key_code(key_name.strip())
                        except (AttributeError, ValueError):
                            speak(f"Invalid key string for {func}: {key_name}. Using default.")
                    return

                # Legacy key -> function files remain compatible.
                for k, v in data.items():
                    try:
                        self.keys[v.strip()] = key.key_code(k.strip())
                    except ValueError:
                        speak(f"Invalid key string for {v}: {k}. Using default.")

    def save(self):
        data = {
            "format": 2,
            "bindings": {func: key.name(code) for func, code in self.keys.items()},
        }
        with open(self.file, "wb") as f:
            f.write(json.dumps(data, indent=4).encode("utf-8", "ignore"))

    def get(self, func, default):
        """returns the key constant asociated with {func}. if that key is not set, return {default}."""
        return self.keys.get(func, default)

    def set(self, k, func, autosave=True):
        """takes a key constant as {k} and a function string as {func} and sets that in the keyconfig. if autosave = True (default), saves the current key configuration to the file automaticly after setting."""
        self.keys[func] = k
        if autosave:
            self.save()

    def unset(self, func, autosave=True):
        """Remove an optional binding without affecting its fallback default."""
        self.keys.pop(func, None)
        if autosave:
            self.save()
