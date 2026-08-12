"""In-process test: the O key (PA Test Mode) must NOT route a broadcasting
music bot to the megaphone. "Broadcast to Megaphone" is an independent
toggle inside the Music Bot menu that the performer turns ON/OFF themselves
- pressing O must not silently force it ON (or OFF).
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

    # --- press O (ON): PA on, but music bot routing untouched ---
    gp._finish_pa_toggle()
    assert gp.pa_test_mode is True, "PA should be ON"
    assert gp.music_bot.broadcast_to_megaphone is False, \
        "O must NOT force broadcast_to_megaphone ON"
    assert not any(ev == ("megaphone_broadcast_lock",) and d.get("locked") is True
                   for _, ev, d in gp.game.network.sent if isinstance(d, dict)), \
        "O ON must not acquire the megaphone lock on behalf of the music bot"
    print("PASS 1: O ON -> PA on, music bot routing untouched (no forced megaphone)")

    # --- press O again (OFF): PA off, music bot still untouched ---
    gp._finish_pa_toggle()
    assert gp.pa_test_mode is False, "PA should be OFF"
    assert gp.music_bot.broadcast_to_megaphone is False, \
        "O OFF must not touch broadcast_to_megaphone"
    print("PASS 2: O OFF -> PA off, music bot routing still untouched")

    # --- O must also not turn OFF a megaphone routing the performer set ---
    gp.music_bot.broadcast_to_megaphone = True  # performer enabled it in the menu
    gp._finish_pa_toggle()
    assert gp.pa_test_mode is True
    assert gp.music_bot.broadcast_to_megaphone is True, \
        "O ON must not revoke a megaphone routing set by the performer"
    gp._finish_pa_toggle()
    assert gp.pa_test_mode is False
    assert gp.music_bot.broadcast_to_megaphone is True, \
        "O OFF must not revoke a megaphone routing set by the performer"
    print("PASS 3: O toggling never touches a performer-set megaphone routing")

    print("ALL PASS: O key controls PA Test Mode only; music bot routing is menu-only")

if __name__ == "__main__":
    main()
