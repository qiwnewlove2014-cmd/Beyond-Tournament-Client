"""Spoken subtitles for the Music Bot (libs/music_bot/subtitles.py).

Covers the whole feature's engine-free half:

- parsing a YouTube ``json3`` caption track into timed lines, in both shapes
  YouTube sends (an uploader's whole-line events, and the automatic track's
  re-emitted growing lines), including the rolling duplicates that would
  otherwise read the same sentence several times;
- choosing the track a player's language order asks for;
- fetching one track per page through an injected yt-dlp stand-in, including
  the two ways it can answer "no": a video with no captions, and YouTube's
  own rate limit;
- aiming cues at the song's position (drop-what-has-gone-past, seek rebasing,
  the sync offset, and the bound that stops feeding a reader that cannot keep
  up);
- the Music Bot wiring: the menu, the persisted settings, the fetch thread,
  and the frame pump.

No game, OpenAL, yt-dlp, network or screen reader is required: the fetch runs
against a fake module, speaking is a recorded call, and every caption track
here is a fixture written out in the test.
"""

import json
import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from libs.music_bot import controller as bot_module
from libs.music_bot import subtitles
from libs.music_bot import MapMusicBot


# ── caption-track fixtures (the shapes YouTube really sends) ────────────────

def track(events):
    """A json3 caption track body, as the timedtext endpoint answers."""
    return json.dumps({"wireMagic": "pb3", "events": list(events)}).encode("utf-8")


def event(start, duration, text):
    """One uploader-track event: the whole line, ended by a newline."""
    return {"tStartMs": start, "dDurationMs": duration,
            "segs": [{"utf8": text}]}


def word_group(start, duration, text):
    """One automatic-caption event: a word group of the line being built."""
    return {"tStartMs": start, "dDurationMs": duration,
            "segs": [{"utf8": text}]}


#: An uploader's track: one event per line, each closed by a newline.
MANUAL = track([
    event(0, 3000, "Hello there\n"),
    event(3000, 2500, "This is a song\n"),
])

#: An automatic track: the same line re-emitted, growing, until it is closed.
ROLLING = track([
    {"tStartMs": 0, "dDurationMs": 0, "wpWinPosId": 1},
    word_group(20, 2400, "Never"),
    word_group(20, 2400, "Never gonna"),
    word_group(20, 2400, "Never gonna give you up\n"),
    word_group(3000, 2400, "Never"),
    word_group(3000, 2400, "Never gonna let you down\n"),
])

JSON3 = {"ext": "json3", "url": "https://www.youtube.com/api/timedtext?v=a&lang=th"}
JSON3_EN = {"ext": "json3", "url": "https://www.youtube.com/api/timedtext?v=a&lang=en"}
SRV3 = {"ext": "srv3", "url": "https://www.youtube.com/api/timedtext?v=a&lang=th"}

PAGE = "https://www.youtube.com/watch?v=abcdefghijk"


def spans(cues):
    return [(cue.start_ms, cue.end_ms, cue.text) for cue in cues]


class ManualTrackTests(unittest.TestCase):
    def test_one_cue_per_line_with_its_own_window(self):
        self.assertEqual(spans(subtitles.parse_json3(MANUAL)),
                         [(0, 3000, "Hello there"),
                          (3000, 5500, "This is a song")])

    def test_bytes_a_string_and_a_decoded_document_read_the_same(self):
        expected = spans(subtitles.parse_json3(MANUAL))
        self.assertEqual(spans(subtitles.parse_json3(MANUAL.decode("utf-8"))),
                         expected)
        self.assertEqual(
            spans(subtitles.parse_json3(json.loads(MANUAL.decode("utf-8")))),
            expected)

    def test_an_event_with_no_segments_is_not_a_line(self):
        cues = subtitles.parse_json3(track([
            {"tStartMs": 0, "dDurationMs": 0, "wsWinStyleId": 1},
            event(0, 2000, "\n"),
            event(2000, 2000, "Only line\n"),
        ]))
        self.assertEqual(spans(cues), [(2000, 4000, "Only line")])

    def test_a_line_split_across_events_is_joined_with_the_missing_space(self):
        cues = subtitles.parse_json3(track([
            event(0, 1000, "Hello"),
            event(1000, 2000, "there\n"),
        ]))
        self.assertEqual([cue.text for cue in cues], ["Hello there"])

    def test_word_timing_inside_one_event_does_not_split_its_line(self):
        cues = subtitles.parse_json3(track([
            {"tStartMs": 0, "dDurationMs": 2000, "segs": [
                {"utf8": "One"}, {"utf8": " two", "tOffsetMs": 700},
                {"utf8": " three\n"}]},
        ]))
        self.assertEqual([cue.text for cue in cues], ["One two three"])


class AutomaticTrackTests(unittest.TestCase):
    def test_a_line_re_emitted_as_it_grows_is_one_cue(self):
        self.assertEqual(spans(subtitles.parse_json3(ROLLING)),
                         [(20, 2420, "Never gonna give you up"),
                          (3000, 5400, "Never gonna let you down")])

    def test_a_line_that_grew_inside_its_own_cue_is_folded(self):
        # A track that closes every re-emission with its own newline: two cues
        # out of the line builder, one line out of the fold.
        cues = subtitles.parse_json3(track([
            event(0, 4000, "Never gonna\n"),
            event(1000, 4000, "Never gonna give\n"),
        ]))
        self.assertEqual(spans(cues), [(0, 5000, "Never gonna give")])

    def test_a_chorus_repeated_on_its_own_clock_stays_two_lines(self):
        cues = subtitles.parse_json3(track([
            event(0, 2000, "Yeah yeah yeah\n"),
            event(2000, 2000, "Yeah yeah yeah\n"),
        ]))
        self.assertEqual(len(cues), 2)

    def test_a_track_with_no_line_breaks_is_one_cue_per_event(self):
        cues = subtitles.parse_json3(track([
            word_group(0, 1000, "One"),
            word_group(1000, 1000, "Two"),
        ]))
        self.assertEqual([cue.text for cue in cues], ["One", "Two"])


class CleanedLineTests(unittest.TestCase):
    def test_music_marks_are_dropped_and_sound_tags_kept(self):
        cues = subtitles.parse_json3(track([
            event(0, 1000, "\u266a\u266a  [Laughter]  \u266b \n"),
        ]))
        self.assertEqual([cue.text for cue in cues], ["[Laughter]"])

    def test_a_very_long_line_is_cut_at_a_word(self):
        line = " ".join(f"word{index}" for index in range(80)) + "\n"
        cues = subtitles.parse_json3(track([event(0, 9000, line)]))
        text = cues[0].text
        self.assertLessEqual(len(text), subtitles.CUE_MAX_CHARS)
        self.assertEqual(text, text.rstrip())
        self.assertFalse(text.endswith("word"))

    def test_a_malformed_track_never_raises(self):
        for payload in (b"not json at all", b"", None, 7, {}, {"events": 5},
                        {"events": [None, 3, {}]}):
            self.assertEqual(subtitles.parse_json3(payload), [], payload)


class TrackSelectionTests(unittest.TestCase):
    def test_the_uploaders_own_track_wins_over_the_automatic_one(self):
        info = {"subtitles": {"th": [JSON3]},
                "automatic_captions": {"th": [JSON3]}}
        chosen = subtitles.select_track(info, ("th", "en"))
        self.assertEqual(chosen.language, "th")
        self.assertFalse(chosen.automatic)

    def test_the_language_order_is_the_players(self):
        info = {"subtitles": {"en": [JSON3_EN], "th": [JSON3]},
                "automatic_captions": {"th": [JSON3]}}
        self.assertEqual(subtitles.select_track(info, ("th", "en")).language, "th")
        self.assertEqual(subtitles.select_track(info, ("en", "th")).language, "en")

    def test_an_automatic_track_is_used_when_there_is_no_manual_one(self):
        info = {"subtitles": {}, "automatic_captions": {"th": [JSON3]}}
        chosen = subtitles.select_track(info, ("th", "en"))
        self.assertTrue(chosen.automatic)

    def test_a_language_without_a_json3_entry_is_not_read(self):
        info = {"subtitles": {"th": [SRV3]}, "automatic_captions": {}}
        self.assertIsNone(subtitles.select_track(info, ("th", "en")))

    def test_a_track_without_a_usable_url_is_not_read(self):
        for entry in ({"ext": "json3"}, {"ext": "json3", "url": ""},
                      {"ext": "json3", "url": "not a url"}):
            info = {"subtitles": {"th": [entry]}}
            self.assertIsNone(subtitles.select_track(info, ("th",)), entry)

    def test_nothing_to_choose_from_answers_none(self):
        self.assertIsNone(subtitles.select_track(None, ("th",)))
        self.assertIsNone(subtitles.select_track({"formats": []}, ("th",)))


# ── the fetch, through a yt-dlp stand-in ───────────────────────────────────

class FakeResponse:
    def __init__(self, payload):
        self.payload = payload
        self.closed = False

    def read(self, size=-1):
        return self.payload

    def close(self):
        self.closed = True


class FakeSession:
    """One prepared yt-dlp session: what it answers, and what it was asked."""

    def __init__(self, info=None, payload=MANUAL, error=None, url_error=None):
        self.info = info if info is not None else {"automatic_captions": {"th": [JSON3]}}
        self.payload = payload
        self.error = error
        self.url_error = url_error
        self.extract_calls = 0
        self.url_calls = []
        self.options = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def extract_info(self, url, download=False):
        self.extract_calls += 1
        self.extract_url = url
        if self.error is not None:
            raise self.error
        return self.info

    def urlopen(self, url):
        self.url_calls.append(url)
        if self.url_error is not None:
            raise self.url_error
        return FakeResponse(self.payload)


class FakeYdlModule:
    """A yt-dlp stand-in whose ``YoutubeDL`` hands back one prepared session."""

    def __init__(self, session):
        self.session = session
        self.options = []

    def YoutubeDL(self, options):
        self.options.append(options)
        return self.session


class CaptionFetcherTests(unittest.TestCase):
    def fetch(self, session, languages=("th",), **kwargs):
        fetcher = subtitles.CaptionFetcher(FakeYdlModule(session))
        with mock.patch.object(subtitles.logger, "log_exception"):
            return fetcher.fetch(PAGE, languages, **kwargs)

    def test_it_reads_the_track_the_language_order_asks_for(self):
        load = self.fetch(FakeSession())
        self.assertEqual(load.reason, "")
        self.assertEqual(load.language, "th")
        self.assertTrue(load.automatic)
        self.assertEqual([cue.text for cue in load.cues],
                         ["Hello there", "This is a song"])

    def test_the_page_is_fetched_once_however_often_the_song_repeats(self):
        session = FakeSession()
        fetcher = subtitles.CaptionFetcher(FakeYdlModule(session))
        for _ in range(3):
            fetcher.fetch(PAGE, ("th",))
        self.assertEqual(session.extract_calls, 1)
        self.assertEqual(len(session.url_calls), 1)

    def test_a_rate_limit_is_reported_as_busy_not_as_missing(self):
        load = self.fetch(FakeSession(error=RuntimeError("HTTP Error 429: Too Many Requests")))
        self.assertEqual(load.reason, subtitles.REASON_BUSY)
        self.assertIn("YouTube", subtitles.reason_sentence(load.reason))

    def test_a_video_without_captions_says_so(self):
        load = self.fetch(FakeSession(info={"formats": []}))
        self.assertEqual(load.reason, subtitles.REASON_NO_TRACK)
        self.assertEqual(subtitles.reason_sentence(load.reason),
                         "No subtitles for this video.")

    def test_a_track_that_is_not_a_caption_track_says_the_same(self):
        load = self.fetch(FakeSession(payload=b"<html>nope</html>"))
        self.assertEqual(load.reason, subtitles.REASON_NO_TRACK)

    def test_a_page_that_is_not_youtube_is_never_asked(self):
        session = FakeSession()
        load = subtitles.CaptionFetcher(FakeYdlModule(session)).fetch(
            "https://example.com/song", ("th",))
        self.assertEqual(load.reason, subtitles.REASON_FAILED)
        self.assertEqual(session.extract_calls, 0)

    def test_a_cancelled_fetch_answers_cancelled_between_its_two_steps(self):
        session = FakeSession()
        load = self.fetch(session, cancelled=lambda: True)
        self.assertEqual(load.reason, subtitles.REASON_CANCELLED)
        self.assertEqual(session.url_calls, [])

    def test_the_module_is_imported_once_and_only_when_it_is_needed(self):
        fetcher = subtitles.CaptionFetcher(FakeYdlModule(FakeSession()))
        with mock.patch.object(subtitles.importlib, "import_module") as importer:
            fetcher.fetch("https://example.com/song", ("th",))
        importer.assert_not_called()

    def test_a_failure_is_remembered_only_briefly(self):
        now = [0.0]
        session = FakeSession(error=RuntimeError("network down"))
        fetcher = subtitles.CaptionFetcher(FakeYdlModule(session),
                                           clock=lambda: now[0])
        with mock.patch.object(subtitles.logger, "log_exception"):
            self.assertEqual(fetcher.fetch(PAGE, ("th",)).reason,
                             subtitles.REASON_FAILED)
            fetcher.fetch(PAGE, ("th",))
            self.assertEqual(session.extract_calls, 1)
            now[0] = subtitles.NEGATIVE_TTL_S + 1
            session.error = None
            load = fetcher.fetch(PAGE, ("th",))
        self.assertEqual(session.extract_calls, 2)
        self.assertTrue(load.cues)


# ── aiming the cues at the song ────────────────────────────────────────────

def cue_list(*spans_):
    return tuple(subtitles.Cue(start, end, text) for start, end, text in spans_)


class SubtitleReaderTests(unittest.TestCase):
    def test_a_line_is_spoken_when_the_song_reaches_it(self):
        reader = subtitles.SubtitleReader()
        reader.load(cue_list((1000, 3000, "Hello"), (5000, 7000, "World")))
        self.assertEqual(reader.pump(0), [])
        self.assertEqual(reader.pump(999), [])
        self.assertEqual(reader.pump(1000), ["Hello"])

    def test_the_same_line_is_not_spoken_twice(self):
        reader = subtitles.SubtitleReader()
        reader.load(cue_list((1000, 3000, "Hello")))
        self.assertEqual(reader.pump(1000), ["Hello"])
        self.assertEqual(reader.pump(1200), [])
        self.assertEqual(reader.pump(2999), [])
        self.assertEqual(reader.spoken, 1)

    def test_a_line_the_song_has_already_passed_is_dropped(self):
        reader = subtitles.SubtitleReader()
        reader.load(cue_list((1000, 3000, "Hello")))
        self.assertEqual(reader.pump(1000 + subtitles.LATE_DROP_MS + 1), [])
        self.assertEqual(reader.dropped, 1)

    def test_the_line_the_song_is_on_now_is_the_one_read_not_the_backlog(self):
        reader = subtitles.SubtitleReader()
        reader.load(cue_list((0, 1000, "One"), (1000, 2000, "Two"),
                             (2000, 3000, "Three")))
        self.assertEqual(reader.pump(2600), ["Three"])
        self.assertEqual(reader.spoken, 1)
        self.assertEqual(reader.dropped, 2)

    def test_a_jump_forward_skips_without_a_backlog(self):
        reader = subtitles.SubtitleReader()
        reader.load(cue_list((0, 1000, "One"), (20000, 21000, "Two"),
                             (40000, 41000, "Three")))
        self.assertEqual(reader.pump(0), ["One"])
        self.assertEqual(reader.pump(60000), [])
        self.assertEqual(reader.pump(60010), [])
        self.assertEqual(reader.spoken, 1)

    def test_a_rewind_re_arms_the_lines_the_song_will_sing_again(self):
        reader = subtitles.SubtitleReader()
        reader.load(cue_list((1000, 3000, "Hello"), (5000, 7000, "World")))
        self.assertEqual(reader.pump(1000), ["Hello"])
        self.assertEqual(reader.pump(6000), ["World"])
        self.assertEqual(reader.pump(1000), ["Hello"])

    def test_the_sync_offset_moves_the_moment_either_way(self):
        early = subtitles.SubtitleReader()
        early.load(cue_list((1000, 3000, "Hello")))
        self.assertEqual(early.pump(600, offset_ms=500), ["Hello"])
        late = subtitles.SubtitleReader()
        late.load(cue_list((1000, 3000, "Hello")))
        self.assertEqual(late.pump(1000), ["Hello"])

    def test_a_reader_that_cannot_keep_up_stops_being_fed(self):
        # Cues that fire every half second but claim two seconds of the
        # reader's time each: the backlog passes MAX_LAG_MS and the line that
        # would go past it is skipped rather than queued.
        reader = subtitles.SubtitleReader()
        reader.load(cue_list(*[(start, start + 2000, f"L{start}")
                               for start in range(0, 3000, 500)]))
        spoken = [reader.pump(position) for position in range(0, 3000, 500)]
        self.assertEqual(sum(len(lines) for lines in spoken), 5)
        self.assertEqual(reader.spoken, 5)
        self.assertEqual(reader.dropped, 1)

    def test_nothing_is_read_before_a_track_is_loaded(self):
        reader = subtitles.SubtitleReader()
        self.assertEqual(reader.pump(9999), [])
        self.assertFalse(reader.ready)
        self.assertEqual(reader.pump(None), [])

    def test_clearing_the_reader_forgets_the_track(self):
        reader = subtitles.SubtitleReader()
        reader.load(cue_list((0, 1000, "Hello")), "th", automatic=True)
        reader.clear()
        self.assertFalse(reader.ready)
        self.assertEqual(reader.pump(100), [])
        self.assertEqual(reader.status(), "No captions loaded.")

    def test_the_status_line_says_what_is_loaded(self):
        reader = subtitles.SubtitleReader()
        reader.load(cue_list((1000, 3000, "Hello"), (5000, 7000, "World")), "th")
        reader.pump(1000)
        self.assertEqual(reader.status(), "Thai captions: 2 lines, 1 read, 0 skipped.")


class SettingLabelTests(unittest.TestCase):
    def test_a_preference_is_a_language_order(self):
        self.assertEqual(subtitles.languages_for("th"), ("th", "en"))
        self.assertEqual(subtitles.languages_for("en"), ("en", "th"))
        self.assertEqual(subtitles.languages_for("th-only"), ("th",))
        self.assertEqual(subtitles.languages_for("something else"), ("th", "en"))

    def test_a_stored_preference_that_is_not_ours_falls_back(self):
        self.assertEqual(subtitles.normalize_preference("th-only"), "th-only")
        self.assertEqual(subtitles.normalize_preference("klingon"),
                         subtitles.DEFAULT_LANGUAGE_PREFERENCE)

    def test_the_offset_is_clamped_and_speaks_its_direction(self):
        self.assertEqual(subtitles.normalize_offset("abc"), 0)
        self.assertEqual(subtitles.normalize_offset(99_000),
                         subtitles.OFFSET_LIMIT_MS)
        self.assertEqual(subtitles.offset_label(0),
                         "Timing: as the video timed it")
        self.assertIn("earlier", subtitles.offset_label(500))
        self.assertIn("later", subtitles.offset_label(-500))

    def test_only_a_youtube_page_has_captions_to_read(self):
        self.assertTrue(subtitles.is_caption_page(PAGE))
        self.assertTrue(subtitles.is_caption_page("https://youtu.be/abcdefghijk"))
        self.assertFalse(subtitles.is_caption_page("https://example.com/song"))
        self.assertFalse(subtitles.is_caption_page("C:/music/song.mp3"))
        self.assertFalse(subtitles.is_caption_page(""))


# ── the Music Bot wiring ───────────────────────────────────────────────────

class FakeMenu:
    """Drop-in for libs.menu.Menu capturing the items it was built with."""

    instances = []

    def __init__(self, game, title, parrent=None):
        self.game = game
        self.title = title
        self.parrent = parrent
        self.items = []
        self.speak_calls = 0
        FakeMenu.instances.append(self)

    def add_items(self, items):
        self.items = list(items)

    def speak_current_item(self):
        self.speak_calls += 1


def patch_menu():
    FakeMenu.instances = []
    return (mock.patch("libs.menu.Menu", FakeMenu),
            mock.patch("libs.menus.set_default_sounds"))


def last_menu():
    return FakeMenu.instances[-1]


def norm(label):
    return label() if callable(label) else label


def make_bot(**overrides):
    """Hermetic MapMusicBot built with __new__ (no game/OpenAL/network)."""
    bot = MapMusicBot.__new__(MapMusicBot)
    bot.subtitles_enabled = False
    bot.subtitle_language = subtitles.DEFAULT_LANGUAGE_PREFERENCE
    bot.subtitle_offset = 0
    bot.subtitle_reader = subtitles.SubtitleReader()
    bot._caption_fetcher = mock.MagicMock()
    bot._subtitle_generation = 0
    bot._subtitle_page = ""
    bot.current_target = PAGE
    bot.mode = "youtube"
    bot.game = SimpleNamespace(put=lambda fn: None, stack=[])
    bot._find_gameplay = lambda: None
    for key, value in overrides.items():
        setattr(bot, key, value)
    return bot


class InlineThread:
    """Runs the fetch body on the calling thread, so a test can watch it."""

    def __init__(self, target=None, daemon=None, **kwargs):
        self.target = target

    def start(self):
        self.target()


def source_of(name):
    """The source body of one MapMusicBot method (how a rule is placed)."""
    import inspect
    return inspect.getsource(getattr(MapMusicBot, name))


class TheSwitchTests(unittest.TestCase):
    def test_the_label_says_which_way_the_switch_is(self):
        bot = make_bot()
        self.assertEqual(bot.subtitle_label(), "Subtitles: OFF")
        bot.subtitles_enabled = True
        self.assertEqual(bot.subtitle_label(),
                         "Subtitles: Thai first, then English")

    def test_it_persists_and_says_which_way_it_went(self):
        bot = make_bot()
        with mock.patch.object(bot_module.options, "set") as opt_set, \
                mock.patch.object(bot_module, "speak") as speak, \
                mock.patch.object(bot_module.threading, "Thread", InlineThread):
            bot.toggle_subtitles()
        self.assertTrue(bot.subtitles_enabled)
        opt_set.assert_called_once_with("music_bot_subtitles", True)
        self.assertIn("Subtitles on", speak.call_args[0][0])
        with mock.patch.object(bot_module.options, "set") as opt_set, \
                mock.patch.object(bot_module, "speak") as speak:
            bot.toggle_subtitles()
        self.assertFalse(bot.subtitles_enabled)
        opt_set.assert_called_once_with("music_bot_subtitles", False)
        self.assertIn("Subtitles off", speak.call_args[0][0])
        self.assertEqual(bot.subtitle_label(), "Subtitles: OFF")


def loaded(language="th", automatic=False):
    """A fetch answer with the fixture track in it, reason and all."""
    return subtitles.CaptionLoad(cues=tuple(subtitles.parse_json3(MANUAL)),
                                 language=language, automatic=automatic,
                                 reason=subtitles.REASON_NONE)


class TheFetchTests(unittest.TestCase):
    def begin(self, bot, load=None, page=PAGE):
        """Run `_begin_subtitles` with the fetch and thread in hand."""
        delivered = []
        bot.game = SimpleNamespace(put=delivered.append, stack=[])
        bot._caption_fetcher = mock.MagicMock()
        bot._caption_fetcher.fetch.return_value = load
        with mock.patch.object(bot_module.threading, "Thread", InlineThread):
            bot._begin_subtitles(page)
        return delivered

    def test_a_fetch_is_asked_for_the_page_and_the_language_order(self):
        bot = make_bot(subtitles_enabled=True)
        self.begin(bot, subtitles.CaptionLoad())
        bot._caption_fetcher.fetch.assert_called_once()
        args, kwargs = bot._caption_fetcher.fetch.call_args
        self.assertEqual(args[0], PAGE)
        self.assertEqual(args[1], ("th", "en"))
        self.assertTrue(callable(kwargs["cancelled"]))

    def test_nothing_is_fetched_while_the_switch_is_off(self):
        bot = make_bot()
        self.assertEqual(self.begin(bot, subtitles.CaptionLoad()), [])
        bot._caption_fetcher.fetch.assert_not_called()

    def test_a_stream_with_no_youtube_page_fetches_nothing(self):
        bot = make_bot(subtitles_enabled=True)
        self.begin(bot, subtitles.CaptionLoad(), page="")
        bot._caption_fetcher.fetch.assert_not_called()

    def test_a_local_file_never_looks_for_captions(self):
        bot = make_bot(subtitles_enabled=True, mode="local")
        self.assertEqual(bot._caption_page(), "")
        bot.mode = "youtube"
        self.assertEqual(bot._caption_page(), PAGE)
        bot.current_target = "https://rr1---sn-x.googlevideo.com/videoplayback?x=1"
        self.assertEqual(bot._caption_page(), "")

    def test_loaded_captions_are_handed_to_the_reader_and_announced(self):
        bot = make_bot(subtitles_enabled=True)
        delivered = self.begin(bot, loaded())
        with mock.patch.object(bot_module, "speak") as speak:
            delivered[0]()
        self.assertTrue(bot.subtitle_reader.ready)
        self.assertEqual(bot.subtitle_reader.language, "th")
        self.assertIn("Thai captions for this track", speak.call_args[0][0])
        self.assertIn("2 lines", speak.call_args[0][0])

    def test_a_track_with_no_captions_says_so_out_loud(self):
        bot = make_bot(subtitles_enabled=True)
        delivered = self.begin(bot,
                               subtitles.CaptionLoad(reason=subtitles.REASON_NO_TRACK))
        with mock.patch.object(bot_module, "speak") as speak:
            delivered[0]()
        speak.assert_called_once_with("No subtitles for this video.")
        self.assertFalse(bot.subtitle_reader.ready)

    def test_an_answer_for_a_track_that_already_changed_is_dropped(self):
        bot = make_bot(subtitles_enabled=True)
        delivered = self.begin(bot, loaded())
        bot._stop_subtitles()
        with mock.patch.object(bot_module, "speak") as speak:
            delivered[0]()
        speak.assert_not_called()
        self.assertFalse(bot.subtitle_reader.ready)

    def test_a_cancelled_fetch_is_never_reported_to_the_player(self):
        bot = make_bot(subtitles_enabled=True)
        delivered = self.begin(
            bot, subtitles.CaptionLoad(reason=subtitles.REASON_CANCELLED))
        with mock.patch.object(bot_module, "speak") as speak:
            delivered[0]()
        speak.assert_not_called()

    def test_turning_the_switch_off_cancels_the_fetch_and_clears_the_track(self):
        bot = make_bot(subtitles_enabled=True)
        delivered = self.begin(bot, loaded())
        delivered[0]()
        self.assertTrue(bot.subtitle_reader.ready)
        bot.toggle_subtitles()
        self.assertFalse(bot.subtitle_reader.ready)
        self.assertEqual(bot.subtitle_reader.pump(60000), [])

    def test_stopping_the_bot_forgets_the_track(self):
        bot = make_bot(subtitles_enabled=True)
        delivered = self.begin(bot, loaded())
        delivered[0]()
        bot._stop_subtitles()
        self.assertFalse(bot.subtitle_reader.ready)
        self.assertEqual(bot._subtitle_page, "")

    def test_the_bot_stop_and_the_frame_loop_own_the_two_calls(self):
        # Where the pieces are wired: a stopped bot stops reading, and the
        # per-frame `loop` is what reads (nothing else calls the pump).
        stop_body = source_of("stop")
        self.assertIn("self._stop_subtitles()", stop_body)
        self.assertIn("self._pump_subtitles()", source_of("loop"))
        self.assertIn("self._begin_subtitles(canonical_url)",
                      source_of("_start_youtube_stream"))


class ThePumpTests(unittest.TestCase):
    def test_the_due_line_is_read_without_interrupting_anything(self):
        bot = make_bot(subtitles_enabled=True)
        bot.track_position = lambda: 1.5
        bot.subtitle_reader.load(cue_list((1000, 3000, "Hello")))
        with mock.patch.object(bot_module, "speak") as speak:
            bot._pump_subtitles()
        speak.assert_called_once_with("Hello", interupt=False)

    def test_nothing_is_read_while_the_switch_is_off(self):
        bot = make_bot()
        bot.track_position = lambda: 1.5
        bot.subtitle_reader.load(cue_list((1000, 3000, "Hello")))
        with mock.patch.object(bot_module, "speak") as speak:
            bot._pump_subtitles()
        speak.assert_not_called()

    def test_a_stream_with_no_position_yet_reads_nothing(self):
        bot = make_bot(subtitles_enabled=True)
        bot.track_position = lambda: None
        bot.subtitle_reader.load(cue_list((0, 3000, "Hello")))
        with mock.patch.object(bot_module, "speak") as speak:
            bot._pump_subtitles()
        speak.assert_not_called()

    def test_the_saved_offset_is_what_the_pump_aims_with(self):
        bot = make_bot(subtitles_enabled=True, subtitle_offset=500)
        bot.track_position = lambda: 0.6
        bot.subtitle_reader.load(cue_list((1000, 3000, "Hello")))
        with mock.patch.object(bot_module, "speak") as speak:
            bot._pump_subtitles()
        speak.assert_called_once_with("Hello", interupt=False)


class TheSubtitleMenuTests(unittest.TestCase):
    def open_menu(self, bot, opener="_open_subtitle_menu"):
        gp = SimpleNamespace(pop_last_substate=lambda: None,
                             add_substate=lambda menu: None)
        bot._find_gameplay = lambda: gp
        with patch_menu()[0], patch_menu()[1]:
            getattr(bot, opener)()
        return last_menu()

    def test_the_menu_carries_the_switch_the_language_the_timing_and_the_status(self):
        bot = make_bot()
        labels = [norm(label) for label, _action in
                  self.open_menu(bot).items]
        self.assertIn("Subtitles: OFF", labels)
        self.assertIn("Language: Thai first, then English", labels)
        self.assertIn("Timing: as the video timed it", labels)
        self.assertIn("No captions loaded.", labels)
        self.assertIn("Back", labels)

    def test_the_mode_menu_offers_the_one_line_onto_it(self):
        body = source_of("_show_mode_menu")
        self.assertIn("self.subtitle_label, self._open_subtitle_menu", body)

    def test_a_language_pick_is_persisted_and_re_reads_the_track(self):
        bot = make_bot(subtitles_enabled=True)
        menu = self.open_menu(bot, "_open_subtitle_language_menu")
        with mock.patch.object(bot_module.options, "set") as opt_set, \
                mock.patch.object(bot_module, "speak"), \
                mock.patch.object(bot, "_begin_subtitles") as begin:
            action = next(cb for label, cb in menu.items
                          if norm(label) == "Thai only")
            action()
        self.assertEqual(bot.subtitle_language, "th-only")
        opt_set.assert_called_once_with("music_bot_subtitle_language", "th-only")
        begin.assert_called_once_with(bot._caption_page())

    def test_the_timing_menu_moves_half_a_second_and_clamps(self):
        bot = make_bot(subtitles_enabled=True)
        menu = self.open_menu(bot, "_open_subtitle_timing_menu")
        earlier = next(cb for label, cb in menu.items
                       if str(label).startswith("Speak 0.5 seconds earlier"))
        later = next(cb for label, cb in menu.items
                     if str(label).startswith("Speak 0.5 seconds later"))
        with mock.patch.object(bot_module.options, "set") as opt_set, \
                mock.patch.object(bot_module, "speak"):
            earlier()
        self.assertEqual(bot.subtitle_offset, subtitles.OFFSET_STEP_MS)
        opt_set.assert_called_with("music_bot_subtitle_offset",
                                   subtitles.OFFSET_STEP_MS)
        with mock.patch.object(bot_module.options, "set"), \
                mock.patch.object(bot_module, "speak"):
            for _ in range(2):
                later()
        self.assertEqual(bot.subtitle_offset, -subtitles.OFFSET_STEP_MS)
        # Past the limit the line stops moving rather than walking off it.
        bot.subtitle_offset = subtitles.OFFSET_LIMIT_MS
        with mock.patch.object(bot_module.options, "set"), \
                mock.patch.object(bot_module, "speak"):
            earlier()
        self.assertEqual(bot.subtitle_offset, subtitles.OFFSET_LIMIT_MS)

    def test_turning_it_off_while_a_song_plays_stops_it_at_once(self):
        bot = make_bot(subtitles_enabled=True)
        bot.subtitle_reader.load(cue_list((0, 3000, "Hello")))
        bot.track_position = lambda: 1.0
        menu = self.open_menu(bot)
        with mock.patch.object(bot_module.options, "set"), \
                mock.patch.object(bot_module, "speak"):
            norm(menu.items[0][0]), menu.items[0][1]()
        self.assertFalse(bot.subtitles_enabled)
        self.assertFalse(bot.subtitle_reader.ready)


if __name__ == "__main__":
    unittest.main()
