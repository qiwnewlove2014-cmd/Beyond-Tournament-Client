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
    login_attempts,
)
from .speech import speak
from .logger import log, log_exception
from .audio_diagnostics import probe as audio_probe

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
        self._restart_in_progress = False
        
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
        if action == "restart_client":
            # The entry point completed this hand-off before Pygame/OpenAL
            # initialization, so no second wait or process termination is
            # needed here.
            return
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

    def _new_network_client(self, address=None):
        """A fresh transport for one login candidate.

        ``address`` is the ``(host, port)`` one candidate names -- the
        endpoint's own name, or a backup address a release build embedded beside
        it (libs/login_attempts.py). The port travels with the address so a
        retry can be a new socket on the same name: a new local port means a new
        NAT mapping, which is what a stale one needs. Without an address the
        configured endpoint is opened, which is what a caller that is not
        walking a list gets.
        """
        if address is None:
            host, port = server_config.get_server_endpoint()
        else:
            host, port = address
        return networking.Client(
            self,
            host,
            port,
            event_handeler.EventHandeler,
        )

    def _open_first_login_attempt(self):
        """Open the transport a login starts on, and remember the retry list.

        The list is the endpoint and the port above it
        (``libs/login_attempts.py``), with the port that last answered moved
        to the front: a player whose way in is the fallback should not pay a
        silence window on every login to rediscover it. Both the login button
        and a reconnect come through here, so a retry after a mid-session drop
        knows where it may go exactly as a fresh login does. Raises whatever
        stops the last candidate, which is what both callers already report.
        """
        host, port = server_config.get_server_endpoint()
        self._login_name = host
        self._login_candidates = login_attempts.candidate_addresses(
            host,
            port,
            server_config.get_fallback_addresses(),
            preferred=options.get_login_port(),
        )
        self._login_port = None
        # A fresh walk: nothing has been dialled, nothing is known to be
        # unfindable, and no backup address has been reached yet.
        self._login_facts = None
        if not self._login_candidates:
            raise OSError("There is no login candidate to open.")
        if not self._open_candidate(0):
            raise self._last_open_error

    def _open_candidate(self, index):
        """Open the first candidate at or after ``index``, and say which.

        A candidate this machine cannot use is walked past rather than reported:
        a name that could not be looked up (the same name is not asked twice --
        ``login_attempts.candidate_available``), a port this machine will not
        open a socket for, and an address this login has already dialled under
        another name. The configured port is exactly where a filter shows up
        first, which is why the walk exists at all. The retry notice is spoken
        only for a *later* candidate, so a login that opens where it always did
        stays as quiet as it always was.

        False means none of them would open; the error that stopped the last one
        is kept for the caller to report.
        """
        candidates = tuple(getattr(self, "_login_candidates", ()))
        facts = self._login_walk()
        last_error = None
        while index < len(candidates):
            candidate = candidates[index]
            if not login_attempts.candidate_available(
                candidate, facts["unresolvable"], facts["doors"]
            ):
                index += 1
                continue
            try:
                self.network = self._new_network_client(candidate)
            except server_config.NameLookupError as error:
                # One answer for one name: every other candidate that shares it
                # is retired, so a login with a backup address does not spend a
                # silence window per port on a name that has no answer at all.
                facts["unresolvable"].add(str(candidate[0]).lower())
                facts["lookup_failed"] = True
                self.network = None
                last_error = error
                index += 1
                continue
            except (OSError, server_config.ServerConfigError) as error:
                self.network = None
                last_error = error
                index += 1
                continue
            self._login_try = index
            self._login_port = candidate[1]
            # The notice is spoken before this door joins the ones already
            # dialled, so its count is the doors behind it plus the ones ahead.
            # A first-door walk says nothing: a name with no answer was never a
            # door, and "trying the connection again" over one this machine
            # never reached is a retry the player did not have. When the login
            # really has been through something, both numbers count doors, so
            # what the player reads out to staff matches the log's tried= list.
            attempt, total = login_attempts.retry_position(
                candidates, index, facts["unresolvable"], facts["doors"]
            )
            if attempt > 1:
                speak(login_attempts.retry_notice(attempt, total), False)
            facts["dialled"].append(candidate)
            facts["doors"].add((str(candidate[0]).lower(), candidate[1]))
            resolved = getattr(self.network, "resolved_host", None)
            if resolved:
                # The door by address as well as by name: a backup that is what
                # this name resolved to is recognised as the same door, while
                # the port above it is still a door nothing has been sent to.
                facts["doors"].add((str(resolved).lower(), candidate[1]))
            # How this door's address was found -- a literal, this machine's
            # resolver, or a public one. The login line reports it, because a
            # player who only gets in through the public resolver has a DNS
            # problem that nothing else in the game would ever say out loud.
            facts["lookup"] = getattr(self.network, "resolved_via", None)
            if login_attempts.is_backup(candidate, getattr(self, "_login_name", "")):
                facts["backup"] = True
            return True
        self._login_try = index
        self._last_open_error = last_error
        return False

    def _silence_report(self):
        """The last word when every candidate was tried and stayed silent."""
        facts = self._login_walk()
        candidates = tuple(getattr(self, "_login_candidates", ()))
        # The ports of what was really dialled, or -- for a walk that never got
        # that far -- of what this login was going to try.
        ports = [port for _, port in (facts["dialled"] or candidates)]
        return login_attempts.silence_message(
            max(len(facts["dialled"]), 1), ports, backup_tried=facts["backup"]
        )

    def _login_walk(self):
        """The facts of the walk this login is on, created on first use.

        One dictionary rather than five attributes on the Game: what happened is
        one thing, and a hand-built Game -- a test, a reconnect that never opened
        anything -- has none of it.
        """
        facts = getattr(self, "_login_facts", None)
        if facts is None:
            facts = {
                "unresolvable": set(),  # names this machine could not look up
                "dialled": [],  # every (host, port) really dialled
                "doors": set(),  # those doors as (host-or-address, port) pairs
                "lookup_failed": False,  # a name was asked for and had no answer
                "lookup": None,  # how the opened door's address was found
                "backup": False,  # a candidate that was not the endpoint's name
            }
            self._login_facts = facts
        return facts

    def _login_failure_words(self, error=None):
        """What the player hears about the login that just failed, from the facts.

        Most specific first: a name that could not be looked up is a different
        failure from a server that never answered, and it is the one the player
        can act on (a mobile hotspot, a different DNS) and the one the Server can
        never see. Anything else keeps the line it always had.
        """
        facts = self._login_walk()
        if facts["lookup_failed"]:
            # A name this machine cannot answer has already been put to a public
            # resolver before we get here (server_config.resolve_host), when the
            # build has one -- so the sentence may say so, and says nothing when
            # it does not.
            return login_attempts.resolution_message(
                backup_tried=facts["backup"],
                public_resolver_tried=server_config.public_lookup_enabled(),
            )
        if error is not None:
            return self._connection_failure_message(error)
        return self._silence_report()

    def _report_login_failure(self, error=None):
        """Say why the login failed, and write the one line that records it.

        The Server only ever records the attempts that *arrived*, and the failure
        this exists for is the one where nothing arrived at all: the player is
        told to ask staff to read the connection log, and the log has nothing to
        read. This machine is the only witness, so it keeps one line whatever the
        outcome (``login_attempts.attempt_report``) -- and the sentence the
        player hears is built from those same facts.
        """
        facts = self._login_walk()
        log(
            login_attempts.attempt_report(
                getattr(self, "_login_name", None),
                facts["dialled"],
                lookup_failed=facts["lookup_failed"],
                lookup="none",
                backup_tried=facts["backup"],
                error=error,
            )
        )
        return self._login_failure_words(error)

    def _report_login_reached(self):
        """One line for the login that *did* open: which door let this machine in.

        The half of the record the Server cannot give. A player whose login only
        works through a backup address has a name their resolver will not answer,
        and nothing else in the game would ever say so -- the login simply works,
        a few seconds slower -- so this line is where that machine's DNS problem
        is known first.
        """
        facts = self._login_walk()
        log(
            login_attempts.attempt_report(
                getattr(self, "_login_name", None),
                facts["dialled"],
                resolved=getattr(self.network, "resolved_host", None),
                answered=facts["dialled"][-1] if facts["dialled"] else None,
                lookup=facts["lookup"],
                backup_tried=facts["backup"],
            )
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
        # A client from an earlier attempt may still be around (a login that
        # gave up used to leave its socket open and unserviced). Release it
        # before a new one takes its place, or the two talk to one account.
        self._close_network()
        # Said before the walk, not after it: opening the first candidate is
        # where a retry notice can be spoken (a configured port this machine
        # will not open, a name that had to be fallen back from), and "trying
        # the connection again" before "connecting to the server" reads as a
        # retry that never happened. The sentence is the same either way; only
        # its place in the order changed.
        speak("Connecting to the server. Please wait...")
        try:
            self._open_first_login_attempt()
        except (OSError, server_config.ServerConfigError) as e:
            self.pop()
            menus.main_menu(self)
            speak(self._report_login_failure(e))
            return
        self.replace(self.login2)

    def _retry_login(self):
        """Walk to the next candidate, or report that none of them answered.

        Only ever reached from ``login2``, where the wait is for the
        *handshake*: nothing the server said has been answered, so this is a
        connection that never opened rather than a login that went quiet. A
        login the server accepted and then left silent belongs to
        ``Client.loop``'s watchdog, which owns that window and never retries it
        -- one rule, one owner, and a struggling server is not hammered.

        Walking is ``_open_candidate``: a candidate this machine cannot use is a
        reason to try the next one, not to report, and only a walk that is spent
        is worth reporting. What is reported then -- by word and by log line --
        is the most specific thing known (``_report_login_failure``): a name that
        could not be looked up, else the error that stopped the last candidate,
        else the silence that covered them all.
        """
        self._close_network()
        if not self._open_candidate(getattr(self, "_login_try", 0) + 1):
            return self.connection_error(
                self._report_login_failure(getattr(self, "_last_open_error", None))
            )
        return self.replace(self.login2)

    def _remember_login_port(self):
        """Keep the port that answered, so the next login starts there.

        Only ever reached once the handshake came back, which is the proof that
        this machine can reach the server on that port. A filter that drops one
        port and lets the other through does not change between logins, so
        paying a silence window to rediscover it every time would be the
        fallback's own cost; the value is an ordering hint and nothing else
        (``candidate_ports`` ignores anything that is not a candidate here).

        The account-creation flow opens its own socket outside this list, so it
        deliberately has no say here: the port a login answered on is the only
        one this remembers.
        """
        port = getattr(self, "_login_port", None)
        if port is None or port == options.get_login_port():
            return
        options.set_login_port(port)

    def login2(self):
        """Wait for the transport, then ask the server to log this account in.

        Both waits (handshake and login) are covered by the client's own
        silence watchdog: it restarts on every packet from the server and every
        request sent, so a slow server is waited for instead of being declared
        dead mid-login (see networking.Client.login_timed_out).
        """
        e = self.network.net.service(0)
        if e.type == enet.EVENT_TYPE_CONNECT:
            self.network.note_handshake()
            # The port answered, so this machine starts with it next time.
            self._remember_login_port()
            # ... and which door it was is the half of the record the Server
            # cannot keep (a login that only works through a backup address).
            self._report_login_reached()
            speak("Logging in. Please wait...")
            self.network.send(
                consts.CHANNEL_MISC,
                "login",
                {
                    "username": options.get("username"),
                    "password": options.get("password"),
                    "version": consts.CLIENT_VERSION,  # 🔢 Version for compatibility check
                    "capabilities": ["music_timeline_v1", "jam_notes_v1"],
                },
            )
            return self.replace(self.network.loop)
        if self.network.login_timed_out():
            # The handshake never came back. That is a connection that did not
            # open, and it is worth one more socket before it is reported.
            self._retry_login()

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
        if e.type == enet.EVENT_TYPE_CONNECT:
            self.network.note_handshake()
            speak("Please wait. Creating your account...")
            self.network.send(
                consts.CHANNEL_MISC,
                "create",
                {
                    "username": options.get("username", ""),
                    "password": options.get("password", ""),
                    "version": consts.CLIENT_VERSION,  # 🔢 Version for compatibility check
                    "capabilities": ["music_timeline_v1", "jam_notes_v1"],
                },
            )
            return self.replace(self.network.loop)
        # No second timeout lives here: Client.loop owns the silence watchdog
        # once the request is out (it restarts on every packet from the
        # server), so one rule decides when an attempt is given up on.
        if self.network.login_timed_out():
            self.connection_error()

    def exit(self):
        self.stack = []

    def start_exit_fade(self, on_faded=None, exit_after=True, announce="Exiting"):
        """Fade all audio to silence over ~1.5s, then run ``on_faded``.

        Announces ``announce`` ("Exiting" by default) and ramps the OpenAL
        listener gain (the global output gain) down to 0. When the fade
        completes, ``on_faded`` runs (if given); with ``exit_after``
        (default) the process then exits, otherwise the caller keeps control
        (e.g. the in-game logout flow, which returns to the main menu and
        announces "Disconnecting" instead). The saved master-volume option
        is never touched. Returns False if a fade is already running.
        """
        if getattr(self, "_exit_fade_started", False):
            return False
        self._exit_fade_started = True
        speak(announce)
        master = self.audio_mngr.volume_categories["master"][0]

        def step(value):
            try:
                # Listener gain is the global output gain (see set_volume):
                # scaling it fades every audio category together.
                self.audio_mngr.listener.gain = value / 100
            except Exception:
                pass

        def done():
            try:
                if on_faded is not None:
                    on_faded()
            except Exception:
                pass
            # Allow a later fade (e.g. exiting from the main menu after an
            # in-game logout fade) to run again.
            self._exit_fade_started = False
            if exit_after:
                self.exit()

        self.automate(
            None, None, 0.0, 1500,
            step_callback=step,
            callback=done,
            start_value=master,
            cancelable=False,
        )
        return True

    def fade_out_and_exit(self):
        """Fade all audio to silence, then exit the game (main menu Exit).

        Used by the main menu's Exit action (and the Esc shortcut that
        triggers it). The menu is swapped for an inert state so no input can
        interrupt the fade.
        """
        if not self.start_exit_fade():
            return
        # Swap the menu for an inert state that blocks all input while the
        # fade plays out.
        self.replace(state.State(self))

    def ask_to_restart_client(self):
        """Open an accessible confirmation before replacing this process."""
        if self._restart_in_progress:
            return
        restart_menu = menu.Menu(
            self,
            "Restart the client? This closes and reopens the game to clear all audio and client resources.",
        )
        menus.set_default_sounds(restart_menu)
        restart_menu.add_items((
            ("Yes, restart the client", self.restart_client),
            ("No, return to the main menu", lambda: menus.main_menu(self)),
        ))
        self.replace(restart_menu)

    @staticmethod
    def _restart_launch_command():
        """Build argv for the existing old-process hand-off protocol.

        A compiled build relaunches its executable directly. Source mode must
        put the Python script after the interpreter. In both cases the child
        receives exactly ``restart_client, cwd, old_pid`` in ``sys.argv[1:]``.
        """
        from .logger import is_compiled
        command = [sys.executable]
        if not is_compiled():
            command.append(os.path.abspath(sys.argv[0]))
        command.extend(("restart_client", os.getcwd(), str(os.getpid())))
        return command

    def _launch_restarted_client(self):
        """Spawn the replacement non-blockingly, then close this Client."""
        try:
            from . import crash_reporting
            # The replacement may terminate us before normal ``finally`` and
            # atexit handlers finish. Remove this session marker first so an
            # intentional restart is never uploaded as an unclean exit.
            crash_reporting.mark_expected_shutdown("client_restart")
            command = self._restart_launch_command()
            subprocess.Popen(
                command,
                cwd=os.getcwd(),
                creationflags=(
                    getattr(subprocess, "CREATE_NO_WINDOW", 0)
                    if sys.platform == "win32" else 0
                ),
            )
            log(f"[RESTART] Replacement Client launched for PID {os.getpid()}")
            # Normal main-loop shutdown saves settings and closes Pygame. The
            # child also waits for/kills this PID if native cleanup gets stuck.
            self.exit()
        except Exception as error:
            log_exception(error, "Restart Client launch")
            self._restart_in_progress = False
            self._exit_fade_started = False
            # mark_expected_shutdown removed our live marker. Re-create it if
            # spawning failed and this process must keep running.
            with contextlib.suppress(Exception):
                from . import crash_reporting
                crash_reporting.begin_session()
            menus.main_menu(self)
            speak("The client could not restart. You returned to the main menu.", True)

    def restart_client(self):
        """Fade out, relaunch the process, and fully release Client resources."""
        if self._restart_in_progress:
            return False
        self._restart_in_progress = True
        if not self.start_exit_fade(
                on_faded=self._launch_restarted_client,
                exit_after=False,
                announce="Restarting client"):
            self._restart_in_progress = False
            return False
        # Block menu input while the uninterruptible fade/relaunch completes.
        self.replace(state.State(self))
        return True

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

    @audio_probe.frame
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
                        audio_probe.call("game.callback", value)
                    except Exception as e:
                        print(f"Error in game queue callback: {e}")
                elif isinstance(value, tuple):
                    # another thread asked to set a value on this class.
                    setattr(self, value[0], value[1])
            self.watchdog.heartbeat()
            with self.lock:
                audio_probe.call("game.display_clocks", self.update, self.delta)
                self.events = audio_probe.call("game.events", pygame.event.get)
                
                # Normalize Thai keyboard layout keycodes to English for hotkeys
                self.events = audio_probe.call("game.keys", keyboard_layout.normalize_events, self.events)
                
                for event in self.events:
                    if (
                        event.type == pygame.KEYDOWN
                        and event.mod & pygame.KMOD_CTRL
                        and get_os() == consts.OS_LINUX
                    ):
                        speak("", True)
                self.audio_mngr.loop()
                for automation_task in self.automations:
                    audio_probe.call("game.automation", automation_task.loop)
                if self.title_clock.elapsed >= 2500:
                    self.instance_mngr.update_title()
                    self.title_clock.restart()
                if self.device_clock.elapsed >= 10000:
                    device = options.get("audio_device", cyal.util.get_default_all_device_specifier())
                    if device == "system default":
                        device = cyal.util.get_default_all_device_specifier()
                    try:
                        if not self.audio_mngr.context.is_connected:
                            try:
                                self.audio_mngr.context.device.reopen(name=device)
                            except Exception:
                                default_dev = cyal.util.get_default_all_device_specifier()
                                if default_dev and default_dev != device:
                                    with contextlib.suppress(Exception):
                                        self.audio_mngr.context.device.reopen(name=default_dev)
                        elif self.audio_mngr.context.device.output_name != device:
                            try:
                                self.audio_mngr.context.device.reopen(name=device)
                            except Exception:
                                pass
                    except Exception as e:
                        log(f"[AUDIO] Device reopen watchdog skipped: {e}")
                    self.device_clock.restart()
                if options.get("mute_on_focus_loss", False):
                    if pygame.key.get_focused() and self.audio_mngr.context.device.paused: 
                        self.audio_mngr.context.device.resume()
                        self.audio_mngr.muted = False
                    elif not pygame.key.get_focused() and self.audio_mngr.context.device.playing: 
                        self.audio_mngr.context.device.pause()
                        self.audio_mngr.muted = True
                if len(self.stack) == 0:
                    self.audio_mngr.instrument_samples.close()
                    self.audio_mngr.crossed_samples.close()
                    self.audio_mngr.map_sounds.close()
                    self.presence_sounds.shutdown()
                    self.midi_hub.shutdown()
                    options.save()
                    self.keyconfig.save()
                    pygame.quit()
                    sys.exit()
                st = self.stack[-1]
                if isinstance(st, state.State):
                    audio_probe.call("game.state", st.update, self.events)
                elif callable(st):
                    audio_probe.call("game.state", st)
                self.last_fps = round(self.clock.get_fps())
                ids_to_remove = []
                for i in self.delayed_functions.copy():
                    if (
                        self.delayed_functions[i].clock.elapsed
                        >= self.delayed_functions[i].time
                    ):
                        if callable(self.delayed_functions[i].function):
                            audio_probe.call("game.delayed", self.delayed_functions[i].function)
                        ids_to_remove.append(i)
                for i in ids_to_remove:
                    del self.delayed_functions[i]
            # High performance mode: 120fps halves audio/queue latency
            # (~8ms average) and speeds up key response. Read live from the
            # options dict so toggling it in the menu takes effect instantly.
            self.delta = audio_probe.pace(self.clock.tick,
                120 if options.get("high_framerate", False) else self.framerate
            )

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
        try:
            clocks_snapshot = list(self.clocks)
        except RuntimeError:
            clocks_snapshot = []
        for i in clocks_snapshot:
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

    def _close_network(self, polite=True):
        """Release the network client without joining its worker.

        Joining inside the locked frame body can deadlock (see
        tests/test_network_teardown.py), so this only asks the worker to
        disconnect and exit. `polite` tells the server to drop the session
        now instead of waiting for its own transport timeout to reap a peer
        that still looks connected.
        """
        network = self.network
        self.network = None
        if network is not None:
            network.close_socket(polite=polite)

    def connection_error(self, message="Connection error [timeout]"):
        """Drop the attempt and say why, in the most specific words we have.

        The caller may pass the line the player should hear (a silent handshake
        that survived its retries is not the same failure as a login the server
        abandoned mid-way), but the default stays for the generic case and for
        the queued fallback ``Client.loop`` uses when it owns the timeout.
        """
        # Drop the connection rather than leaving it open and unserviced: a
        # half-finished login used to keep a session on the server that the
        # player could not get back into ("user already logged in") for as long
        # as ENet took to time the peer out.
        self._close_network()
        if getattr(self, "reconnecting", False):
            self.replace(self.reconnect_state)
        else:
            menus.main_menu(self)
            speak(message, False)

    def reconnect_state(self):
        if not hasattr(self, "reconnect_clock"):
            self.reconnect_clock = self.new_clock()
            self.reconnect_clock.elapsed = 3000  # Trigger immediately on first run
        
        if self.reconnect_clock.elapsed >= 3000:
            self.reconnect_clock.restart()
            speak("Connecting to the server. Please wait...", False)
            # The attempt that got us here may still hold a socket (a no-op when
            # connection_error already released it): never leave one open while
            # its replacement takes over.
            self._close_network()
            try:
                self._open_first_login_attempt()
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
