"""Regression tests: network teardown must never block the main thread while
game.lock is held.

Root cause of the intermittent freeze ("game freezes when a chat message is
sent while a map transition is in flight"):

- game.py loop_function holds game.lock across the whole frame, including
  st.update().
- networking.Client.run -> loop acquires that SAME lock around every received
  packet (chat echo / map data).
- gameplay.exit() (invoked by game.pop() inside the locked frame) used to call
  network.join() while still holding the lock. If the network worker was parked
  on `with self.game.lock:` processing a just-arrived packet, it could never
  reach the None terminator, so join() waited forever -> permanent freeze.

The fix: teardown stops the worker's polling and queues the terminator but
never joins (the worker is a daemon and drains within milliseconds).
"""

import json
import os
import queue
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import enet

from libs import networking


class GameStub:
    """Minimal stand-in for Game: lock + queue + put, like the real Game."""

    def __init__(self):
        self.lock = threading.RLock()
        self.queue = queue.SimpleQueue()
        self.get = self.queue.get_nowait

    def new_clock(self):
        class _Clock:
            def __init__(self):
                self.elapsed = 0.0

        return _Clock()

    def put(self, value):
        self.queue.put_nowait(value)

    def connection_error(self):
        pass

    def disconnected(self):
        pass


class FakeEventHandler:
    """Event handler invoked from the real Client.loop receive branch."""

    def __init__(self, client, game):
        self.client = client
        self.game = game

    def ping(self, data):
        # Runs inside `with self.game.lock:` (see Client.loop). Sleeping here
        # widens the window so the main thread can grab the lock mid-handler,
        # exactly like a slow chat-echo handler during a transition burst.
        time.sleep(0.05)


class FakeNet:
    """Minimal enet host stub: every service() returns one receive event."""

    def __init__(self):
        payload = json.dumps({"event": "ping", "data": {}}).encode()
        self._packet = enet.Packet(payload, flags=enet.PACKET_FLAG_RELIABLE)

    def service(self, timeout):
        class Event:
            type = enet.EVENT_TYPE_RECEIVE
            channelID = 0
            packet = self._packet

        return Event()

    def flush(self):
        pass


def make_parked_client():
    """Return (game, client) with the network worker parked on game.lock in
    its receive branch (the exact freeze precondition)."""
    game = GameStub()
    client = networking.Client(game, "127.0.0.1", 1, FakeEventHandler)
    client.should_poll = False  # thread starts idle; deterministic from here
    client.net = FakeNet()
    game.lock.acquire()  # main thread: start of a frame (holds the lock)
    try:
        client.put(("should_poll", True))
        # The worker polls, gets the fake packet and parks on `with
        # self.game.lock:` — chat echo / map packet arriving mid-frame.
        deadline = time.time() + 2.0
        while time.time() < deadline:
            if not client.is_alive():
                break
            # Give the worker time to reach the lock wait.
            time.sleep(0.02)
        return game, client
    except Exception:
        game.lock.release()
        raise


class TestNetworkTeardown(unittest.TestCase):
    def tearDown(self):
        # Always release the frame lock so the daemon worker can exit.
        try:
            self._game.lock.release()
        except (RuntimeError, AttributeError):
            pass

    def test_non_blocking_teardown_does_not_deadlock(self):
        """The teardown used by gameplay.exit() must return immediately even
        when the network worker is parked on game.lock (the old join() froze
        the client here forever)."""
        game, client = make_parked_client()
        self._game = game
        # Confirm the worker really is parked on the lock: it must be alive
        # and NOT exit while we still hold the lock.
        self.assertTrue(client.is_alive(), "worker should still be running")

        # gameplay.exit() teardown (non-blocking): stop polling + terminate.
        started = time.monotonic()
        client.put(("should_poll", False))
        client.put(None)
        elapsed = time.monotonic() - started
        self.assertLess(
            elapsed, 0.5,
            "teardown blocked the main thread while holding game.lock",
        )

        # After the frame releases the lock, the daemon worker drains the
        # terminator and exits on its own.
        game.lock.release()
        self._game = None
        client.join(timeout=2.0)
        self.assertFalse(client.is_alive(), "worker should have exited")

    def test_join_under_lock_is_the_deadlock(self):
        """Sanity: the OLD pattern (join while holding the lock with the
        worker parked) hangs — this is what the fix removes."""
        game, client = make_parked_client()
        self._game = game
        self.assertTrue(client.is_alive())
        client.put(None)
        client.join(timeout=0.6)
        self.assertTrue(
            client.is_alive(),
            "join under lock must hang while the worker waits on game.lock",
        )


if __name__ == "__main__":
    unittest.main()
