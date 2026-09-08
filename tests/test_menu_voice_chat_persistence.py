import unittest
from types import SimpleNamespace
from unittest import mock

from libs import gameplay, menu


class TestMenuKeepsToggleVoiceChat(unittest.TestCase):
    """Opening a menu (or pressing any key that opens one) must not kill
    toggle-mode voice chat. The old Push-to-Talk-era kill switch stopped
    recording on every menu frame; it is now gated to non-toggle recordings.
    """

    def make_menu(self, gameplay_obj):
        game = SimpleNamespace(
            direct_soundgroup=object(),
            gameplay=None,
        )
        return menu.Menu(game, "test menu", parrent=gameplay_obj)

    def test_toggle_mode_survives_menu_update(self):
        gp = SimpleNamespace(
            voice_chat=SimpleNamespace(recording=True),
            voice_chat_stop=mock.Mock(),
            voice_chat_toggle_on=True,
        )
        m = self.make_menu(gp)
        m.update([])
        gp.voice_chat_stop.assert_not_called()

    def test_legacy_ptt_recording_still_stops_on_menu_update(self):
        # A recording started outside toggle mode (stuck PTT key release that
        # the menu swallowed) must still be stopped for safety.
        gp = SimpleNamespace(
            voice_chat=SimpleNamespace(recording=True),
            voice_chat_stop=mock.Mock(),
        )
        m = self.make_menu(gp)
        m.update([])
        gp.voice_chat_stop.assert_called_once_with(0)


class TestVoiceChatToggleFlag(unittest.TestCase):
    def test_toggle_on_sets_toggle_mode_when_recording_starts(self):
        gp = object.__new__(gameplay.Gameplay)
        gp.voice_chat = SimpleNamespace(recording=False, audio_input=object())
        gp.voice_chat_toggle_on = False

        def fake_start(_mod):
            gp.voice_chat.recording = True

        with mock.patch.object(gameplay.Gameplay, "voice_chat_start", side_effect=fake_start), \
                mock.patch.object(gameplay, "speak"):
            gp.voice_chat_toggle(0)

        self.assertTrue(gp.voice_chat_toggle_on)

    def test_toggle_off_clears_toggle_mode(self):
        gp = SimpleNamespace(
            game=SimpleNamespace(
                call_after=mock.Mock(),
                direct_soundgroup=SimpleNamespace(play=mock.Mock()),
            ),
            voice_chat=SimpleNamespace(
                recording=True,
                audio_input=SimpleNamespace(stop=mock.Mock()),
                voice_chat_finish=None,
            ),
            voice_chat_using_megaphone=True,
            voice_chat_toggle_on=True,
        )
        gameplay.Gameplay.voice_chat_stop(gp, 0)
        self.assertFalse(gp.voice_chat_toggle_on)


if __name__ == "__main__":
    unittest.main()