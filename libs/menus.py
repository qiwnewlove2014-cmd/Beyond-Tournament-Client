import contextlib
import functools
import re
from .logger import log
from operator import mod
from string import Formatter, Template
from . import (
    audio_manager,
    consts,
    drum_keyconfig,
    keyconfig,
    menu,
    options,
    server_config,
    speech,
    string_utils,
    updater,
)
from .key_config_screen import Key_config_screen
from .os_tools import get_os
import pygame
import cyal.util


DEFAULT_LOCATION_TEMPLATE = (
    "{x}, \r\n"
    "{y}, \r\n"
    "{z}, \r\n"
    "On {tile} \r\n"
    "Facing {direction} at {angle} degrees with a pitch of {pitch} degrees. \r\n"
    "You are leaning by {lean} degrees and you are {balanced}. "
)

LOCATION_TEMPLATE_PRESETS = (
    ("Full details", DEFAULT_LOCATION_TEMPLATE),
    ("Compact", "{x}, {y}, {z}. On {tile}. Facing {direction}."),
    ("Coordinates only", "X {x}. Y {y}. Z {z}."),
    (
        "Navigation",
        "X {x}. Y {y}. Z {z}. Facing {direction} at {angle} degrees "
        "with a pitch of {pitch} degrees.",
    ),
    (
        "Surface and posture",
        "On {tile}. You are leaning by {lean} degrees and you are {balanced}.",
    ),
)

LOCATION_TEMPLATE_FIELDS = frozenset(
    {
        "x",
        "y",
        "z",
        "x_rounded",
        "y_rounded",
        "z_rounded",
        "tile",
        "direction",
        "angle",
        "pitch",
        "lean",
        "balanced",
    }
)

LOCATION_TEMPLATE_COMPONENTS = (
    ("x", "X coordinate", "X {x}."),
    ("y", "Y coordinate", "Y {y}."),
    ("z", "Z coordinate", "Z {z}."),
    ("tile", "Surface or tile", "On {tile}."),
    ("direction", "Facing direction", "Facing {direction}."),
    ("angle", "Horizontal angle", "Horizontal angle {angle} degrees."),
    ("pitch", "Vertical pitch", "Pitch {pitch} degrees."),
    ("lean", "Lean angle", "Lean {lean} degrees."),
    ("balanced", "Balance status", "You are {balanced}."),
)

LOCATION_TEMPLATE_PREVIEW_VALUES = {
    "x": 12,
    "y": 34,
    "z": 1,
    "x_rounded": 12,
    "y_rounded": 34,
    "z_rounded": 1,
    "tile": "grass",
    "direction": "north",
    "angle": 0,
    "pitch": 0,
    "lean": 0,
    "balanced": "balanced",
}

def linux_change_speech_module(game, func_call, replace_call = None, parent=None):
    def set_module(module):
        speech.linux_speaker.set_output_module(module)
        options.set("linux_speech_module", module) 

    modules_menu = menu.Menu(game, "Select your speech module", parrent=parent)
    set_default_sounds(modules_menu)
    items = []
    for i in speech.linux_speaker.list_output_modules():
        items.append((i, functools.partial(set_module, i)))
    items.append(("back", func_call))
    modules_menu.add_items(items)
    if replace_call is None: (modules_menu)
    else: replace_call(modules_menu)


def linux_change_rate(game, func_call, replace_call=None):
    def set_rate(rate):
        try:
            speech.linux_speaker.set_rate(int(rate))
            options.set("linux_speech_rate", int(rate)) 
            speech.speak("Done!")
        except ValueError:
            speech.speak("Input a valid number please?")
        func_call()

    if replace_call is None: replace_call = game.replace
    replace_call(game.input.run("Input the rate you want to set", handeler=set_rate))


def linux_change_pitch(game, func_call, replace_call=None):
    def set_pitch(pitch):
        try:
            speech.linux_speaker.set_pitch(int(pitch))
            options.set("linux_speech_pitch", int(pitch))
        except ValueError:
            speech.speak("Input a valid number please?")
        func_call()

    if replace_call is None: replace_call = game.replace
    replace_call(game.input.run("Input the pitch you want to set", handeler=set_pitch))


def linux_change_volume(game, func_call, replace_call=None):
    def set_volume(volume):
        try:
            speech.linux_speaker.set_volume(int(volume))
            options.set("linux_speech_volume", int(volume))
        except ValueError:
            speech.speak("Input a valid number please?")
        func_call()

    if replace_call is None: replace_call = game.replace
    replace_call(game.input.run("Input the volume you want to set", handeler=set_volume))


def accounts_menu(game):
    accounts = options.get("accounts", [])
    if not accounts:
        old_username = options.get("username")
        old_password = options.get("password")
        if old_username and old_password:
            accounts.append({"username": old_username, "password": old_password})
            options.set("accounts", accounts)
    
    if not accounts:
        no_account(game)
        speech.speak("No credentials menu", False)
        return

    m = menu.Menu(game, "Select an account to login with")
    set_default_sounds(m)
    
    items = []
    for acc in accounts:
        uname = acc.get("username")
        items.append((f"Login with {uname}", functools.partial(game.login_with, uname, acc.get("password"))))
        items.append((f"Delete {uname} (Note: Removes the account from this client only, server data is unaffected)", functools.partial(delete_account, game, uname)))
    
    items.append(("Go back", lambda: main_menu(game)))
    m.add_items(items)
    m.set_music("music/10.ogg")
    game.replace(m)

def delete_account(game, username):
    accounts = options.get("accounts", [])
    accounts = [acc for acc in accounts if acc.get("username") != username]
    options.set("accounts", accounts)
    
    if options.get("username") == username:
        options.set("username", "")
        options.set("password", "")
        
    speech.speak(f"Deleted account {username}.")
    accounts_menu(game)

def main_menu(game):
    """replace the current game state with the main menu."""
    if hasattr(game, 'instance_mngr'):
        game.instance_mngr.set_character(None)
    # An in-game logout fade drops the listener gain to silence; restore it
    # so the menu (and its music) is audible again.
    try:
        master = game.audio_mngr.volume_categories["master"][0]
        game.audio_mngr.listener.gain = master / 100
    except Exception:
        pass
    m = menu.Menu(
        game,
        "Main menu.",
    )
    set_default_sounds(m)
    m.add_items(
        (
            ("Login", lambda: accounts_menu(game)),
            ("Set account", game.set_account),
            ("Create account", game.create_account),
            ("options", lambda: options_menu(game, lambda: main_menu(game))),
            ("Check for Updates", lambda: game.replace(updater.Updater(game))),
            ("Restart Client", game.ask_to_restart_client),
            # Esc on the root main menu reaches this item too (menu.py matches
            # the "exit" keyword) — fade the audio out smoothly before quitting.
            ("Exit", game.fade_out_and_exit),
        )
    )
    m.set_music("music/10.ogg")
    game.replace(m)


def no_account(game):
    """append the no account menu to the games stack."""
    m = menu.Menu(
        game,
        "you have no account set, would you like to set an account or create a new one? ",
    )
    m.add_items([
        ("Set an account with existing credentials", game.set_account),
        ("Create a new account from scratch", game.create_account),
        ("go back", lambda: main_menu(game))
    ])
    set_default_sounds(m)
    game.replace(m)


class OptionsMenu(menu.Menu):
    """Options menu with an inline Left/Right turning-sensitivity control."""

    def __init__(self, game, title, parent=None):
        super().__init__(game, title, parrent=parent)
        self.turning_sensitivity_item_index = None

    @staticmethod
    def turning_sensitivity_value_text():
        level = options.get_turning_sensitivity()
        label = options.get_turning_sensitivity_label(level)
        return f"{label}, level {level} of 4"

    @classmethod
    def turning_sensitivity_item_text(cls):
        return (
            f"Turning sensitivity. Current setting: {cls.turning_sensitivity_value_text()}. "
            "Press Left or Right to adjust. Press Escape when finished."
        )

    def _adjust_turning_sensitivity(self, direction):
        current = options.get_turning_sensitivity()
        updated = max(1, min(4, current + direction))
        if updated == current:
            if self.edge:
                self.direct_soundgroup.play(self.edge, cat="ui")
            speech.speak(
                f"{self.turning_sensitivity_value_text()}. Limit.",
                id="turning_sensitivity",
            )
            return
        options.set_turning_sensitivity(updated)
        speech.speak(
            self.turning_sensitivity_value_text(),
            id="turning_sensitivity",
        )

    def update(self, events):
        remaining_events = []
        for event in events:
            on_turning_sensitivity = (
                self.turning_sensitivity_item_index is not None
                and self.pos == self.turning_sensitivity_item_index
            )
            if (
                on_turning_sensitivity
                and event.type == pygame.KEYDOWN
                and event.key == pygame.K_LEFT
            ):
                self._adjust_turning_sensitivity(-1)
            elif (
                on_turning_sensitivity
                and event.type == pygame.KEYDOWN
                and event.key == pygame.K_RIGHT
            ):
                self._adjust_turning_sensitivity(1)
            else:
                remaining_events.append(event)
        return super().update(remaining_events)


def options_menu(game, func_call, replace_call=None, parent=None, in_game=False):
    """append the options menu to the games stack."""
    m = OptionsMenu(game, "Options menu", parent=parent)
    set_default_sounds(m)

    def open_location_template_menu():
        if in_game and parent is not None:
            return_to_options = parent.pop_last_substate
            navigate_location_menu = parent.replace_last_substate
        else:
            return_to_options = lambda: options_menu(
                game,
                func_call,
                replace_call=replace_call,
                parent=parent,
                in_game=in_game,
            )
            navigate_location_menu = replace_call
        configure_location_template(
            game,
            func_call=return_to_options,
            replace_call=replace_call,
            navigation_call=navigate_location_menu,
        )

    turning_sensitivity_item = (
        m.turning_sensitivity_item_text,
        lambda: None,
    )
    if server_config.is_production_build():
        endpoint_items = []
    else:
        endpoint_items = [
            (f"Server hostname: {options.get('host', consts.DEFAULT_HOST)}", lambda: configure_host(game, func_call, replace_call)),
            (f"Server port: {options.get('port', consts.DEFAULT_PORT)}", lambda: configure_port(game, func_call, replace_call)),
        ]
    items = endpoint_items + [
        (f"Select output device - currently set to {options.get('audio_device', '==============system default')[14:]}", lambda: output_menu(game, func_call=func_call if in_game else lambda: options_menu(game, func_call, replace_call=replace_call, parent=parent, in_game=in_game), replace_call=replace_call, parent=parent)),
        (f"Select input device - currently set to {options.get('audio_input_device', '==============system default')[14:]}", lambda: input_menu(game, func_call=func_call if in_game else lambda: options_menu(game, func_call, replace_call=replace_call, parent=parent, in_game=in_game), replace_call=replace_call, parent=parent, in_game=in_game)),
        (f"Select instrument input device - currently set to {options.get('audio_instrument_input_device', '==============system default')[14:]}", lambda: input_menu(game, func_call=func_call if in_game else lambda: options_menu(game, func_call, replace_call=replace_call, parent=parent, in_game=in_game), replace_call=replace_call, parent=parent, in_game=in_game, target="instrument")),
        (f"Voice Chat Jitter Buffer: {options.get('jitter_buffer', 60)}", lambda: configure_jitter_buffer(game, func_call, replace_call)),
        (game.toggle_item("Voice Chat", "voice_chat", True)),
        (game.toggle_item("microphone", "microphone", True)),
        (game.toggle_item("Player beacons", "beacons")),
        (game.toggle_item("Wall proximity tone", "wall_tone", False)),
        (game.toggle_item("Compass turn cue", "compass_turn_cue", True)),
        turning_sensitivity_item,
        (game.toggle_item("play intro at start up", "play_intro_at_start")),
        (
            game.toggle_item(
                "Stream ambience: turning this off might introduce more memory usage and map loading time, but better performance and less CPU usage",
                "stream_ambience",
            )
        ),
        (
            game.toggle_item(
                "High performance mode: turning this on raises the game framerate from 60 to 120, so incoming music notes, voices, and your key presses reach your ears up to twice as fast. It uses more CPU, so turn it off if your computer gets hot or slows down",
                "high_framerate",
            )
        ),
        (game.toggle_item("Mute audio when the game window does not have focus", "mute_on_focus_loss")),
        (game.toggle_item("Mute speech when out of the game window", "mute_speech_on_focus_loss")),
        (game.toggle_item("Keyboard typing sounds", "keyboard_typing_sounds", True)),
        (game.toggle_item(
            "speak your direction when finished turning", 
            "speak_on_turn",
            False,
        )),
        (game.toggle_item("receive typing indicators", "typing")),
        (
            "Set how you would like timestamps in the end of buffer items to be displayed",
            lambda: buffer_timing_menu(game, func_call=func_call if in_game else lambda: options_menu(game, func_call, in_game=in_game), replace_call=replace_call, parent=parent),
        ),
        (
            "Set which HRTF Model you would like to use. Currently set to "+str(options.get("hrtf_model", game.audio_mngr.hrtf.current_model)),
            lambda: hrtf_model_menu(game, func_call=func_call if in_game else lambda: options_menu(game, func_call, in_game=in_game), replace_call=replace_call, parent=parent)
        ),
        (
            lambda: "Configure location announcement. Current setting: "
            + location_template_name(
                options.get("location_template", DEFAULT_LOCATION_TEMPLATE)
            ),
            open_location_template_menu,
        ),
        ("Configure key bindings.", lambda: keyconfig_menu(game, func_call=func_call if in_game else lambda: options_menu(game, func_call, in_game=in_game), replace_call=replace_call, parent=parent, in_game=in_game)),
        ("Configure drum keys.", lambda: drum_keyconfig_menu(game, func_call=func_call if in_game else lambda: options_menu(game, func_call, in_game=in_game), replace_call=replace_call, parent=parent, in_game=in_game)),
    ]
    if in_game:
        items.append((
            "Configure custom online and offline sounds",
            lambda: presence_sounds_menu(game, func_call, replace_call=replace_call, parent=parent),
        ))
    items.append(("Back", lambda: func_call()))
    m.add_items(items)
    m.turning_sensitivity_item_index = m.items.index(turning_sensitivity_item)
    # Opening Options from gameplay must not start the main-menu music.  It
    # allocates another direct OpenAL source while map music / jukebox relay
    # sources are already streaming, which can make constrained audio devices
    # evict or stall an existing stream.  Menu music remains appropriate while
    # browsing from the title screens.
    if not in_game:
        m.set_music("music/10.ogg")
    if replace_call is None: game.replace(m)
    else: replace_call(m)


def presence_sounds_menu(game, func_call, replace_call=None, parent=None):
    m = menu.Menu(game, "Custom online and offline sounds", parrent=parent)
    set_default_sounds(m)
    manager = game.presence_sounds
    online_status = "custom" if manager.own_sound_ids["online"] else "default"
    offline_status = "custom" if manager.own_sound_ids["offline"] else "default"
    m.add_items([
        (f"Upload online sound. Current: {online_status}", lambda: manager.choose_and_upload("online")),
        (f"Upload offline sound. Current: {offline_status}", lambda: manager.choose_and_upload("offline")),
        ("Restore default online sound", lambda: manager.clear("online")),
        ("Restore default offline sound", lambda: manager.clear("offline")),
        ("Requirements: OGG Vorbis, 512 kilobytes maximum, 0.25 to 10 seconds", lambda: None),
        ("Back", func_call),
    ])
    if replace_call is None:
        game.replace(m)
    else:
        replace_call(m)


def buffer_timing_menu(game, func_call, replace_call=None, parent=None):
    """append the buffer time display menu to the games stack."""
    m = menu.Menu(
        game,
        "How would you like timestamps to be displayed in buffer items?",
        parrent=parent
    )
    set_default_sounds(m)
    m.add_items(
        (
            ("Absolute time", lambda: set_buffer_timing(game, 1, func_call)),
            ("Relative time", lambda: set_buffer_timing(game, 2, func_call)),
            ("Don't display timestamps", lambda: set_buffer_timing(game, 3, func_call)),
            ("Back", func_call),
        )
    )
    if replace_call is None: game.replace(m)
    else: replace_call(m)

def hrtf_model_menu(game, func_call, replace_call=None, parent=None):
    m = menu.Menu(
        game, 
        "Select your HRTF model",
        parrent=parent
    )
    set_default_sounds(m)
    for model in game.audio_mngr.hrtf.models():
        m.add_items([
            (model, functools.partial(set_hrtf_model, model, game, func_call))
        ])
    m.add_items([
        ("Disable HRTF", lambda: set_hrtf_model(None, game, func_call)),
        ("go back", func_call)
    ])
    if replace_call is None: game.replace(m)
    else: replace_call(m)

def set_hrtf_model(model, game, func_call):
    game.audio_mngr.hrtf.use(model)
    options.set("hrtf_model", model)
    func_call()
    speech.speak(f"using HRTF model {model}")


def keyconfig_menu(game, func_call, replace_call=None, parent=None, in_game=False, restore_pos=None):
    """append a menu for binding keyboard keys to functions.

    restore_pos: if set, the menu cursor starts at this index instead of the top.
    This prevents the user from having to scroll back down after each key bind.
    """
    default_keys = keyconfig.Keyconfig("default_keyconfig.json")
    m = menu.Menu(
        game,
        "Please select a function to bind a key to.",
        parrent=parent
    )
    set_default_sounds(m)
    if replace_call is None: replace_call=game.replace
    items = []
    for i in default_keys.keys.keys():
        # Capture the current index so we can restore the cursor to THIS item
        # after the key is bound. We use a default argument (_idx=current_index)
        # to bind the value IMMEDIATELY — without it, Python's late-binding
        # closure would make every lambda use the final loop value, causing
        # the cursor to always jump to the last item.
        current_index = len(items)
        if in_game:
            on_bind_done = lambda _idx=current_index: [parent.pop_last_substate(), keyconfig_menu(game, func_call, replace_call=parent.replace_last_substate, parent=parent, in_game=True, restore_pos=_idx)]
        else:
            on_bind_done = lambda _idx=current_index: keyconfig_menu(game, func_call, in_game=False, restore_pos=_idx)

        func = functools.partial(replace_call, Key_config_screen(game, i, options_menu=on_bind_done))
        items.append(
            (
                f"{i}: {string_utils.friendly_key_name(game.keyconfig.get(i, default_keys.keys[i]))}",
                func,
            )
        )

    # the list comprehention above basicly adds all the keys of default_keys.keys(which are function strings) as the item text and a lambda function that will append a key config screen for that function.
    items.append(("Back", func_call))
    m.add_items(items)
    # Restore cursor to the item the user was on before binding (if any).
    if restore_pos is not None and 0 <= restore_pos < len(m.items):
        m.pos = restore_pos
    replace_call(m)


def drum_keyconfig_menu(
    game,
    func_call,
    replace_call=None,
    parent=None,
    in_game=False,
    restore_pos=None,
):
    """List each pad with its primary and optional alternate key."""
    m = menu.Menu(game, "Configure drum keys.", parrent=parent)
    set_default_sounds(m)
    if replace_call is None:
        replace_call = game.replace

    # In-game menus live on Gameplay's substate stack.  Opening a child must
    # push it, while refreshing this menu must replace only the current top.
    # Outside gameplay, both operations use the normal game-state replacement.
    open_child = parent.add_substate if in_game else replace_call
    refresh_current = parent.replace_last_substate if in_game else replace_call

    items = []
    for binding in drum_keyconfig.DRUM_BINDINGS:
        current_index = len(items)
        primary = drum_keyconfig.binding_key(game.keyconfig, binding)
        primary_name = (
            string_utils.friendly_key_name(primary)
            if primary is not None
            else "unassigned"
        )
        alternate = drum_keyconfig.alternate_key(game.keyconfig, binding)
        alternate_name = (
            string_utils.friendly_key_name(alternate)
            if alternate is not None
            else "unassigned"
        )
        open_pad_menu = functools.partial(
            drum_pad_keyconfig_menu,
            game,
            binding,
            func_call,
            replace_call=open_child,
            parent=parent,
            in_game=in_game,
            list_position=current_index,
        )
        items.append((
            f"{binding.label}: primary {primary_name}, "
            f"alternate {alternate_name}",
            open_pad_menu,
        ))

    def reset_defaults():
        drum_keyconfig.restore_defaults(game.keyconfig)
        speech.speak("Drum keys restored to defaults.")
        drum_keyconfig_menu(
            game,
            func_call,
            replace_call=refresh_current,
            parent=parent,
            in_game=in_game,
            restore_pos=len(drum_keyconfig.DRUM_BINDINGS),
        )

    def clear_all():
        drum_keyconfig.clear_all(game.keyconfig)
        speech.speak("Drum keys cleared. Set new primary or alternate keys now.")
        drum_keyconfig_menu(
            game,
            func_call,
            replace_call=refresh_current,
            parent=parent,
            in_game=in_game,
            restore_pos=len(drum_keyconfig.DRUM_BINDINGS) + 1,
        )

    items.append(("Restore default drum keys.", reset_defaults))
    items.append(("Clear drum keys.", clear_all))
    items.append(("Back", func_call))
    m.add_items(items)
    if restore_pos is not None and 0 <= restore_pos < len(m.items):
        m.pos = restore_pos
    replace_call(m)


def drum_pad_keyconfig_menu(
    game,
    binding,
    func_call,
    replace_call,
    parent=None,
    in_game=False,
    list_position=0,
):
    """Configure both playable key slots for one drum pad."""
    primary = drum_keyconfig.binding_key(game.keyconfig, binding)
    primary_name = (
        string_utils.friendly_key_name(primary)
        if primary is not None
        else "unassigned"
    )
    alternate = drum_keyconfig.alternate_key(game.keyconfig, binding)
    alternate_name = (
        string_utils.friendly_key_name(alternate)
        if alternate is not None
        else "unassigned"
    )
    m = menu.Menu(game, f"Configure {binding.label} keys.", parrent=parent)
    set_default_sounds(m)

    open_child = parent.add_substate if in_game else replace_call
    refresh_current = parent.replace_last_substate if in_game else replace_call

    def reopen_pad_menu():
        if in_game:
            # A completed/canceled capture is still the top substate. Remove it
            # before replacing the underlying pad menu with refreshed labels.
            parent.pop_last_substate()
        drum_pad_keyconfig_menu(
            game,
            binding,
            func_call,
            replace_call=refresh_current,
            parent=parent,
            in_game=in_game,
            list_position=list_position,
        )

    def open_key_capture(function, display_name):
        validator = functools.partial(
            drum_keyconfig.validate_key,
            game.keyconfig,
            function,
        )
        open_child(Key_config_screen(
            game,
            function,
            options_menu=reopen_pad_menu,
            display_name=display_name,
            key_validator=validator,
            cancel_keys=(pygame.K_ESCAPE,),
        ))

    def clear_alternate():
        drum_keyconfig.clear_alternate(game.keyconfig, binding)
        speech.speak(f"{binding.label} alternate key cleared.")
        drum_pad_keyconfig_menu(
            game,
            binding,
            func_call,
            replace_call=refresh_current,
            parent=parent,
            in_game=in_game,
            list_position=list_position,
        )

    def return_to_list():
        if in_game:
            parent.pop_last_substate()
            return
        drum_keyconfig_menu(
            game,
            func_call,
            replace_call=replace_call,
            parent=parent,
            in_game=in_game,
            restore_pos=list_position,
        )

    items = [
        (
            f"Set primary key. Currently {primary_name}.",
            functools.partial(
                open_key_capture,
                binding.function,
                f"{binding.label} primary",
            ),
        ),
        (
            f"Set alternate key. Currently {alternate_name}.",
            functools.partial(
                open_key_capture,
                binding.alternate_function,
                f"{binding.label} alternate",
            ),
        ),
    ]
    if alternate is not None:
        items.append(("Clear alternate key.", clear_alternate))
    items.append(("Back", return_to_list))
    m.add_items(items)
    replace_call(m)


def update_question(game, canceler):
    """ask user if they want to update. replace with {canceler} if the user presses no"""
    m = menu.Menu(game, "An update is available! Would you like to update now?")
    set_default_sounds(m)
    m.add_items(
        (
            ("Yes", lambda: game.replace(updater.Updater(game, check=False))),
            ("No", lambda: game.replace(canceler)),
        )
    )
    game.replace(m)


def set_default_sounds(m):
    m.set_sounds(
        click="menu/move.ogg",
        enter="menu/select.ogg",
        open="menu/open.ogg",
        close="menu/close.ogg",
    )

def location_template_fields(template):
    """Return the simple replacement fields used by a location template."""
    fields = set()
    for _, field_name, _, _ in Formatter().parse(template):
        if field_name is not None:
            fields.add(field_name)
    return fields


def validate_location_template(template):
    """Validate a user-entered location template without formatting game data."""
    if not isinstance(template, str) or not template.strip():
        return False, "The template cannot be empty."
    if len(template) > 1000:
        return False, "The template is too long. The maximum is 1000 characters."

    try:
        parsed = list(Formatter().parse(template))
    except ValueError:
        return False, "The opening and closing braces do not match."

    used_fields = set()
    for _, field_name, format_spec, conversion in parsed:
        if field_name is None:
            continue
        if not field_name:
            return False, "Empty braces are not allowed."
        if field_name not in LOCATION_TEMPLATE_FIELDS:
            return False, f"Unknown variable: {field_name}."
        if format_spec:
            return False, "Format codes after a colon are not supported."
        if conversion:
            return False, "Variable conversions are not supported."
        used_fields.add(field_name)

    if not used_fields:
        return False, "Include at least one location variable."
    return True, ""


def render_location_template_preview(template):
    valid, error = validate_location_template(template)
    if not valid:
        raise ValueError(error)
    return " ".join(template.format(**LOCATION_TEMPLATE_PREVIEW_VALUES).split())


def location_template_name(template):
    for name, preset in LOCATION_TEMPLATE_PRESETS:
        if template == preset:
            return name
    valid, _ = validate_location_template(template)
    return "Custom" if valid else "Invalid custom template"


def build_custom_location_template(selected_fields):
    parts = [
        template_part
        for field_name, _, template_part in LOCATION_TEMPLATE_COMPONENTS
        if field_name in selected_fields
    ]
    return " ".join(parts)


def _save_location_template(template, name, func_call):
    valid, error = validate_location_template(template)
    if not valid:
        speech.speak(f"Template not saved. {error}")
        return
    options.set("location_template", template)
    speech.speak(
        f"Location announcement set to {name}. Example: "
        f"{render_location_template_preview(template)}"
    )
    func_call()


def _preview_location_template(template):
    valid, error = validate_location_template(template)
    if not valid:
        speech.speak(f"This template is invalid. {error}")
        return
    speech.speak(f"Example: {render_location_template_preview(template)}")


def configure_location_template(
    game,
    func_call,
    replace_call=None,
    navigation_call=None,
):
    """Open an accessible preset and custom location-announcement menu."""
    if replace_call is None:
        replace_call = game.replace
    if navigation_call is None:
        navigation_call = replace_call

    current = options.get("location_template", DEFAULT_LOCATION_TEMPLATE)
    m = menu.Menu(
        game,
        "Configure location announcement. Current setting: "
        f"{location_template_name(current)}.",
    )
    set_default_sounds(m)

    items = []
    for name, preset in LOCATION_TEMPLATE_PRESETS:
        items.append(
            (
                f"Use {name}. Example: {render_location_template_preview(preset)}",
                functools.partial(
                    _save_location_template,
                    preset,
                    name,
                    func_call,
                ),
            )
        )

    return_to_config = functools.partial(
        configure_location_template,
        game,
        func_call,
        navigation_call,
        navigation_call,
    )
    items.extend(
        [
            (
                "Build a custom announcement by choosing individual parts",
                functools.partial(
                    configure_custom_location_template,
                    game,
                    func_call,
                    navigation_call,
                    None,
                ),
            ),
            (
                "Advanced raw template editor",
                functools.partial(
                    configure_advanced_location_template,
                    game,
                    func_call,
                    navigation_call,
                    return_to_config,
                ),
            ),
            (
                "Preview current announcement",
                functools.partial(_preview_location_template, current),
            ),
            (
                "Reset to default Full details",
                functools.partial(
                    _save_location_template,
                    DEFAULT_LOCATION_TEMPLATE,
                    "Full details",
                    func_call,
                ),
            ),
            ("Back", func_call),
        ]
    )
    m.add_items(items)
    replace_call(m)


def configure_custom_location_template(
    game,
    func_call,
    replace_call=None,
    selected_fields=None,
):
    if replace_call is None:
        replace_call = game.replace
    if selected_fields is None:
        current = options.get("location_template", DEFAULT_LOCATION_TEMPLATE)
        valid, _ = validate_location_template(current)
        if valid:
            selected_fields = location_template_fields(current).intersection(
                {component[0] for component in LOCATION_TEMPLATE_COMPONENTS}
            )
            if not selected_fields:
                selected_fields = {
                    component[0] for component in LOCATION_TEMPLATE_COMPONENTS
                }
        else:
            selected_fields = {
                component[0] for component in LOCATION_TEMPLATE_COMPONENTS
            }
    else:
        selected_fields = set(selected_fields)

    def reopen_builder():
        configure_custom_location_template(
            game,
            func_call,
            replace_call,
            selected_fields,
        )

    def toggle_component(field_name):
        if field_name in selected_fields:
            selected_fields.remove(field_name)
        else:
            selected_fields.add(field_name)
        reopen_builder()

    draft = build_custom_location_template(selected_fields)
    m = menu.Menu(
        game,
        "Build a custom location announcement. Choose each part to include or exclude it.",
    )
    set_default_sounds(m)
    items = []
    for field_name, label, _ in LOCATION_TEMPLATE_COMPONENTS:
        status = "Included" if field_name in selected_fields else "Excluded"
        items.append(
            (
                f"{label}: {status}. Press Enter to toggle.",
                functools.partial(toggle_component, field_name),
            )
        )
    items.extend(
        [
            (
                "Preview custom announcement",
                functools.partial(_preview_location_template, draft),
            ),
            (
                "Save custom announcement",
                functools.partial(
                    _save_location_template,
                    draft,
                    "Custom",
                    func_call,
                ),
            ),
            (
                "Back to location announcement choices",
                functools.partial(
                    configure_location_template,
                    game,
                    func_call,
                    replace_call,
                    replace_call,
                ),
            ),
        ]
    )
    m.add_items(items)
    replace_call(m)


def configure_advanced_location_template(
    game,
    func_call,
    replace_call=None,
    return_call=None,
):
    if replace_call is None:
        replace_call = game.replace
    if return_call is None:
        return_call = func_call
    replace_call(
        game.input.run(
            "Advanced template editor. Use braces around supported variables: "
            "x, y, z, tile, direction, angle, pitch, lean, and balanced. "
            "Press Enter on an empty input to cancel.",
            default=options.get("location_template", DEFAULT_LOCATION_TEMPLATE),
            handeler=lambda message: configure_location_template2(
                game,
                message,
                func_call,
                return_call,
            ),
        )
    )


def configure_location_template2(game, message, func_call, return_call=None):
    """Validate and save an advanced raw template; kept for compatibility."""
    if return_call is None:
        return_call = func_call
    if not message.strip():
        speech.speak("Canceled. Location announcement was not changed.")
        return_call()
        return

    valid, error = validate_location_template(message)
    if not valid:
        speech.speak(f"Template not saved. {error}")
        return_call()
        return

    options.set("location_template", message)
    speech.speak(
        "Custom location announcement saved. Example: "
        f"{render_location_template_preview(message)}"
    )
    func_call()


def set_buffer_timing(game, option, func_call):
    options.set("buffer_timing", option)
    func_call()




def output_menu(game, func_call, replace_call=None, parent=None):
    m = menu.Menu(game, "select audio output", parrent=parent)
    set_default_sounds(m)
    
    m.add_items([
        (f"system default: {cyal.util.get_default_all_device_specifier()[14:]}", lambda: set_device(game, "system default", func_call))
    ])
    for device in cyal.util.get_all_device_specifiers():
        m.add_items([
            (device[14:], functools.partial(set_device, game, device, func_call))
        ])
    m.add_items([
        ("go back", func_call)
    ])
    if replace_call is None: game.replace(m)
    else: replace_call(m)
    

def set_device(game, device, func_call):
    options.set("audio_device", device)
    dev_name = device
    if dev_name == "system default":
        dev_name = cyal.util.get_default_all_device_specifier()
    try:
        game.audio_mngr.context.device.reopen(name=dev_name)
    except Exception as e:
        log(f"[AUDIO] Failed to switch to audio device {dev_name!r}: {e}")
        default_dev = cyal.util.get_default_all_device_specifier()
        if default_dev and default_dev != dev_name:
            with contextlib.suppress(Exception):
                game.audio_mngr.context.device.reopen(name=default_dev)
    with contextlib.suppress(Exception):
        game.audio_mngr.hrtf.use(options.get("hrtf_model", "oalsoft_hrtf_48000"))
    func_call()

def input_menu(game, func_call, replace_call=None, parent=None, in_game=False, target="voice"):
    m = menu.Menu(game, "select instrument audio input" if target == "instrument" else "select audio input", parrent=parent)
    set_default_sounds(m)
    capture = cyal.CaptureExtension()
    m.add_items([
        (f"system default: {str(capture.default_device)[14:]}", lambda: set_input_device(game, 'system default', func_call, parent, capture, in_game, target))
    ])
    from . import instrument_input as _instr
    if target == "instrument":
        # Put likely guitar/bass interfaces and USB effects pedals first,
        # tagged, so they are easy to find in the instrument input menu.
        entries = [(label, functools.partial(
            set_input_device, game, device, func_call, parent, capture,
            in_game, target))
            for label, device in _instr.instrument_menu_entries(capture.devices)]
    else:
        entries = [(device[14:], functools.partial(
            set_input_device, game, device, func_call, parent, capture,
            in_game, target))
            for device in capture.devices]
    for label, fn in entries:
        m.add_items([(label, fn)])
    m.add_items([
        ("go back", func_call)
    ])
    if replace_call is None: game.replace(m)
    else: replace_call(m)
    

def set_input_device(game, device, func_call, parent, capture, in_game=False, target="voice"):
    if target == "instrument":
        option_key = "audio_instrument_input_device"
    else:
        option_key = "audio_input_device"
    options.set(option_key, device)
    if device == "system default": device = str(capture.default_device.decode('utf-8'))
    if in_game:
        if target == "instrument":
            from . import instrument_input
            owner = getattr(parent, "instrument_input", None)
            if owner is None:
                owner = parent.instrument_input = instrument_input.InstrumentInput(parent.game)
            owner.reopen(device)
        elif hasattr(parent, 'voice_chat') and parent.voice_chat:
            current_name = None
            if getattr(parent.voice_chat, 'audio_input', None):
                current_name = getattr(parent.voice_chat.audio_input, 'name', None)
                if isinstance(current_name, bytes):
                    current_name = current_name.decode('utf-8')
            if current_name != device:
                if getattr(parent.voice_chat, 'audio_input', None):
                    del parent.voice_chat.audio_input
                
                parent.voice_chat.stereo = False
                parent.voice_chat.audio_input = None
                device_encoded = device.encode()
                for fmt, is_stereo in ((cyal.BufferFormat.MONO16, False), (cyal.BufferFormat.STEREO16, True)):
                    try:
                        parent.voice_chat.audio_input = parent.voice_chat.capture_ext.open_device(name=device_encoded, sample_rate=48000, format=fmt)
                        parent.voice_chat.stereo = is_stereo
                        break
                    except (cyal.exceptions.DeviceNotFoundError, TypeError):
                        pass
                
                if not parent.voice_chat.audio_input:
                    speech.speak(f"Failed to load audio device: {device}")
    func_call()


def configure_jitter_buffer(game, func_call, replace_call=None):
    if replace_call is None: replace_call = game.replace
    replace_call(
        game.input.run(
            "Enter the value for your voice chat Jitter buffer. This is how long the client should wait  to start playing voice chats to allow for audio data to back up, preventing stuttering. A lower jitter buffer will decrease latency but may cause stuttering if internet is not stable enough. A higher jitter buffer will increase latency but will have a more stable sound. Minimum is 20ms and maximum is 120ms.",
            default=str(options.get("jitter_buffer", 60)),
            handeler=lambda message: configure_jitter_buffer2(game, message, func_call)
        )
    )

def configure_jitter_buffer2(game, message, func_call):
    if message.strip()=="":
        func_call()
        speech.speak("canceled")
        return
    # Robustly parse the user's input: strip anything that is not a digit (for
    # example a stray trailing backslash like "12010\\") and clamp to the valid
    # range.  Invalid input falls back to the stored value instead of raising.
    digits = re.sub(r"\D", "", message)
    try:
        value = int(digits) if digits else int(options.get("jitter_buffer", 60))
    except ValueError:
        value = int(options.get("jitter_buffer", 60))
    if value < 20: value = 20
    if value > 120: value = 120

    options.set("jitter_buffer", value)
    game.audio_mngr.silent_buffer = bytearray(96 * options.get("jitter_buffer", 60))
    func_call()




def configure_host(game, func_call, replace_call=None):
    if replace_call is None: replace_call = game.replace
    replace_call(
        game.input.run(
            "Enter the hostname of the server to connect to.",
            default=str(options.get("host", consts.DEFAULT_HOST)),
            handeler=lambda message: configure_host2(game, message, func_call)
        )
    )

def configure_host2(game, message, func_call):
    if message.strip()=="": 
        func_call()
        speech.speak("canceled")
        return

    options.set("host", message)
    func_call()


def configure_port(game, func_call, replace_call=None):
    if replace_call is None: replace_call = game.replace
    replace_call(
        game.input.run(
            "Enter the UDP port of the server to connect to.",
            default=str(options.get("port", consts.DEFAULT_PORT)),
            handeler=lambda message: configure_port2(game, message, func_call)
        )
    )

def configure_port2(game, message, func_call):
    if message.strip()=="":
        func_call()
        speech.speak("canceled")
        return
    # Strip non-digit characters (e.g. a stray trailing backslash) before
    # parsing so malformed input does not crash the main loop.
    digits = re.sub(r"\D", "", message)
    try:
        message = int(digits) if digits else 0
    except ValueError:
        message = 0
    if message not in range(1, 2 **     16):
        func_call()
        speech.speak("Invalid port number")
        return

    options.set("port", message)
    func_call()
