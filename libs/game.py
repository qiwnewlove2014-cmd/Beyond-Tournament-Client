# todo: add a less ugly way of handeling account creation and logging in.
import threading
import os, signal
import shutil
import subprocess
import time
import queue
from string import whitespace
import contextlib
import weakref
import sys
from collections import namedtuple
import enet
import pygame
import cyal.util, cyal.exceptions
import requests

from .version import version

from . import clock, consts, event_handeler, menus
from . import (
    menu,
    networking,
    options,
    virtual_input,
    state,
    audio_manager,
    speech,
    updater,
    presence_sounds,
    server_config,
    path_utils,
    automation,
    instance_manager,
    anti_cheat,
    keyboard_layout,
    watchdog,
)
from .speech import speak
from .logger import log, log_exception

# The agreement players read before creating an account. Written to be
# polite and short — spoken aloud by the screen reader, so each line is a
# complete, plain sentence. Keep it in sync with server/agreement.txt.
AGREEMENT_TEXT = """Welcome to Beyond Tournament! Before creating your account, please read our simple agreement.
1. Be respectful. Treat other players the way you would like to be treated. Harassment, hate speech, and bullying are not allowed.
2. Play fairly. Cheating, hacking, or abusing bugs is not allowed.
3. Your account is yours. Do not share your password, and remember that you are responsible for what happens on your account.
4. Be honest. Do not impersonate staff members or make false reports.
5. Listen to the staff. Moderators and developers keep the server safe and fun, and their decisions should be respected.
6. Have fun. This is a game for everyone. If you see a problem or have an idea, please tell us — we are always happy to listen.
By choosing Yes below, you agree to follow these simple rules. Thank you for being part of our community!
"""
from .os_tools import get_os
from .keyconfig import Keyconfig
from .midi import MidiHub
import psutil
import webbrowser

Delayed_function = namedtuple("delayed_function", ["clock", "time", "function"])


class Game:
    def __init__(self, screen):
        self.screen = screen
        
        self.font = pygame.font.SysFont("calibri", 24)
        self.lock = threading.RLock()
        self.queue = queue.SimpleQueue()
        self.get = self.queue.get_nowait
        
        # Anti-Cheat Testing Variable
        self.test_money = anti_cheat.SecureInt(5000)
        self.clocks = weakref.WeakSet()
        self.keyconfig = Keyconfig(
            f"{options.config_dirs.user_config_dir}/keyconfig.json"
        )
        options.load()
        self.framerate = 60
        self.delta = 1 / self.framerate * 1000
        self.clock = pygame.time.Clock()
        self.stack = []
        self.events = []
        self.input_history = [""]
        self.input = virtual_input.Virtual_input(self)
        self.network = None
        self.last_fps = 60
        self.automations = []
        self.exclude_water = []
        self.ignore_others_water=False
        self.delayed_functions = {}
        self.ids = 0
        self.mouse_buttons = {
            "left": False,
            "middle": False,
            "right": False,
            "wheel_up": False,
            "wheel_down": False,
        }
        
        self.audio_mngr = audio_manager.AudioManager()
        # Game owns the sole PortMidi worker for the entire process lifetime.
        self.midi_hub = MidiHub(announce=speak)
        self.device_clock = self.new_clock()
        self.title_clock = self.new_clock()
        self.direct_soundgroup = self.audio_mngr.create_soundgroup(True)
        self.audio_mngr.preload_ui_sounds()
        self.presence_sounds = presence_sounds.PresenceSoundManager(self)
        self.instance_mngr = instance_manager.InstanceManager()
        self.instance_mngr.update_title()
        self.reconnecting = False
        self._recovery_in_progress = False
        
        try:
            anti_cheat.set_game_reference(self)
        except Exception: pass

        self.watchdog = watchdog.GameWatchdog(self)
        self.watchdog.start()

    def start(self):
        if len(sys.argv) > 3:
            self.parse_arguments()
        # self.globals=globals
        if not options.get("heard_intro", False) or options.get("play_intro_at_start", False):
            options.set("heard_intro", True)
            sound = self.audio_mngr.play_unbound("intro.ogg", 0, 0, 0, False, direct=True)
            self.suspend(14.5)
            speak("Beyond Tournament!")
            self.suspend(4.5)
        if "__compiled__" in globals():
            self.append(updater.Updater(self, silent_if_uptodate=True))
        else:
            menus.main_menu(self)
            speak("Bypassing updater in uncompiled version...", False)

    def parse_arguments(self):
        action, param, pid = sys.argv[1], sys.argv[2], sys.argv[3]
        with contextlib.suppress(Exception):
            os.kill(int(pid), signal.SIGTERM)
        speak("Please wait...")
        while psutil.pid_exists(int(pid)):
            time.sleep(0.018)
        if action == "move_to":
            # a past version asked this version to move itself.
            speak("Copying files...")
            path_utils.copy_folder("./", param)
            last_cwd = os.getcwd()
            subprocess.Popen(
                [f"{param}/Beyond Tournament.exe", "rm_dir", last_cwd, str(os.getpid())],
                cwd=param,
            )
            return self.exit()
        elif action == "rm_dir":
            if os.path.exists(param):
                shutil.rmtree(param)

    def make_text(self):
        lines = [i[0] for i in speech.history[-3:]]
        return [self.font.render(line, True, "white") for line in lines]

    def put(self, value):
        """puts value into the event queue to be processed. value could be one of the following:
        None: breaks the event loop. you can put that before joining the thread.
        callable(). calls the given callable inside this thread with no arguments. you could pass a lambda function if you want to call a function with arguments.
        tuple(string, any): set's the value of the variable named as the string given in [0] as the value given in [1]
        """
        self.queue.put_nowait(value)

    def new_id(self):
        self.ids += 1
        return self.ids

    def call_after(self, time, function):  # sourcery skip: avoid-builtin-shadow
        """call{function} after {time}ms. returns an id that you could use to stop a function before its executed"""
        id = self.new_id()
        delayed_function = Delayed_function(self.new_clock(), time, function)
        self.delayed_functions[id] = delayed_function
        return id

    def cancel_before(self, id):
        """takes an id and prevents the delayed function of that id (if any) from running if they havent been ran yet."""
        if self.delayed_functions[id]:
            del self.delayed_functions[id]

    def toggle(self, key, on_text="on", off_text="off", default=False):
        """toggle options[key]. speaks the new state(whether on or off)"""
        if option := options.get(key, default):
            speak(off_text)
            options.set(key, False)
            return False
        else:
            speak(on_text)
            options.set(key, True)
            return True

    def toggle_state(self, text, key, default=False):
        """returns {text} and whether its on or off. for example: test. off"""
        st = "on" if options.get(key, default) else "off"
        return f"{text}. {st}"

    def toggle_item(self, text, key, default=False):
        """returns a tuple to toggle {key} with the title as {text}. the tuple is accepted by Menu as a menu item only."""
        return (lambda: self.toggle_state(text, key, default=default), lambda: self.toggle(key, default=default))

    def _new_network_client(self):
        host, port = server_config.get_server_endpoint()
        return networking.Client(
            self,
            host,
            port,
            event_handeler.EventHandeler,
        )

    def _connection_failure_message(self, error):
        if isinstance(error, server_config.ServerConfigError):
            return str(error)
        if server_config.is_production_build():
            return "Failed to connect to the official server."
        return "Failed to connect. \r\n{error}".format(error=error)

    def login_with(self, username, password):
        options.set("username", username)
        options.set("password", password)
        self.login()

    def add_account_to_list(self, username, password):
        accounts = options.get("accounts", [])
        accounts = [acc for acc in accounts if acc.get("username") != username]
        accounts.append({"username": username, "password": password})
        options.set("accounts", accounts)

    def login(self):
        username = options.get("username")
        password = options.get("password")
        if not username or not password:
            menus.no_account(self)
            return speak("No credentials menu", False)
        try:
            self.network = self._new_network_client()
        except (OSError, server_config.ServerConfigError) as e:
            self.pop()
            menus.main_menu(self)
            speak(self._connection_failure_message(e))
            return
        speak("Connecting to the server. Please wait...")
        self.replace(self.login2)

    def login2(self):
        if self.network.timeout_clock.elapsed >= consts.TIMEOUT:
            return self.connection_error()
        e = self.network.net.service(0)
        if not self.network.connected and e.type == enet.EVENT_TYPE_CONNECT:
            speak("Logging in. Please wait...")
            self.network.send(
                consts.CHANNEL_MISC,
                "login",
                {
                    "username": options.get("username"),
                    "password": options.get("password"),
                    "version": consts.CLIENT_VERSION,  # 🔢 Version for compatibility check
                    "capabilities": ["music_timeline_v1"],
                },
            )
            self.replace(self.network.loop)

    def set_account(self):
        self.append(self.input.run("Enter your username.", handeler=self.set_account2))

    def set_account2(self, username):
        if username.strip()=="": 
            return self.cancel()
        # change any whitespaces with dashes.
        for i in whitespace:
            username = username.replace(i, "-", -1)
        options.set("username", username)
        self.replace(
            self.input.run("Enter your password.", handeler=self.set_account_done, password=True)
        )

    def set_account_done(self, password):
        if password.strip()=="":
            return self.cancel()
        options.set("password", password)
        self.add_account_to_list(options.get("username"), password)
        self.pop()
        self.pop()
        self.login()
        speak("done.", False)

    def create_account(self):
        m = menu.Menu(self, "Do you agree with this game's agreement?")
        menus.set_default_sounds(m)
        m.add_items(
            [
                ("Read the agreement", self._agreement_menu),
                (
                    "Yes, I have read, understood, and agreed to everything in the agreement.",
                    lambda: self.replace(
                        self.input.run(
                            "Enter your username.", handeler=self.create_account2
                        )
                    ),
                ),
                ("No, I disagree.", lambda: menus.main_menu(self)),
            ]
        )
        self.replace(m)

    def _agreement_menu(self):
        """Read-only, scrollable copy of the agreement. It is pushed on top of
        the create-account menu (append, never replace) so Back/ESC return to
        the agreement question instead of popping into nothing and exiting."""
        m = menu.Menu(self, "Beyond Tournament agreement", autoclose=False, parrent=self)
        menus.set_default_sounds(m)
        items = []
        for line in AGREEMENT_TEXT.strip().splitlines():
            line = line.strip()
            if line:
                items.append((line, lambda: None))
        items.append(("Back to the agreement question", lambda: self.pop()))
        m.add_items(items)
        m.pos = -1
        self.append(m)

    def create_account2(self, username):
        # change any whitespaces with dashes.
        for i in whitespace:
            username = username.replace(i, "-", -1)
        if len(username) < 3 or len(username) > 25:
            # Never pop into an empty stack: return to the main menu instead.
            menus.main_menu(self)
            return speak(
                "Canceled. Your username must be 4 to 25 characters."
            )
        options.set("username", username)
        self.replace(
            self.input.run("Enter your password.", handeler=self.create_account3, password=True)
        )

    def create_account3(self, password):
        if password.strip()=="":
            menus.main_menu(self)
            return speak("Canceled.")
        if len(password) > 70:
            menus.main_menu(self)
            return speak("Canceled. Your password must be less than 70 characters.")
        options.set("password", password)
        self.add_account_to_list(options.get("username"), password)
        if self.network:
            # Never join here: this handler runs inside the locked frame body
            # (input handlers are invoked from st.update under self.lock), and
            # the network worker takes that same lock around every received
            # packet. If it is parked on the lock, join() deadlocks the whole
            # client. The worker is a daemon: stop polling and queue the
            # terminator; it drains and exits on its own.
            self.network.put(("should_poll", False))
            self.network.put(None)
        try:
            self.network = self._new_network_client()
        except (OSError, server_config.ServerConfigError) as e:
            menus.main_menu(self)
            speak(self._connection_failure_message(e))
            return
        self.replace(self.creating)

    def creating(self):
        e = self.network.net.service(0)
        if not self.network.connected and e.type == enet.EVENT_TYPE_CONNECT:
            speak("Please wait. Creating your account...")
            self.network.send(
                consts.CHANNEL_MISC,
                "create",
                {
                    "username": options.get("username", ""),
                    "password": options.get("password", ""),
                    "version": consts.CLIENT_VERSION,  # 🔢 Version for compatibility check
                    "capabilities": ["music_timeline_v1"],
                },
            )
            return self.replace(self.network.loop)
        if (
            not self.network.connected
            and self.network.timeout_clock.elapsed >= consts.TIMEOUT
        ):
            self.network.timeout_clock.restart()
            self.connection_error()

    def exit(self):
        self.stack = []

    def new_clock(self):
        cl = clock.Clock()
        self.clocks.add(cl)
        return cl

    def loop(self):
        while True:
            # Continuously verify the anti-cheat shadow value
            # If Cheat Engine modifies this in memory, get() will crash the game!
            self.test_money.get()
            try:
                self.loop_function()
            except Exception as e:
                # Keep anti-cheat verification outside this handler on purpose.
                self.recover_from_exception(e, "Game main loop")

    def recover_from_exception(self, error, context):
        """Return to a safe menu after a recoverable Python exception."""
        if self._recovery_in_progress:
            return
        self._recovery_in_progress = True
        log_exception(error, context)
        # Persist before touching the network.  If its worker is already broken,
        # this report will be uploaded on the player's next authenticated login.
        try:
            from . import crash_reporting
            crash_reporting.queue_exception(error, context, "recovered")
            crash_reporting.send_pending(self)
        except Exception as report_error:
            log(f"[CRASH REPORT] Could not queue report: {report_error}")
        try:
            network = self.network
            self.network = None
            if network:
                # Never join here: its worker may be waiting on the game lock.
                network.put(None)
        except Exception as cleanup_error:
            log(f"[RECOVERY] Could not stop network worker: {cleanup_error}")
        try:
            gameplay = getattr(self, "gameplay", None)
            self.midi_hub.release_owner(gameplay, reason="crash_recovery")
            voice = getattr(gameplay, "voice_chat", None)
            if voice:
                voice.close()
        except Exception as cleanup_error:
            log(f"[RECOVERY] Could not close voice chat: {cleanup_error}")
        try:
            self.stack = []
            menus.main_menu(self)
            speak("A client error was recovered. You returned to the main menu. Please send client debug log to the developer.", False)
        except Exception as recovery_error:
            log_exception(recovery_error, "Game recovery")
        finally:
            self._recovery_in_progress = False

    def loop_function(self):
            queue_limit = 500
            while not self.queue.empty() and queue_limit > 0:
                queue_limit -= 1
                try:
                    value = self.get()
                except Exception:
                    break
                if value is None:
                    # another thread asked this thread to terminate, so lets break.
                    return False
                elif callable(value):
                    try:
                        value()
                    except Exception as e:
                        print(f"Error in game queue callback: {e}")
                elif isinstance(value, tuple):
                    # another thread asked to set a value on this class.
                    setattr(self, value[0], value[1])
            self.watchdog.heartbeat()
            with self.lock:
                self.update(self.delta)
                self.events = pygame.event.get()
                
                # Normalize Thai keyboard layout keycodes to English for hotkeys
                self.events = keyboard_layout.normalize_events(self.events)
                
                for event in self.events:
                    if (
                        event.type == pygame.KEYDOWN
                        and event.mod & pygame.KMOD_CTRL
                        and get_os() == consts.OS_LINUX
                    ):
                        speak("", True)
                self.audio_mngr.loop()
                for automation_task in self.automations:
                    automation_task.loop()
                if self.title_clock.elapsed >= 2500:
                    self.instance_mngr.update_title()
                    self.title_clock.restart()
                if self.device_clock.elapsed >= 10000:
                    device = options.get("audio_device", cyal.util.get_default_all_device_specifier())
                    if device == "system default": device = cyal.util.get_default_all_device_specifier()
                    try:
                        if not self.audio_mngr.context.is_connected: 
                            self.audio_mngr.context.device.reopen(name=device)
                    except cyal.exceptions.InvalidDeviceError:
                        self.audio_mngr.context.device.reopen(name=cyal.util.get_default_all_device_specifier())
                    if self.audio_mngr.context.device.output_name != device:
                        try: 
                            self.audio_mngr.context.device.reopen(name=device)
                        except cyal.exceptions.InvalidDeviceError:
                            pass
                    self.device_clock.restart()
                if options.get("mute_on_focus_loss", False):
                    if pygame.key.get_focused() and self.audio_mngr.context.device.paused: 
                        self.audio_mngr.context.device.resume()
                        self.audio_mngr.muted = False
                    elif not pygame.key.get_focused() and self.audio_mngr.context.device.playing: 
                        self.audio_mngr.context.device.pause()
                        self.audio_mngr.muted = True
                if len(self.stack) == 0:
                    self.presence_sounds.shutdown()
                    self.midi_hub.shutdown()
                    options.save()
                    self.keyconfig.save()
                    pygame.quit()
                    sys.exit()
                st = self.stack[-1]
                if isinstance(st, state.State):
                    st.update(self.events)
                elif callable(st):
                    st()
                self.last_fps = round(self.clock.get_fps())
                ids_to_remove = []
                for i in self.delayed_functions.copy():
                    if (
                        self.delayed_functions[i].clock.elapsed
                        >= self.delayed_functions[i].time
                    ):
                        if callable(self.delayed_functions[i].function):
                            self.delayed_functions[i].function()
                        ids_to_remove.append(i)
                for i in ids_to_remove:
                    del self.delayed_functions[i]
            self.delta = self.clock.tick(self.framerate)

    def update(self, delta):
        self.screen.fill("black")
        texts = self.make_text()
        for i, text in enumerate(texts):
            self.screen.blit(
                text,
                text.get_rect(
                    center=(
                        self.screen.get_width() // 2,
                        self.screen.get_height() // 3 + (50 * i),
                    )
                ),
            )
        pygame.display.update()
        for i in self.clocks:
            i.update(delta)

    def pop(self):
        with contextlib.suppress(IndexError):
            prev = self.stack.pop()
            if isinstance(prev, state.State):
                prev.exit()
            return prev

    def append(self, st):
        self.stack.append(st)
        if isinstance(st, state.State):
            st.enter()
        return st

    def replace(self, st):
        self.pop()
        return self.append(st)

    def disconnected(self):
        if self.network:
            self.network.put(None)
            self.network.join()
            self.network = None
        
        if getattr(self, "reconnecting", False):
            self.replace(self.reconnect_state)
        else:
            menus.main_menu(self)

    def connection_error(self):
        if getattr(self, "reconnecting", False):
            self.replace(self.reconnect_state)
        else:
            menus.main_menu(self)
            speak("Connection error [timeout]", False)

    def reconnect_state(self):
        if not hasattr(self, "reconnect_clock"):
            self.reconnect_clock = self.new_clock()
            self.reconnect_clock.elapsed = 3000  # Trigger immediately on first run
        
        if self.reconnect_clock.elapsed >= 3000:
            self.reconnect_clock.restart()
            speak("Connecting to the server. Please wait...", False)
            try:
                self.network = self._new_network_client()
                self.replace(self.login2)
            except (OSError, server_config.ServerConfigError):
                pass

    def cancel(self, message="Canceled."):
        self.pop()
        speak(message)

    def automate(self, object, attribute, target_value, time, callback=None, time_step=20, step_callback=None, start_value=None, cancelable=True):
        if cancelable and object is not None and attribute is not None:
            for task in list(self.automations):
                if task.object is object and task.attribute == attribute and task.cancelable:
                    self.automations.remove(task)
        
        task = automation.Automation_Task(
            self, object, attribute, target_value, time, time_step=time_step, callback=callback, step_callback=step_callback, start_value=start_value, cancelable=cancelable
        )
        self.automations.append(task)
        return task
    
    #a function that suspends input and blocking network and game threads without causing the app to stop presponding, when suspension is over, all incoming packets and events are processed.
    def suspend(self, secs):
        self.append(state.State(self))
        for i in range(0, int(secs / 0.02)):
            time.sleep(0.02)
            self.loop_function()
        self.pop()
