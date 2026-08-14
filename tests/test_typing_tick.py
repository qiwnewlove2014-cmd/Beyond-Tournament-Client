import unittest

from libs.virtual_input import Virtual_input

CHANNEL_SOUND = 1


class FakeClock:
    def __init__(self):
        self.elapsed = 0.0

    def restart(self):
        self.elapsed = 0.0


class FakeNetwork:
    def __init__(self):
        self.sent = []

    def send(self, channel, event, data=None, reliable=True):
        self.sent.append((channel, event, data))


class FakeGame:
    def __init__(self):
        self.network = FakeNetwork()
        self.input_history = [""]

    def new_clock(self):
        return FakeClock()


def make_input(**kwargs):
    game = FakeGame()
    return Virtual_input(game, **kwargs)


class TypingTickTests(unittest.TestCase):
    def test_tick_sent_while_typing(self):
        v = make_input()
        v.typing = True
        v._send_typing_tick()
        self.assertEqual(len(v.game.network.sent), 1)
        channel, event, data = v.game.network.sent[0]
        self.assertEqual(channel, CHANNEL_SOUND)
        self.assertEqual(event, "typing_sound")
        self.assertEqual(data, {})

    def test_throttled_to_one_tick_per_window(self):
        v = make_input()
        v.typing = True
        v._send_typing_tick()
        v._send_typing_tick()
        v._send_typing_tick()
        self.assertEqual(len(v.game.network.sent), 1)

    def test_no_tick_for_slash_commands(self):
        v = make_input(initial_msg="/help")
        v.typing = True
        v._send_typing_tick()
        self.assertEqual(v.game.network.sent, [])

    def test_no_tick_for_hidden_fields(self):
        v = make_input(password=True)
        v.typing = True
        v._send_typing_tick()
        self.assertEqual(v.game.network.sent, [])

    def test_no_tick_when_not_typing(self):
        v = make_input()
        v._send_typing_tick()
        self.assertEqual(v.game.network.sent, [])

    def test_no_tick_without_network(self):
        v = make_input()
        v.typing = True
        v.game.network = None
        v._send_typing_tick()  # must not raise
        self.assertEqual(getattr(v, "_last_typing_tick", 0.0), 0.0)


if __name__ == "__main__":
    unittest.main()
