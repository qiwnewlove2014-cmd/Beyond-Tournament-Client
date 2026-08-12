"""In-process test: O key toggle (PA Test Mode) must route a broadcasting
music bot to the megaphone when PA turns ON and back to the normal channel
when PA turns OFF (and release/acquire the megaphone lock accordingly).
"""
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ---- mocks -------------------------------------------------------------
class FakeNetwork:
    def __init__(self):
        self.sent = []
    def send(self, channel, event, data):
        self.sent.append((channel, event, data))

class FakeGame:
    def __init__(self):
        self.network = FakeNetwork()
        self.direct_soundgroup = types.SimpleNamespace(play=lambda *a, **k: None)

class FakeMegaphone:
    sources = [object()]  # non-empty -> PA speakers available

class FakeVoiceChat:
    recording = False
    vc_compression = None

class FakeMusicBot:
    def __init__(self):
        self.broadcast_enabled = True
        self.broadcast_to_megaphone = False

class FakeKC:
    def get(self, key, default):
        return default

def build_gameplay():
    from libs import consts, gameplay  # noqa
    gp = gameplay.Gameplay.__new__(gameplay.Gameplay)
    gp.game = FakeGame()
    gp.game_started = False
    gp.megaphone = FakeMegaphone()
    gp.map = types.SimpleNamespace(megaphone_speakers=[object()])
    gp.voice_channels = {consts.CHANNEL_MEGAPHONE: object()}
    gp.pa_test_mode = False
    gp.kc = FakeKC()
    gp.music_bot = FakeMusicBot()
    gp.voice_chat = FakeVoiceChat()
    gp.is_staff = True
    gp.is_builder = False
    gp.is_technician = False
    gp.can_broadcast_megaphone = False
    return gp

def main():
    from libs import consts
    gp = build_gameplay()

    # --- initial state ---
    assert gp.pa_test_mode is False
    assert gp.music_bot.broadcast_to_megaphone is False
    assert gp.music_bot.broadcast_enabled is True

    # --- press O (ON): PA on, music -> megaphone, lock acquired ---
    gp._finish_pa_toggle()
    assert gp.pa_test_mode is True, "PA should be ON"
    assert gp.music_bot.broadcast_to_megaphone is True, "music should route to megaphone"
    assert (consts.CHANNEL_MISC, "megaphone_broadcast_lock", {"locked": True}) in gp.game.network.sent, \
        "lock acquire should be sent"
    print("PASS 1: O ON -> PA on + music routed to megaphone + lock acquired")

    # --- press O again while still broadcasting (OFF): back to normal channel ---
    gp._finish_pa_toggle()
    assert gp.pa_test_mode is False, "PA should be OFF"
    assert gp.music_bot.broadcast_to_megaphone is False, "music should go back to normal channel"
    assert (consts.CHANNEL_MISC, "megaphone_broadcast_lock", {"locked": False}) in gp.game.network.sent, \
        "lock release should be sent"
    print("PASS 2: O OFF -> PA off + music back to normal channel + lock released")

    # --- press O again (ON) but music bot NOT broadcasting -> no routing change ---
    gp.music_bot.broadcast_enabled = False
    gp.music_bot.broadcast_to_megaphone = False
    gp._finish_pa_toggle()
    assert gp.pa_test_mode is True
    assert gp.music_bot.broadcast_to_megaphone is False, "non-broadcasting bot must not be routed"
    print("PASS 3: O ON with broadcast disabled -> PA on but music untouched")

    # --- O OFF with bot that was NOT routed -> no spurious lock release beyond state ---
    gp.music_bot.broadcast_enabled = True
    gp._finish_pa_toggle()
    assert gp.pa_test_mode is False
    assert gp.music_bot.broadcast_to_megaphone is False
    print("PASS 4: O OFF with clean bot -> stays on normal channel")

    print("ALL PASS: O key toggles music bot routing both ways correctly")

if __name__ == "__main__":
    main()
