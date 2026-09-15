"""Routing the Music Bot through a cabinet's cinema room.

The point of this output mode is testing a room without queueing a song on the
cabinet: pick a cabinet in the Music Bot menu and the song comes out of the
speakers around it. What these tests protect is the seam between the two
systems -- one stream feeds a room, the bot's normal ear source is never left
playing alongside it, the room survives a map reload, and the bot's room and
the jukebox's own room are separate banks so neither can tear the other down.
"""

import os
import sys
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

import cyal

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from libs import jukebox
from libs.audio.cinema import CinemaSpeakerBank, host_for, set_enabled
from libs.music_bot import MapMusicBot
from libs.world_map import Map

ANCHOR = (10.0, 20.0, 0.0)


class FakeSource:
    def __init__(self, context):
        self.context = context
        self.position = None
        self.gain = 0.0
        self.buffers_queued = 0
        self.buffers_processed = 0
        self.state = cyal.SourceState.STOPPED
        self.deleted = False
        self.played = 0

    def play(self):
        self.played += 1
        self.state = cyal.SourceState.PLAYING

    def stop(self):
        self.state = cyal.SourceState.STOPPED

    def delete(self):
        self.deleted = True
        self.context.deleted.append(self)

    def queue_buffers(self, buffers):
        self.buffers_queued += 1

    def unqueue_buffers(self):
        if self.buffers_processed <= 0:
            return None
        self.buffers_processed -= 1
        self.buffers_queued = max(0, self.buffers_queued - 1)
        return []


class FakeBuffer:
    def set_data(self, data, sample_rate=None, format=None):
        self.data = bytes(data)


class FakeContext:
    def __init__(self):
        self.created = []
        self.deleted = []

    def gen_source(self, **kwargs):
        source = FakeSource(self)
        self.created.append(source)
        return source

    def gen_buffer(self):
        return FakeBuffer()


class FakeGame:
    def __init__(self):
        self.audio_mngr = SimpleNamespace(
            context=FakeContext(),
            efx=SimpleNamespace(send=lambda *a, **k: None),
            filter=[],
            position=ANCHOR,
            volume_categories={"jukebox": [100], "music": [100]},
        )
        # ``voice_chat`` is read by loop() every frame; None means "nobody is
        # talking into the PA", which is the duck-free case.
        # ``can_use_cinema_speakers`` mirrors the Server's login snapshot: the
        # routing is Developer/Contributor only, so a test that wants a plain
        # Music Bot turns it off the same way a lower-ranked account gets it.
        self.gameplay = SimpleNamespace(map=SimpleNamespace(), voice_chat=None,
                                        can_use_cinema_speakers=True)


def place_speakers(game, entries, anchor=ANCHOR):
    from math import cos, radians, sin
    map_obj = Map(game)
    for index, (channel, bearing) in enumerate(entries):
        angle = radians(bearing)
        x = anchor[0] + sin(angle) * 8.0
        y = anchor[1] + cos(angle) * 8.0
        map_obj.spawn_cinemaSpeaker(minx=x - 0.5, maxx=x + 0.5, miny=y - 0.5,
                                    maxy=y + 0.5, minz=0, maxz=1,
                                    id=f"spk{index}", channel=channel)
    game.gameplay.map = map_obj
    return map_obj


def make_bot(game, **attributes):
    """A MapMusicBot without its constructor (no device, no menus).

    Only the state the paths under test actually read is filled in; the
    attributes here are the ones ``stop``, ``set_volume`` and the stream start
    path touch, so a routing change can be exercised end to end without an
    audio device or a settings file.
    """
    bot = MapMusicBot.__new__(MapMusicBot)
    bot.game = game
    bot.volume = 50
    bot.enabled = True
    bot.streamer = None
    bot.stream_source = None
    bot.playing = False
    bot.paused = False
    bot.searching = False
    bot.is_loading_stream = False
    bot.mode = "idle"
    bot.current_title = "Song"
    bot.current_target = "http://example.com/a.mp3"
    bot.current_source = "youtube"
    bot.current_duration = None
    bot.feed_tracks = []
    bot.feed_index = -1
    bot.play_queue = []
    bot.play_queue_index = -1
    bot.play_queue_label = ""
    bot.next_up_queue = []
    bot.current_local_sound = None
    bot._playback_generation_lock = threading.Lock()
    bot._playback_generation = 0
    bot._stream_announced = False
    bot._current_reverb_slot = None
    bot.broadcast_enabled = False
    bot.broadcast_to_megaphone = False
    bot.live_relay_streamer = None
    bot.reverb_enabled = True
    bot.crossfade_enabled = False
    bot.cinema_target = None
    bot.cinema_bank = None
    bot.cinema_bank_key = None
    bot.cinema_cabinet = None
    bot._find_gameplay = lambda: game.gameplay
    for key, value in attributes.items():
        setattr(bot, key, value)
    return bot


class FakeMenu:
    """Drop-in for libs.menu.Menu capturing the items it was built with."""

    def __init__(self, *args, **kwargs):
        self.items = []

    def add_items(self, items):
        self.items = list(items)

    def speak_current_item(self):
        pass


def norm(label):
    """Menu labels may be dynamic callables; resolve them for assertions."""
    return label() if callable(label) else label


class CinemaMenuTests(unittest.TestCase):
    def test_the_menu_offers_every_cabinet_on_the_map(self):
        game = FakeGame()
        map_obj = place_speakers(game, [("front_l", -30), ("front_r", 30)])
        map_obj.spawn_jukebox(minx=9, maxx=10, miny=19, maxy=20, minz=0, maxz=1,
                              id="box_a")
        map_obj.spawn_jukebox(minx=79, maxx=80, miny=19, maxy=20, minz=0, maxz=1,
                              id="box_b")
        bot = make_bot(game)
        cabinets = bot.cinema_cabinets()
        self.assertEqual([entry[0] for entry in cabinets], ["box_a", "box_b"])
        self.assertIsNotNone(cabinets[0][2])
        self.assertIsNone(cabinets[1][2])
        self.assertIn("OFF", bot.cinema_target_label())

    def test_the_label_names_the_room_once_a_cabinet_is_chosen(self):
        game = FakeGame()
        map_obj = place_speakers(game, [("front_l", -30), ("front_r", 30)])
        map_obj.spawn_jukebox(minx=9, maxx=10, miny=19, maxy=20, minz=0, maxz=1,
                              id="box_a")
        bot = make_bot(game)
        with mock.patch("libs.music_bot.controller.speak"), \
                mock.patch("libs.music_bot.controller.options.set"):
            bot.set_cinema_target("box_a")
        label = bot.cinema_target_label()
        self.assertIn("box_a", label)
        self.assertIn("front_only", label)

    def test_choosing_a_cabinet_is_remembered(self):
        game = FakeGame()
        map_obj = place_speakers(game, [("front_l", -30), ("front_r", 30)])
        map_obj.spawn_jukebox(minx=9, maxx=10, miny=19, maxy=20, minz=0, maxz=1,
                              id="box_a")
        bot = make_bot(game)
        with mock.patch("libs.music_bot.controller.speak"), \
                mock.patch("libs.music_bot.controller.options.set") as store:
            bot.set_cinema_target("box_a")
        store.assert_called_once_with("music_bot_cinema_target", "box_a")

    def test_a_cabinet_without_speakers_says_which_wall_is_missing(self):
        game = FakeGame()
        map_obj = Map(game)
        map_obj.spawn_jukebox(minx=9, maxx=10, miny=19, maxy=20, minz=0, maxz=1,
                              id="box_a")
        game.gameplay.map = map_obj
        bot = make_bot(game)
        with mock.patch("libs.music_bot.controller.speak") as speech, \
                mock.patch("libs.music_bot.controller.options.set"):
            bot.set_cinema_target("box_a")
        self.assertIsNone(bot._ensure_cinema_bank())
        complaint = " ".join(str(call) for call in speech.call_args_list)
        # Not "no speakers": this cabinet has none, and the menu has to say
        # which wall is missing or the tester just counts the speakers again.
        self.assertIn("no cinema speaker within", complaint)
        self.assertIn("cannot play through its speakers", complaint)
        # Playing still works: it just comes out at the listener's ears.
        bot._create_stream_source = MapMusicBot._create_stream_source.__get__(bot)
        bot._new_bot_source = lambda: FakeSource(game.audio_mngr.context)
        with mock.patch("libs.music_bot.controller.speak"):
            bot._create_stream_source()
        self.assertIsNotNone(bot.stream_source)

    def test_speakers_placed_on_a_map_with_no_jukebox_are_explained(self):
        """The exact trap: a room of speakers and no cabinet to anchor it.

        The speakers are useless on their own -- a room is resolved around a
        cabinet -- so the menu must say "place a Jukebox", not "no speakers".
        """
        game = FakeGame()
        map_obj = place_speakers(game, [("auto", -90), ("auto", 90)])
        game.gameplay.map = map_obj
        bot = make_bot(game)
        self.assertEqual(bot.cinema_cabinets(), [])
        help_text = bot._cinema_map_help()
        self.assertIn("2 cinema speaker", help_text)
        self.assertIn("no Jukebox", help_text)
        self.assertIn("Musical Instrument", help_text)

    def test_a_map_with_nothing_at_all_still_names_the_jukebox(self):
        game = FakeGame()
        bot = make_bot(game)
        self.assertEqual(bot.cinema_cabinets(), [])
        self.assertIn("no Jukebox", bot._cinema_map_help())

    def test_a_cabinet_whose_speakers_cannot_make_a_room_names_the_reason(self):
        """Two speakers on one line never make a stereo front pair.

        This is the other half of the trap: the cabinet exists, the speakers
        exist, and the room is still refused. The menu has to carry the
        resolver's own reason or the map data can never be fixed.
        """
        game = FakeGame()
        map_obj = Map(game)
        for index, x in enumerate((10.0, 20.0)):
            map_obj.spawn_cinemaSpeaker(minx=int(x), maxx=int(x) + 1, miny=5, maxy=6,
                                        minz=0, maxz=1, id=f"spk{index}", channel="auto")
        map_obj.spawn_jukebox(minx=10, maxx=11, miny=5, maxy=6, minz=0, maxz=1,
                              id="box_a")
        game.gameplay.map = map_obj
        bot = make_bot(game)
        cabinets = bot.cinema_cabinets()
        self.assertEqual([entry[0] for entry in cabinets], ["box_a"])
        self.assertIsNone(cabinets[0][2])
        reason = bot._cinema_problem(cabinets[0][1], "box_a")
        self.assertIn("no stereo front pair", reason)


class CinemaOutputTests(unittest.TestCase):
    def setUp(self):
        self.game = FakeGame()
        map_obj = place_speakers(self.game, [
            ("front_l", -30), ("front_c", 0), ("front_r", 30),
            ("side_l", -90), ("side_r", 90)])
        map_obj.spawn_jukebox(minx=9, maxx=10, miny=19, maxy=20, minz=0, maxz=1,
                              id="box_a")
        self.map = map_obj
        set_enabled(self.game, True)

    def routed_bot(self, cabinet="box_a"):
        bot = make_bot(self.game)
        with mock.patch("libs.music_bot.controller.speak"), \
                mock.patch("libs.music_bot.controller.options.set"):
            bot.set_cinema_target(cabinet)
        return bot

    def test_routing_replaces_the_ear_source_with_the_room(self):
        bot = self.routed_bot()
        bot._new_bot_source = lambda: self.fail("no ear source may be created")
        bot._create_stream_source()
        self.assertIsNone(bot.stream_source)
        self.assertIsInstance(bot.cinema_bank, CinemaSpeakerBank)
        self.assertEqual(len(bot.cinema_bank.sources), 5)
        self.assertEqual(bot.cinema_bank_key, "musicbot:box_a")

    def test_the_bot_room_and_the_jukebox_room_are_separate(self):
        bot = self.routed_bot()
        bot._create_stream_source()
        player = jukebox.JukeboxPlayer(self.game)
        with mock.patch("libs.music_bot.AudioStreamer") as streamer:
            player.play("box_a", 10, 20, 0, "Song", "http://example.com/a.mp3",
                        60, transport="direct")
        jukebox_bank = streamer.call_args.kwargs["cinema"]
        self.assertIsInstance(jukebox_bank, CinemaSpeakerBank)
        self.assertIsNot(jukebox_bank, bot.cinema_bank)
        # Stopping the bot's room must not silence the jukebox's.
        bot.stop()
        self.assertIsNone(bot.cinema_bank)
        self.assertEqual(len(jukebox_bank.sources), 5)
        self.assertEqual(len(self.game.audio_mngr.context.created), 10)

    def test_stop_silences_and_returns_the_room(self):
        bot = self.routed_bot()
        bot._create_stream_source()
        bank = bot.cinema_bank
        bot.stop()
        self.assertTrue(bank._stopped)
        self.assertEqual(bank.sources, ())
        self.assertIsNone(bot.cinema_bank)
        self.assertIsNone(host_for(self.game, create=False).bank("musicbot:box_a"))

    def test_a_room_taken_away_is_rebuilt_around_the_live_stream(self):
        bot = self.routed_bot()
        bot._create_stream_source()
        first = bot.cinema_bank
        streamer = SimpleNamespace(cinema=first, source=None)
        bot.streamer = streamer
        # A jukebox stop or map reload can release the shared registry entry.
        host_for(self.game, create=False).release("musicbot:box_a")
        bot._update_cinema_output()
        self.assertIsNot(bot.cinema_bank, first)
        self.assertIsInstance(bot.cinema_bank, CinemaSpeakerBank)
        self.assertIs(streamer.cinema, bot.cinema_bank)
        self.assertIs(streamer.source, bot.cinema_bank.primary_source)

    def test_volume_and_eq_reach_the_room(self):
        bot = self.routed_bot()
        bot._create_stream_source()
        bank = bot.cinema_bank
        bot.eq_profile, bot.eq_values = "bass_boost", {"bass": 50}
        bot._get_bot_eq_slot = lambda profile, values: 7
        with mock.patch("libs.music_bot.controller.options.set"):
            bot.set_volume(80)
        self.assertEqual(bank.volume, 80)
        bot._apply_bot_eq()
        self.assertEqual(bank.eq_slot, 7)

    def test_a_seek_through_the_room_starts_from_where_the_song_has_got_to(self):
        """Seeking is measured from the position the room has played to.

        The bot asks its streamer how far into the song it is and adds the
        jump to that. A room feeds through the bank rather than a source of
        its own, so if that position never leaves the seek offset every
        forward seek restarts beside the intro -- a listener four minutes in
        presses seek +30 and the song jumps back to 30 seconds.
        """
        bot = self.routed_bot()
        bot._create_stream_source()
        from libs.music_bot import AudioStreamer
        streamer = AudioStreamer(self.game, "http://example.com/a.mp3", None,
                                 volume=100, bot=SimpleNamespace(),
                                 cinema=bot.cinema_bank)
        streamer.is_alive = lambda: True
        bot.streamer = streamer
        bot.playing = True
        chunk = bytes(streamer.SAMPLES_PER_BUFFER * streamer.channels * 2)
        for _ in range(10):         # 200 ms of the song, through the room
            self.assertTrue(streamer._queue_local(chunk))
        self.assertAlmostEqual(bot.track_position(), 0.2, places=6)

        restarts = []
        bot._seek_restart = lambda position: restarts.append(position)
        with mock.patch("libs.music_bot.controller.speak"):
            bot.seek_by(30)
        self.assertAlmostEqual(restarts[0], 30.2, places=6)

    def test_crossfade_is_bypassed_so_one_stream_feeds_the_room(self):
        bot = self.routed_bot()
        bot.crossfade_enabled = True
        bot.is_loading_stream = False
        bot._remaining_seconds = lambda: 1.0
        bot._start_crossfade_roll()
        self.assertIsNone(getattr(bot, "_crossfade", None))

    def test_starting_a_track_through_the_room_does_not_report_an_audio_error(self):
        """A room has no ear source, and that must not read as a failure.

        The start path bailed out with "Audio error." whenever ``stream_source``
        was missing -- which is exactly what cinema routing leaves it, because
        the room owns the sources. The room then resolved cleanly, the menu
        said so, and no song ever started.
        """
        bot = self.routed_bot()
        streamer = mock.MagicMock()
        streamer.ready_event = threading.Event()
        with mock.patch("libs.music_bot.controller.AudioStreamer",
                        return_value=streamer) as factory, \
                mock.patch("libs.music_bot.controller.speak") as speech:
            bot._start_youtube_stream("http://example.com/a.mp3", "Song")
        bank = bot.cinema_bank
        self.assertFalse(any("Audio error" in str(call) for call in speech.call_args_list))
        factory.assert_called_once()
        self.assertIs(factory.call_args.kwargs["cinema"], bank)
        self.assertIs(factory.call_args.args[2], bank.primary_source)
        streamer.start.assert_called_once()
        self.assertTrue(bot.playing)
        self.assertIsNone(bot.stream_source)

    def test_the_loop_hands_the_duck_to_the_room(self):
        """A room has no single source to duck, so the bank takes the duck."""
        bot = self.routed_bot()
        bot._create_stream_source()
        bot.paused = True            # loop() returns right after the output upkeep
        bot.duck_multiplier = 0.45
        bot.loop()
        self.assertEqual(bot.cinema_bank.duck, bot.duck_multiplier)
        self.assertLess(bot.cinema_bank.duck, 1.0)

    def test_the_bot_room_follows_the_music_slider_not_the_jukebox_one(self):
        """The room is the bot's output, so the Music slider moves it.

        The cabinet's own playback deliberately answers to its own jukebox
        category; a room the bot is feeding must not start following that one
        instead, or the player's two sliders would be swapped.
        """
        bot = self.routed_bot()
        bot._create_stream_source()
        bank = bot.cinema_bank
        self.assertEqual(bank.category, "music")
        volumes = self.game.audio_mngr.volume_categories
        volumes["music"] = [100]
        volumes["jukebox"] = [100]
        bank.set_duck(1.0)
        bank.update_output()
        full = [source.gain for source in bank.sources]
        self.assertTrue(all(gain > 0 for gain in full))

        # Ducking (a megaphone broadcast) must reach every speaker.
        bank.set_duck(0.5)
        bank.update_output()
        for before, after in zip(full, [source.gain for source in bank.sources]):
            self.assertAlmostEqual(after, before * 0.5, places=6)

        # Halving the Music slider halves the room, while the Jukebox slider
        # (moved the other way) changes nothing at all.
        bank.set_duck(1.0)
        volumes["music"] = [50]
        volumes["jukebox"] = [25]
        bank.update_output()
        for before, after in zip(full, [source.gain for source in bank.sources]):
            self.assertAlmostEqual(after, before * 0.5, places=6)

    def test_without_a_target_the_bot_is_untouched(self):
        bot = make_bot(self.game)
        with mock.patch("libs.music_bot.controller.speak"), \
                mock.patch("libs.music_bot.controller.options.set"):
            bot.set_cinema_target(None)
        bot._new_bot_source = lambda: FakeSource(self.game.audio_mngr.context)
        bot._sync_map_reverb = lambda *a, **k: True
        bot._create_stream_source()
        self.assertIsNotNone(bot.stream_source)
        self.assertIsNone(bot.cinema_bank)

    def test_turning_routing_off_mid_track_keeps_the_track(self):
        bot = self.routed_bot()
        bot._create_stream_source()
        restarts = []
        bot.playing = True
        bot.track_position = lambda: 12.0
        bot._seek_restart = lambda position: restarts.append(position)
        with mock.patch("libs.music_bot.controller.speak"), \
                mock.patch("libs.music_bot.controller.options.set"):
            bot.set_cinema_target(None)
        self.assertIsNone(bot.cinema_bank)
        # The track keeps playing, restarted at the same position at the ears.
        self.assertEqual(restarts, [12.0])


class CinemaRoutingPermissionTests(unittest.TestCase):
    """Developer/Contributor only: everyone else gets a plain Music Bot.

    Feeding a room takes a cabinet's speakers over, so the Server owns the
    rule (``can_use_cinema_speakers``) and the client mirrors it: the menu
    line is hidden entirely, and a choice saved before a rank change stops
    being acted on instead of leaving a room the account can no longer see or
    switch off.
    """

    def routed_game(self):
        game = FakeGame()
        map_obj = place_speakers(game, [("front_l", -30), ("front_r", 30)])
        map_obj.spawn_jukebox(minx=9, maxx=10, miny=19, maxy=20, minz=0, maxz=1,
                              id="box_a")
        game.gameplay.map = map_obj
        return game

    def test_the_server_flag_is_what_grants_it(self):
        game = self.routed_game()
        bot = make_bot(game)
        self.assertTrue(bot.cinema_routing_allowed())
        game.gameplay.can_use_cinema_speakers = False
        self.assertFalse(bot.cinema_routing_allowed())
        del game.gameplay.can_use_cinema_speakers
        self.assertFalse(
            bot.cinema_routing_allowed(),
            "a Server that predates the flag must not grant the routing",
        )

    def test_a_saved_target_plays_at_the_ears_without_the_permission(self):
        game = self.routed_game()
        bot = make_bot(game)
        bot.cinema_target = "box_a"
        self.assertEqual(bot.cinema_active_target(), "box_a")
        game.gameplay.can_use_cinema_speakers = False
        self.assertIsNone(bot.cinema_active_target())
        # The room is never taken, and the bot falls back to its ear source.
        self.assertIsNone(bot._ensure_cinema_bank())
        bot._new_bot_source = lambda: FakeSource(game.audio_mngr.context)
        bot._sync_map_reverb = lambda *a, **k: True
        with mock.patch("libs.music_bot.controller.speak"):
            bot._create_stream_source()
        self.assertIsNotNone(bot.stream_source)
        self.assertIsNone(bot.cinema_bank)

    def test_the_room_menu_is_refused_without_the_permission(self):
        from libs.gameplay import Gameplay

        game = self.routed_game()
        gp = mock.MagicMock(spec=Gameplay)
        gp.substates = []
        gp.map = game.gameplay.map
        gp.can_use_cinema_speakers = False
        game.gameplay = gp
        bot = make_bot(game)
        bot._open_cinema_menu()
        self.assertEqual(gp.substates, [])
        gp.add_substate.assert_not_called()
        gp.pop_last_substate.assert_not_called()


class LiveInstrumentRoutingTests(unittest.TestCase):
    """The two listening switches, next to the song's own routing line.

    A live note is not a stream: it is played at the room's speakers by every
    listener's own client, so it is a listening choice (like the Cinema
    Speakers line) rather than something the performer forces on anyone -- and
    so is hearing a song through a room at all. Both lines are the listener's,
    both are on for everyone, and both live here instead of in Options.
    """

    KEYS = ("cinema_live_instruments", "music_bot_instruments_cinema",
            "cinema_speakers")

    def setUp(self):
        # The toggles write into the shared options store; leave it as found.
        from libs import options
        self._saved = {key: options.prefs.pop(key, None) for key in self.KEYS}

    def tearDown(self):
        from libs import options
        for key, value in self._saved.items():
            options.prefs.pop(key, None)
            if value is not None:
                options.prefs[key] = value

    def routed_game(self):
        game = FakeGame()
        map_obj = place_speakers(game, [("front_l", -30), ("front_r", 30)])
        game.gameplay.map = map_obj
        return game

    def test_it_is_on_for_a_new_listener(self):
        """Nobody has to find a switch to hear the band through the room.

        A live note is one sample per speaker on the listener's own client and
        takes nothing from anybody, so this is not a staff switch: the default
        is what every player gets.
        """
        from libs.audio.cinema import live as cinema_live
        game = self.routed_game()
        bot = make_bot(game)
        self.assertTrue(cinema_live.live_instruments_enabled())
        self.assertTrue(bot.instruments_cinema_active())
        self.assertIn("cabinet", bot.instruments_cinema_label())
        self.assertIn("Cinema rooms: ON", bot.cinema_rooms_label())

    def test_the_menu_line_and_the_instruments_read_one_setting(self):
        """The line saves exactly where the playback path reads.

        Two keys meant the line could look switched while the room stayed
        silent (or the other way round), which is invisible until somebody
        stands in the hall and listens.
        """
        from libs import options
        from libs.audio.cinema import live as cinema_live
        game = self.routed_game()
        bot = make_bot(game)
        with mock.patch("libs.music_bot.controller.speak"), \
                mock.patch.object(options, "save"):
            bot.toggle_instruments_cinema()                 # turn it off
        # The key the instruments read, written by the line that flipped it.
        self.assertFalse(options.get(cinema_live.OPTION_ENABLED))
        self.assertFalse(cinema_live.live_instruments_enabled())
        self.assertFalse(bot.instruments_cinema_active())
        self.assertIn("OFF", bot.instruments_cinema_label())
        with mock.patch("libs.music_bot.controller.speak"), \
                mock.patch.object(options, "save"):
            bot.toggle_instruments_cinema()                 # and back on
        self.assertTrue(cinema_live.live_instruments_enabled())
        self.assertTrue(bot.instruments_cinema_active())

    def test_a_preference_saved_under_the_old_name_still_counts(self):
        """The key was renamed once the switch stopped being the bot's own."""
        from libs import options
        from libs.audio.cinema import live as cinema_live
        options.prefs.pop("cinema_live_instruments", None)
        options.prefs["music_bot_instruments_cinema"] = False
        self.assertFalse(cinema_live.live_instruments_enabled())
        options.prefs["cinema_live_instruments"] = True
        self.assertTrue(cinema_live.live_instruments_enabled())

    def test_a_player_who_cannot_open_the_menu_still_hears_the_band(self):
        """The switches are for reaching them; the default is for everyone.

        Both lines follow the Music Bot's own access rule, so neither can
        appear in a menu the account cannot open -- but that rule only decides
        who may *change* the setting. A listener who cannot reach the line
        gets the default, which is the band through the room.
        """
        game = self.routed_game()
        bot = make_bot(game)
        self.assertTrue(bot.cinema_listening_allowed())
        for flag in ("can_use_cinema_speakers", "can_use_music_bot",
                     "can_broadcast_megaphone", "is_staff", "is_builder",
                     "is_technician"):
            if hasattr(game.gameplay, flag):
                delattr(game.gameplay, flag)
        # A regular player never sees the Music Bot, and so neither line.
        self.assertFalse(bot.cinema_listening_allowed())
        self.assertTrue(bot.instruments_cinema_active())
        # Any staff level is enough -- a builder is not a contributor.
        game.gameplay.is_builder = True
        self.assertTrue(bot.cinema_listening_allowed())

    def test_the_server_flag_still_grants_the_lines(self):
        game = self.routed_game()
        for flag in ("can_use_music_bot", "is_staff", "is_builder",
                     "is_technician"):
            if hasattr(game.gameplay, flag):
                delattr(game.gameplay, flag)
        game.gameplay.can_use_cinema_speakers = False
        # A Server older than the staff snapshot but with the routing flag on
        # is still authorization: the flag is the Server's own word.
        self.assertFalse(make_bot(game).cinema_listening_allowed())
        game.gameplay.can_use_cinema_speakers = True
        self.assertTrue(make_bot(game).cinema_listening_allowed())

    def test_the_switches_are_lines_on_the_music_bot_menu(self):
        """Where they live: with the other listening lines, not in Options."""
        game = self.routed_game()
        bot = make_bot(game, queue_mode=False, next_up_queue=[])
        captured = []
        game.gameplay.add_substate = captured.append
        game.gameplay.pop_last_substate = lambda: None
        with mock.patch("libs.menu.Menu", FakeMenu), \
                mock.patch("libs.menus.set_default_sounds"):
            bot._show_mode_menu()
        labels = [norm(label) for label, _action in captured[-1].items]
        self.assertTrue(any(label.startswith("Cinema rooms: ON")
                            for label in labels), labels)
        self.assertTrue(any(label.startswith("Instruments:")
                            for label in labels), labels)
        self.assertTrue(any(label.startswith("Speech:")
                            for label in labels), labels)

    def test_the_speech_switch_is_one_setting_the_voice_paths_read(self):
        """The line and the audio behind it cannot drift apart.

        A voice is played at the room's speakers by every listener's own
        client (libs/audio/cinema/speech.py), so off is the map's PA -- and
        the key the line flips is the key that path reads.
        """
        from libs import options
        from libs.audio.cinema import speech as cinema_speech
        bot = make_bot(FakeGame())
        self.assertTrue(cinema_speech.speech_enabled())
        self.assertIn("cabinet", bot.speech_cinema_label())
        with mock.patch("libs.music_bot.controller.speak"), \
                mock.patch.object(options, "save"):
            bot.toggle_speech_cinema()                       # back to the PA
        self.assertFalse(options.get(cinema_speech.OPTION_ENABLED))
        self.assertFalse(cinema_speech.speech_enabled())
        self.assertIn("PA", bot.speech_cinema_label())
        with mock.patch("libs.music_bot.controller.speak"), \
                mock.patch.object(options, "save"):
            bot.toggle_speech_cinema()                       # and through the room
        self.assertTrue(cinema_speech.speech_enabled())

    def test_the_rooms_switch_releases_the_room_that_is_playing(self):
        """Off has to be heard now, not at the end of the song."""
        from libs import options
        from libs.audio.cinema import plugin as cinema_plugin
        game = self.routed_game()
        bot = make_bot(game)
        host = SimpleNamespace(calls=[],
                               set_enabled=lambda enabled: host.calls.append(enabled))
        with mock.patch.object(cinema_plugin, "host_for", return_value=host), \
                mock.patch("libs.music_bot.controller.speak"), \
                mock.patch.object(options, "save"):
            bot.toggle_cinema_rooms()
        self.assertEqual(host.calls, [False])
        self.assertFalse(options.get("cinema_speakers"))
        self.assertIn("OFF", bot.cinema_rooms_label())
        with mock.patch.object(cinema_plugin, "host_for", return_value=host), \
                mock.patch("libs.music_bot.controller.speak"), \
                mock.patch.object(options, "save"):
            bot.toggle_cinema_rooms()
        self.assertEqual(host.calls, [False, True])
        self.assertTrue(options.get("cinema_speakers"))
        self.assertIn("ON", bot.cinema_rooms_label())

    def test_flipping_the_rooms_switch_builds_no_host_out_of_nothing(self):
        """A game that never used cinema is left exactly as it was."""
        from libs import options
        from libs.audio.cinema import plugin as cinema_plugin
        game = self.routed_game()
        bot = make_bot(game)
        with mock.patch("libs.music_bot.controller.speak"), \
                mock.patch.object(options, "save"):
            bot.toggle_cinema_rooms()
        self.assertIsNone(cinema_plugin.host_for(game, create=False))


class OwnSoundLineTests(unittest.TestCase):
    """The read-only line beside the three listening switches.

    The three lines above it choose how *you* hear a room; this one is the
    other direction -- where other players hear *you* -- and is decided by staff
    on the Server. So it has to exist in the menu a listener actually opens,
    read rather than toggle, and change nothing at all when it is pressed: a
    line that looked like a setting somebody can turn off would be a switch the
    Server would never honour.
    """

    def _bot(self, own=9, me="Somchai"):
        game = FakeGame()
        gp = game.gameplay
        gp.add_substate = lambda menu: None
        gp.pop_last_substate = lambda: None
        gp.voice_channels = {}
        gp.player = SimpleNamespace(name=me)
        gp.own_voice_channel = own
        return game, gp, make_bot(game, queue_mode=False, next_up_queue=[])

    def _open_menu(self, bot):
        made = []

        class Capturing(FakeMenu):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                made.append(self)

        with mock.patch("libs.menu.Menu", Capturing), \
                mock.patch("libs.menus.set_default_sounds"):
            bot._show_mode_menu()
        return made[-1]

    def _labels(self, menu):
        return [norm(label) for label, _action in menu.items]

    def test_it_sits_beside_the_three_listening_switches(self):
        _game, _gp, bot = self._bot()
        menu = self._open_menu(bot)
        labels = self._labels(menu)
        speech = next(index for index, text in enumerate(labels)
                      if text.startswith("Speech:"))
        self.assertTrue(any(text.startswith("Cinema rooms:") for text in labels))
        self.assertTrue(any(text.startswith("Instruments:") for text in labels))
        self.assertTrue(any(text.startswith("Your sound:") for text in labels))
        self.assertEqual(
            next(text for text in labels if text.startswith("Your sound:")),
            "Your sound: where you stand (no staff pan)")
        self.assertEqual(labels[speech + 1].split(":")[0], "Your sound",
                         "the reading line belongs under the listening ones")

    def test_pressing_it_says_where_you_are_and_writes_nothing(self):
        _game, _gp, bot = self._bot()
        menu = self._open_menu(bot)
        with mock.patch("libs.music_bot.controller.options.set") as store, \
                mock.patch("libs.music_bot.controller.speak") as speech:
            item = next(action for label, action in menu.items
                        if norm(label).startswith("Your sound:"))
            item()
        self.assertFalse(store.called)
        said = " ".join(str(call) for call in speech.call_args_list)
        self.assertIn("No staff pan is moving your voice", said)
        self.assertIn("where you stand", said)

    def test_the_line_names_the_room_and_the_side_once_you_are_moved(self):
        from libs.audio.cinema import pan as cinema_pan
        _game, gp, bot = self._bot()
        cinema_pan.table_for(gp).set("Somchai", 9, "j3", "left")
        self.assertEqual(bot.own_sound_label(),
                         "Your sound: heard from jukebox j3, towards the left "
                         "of the room")
        with mock.patch("libs.music_bot.controller.speak") as speech:
            bot.announce_own_sound()
        # One sentence, built by the pan module, said both here and at the
        # moment a pan lands (pan.own_notice): the menu and the announcement
        # cannot disagree about the room, the side, or the switches.
        self.assertEqual(speech.call_args.args[0], cinema_pan.own_report(gp))
        self.assertEqual(speech.call_args.args[0],
                         cinema_pan.own_notice(gp, cinema_pan.own_entry(gp), None))

    def test_a_client_that_does_not_know_its_channel_still_reads_a_line(self):
        # The login snapshot may predate the field; the line must still be there
        # (it just cannot name a room) rather than vanish from the menu.
        _game, _gp, bot = self._bot(own=None)
        self.assertIn("Your sound:", bot.own_sound_label())


if __name__ == "__main__":
    unittest.main()
