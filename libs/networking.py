import contextlib
import time
import json
import threading
import queue

import enet

from .consts import TIMEOUT
from . import consts
from . import server_config

class Client(threading.Thread):
    def __init__(self, game, host, port, event_handeler):
        super().__init__(daemon=True)
        self.game = game
        self.timeout_clock = game.new_clock()
        self.host = host
        self.port = port
        self.queue = queue.SimpleQueue()
        self.get = self.queue.get_nowait
        self.event_handeler = event_handeler(self, self.game)
        # One lookup, here, and the answer is what the transport is built on: a
        # name with no answer is a NameLookupError -- an OSError, so every
        # "this attempt did not open" path still catches it -- instead of a bare
        # resolution failure at the menu that nobody could classify. A literal
        # is not looked up at all, and a name this machine's own resolver cannot
        # answer is put to a public resolver over HTTPS before it is given up on
        # (server_config.resolve_host_with_source).
        resolved, via = server_config.resolve_host_with_source(host)
        if resolved is None:
            raise server_config.NameLookupError(
                f"The server's name could not be looked up: {host}"
            )
        # The address this transport really went to, which is what a login may
        # never dial twice (game._open_candidate) and what a log line reports.
        self.resolved_host = resolved
        # How that address was found -- a literal, this machine's resolver, or a
        # public one. A player who only gets in through the public resolver has
        # a DNS problem, and this is the fact that says so.
        self.resolved_via = via
        self.address = enet.Address(resolved.encode(), port)
        self.net = enet.Host(None, 1, 256, 0, 0)
        self.peer = self.net.connect(self.address, 256)
        # The server accepted the transport (ENet connect event).
        self.connected = False
        # The login finished: the server's snapshot arrived. The login watchdog
        # below guards only the window before this is set -- it is what a slow
        # login needs (see login_timed_out).
        self.logged_in = False
        self.should_poll = False
        self.disconnected = False  # whether an unexpected disconnect happened
        self.closing = False  # close_socket already asked the worker to exit
        self.start()

    def note_server_activity(self):
        """Restart the login watchdog: the server said something, or we just
        asked it something it owes an answer to. Any sign of life keeps the
        attempt alive, however slow the machine behind it turns out to be.
        """
        self.timeout_clock.restart()

    def note_handshake(self):
        """The server accepted the connection (ENet connect event).

        Note this only says the transport is up. It must NOT disarm the login
        watchdog: whether the login is still pending is `logged_in`, not this.
        """
        self.connected = True
        self.note_server_activity()

    def login_timed_out(self):
        """True when the server has gone quiet for too long during a login.

        Silence is measured from the last sign of life, never from the moment
        the player pressed log in: a handshake, a database lookup and a map
        snapshot are all work the server may legitimately be slow at, and
        giving up on it mid-way left a session nobody owned (which then
        refused the player's next login for as long as ENet took to reap the
        peer). `logged_in` is what ends the window -- timeouts only ever cover
        a login that never finished.
        """
        return not self.logged_in and self.timeout_clock.elapsed >= TIMEOUT

    def close_socket(self, polite=True):
        """Release this client: ask the worker to disconnect and exit.

        Never joins -- callers run inside the locked frame body, where joining
        can deadlock (see tests/test_network_teardown.py). `polite` sends the
        server a real disconnect so it can drop the session at once instead of
        waiting for its own transport timeout to reap a peer that is still
        registered.
        """
        if self.closing:
            return
        self.closing = True
        if polite:
            self.put(self._request_disconnect)
        self.put(("should_poll", False))
        self.put(None)

    def _request_disconnect(self):
        """Runs inside the worker thread, which is the thread that services the
        ENet host, so the disconnect command is queued and flushed there."""
        try:
            self.peer.disconnect()
            self.net.flush()
        except Exception:
            pass

    def put(self, value):
        """puts value into the event queue to be processed. value could be one of the following:
        None: breaks the event loop. you can put that before joining the thread.
        callable(). calls the given callable inside this thread with no arguments. you could pass a lambda function if you want to call a function with arguments.
        tuple(string, any): set's the value of the variable named as the string given in [0] as the value given in [1]
        """
        self.queue.put_nowait(value)

    def run(self):
        while True:
            time.sleep(0.0002)
            if not self.queue.empty():
                value = self.get()
                if value is None:
                    # main thread asked this thread to terminate, so lets break.
                    self.net.flush()
                    break
                elif callable(value):
                    value()
                elif isinstance(value, tuple):
                    # the main thread asked to set a value on this class.
                    setattr(self, value[0], value[1])
            if self.should_poll and not self.disconnected:
                self.loop()

    def loop(self, ignore_timeout=False):
        try:
            event = self.net.service(0)
        except OSError as e:
            # The ENet socket died at the OS level (adapter change, sleep/resume,
            # VPN switch, abrupt network loss). That is a disconnect, not a code
            # crash: take the normal disconnect path instead of letting the
            # exception kill this worker thread.
            from .logger import log_exception
            log_exception(e, "Client.loop enet service")
            self.connected = False
            self.disconnected = True
            self.game.put(self.game.disconnected)
            return
        if not ignore_timeout and self.login_timed_out():
            # silence: give up on this login attempt
            self.game.put(self.game.connection_error)
            self.disconnected = True
        elif event.type == enet.EVENT_TYPE_CONNECT:
            self.note_handshake()
        elif event.type == enet.EVENT_TYPE_DISCONNECT:
            self.connected = False
            self.disconnected = True
            self.game.put(self.game.disconnected)
        elif event.type == enet.EVENT_TYPE_RECEIVE:
            self.note_server_activity()
            try:
                data = None
                if event.channelID < consts.CHANNEL_VOICECHAT: 
                    if not event.packet.data: return
                    data = json.loads(event.packet.data)
                    if not isinstance(data, dict) or not hasattr(self.event_handeler, data.get("event", "")): return
                elif event.channelID >= consts.CHANNEL_VOICECHAT: data = event.packet.data
                with self.game.lock:
                    self.handle_event(data, event.channelID)
            except Exception as e:
                from .logger import log_exception
                log_exception(e, f"Client.loop packet receive (channel={event.channelID})")

    def handle_event(self, data, channelID):
        try:
            if channelID == consts.CHANNEL_MUSICBOT:
                return self.event_handeler.process_music_data(data)
            elif channelID == consts.CHANNEL_MUSICBOT_TIMELINE:
                return self.event_handeler.process_music_timeline_data(data)
            elif channelID == consts.CHANNEL_JUKEBOX_RELAY:
                return self.event_handeler.process_jukebox_relay(data)
            elif channelID < consts.CHANNEL_VOICECHAT:
                event_name = data.get("event")
                if not event_name:
                    return
                handler = getattr(self.event_handeler, event_name, None)
                if handler:
                    return handler(data.get("data"))
            elif channelID >= consts.CHANNEL_VOICECHAT:
                return self.event_handeler.process_voice_data(data, channelID)
        except Exception as e:
            from .logger import log_exception
            log_exception(e, f"handle_event (channel={channelID}, event={data.get('event') if isinstance(data, dict) else 'raw'})")

    def send(self, channel, event, data=None, reliable=True):
        # a function that will just tell the thread to send a packet for thread safety. it shouldn't be called inside the thread.
        self.put(lambda: self.send2(channel, event, data, reliable))

    def send2(self, channel, event, data=None, reliable=True):
        # actually send's a packet. this should only be called inside the thread.
        if data is None:
            data = {}
        if channel < consts.CHANNEL_VOICECHAT: data = json.dumps({"event": event, "data": data}).encode()
        else: data = bytes(data)
        # Audio channels (normal voice, music bot, megaphone; >= CHANNEL_VOICECHAT)
        # are real-time: force UNRELIABLE so a lost packet drops cleanly and the
        # stream keeps flowing. A reliable audio packet that gets lost stalls the
        # whole queue behind an ENet retransmission (head-of-line blocking), which
        # turns one dropped frame into a 100-500ms latency spike for every
        # subsequent frame. The client jitter buffer already absorbs/drops old
        # frames, so retransmitting audio is never useful.
        if channel >= consts.CHANNEL_VOICECHAT:
            reliable = False
        packet = enet.Packet(
            data,
            flags=(
                enet.PACKET_FLAG_RELIABLE
                if reliable
                else enet.PACKET_FLAG_UNRELIABLE_FRAGMENT
            ),
        )
        self.peer.send(channel, packet)
        # The server owes us an answer from this moment on: a login request
        # that is slow to be answered must not look like a dead server.
        self.note_server_activity()
