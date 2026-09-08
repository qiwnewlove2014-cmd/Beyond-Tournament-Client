"""Megaphone music / voice coexistence tests.

Covers three behaviours:
- Talking through the megaphone is NEVER blocked by the music-bot broadcast
  slot (the single-owner lock gates music uploads only, never voice).
- When the server replies ``music_taken`` (someone else holds the single
  music slot) the client reverts the optimistic "Broadcast to Megaphone"
  toggle so the UI cannot claim a broadcast that never reaches the PA.
- The music bot ducks the PA music while OTHERS speak on the megaphone:
  remote PA frames are stamped in ``process_voice_data`` and the owner's
  ``duck_multiplier`` scales both the local source and the uploaded PCM,
  so every listener hears the dip together.
"""
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from libs import consts, event_handeler, gameplay, options
from libs.music_bot.controller import MapMusicBot


class TestVoiceNeverLockedByMusicSlot(unittest.TestCase):
    def make_gameplay(self, lock_owner="Alice", player_name="Bob"):
        gp = object.__new__(gameplay.Gameplay)
        gp.pa_test_mode = False
        gp.game_started = True
        gp.wmanager = SimpleNamespace(
            activeWeapon=SimpleNamespace(name="Megaphone"))
        gp.voice_channels = {
            consts.CHANNEL_MEGAPHONE: SimpleNamespace(vc_compression=object()),
        }
        gp.megaphone = SimpleNamespace(
            sources=[object()],
            lock_owner=lock_owner,
            lock_owners=set(),
            setup_megaphone_speakers=mock.Mock(),
        )
        gp.player = SimpleNamespace(name=player_name)
        gp.voice_chat = SimpleNamespace(
            audio_input=SimpleNamespace(start=mock.Mock()),
            recording=False,
            vc_compression=None,
        )
        gp.game = SimpleNamespace(direct_soundgroup=SimpleNamespace(play=mock.Mock()))
        gp.voice_chat_using_megaphone = False
        gp.voice_chat_toggle_on = False
        return gp

    def test_talking_starts_while_another_staff_holds_music_slot(self):
        gp = self.make_gameplay(lock_owner="Alice", player_name="Bob")
        with mock.patch.object(options, "get", return_value=True), \
                mock.patch.object(gameplay, "speak") as speak:
            gp.voice_chat_start(0)

        gp.voice_chat.audio_input.start.assert_called_once()
        self.assertTrue(gp.voice_chat.recording)
        self.assertTrue(gp.voice_chat_using_megaphone)
        # No "locked" refusal was ever spoken.
        for call in speak.call_args_list:
            self.assertNotIn("locked", str(call).lower())

    def test_talking_starts_when_player_is_the_music_owner(self):
        gp = self.make_gameplay(lock_owner="Bob", player_name="Bob")
        with mock.patch.object(options, "get", return_value=True), \
                mock.patch.object(gameplay, "speak"):
            gp.voice_chat_start(0)
        self.assertTrue(gp.voice_chat.recording)


class TestMusicTakenRevertsToggle(unittest.TestCase):
    def test_music_taken_clears_broadcast_to_megaphone(self):
        handler = event_handeler.EventHandeler.__new__(event_handeler.EventHandeler)
        handler.gameplay = SimpleNamespace(
            megaphone=SimpleNamespace(lock_owner=None, lock_owners=set()),
            music_bot=SimpleNamespace(broadcast_to_megaphone=True),
        )
        with mock.patch.object(event_handeler, "speak"):
            handler.megaphone_lock_state({
                "owner": "Alice",
                "owners": ["Alice"],
                "music_taken": True,
            })
        self.assertFalse(handler.gameplay.music_bot.broadcast_to_megaphone)
        self.assertEqual(handler.gameplay.megaphone.lock_owner, "Alice")
        self.assertEqual(handler.gameplay.megaphone.lock_owners, {"Alice"})

    def test_lock_state_without_music_taken_keeps_toggle(self):
        handler = event_handeler.EventHandeler.__new__(event_handeler.EventHandeler)
        handler.gameplay = SimpleNamespace(
            megaphone=SimpleNamespace(lock_owner=None, lock_owners=set()),
            music_bot=SimpleNamespace(broadcast_to_megaphone=True),
        )
        handler.megaphone_lock_state({"owner": "Alice", "owners": ["Alice"]})
        self.assertTrue(handler.gameplay.music_bot.broadcast_to_megaphone)


class TestRemoteVoiceStamp(unittest.TestCase):
    def make_handler(self):
        handler = event_handeler.EventHandeler.__new__(event_handeler.EventHandeler)
        handler.gameplay = SimpleNamespace(
            voice_channels={
                consts.CHANNEL_MEGAPHONE: SimpleNamespace(
                    vc_compression=SimpleNamespace(recieve=mock.Mock()),
                ),
            },
            megaphone=SimpleNamespace(
                get_megaphone_player_sources=mock.Mock(return_value=[object()]),
            ),
        )
        return handler

    def test_megaphone_frames_stamp_remote_activity(self):
        handler = self.make_handler()
        before = time.monotonic()
        with mock.patch.object(options, "get", return_value=True):
            handler.process_voice_data(b"\x01\xf8\xff\xfe", consts.CHANNEL_MEGAPHONE)
        after = time.monotonic()
        stamp = handler.gameplay._last_remote_megaphone_voice_ts
        self.assertIsNotNone(stamp)
        self.assertGreaterEqual(stamp, before)
        self.assertLessEqual(stamp, after)

    def test_voice_chat_disabled_skips_stamp(self):
        handler = self.make_handler()
        with mock.patch.object(options, "get", return_value=False):
            handler.process_voice_data(b"\x01\xf8\xff\xfe", consts.CHANNEL_MEGAPHONE)
        self.assertFalse(hasattr(handler.gameplay, "_last_remote_megaphone_voice_ts"))


class TestMusicDuckWhenOthersSpeak(unittest.TestCase):
    def make_bot(self, gp, broadcast_to_megaphone=True):
        return SimpleNamespace(
            enabled=True,
            broadcast_to_megaphone=broadcast_to_megaphone,
            broadcast_enabled=False,
            duck_multiplier=1.0,
            stream_source=None,
            playing=False,
            paused=False,
            _ensure_live_relay_streamer=mock.Mock(),
            _find_gameplay=lambda: gp,
        )

    def run_loop(self, bot):
        MapMusicBot.loop(bot)
        return bot.duck_multiplier

    def test_ducks_when_remote_voice_is_recent(self):
        gp = SimpleNamespace(
            voice_chat=None,
            _last_remote_megaphone_voice_ts=time.monotonic(),
        )
        bot = self.make_bot(gp)
        # The LERP takes ~10% of the remaining gap per frame: let it converge.
        for _ in range(30):
            duck = self.run_loop(bot)
        self.assertLess(duck, 0.5, f"expected duck, got {duck}")

    def test_duck_recovers_after_remote_voice_stops(self):
        gp = SimpleNamespace(voice_chat=None)
        bot = self.make_bot(gp)
        # Engage the duck with fresh remote frames, then let them go stale.
        gp._last_remote_megaphone_voice_ts = time.monotonic()
        for _ in range(30):
            self.run_loop(bot)
        self.assertLess(bot.duck_multiplier, 0.5)
        gp._last_remote_megaphone_voice_ts = 0
        for _ in range(60):
            duck = self.run_loop(bot)
        self.assertGreater(duck, 0.95, f"expected recovery, got {duck}")

    def test_no_duck_without_remote_voice(self):
        gp = SimpleNamespace(voice_chat=None, _last_remote_megaphone_voice_ts=0)
        duck = self.run_loop(self.make_bot(gp))
        self.assertEqual(duck, 1.0)

    def test_no_duck_when_music_not_on_megaphone(self):
        gp = SimpleNamespace(
            voice_chat=None,
            _last_remote_megaphone_voice_ts=time.monotonic(),
        )
        duck = self.run_loop(self.make_bot(gp, broadcast_to_megaphone=False))
        self.assertEqual(duck, 1.0)

    def test_no_stamp_attribute_means_no_duck(self):
        gp = SimpleNamespace(voice_chat=None)
        duck = self.run_loop(self.make_bot(gp))
        self.assertEqual(duck, 1.0)


if __name__ == "__main__":
    unittest.main()