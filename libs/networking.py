import contextlib
import time
import json
import threading
import queue

import enet

from .consts import TIMEOUT
from . import consts

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
        self.address = enet.Address(host.encode(), port)
        self.net = enet.Host(None, 1, 256, 0, 0)
        self.peer = self.net.connect(self.address, 256)
        self.connected = False
        self.should_poll = False
        self.disconnected = False  # whether an unexpected disconnect happened
        self.start()

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
        event = self.net.service(0)
        if (
            not ignore_timeout
            and not self.connected
            and self.timeout_clock.elapsed >= TIMEOUT
        ):
            # timeout
            self.game.put(self.game.connection_error)
            self.disconnected = True
        elif not self.connected and event.type == enet.EVENT_TYPE_CONNECT:
            self.connected = True
        elif event.type == enet.EVENT_TYPE_DISCONNECT:
            self.connected = False
            self.disconnected = True
            self.game.put(self.game.disconnected)
        elif event.type == enet.EVENT_TYPE_RECEIVE:
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
