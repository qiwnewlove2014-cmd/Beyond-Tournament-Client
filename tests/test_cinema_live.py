"""A cinema room that follows the map, and stays in step while it plays.

Two failures these tests pin down, both reported from a live test room:

* placing a speaker mid-song changed nothing until the routing was toggled off
  and on again (which stops and restarts the song), because the room was only
  ever resolved when a track started and a re-resolved room was treated as the
  same room whenever its anchor and profile name matched;
* pause/resume left the speakers permanently out of step (heard as one of them
  lagging the song), because pausing touched the single source the stream was
  handed and left the rest of the room playing.

Everything here is offline: the OpenAL objects are fakes, and the room's own
decisions are checked through the bank and the host.
"""

import importlib.util
import os
import queue
import sys
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

import cyal

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from libs import jukebox, jukebox_relay
from libs.audio.cinema import (AUTO_PROFILE, CinemaRenderer, CinemaSpeakerBank,
                               host_for, set_enabled)
from libs.audio.cinema.layout import CinemaSpeakerSpec
from libs.music_bot import MapMusicBot
from libs.music_bot.streaming import AudioStreamer
from libs.world_map import Map

ANCHOR = (10.0, 20.0, 0.0)


class FakeSource:
    """Minimal OpenAL source: a queue of buffers and a play state."""

    def __init__(self, context):
        self.context = context
        self.position = None
        self.rolloff_factor = None
        self.reference_distance = None
        self.max_distance = None
        self.spatialize = None
        self.direct_channels = None
        self.direct_filter = None
        self.gain = 0.0
        self.buffers = []
        self.buffers_processed = 0
        self.state = cyal.SourceState.STOPPED
        self.played = 0
        self.deleted = False

    @property
    def buffers_queued(self):
        return len(self.buffers)

    def play(self):
        self.played += 1
        self.state = cyal.SourceState.PLAYING

    def pause(self):
        self.state = cyal.SourceState.PAUSED

    def stop(self):
        self.state = cyal.SourceState.STOPPED

    def delete(self):
        self.deleted = True
        self.context.deleted.append(self)

    def queue_buffers(self, buffer):
        self.buffers.append(buffer)

    def unqueue_buffers(self):
        if self.buffers_processed <= 0:
            return None
        self.buffers_processed -= 1
        if self.buffers:
            self.buffers.pop(0)
        return []


class FakeBuffer:
    def __init__(self):
        self.data = None

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


class FakeAudio:
    def __init__(self):
        self.context = FakeContext()
        self.efx = SimpleNamespace(send=lambda *a, **k: None)
        self.filter = []
        self.position = ANCHOR
        self.volume_categories = {"jukebox": [100], "music": [100]}

    def gen_filter(self, kind, *params):
        return ("filter", kind, params)


class FakeGame:
    def __init__(self):
        self.audio_mngr = FakeAudio()
        # ``can_use_cinema_speakers`` stands in for the Server's login
        # snapshot: routing a bot into a room is Developer/Contributor only,
        # and a lower-ranked account gets False here (see
        # test_music_bot_cinema.CinemaRoutingPermissionTests).
        self.gameplay = SimpleNamespace(map=SimpleNamespace(), voice_chat=None,
                                        can_use_cinema_speakers=True)


def frame(tag, size=8):
    """A distinguishable stereo frame: ``tag`` in both channels."""
    return bytes([tag, 0] * size), bytes([tag, 1] * size)


def specs(profile_slots, anchor=ANCHOR, radius=8.0):
    """Specs placed on a ring around the anchor, in the room's own axes."""
    from math import cos, radians, sin
    from libs.audio.cinema.layout import IDEAL_BEARING
    out = []
    for slot in profile_slots:
        angle = radians(IDEAL_BEARING[slot])
        out.append(CinemaSpeakerSpec(
            slot,
            (anchor[0] + sin(angle) * radius,
             anchor[1] + cos(angle) * radius,
             anchor[2]),
        ))
    return out


class SpeakerDelayTests(unittest.TestCase):
    """A delay trim is decorrelation the room actually plays.

    The trim exists so a wall of speakers does not comb-filter into one thick
    centre: two speakers fed the same programme a few milliseconds apart read
    as a wide wall instead of a loud point. The cut is made per sample, not per
    frame, because a transport frame is 10-20 ms while the values an installer
    actually dials in are 1-30 ms -- rounding to whole frames would turn "5 ms"
    into nothing at all, which is how a control ends up looking broken.
    """

    SAMPLES = 480                  # 10 ms at 48 kHz: a real frame's size

    def make(self, delays):
        """A front pair, one entry per (slot, delay_ms)."""
        from math import cos, radians, sin
        from libs.audio.cinema.layout import IDEAL_BEARING
        game = FakeGame()
        room_specs = []
        for slot, delay in delays.items():
            angle = radians(IDEAL_BEARING[slot])
            room_specs.append(CinemaSpeakerSpec(
                slot,
                (ANCHOR[0] + sin(angle) * 8.0,
                 ANCHOR[1] + cos(angle) * 8.0,
                 ANCHOR[2]),
                delay_ms=delay,
            ))
        renderer = CinemaRenderer(ANCHOR, "front_only", specs=room_specs)
        return CinemaSpeakerBank(game, renderer, volume=100, cabinet_volume=100,
                                 occlusion_provider=lambda *a: 0)

    def frame(self, tag):
        return frame(tag, size=self.SAMPLES)

    def test_a_trimmed_speaker_plays_the_same_audio_a_trim_later(self):
        bank = self.make({"front_l": 0.0, "front_r": 5.0})
        for tag in range(4):
            self.assertTrue(bank.queue_frame(*self.frame(tag)))
        left = bank.slot_sources["front_l"].buffers[-1].data
        right = bank.slot_sources["front_r"].buffers[-1].data
        # 5 ms = 240 samples = 480 bytes back from the newest audio: the window
        # on the trimmed speaker is the tail of the frame before last plus the
        # head of the last frame -- the same music, 5 ms late, not silence and
        # not a copy.
        self.assertEqual(left, self.frame(3)[0])
        self.assertEqual(right, self.frame(2)[1][480:] + self.frame(3)[1][:480])

    def test_the_trim_is_not_rounded_to_a_whole_frame(self):
        bank = self.make({"front_l": 0.0, "front_r": 5.0})
        for tag in range(3):
            bank.queue_frame(*self.frame(tag))
        right = bank.slot_sources["front_r"].buffers[-1].data
        # A frame here is 10 ms, so a frame-granular implementation would have
        # applied either 0 or 10 ms and this would not line up.
        self.assertEqual(right, self.frame(1)[1][480:] + self.frame(2)[1][:480])

    def test_a_trim_longer_than_a_frame_waits_for_the_history(self):
        bank = self.make({"front_l": 0.0, "front_r": 20.0})
        # 20 ms is two frames: the first frames cannot be cut that far back, so
        # the trimmed speaker is simply not fed yet -- never a silent buffer.
        self.assertTrue(bank.queue_frame(*self.frame(0)))
        self.assertEqual(bank.slot_sources["front_r"].buffers_queued, 0)
        bank.queue_frame(*self.frame(1))
        self.assertEqual(bank.slot_sources["front_r"].buffers_queued, 0)
        bank.queue_frame(*self.frame(2))
        self.assertEqual(bank.slot_sources["front_r"].buffers_queued, 1)
        # Its first window is the whole first frame: 20 ms behind the room.
        self.assertEqual(bank.slot_sources["front_r"].buffers[-1].data,
                         self.frame(0)[1])

    def test_an_untouched_room_is_byte_for_byte_what_it_was(self):
        bank = self.make({"front_l": 0.0, "front_r": 0.0})
        for tag in range(3):
            bank.queue_frame(*self.frame(tag))
        self.assertEqual(bank.slot_sources["front_l"].buffers[-1].data,
                         self.frame(2)[0])
        self.assertEqual(bank.slot_sources["front_r"].buffers[-1].data,
                         self.frame(2)[1])

    def test_the_room_still_starts_and_plays_with_a_trim(self):
        bank = self.make({"front_l": 0.0, "front_r": 25.0})
        for tag in range(6):
            bank.queue_frame(*self.frame(tag))
        bank.start_playback()
        self.assertTrue(bank.playing())
        # The trimmed speaker holds less audio by design, and the room still
        # reports a queue the transport can measure.
        self.assertGreater(bank.queued_frames(), 0)
        self.assertLess(bank.queued_frames(),
                        bank.slot_sources["front_l"].buffers_queued)

    def test_a_trim_deeper_than_the_room_can_hold_still_plays(self):
        """Never a dead speaker: it plays the deepest cut the history reaches.

        The trim is played as a cut into audio the room still holds, so a
        speaker asking for more history than the room keeps (tiny frames, a
        deep trim) would otherwise never be fed at all -- silent for the whole
        song with nothing on screen to say why.
        """
        def tiny(tag):
            return frame(tag, size=8)                  # 8 samples, not 480

        bank = self.make({"front_l": 0.0, "front_r": 100.0})
        for tag in range(12):
            bank.queue_frame(*tiny(tag))
        right = bank.slot_sources["front_r"]
        # 100 ms is 4800 samples, the room's history reaches 88, so it plays
        # the oldest frame it still holds rather than nothing at all.
        self.assertEqual(right.buffers_queued, 1)
        self.assertEqual(right.buffers[-1].data, tiny(0)[1])
        self.assertNotEqual(right.buffers[-1].data, tiny(11)[1])

    def test_a_room_where_every_speaker_carries_a_trim_still_plays(self):
        """Nothing else can seed the room's history once every speaker is late.

        A trim is played as a cut into the audio the room still holds, so a
        trimmed speaker is only fed once that much audio is queued -- a speaker
        starting a few milliseconds late is what a trim IS. That wait needs
        some other speaker to queue the first frames, and a room whose every
        speaker was given a delay has none: the room never queued its first
        frame, so it never had the history it was waiting for and stayed silent
        for the whole song (the reported "I set a delay on each speaker and
        cinema mode went quiet").
        """
        bank = self.make({"front_l": 20.0, "front_r": 20.0})
        for tag in range(6):
            self.assertTrue(bank.queue_frame(*self.frame(tag)))

        for slot in ("front_l", "front_r"):
            self.assertEqual(bank.slot_sources[slot].buffers_queued, 6, slot)
        self.assertEqual(bank.frames_queued, 6)
        # The first frames play a shorter cut (the room cannot reach 20 ms back
        # yet); the full trim is in force as soon as the history is deep
        # enough, which here is the same programme 20 ms -- one frame's worth --
        # behind the live frame.
        self.assertEqual(bank.slot_sources["front_l"].buffers[-1].data,
                         self.frame(3)[0])

    def test_realign_refills_a_trimmed_speaker_with_trimmed_frames(self):
        """A refill hands it the delayed programme, never the live frame.

        Filling it like an untrimmed speaker lets it catch up with the room
        for as long as that fill lasts and then jump backwards when the next
        trimmed window arrives -- a slip in one speaker, right after a pause,
        which is exactly when a room is most likely to be re-formed.
        """
        bank = self.make({"front_l": 0.0, "front_r": 5.0})
        for tag in range(8):
            bank.queue_frame(*self.frame(tag))
        bank.start_playback()
        right = bank.slot_sources["front_r"]
        for _ in range(3):
            right.buffers.pop(0)          # it comes back shallow
        recorded = spy_render(bank.renderer)

        self.assertTrue(bank.realign(play=False))

        # Every frame it is given is cut five milliseconds back from its own
        # end, so its queue stays a trim behind the room instead of level with
        # it. The oldest frame has no history to cut into, so it is skipped --
        # exactly as it was while the room was first filling.
        self.assertEqual(recorded, [
            (self.frame(0)[0][480:] + self.frame(1)[0][:480],
             self.frame(0)[1][480:] + self.frame(1)[1][:480]),
            (self.frame(1)[0][480:] + self.frame(2)[0][:480],
             self.frame(1)[1][480:] + self.frame(2)[1][:480]),
            (self.frame(2)[0][480:] + self.frame(3)[0][:480],
             self.frame(2)[1][480:] + self.frame(3)[1][:480]),
        ])
        self.assertEqual(right.buffers_queued, 7)


class RoomLagMeasurementTests(unittest.TestCase):
    """What a remote instrument note has to wait out to land on the beat.

    Remote jam notes are delayed by how far behind the song's shared clock the
    listener's own audio is. A room is the one output whose audio does not sit
    on a single source, so it has to measure itself: one queued frame per
    speaker IS the backlog once the frame's size is known, and a per-speaker
    delay trim is latency the song gains that no queue depth can see.
    """

    def make(self, delays=None, **kwargs):
        from math import cos, radians, sin
        from libs.audio.cinema.layout import IDEAL_BEARING
        game = FakeGame()
        room_specs = []
        for slot, delay in (delays or {"front_l": 0.0, "front_r": 0.0}).items():
            angle = radians(IDEAL_BEARING[slot])
            room_specs.append(CinemaSpeakerSpec(
                slot,
                (ANCHOR[0] + sin(angle) * 8.0,
                 ANCHOR[1] + cos(angle) * 8.0,
                 ANCHOR[2]),
                delay_ms=delay,
            ))
        renderer = CinemaRenderer(ANCHOR, "front_only", specs=room_specs)
        return CinemaSpeakerBank(game, renderer, volume=100, cabinet_volume=100,
                                 occlusion_provider=lambda *a: 0, **kwargs)

    def test_a_frame_is_measured_not_assumed(self):
        """The direct streamer hands 20 ms frames and the relay 40 ms."""
        bank = self.make()
        self.assertEqual(bank.frame_ms(), 20.0)          # nothing queued yet
        bank.queue_frame(*frame(0, size=960))            # 20 ms at 48 kHz
        self.assertEqual(bank.frame_ms(), 20.0)
        bank.queue_frame(*frame(1, size=1920))           # the relay's 40 ms
        self.assertEqual(bank.frame_ms(), 40.0)

    def test_the_queue_depth_is_the_rooms_distance_behind_the_live_edge(self):
        bank = self.make()
        for tag in range(5):
            bank.queue_frame(*frame(tag, size=960))
        self.assertEqual(bank.buffered_ms(), 100)

    def test_a_room_is_never_reported_as_level_with_the_live_edge(self):
        # Same floor as the plain stereo path: the frame being staged right
        # now is still ahead of what is audible.
        bank = self.make()
        self.assertEqual(bank.buffered_ms(), 20)

    def test_delay_trims_are_latency_the_queue_cannot_see(self):
        bank = self.make({"front_l": 0.0, "front_r": 60.0})
        for tag in range(6):
            bank.queue_frame(*frame(tag, size=960))
        # The trimmed speaker is fed the same programme 60 ms late, so the
        # room has gained 60 ms on top of whatever it has queued.
        self.assertEqual(bank.extra_latency_ms(), 60)

    def test_an_untrimmed_room_adds_nothing(self):
        bank = self.make()
        for tag in range(6):
            bank.queue_frame(*frame(tag, size=960))
        self.assertEqual(bank.extra_latency_ms(), 0)
        self.assertEqual(bank.buffered_ms(), 120)


class RendererSignatureTests(unittest.TestCase):
    """The room has to be able to tell "the same room" from "a changed one"."""

    def test_the_same_speakers_are_the_same_room(self):
        first = CinemaRenderer(ANCHOR, "front_stage",
                               specs=specs(["front_l", "front_c", "front_r"]))
        second = CinemaRenderer(ANCHOR, "front_stage",
                                specs=specs(["front_l", "front_c", "front_r"]))
        self.assertEqual(first.signature, second.signature)

    def test_an_added_speaker_is_a_different_room(self):
        before = CinemaRenderer(ANCHOR, "front_only", specs=specs(["front_l", "front_r"]))
        after = CinemaRenderer(ANCHOR, "front_stage",
                               specs=specs(["front_l", "front_c", "front_r"]))
        self.assertNotEqual(before.signature, after.signature)

    def test_a_moved_or_trimmed_speaker_is_a_different_room(self):
        here = specs(["front_l", "front_r"])
        moved = specs(["front_l", "front_r"])
        moved[1].position = (moved[1].position[0] + 2.0, moved[1].position[1],
                             moved[1].position[2])
        trimmed = specs(["front_l", "front_r"])
        trimmed[0].level = 0.5
        base = CinemaRenderer(ANCHOR, "front_only", specs=here)
        self.assertNotEqual(base.signature, CinemaRenderer(
            ANCHOR, "front_only", specs=moved).signature)
        self.assertNotEqual(base.signature, CinemaRenderer(
            ANCHOR, "front_only", specs=trimmed).signature)


def spy_render(renderer):
    """Record the stereo frames a renderer is asked for, in order."""
    recorded = []
    original = renderer.render

    def wrapper(left, right):
        recorded.append((left, right))
        return original(left, right)

    renderer.render = wrapper
    return recorded


class BankReshapeTests(unittest.TestCase):
    """Re-shaping a room in place, so the stream feeding it never stops."""

    def make(self, slots, profile=None):
        game = FakeGame()
        renderer = CinemaRenderer(ANCHOR, profile, specs=specs(slots))
        bank = CinemaSpeakerBank(game, renderer, volume=100, cabinet_volume=100,
                                 occlusion_provider=lambda *a: 0)
        return game, bank

    def prime(self, bank, count=3):
        for index in range(count):
            self.assertTrue(bank.queue_frame(*frame(index)))
        bank.start_playback()

    def test_a_placed_speaker_joins_the_room_that_is_playing(self):
        game, bank = self.make(["front_l", "front_r"], profile="front_only")
        self.prime(bank)
        left, right = bank.slot_sources["front_l"], bank.slot_sources["front_r"]
        created_before = len(game.audio_mngr.context.created)

        self.assertTrue(bank.reconfigure(CinemaRenderer(
            ANCHOR, "front_stage", specs=specs(["front_l", "front_c", "front_r"]))))

        self.assertEqual(bank.renderer.profile.name, "front_stage")
        self.assertEqual(len(bank.sources), 3)
        # The speakers that did not change are the same OpenAL sources, still
        # holding what they were playing.
        self.assertIs(bank.slot_sources["front_l"], left)
        self.assertIs(bank.slot_sources["front_r"], right)
        self.assertEqual(left.buffers_queued, 3)
        # The new one is a fresh source, already fed like the rest.
        self.assertEqual(len(game.audio_mngr.context.created), created_before + 1)
        centre = bank.slot_sources["front_c"]
        self.assertEqual(centre.buffers_queued, 3)
        self.assertEqual(centre.state, cyal.SourceState.PLAYING)

    def test_the_new_speaker_holds_the_frames_the_room_is_playing(self):
        game, bank = self.make(["front_l", "front_r"], profile="front_only")
        self.prime(bank, 2)
        held = list(bank._recent)
        room = CinemaRenderer(ANCHOR, "front_stage",
                              specs=specs(["front_l", "front_c", "front_r"]))
        recorded = spy_render(room)
        bank.reconfigure(room)
        # It is fed the frames the room is still holding, in order, so it
        # starts on the same content instant instead of at the front of a
        # fresh queue (a whole queue's worth ahead of the room).
        centre = bank.slot_sources["front_c"]
        self.assertEqual(len(centre.buffers), 2)
        self.assertEqual(recorded, held)

    def test_a_deleted_speaker_leaves_and_the_rest_play_on(self):
        game, bank = self.make(["front_l", "front_c", "front_r"], profile="front_stage")
        self.prime(bank)
        centre = bank.slot_sources["front_c"]
        left = bank.slot_sources["front_l"]

        self.assertTrue(bank.reconfigure(CinemaRenderer(
            ANCHOR, "front_only", specs=specs(["front_l", "front_r"]))))

        self.assertEqual(len(bank.sources), 2)
        self.assertTrue(centre.deleted)
        self.assertNotIn("front_c", bank.slot_sources)
        self.assertNotIn("front_c", bank._pools)
        self.assertIs(bank.slot_sources["front_l"], left)
        self.assertEqual(left.buffers_queued, 3)

    def test_an_unchanged_room_is_left_alone(self):
        game, bank = self.make(["front_l", "front_r"], profile="front_only")
        self.prime(bank)
        created_before = len(game.audio_mngr.context.created)
        self.assertFalse(bank.reconfigure(CinemaRenderer(
            ANCHOR, "front_only", specs=specs(["front_l", "front_r"]))))
        self.assertEqual(len(game.audio_mngr.context.created), created_before)
        self.assertTrue(bank.playing())

    def test_a_paused_room_stays_paused_when_it_is_reshaped(self):
        game, bank = self.make(["front_l", "front_r"], profile="front_only")
        self.prime(bank)
        bank.set_paused(True)
        bank.reconfigure(CinemaRenderer(
            ANCHOR, "front_stage", specs=specs(["front_l", "front_c", "front_r"])))
        self.assertNotEqual(bank.slot_sources["front_c"].state,
                            cyal.SourceState.PLAYING)
        bank.set_paused(False)
        self.assertTrue(bank.playing())


class BankLockStepTests(unittest.TestCase):
    """No speaker may keep playing a different instant of the song."""

    def make(self, slots, profile="front_stage"):
        game = FakeGame()
        renderer = CinemaRenderer(ANCHOR, profile, specs=specs(slots))
        bank = CinemaSpeakerBank(game, renderer, volume=100, cabinet_volume=100,
                                 occlusion_provider=lambda *a: 0)
        return game, bank

    def test_a_stopped_speaker_is_not_fed_the_frames_it_missed(self):
        """Feeding it would restart it behind the room for the whole song."""
        game, bank = self.make(["front_l", "front_c", "front_r"])
        bank.queue_frame(*frame(0))
        bank.queue_frame(*frame(1))
        bank.start_playback()
        centre = bank.slot_sources["front_c"]
        # It ran dry: nothing queued, not playing.
        centre.buffers.clear()
        centre.state = cyal.SourceState.STOPPED

        self.assertNotIn("front_c", bank._feed_slots())
        self.assertTrue(bank.queue_frame(*frame(2)))
        self.assertEqual(centre.buffers_queued, 0)
        self.assertEqual(bank.slot_sources["front_l"].buffers_queued, 3)
        # The room reports the queues it actually feeds, so the transports'
        # backpressure still measures the room and not a dead speaker.
        self.assertEqual(bank.queued_frames(), 3)

    def test_starting_a_speaker_puts_it_back_on_the_room_s_instant(self):
        game, bank = self.make(["front_l", "front_c", "front_r"])
        bank.queue_frame(*frame(0))
        bank.queue_frame(*frame(1))
        bank.start_playback()
        centre = bank.slot_sources["front_c"]
        centre.buffers.clear()
        centre.state = cyal.SourceState.STOPPED
        bank.queue_frame(*frame(2))
        recorded = spy_render(bank.renderer)

        bank.start_playback()

        # It plays the same frames the rest of the room is holding, in order.
        self.assertEqual(centre.buffers_queued, 3)
        self.assertEqual(bank.slot_sources["front_l"].buffers_queued, 3)
        self.assertEqual(recorded, list(bank._recent)[-3:])
        self.assertTrue(bank.playing())

    def test_a_shallow_speaker_gets_only_the_frames_it_is_missing(self):
        game, bank = self.make(["front_l", "front_c", "front_r"])
        for index in range(3):
            bank.queue_frame(*frame(index))
        bank.start_playback()
        centre = bank.slot_sources["front_c"]
        centre.buffers.pop(0)
        centre.buffers.pop(0)          # holds one of the three
        held = list(bank._recent)
        recorded = spy_render(bank.renderer)
        self.assertTrue(bank.realign(play=False))
        self.assertEqual(centre.buffers_queued, 3)
        # The two frames before the one it still held -- not the two newest.
        self.assertEqual(recorded, held[:-1])

    def test_realign_does_nothing_when_the_room_is_already_level(self):
        game, bank = self.make(["front_l", "front_r"])
        for index in range(2):
            bank.queue_frame(*frame(index))
        bank.start_playback()
        recorded = spy_render(bank.renderer)
        self.assertFalse(bank.realign(play=False))
        self.assertEqual(recorded, [])

    def test_pausing_holds_and_releases_every_speaker(self):
        game, bank = self.make(["front_l", "front_c", "front_r",
                                "side_l", "side_r"])
        for index in range(2):
            bank.queue_frame(*frame(index))
        bank.start_playback()
        self.assertTrue(bank.set_paused(True))
        states = {source.state for source in bank.sources}
        self.assertEqual(states, {cyal.SourceState.PAUSED})
        self.assertTrue(bank.set_paused(False))
        self.assertTrue(bank.playing())


class HostReshapeTests(unittest.TestCase):
    """What the jukebox and the music bot actually ask for."""

    def build(self, entries):
        from libs.audio.cinema import map_speakers

        def place(map_obj, entries):
            from math import cos, radians, sin
            for channel, bearing, *rest in entries:
                level = rest[0] if rest else 100
                angle = radians(bearing)
                x = ANCHOR[0] + sin(angle) * 8.0
                y = ANCHOR[1] + cos(angle) * 8.0
                map_obj.spawn_cinemaSpeaker(minx=x - 0.5, maxx=x + 0.5,
                                            miny=y - 0.5, maxy=y + 0.5,
                                            minz=0, maxz=1,
                                            id=f"spk_{channel}_{bearing}",
                                            channel=channel, level=level)

        game = FakeGame()
        set_enabled(game, True)
        map_obj = Map(game)
        place(map_obj, entries)
        game.gameplay.map = map_obj
        self.map = map_obj
        self.place = place
        return game

    def acquire(self, host, game, cabinet="j1", anchor=ANCHOR):
        """The call the jukebox makes: resolve the map, then take the room."""
        from libs.audio.cinema import cinema_room
        plan = cinema_room(game, cabinet, anchor)
        if plan is None:
            return None
        return host.acquire_bank(cabinet, anchor, profile=plan.profile,
                                 specs=plan.specs, placement=plan.placement)

    def test_adding_a_speaker_re_shapes_the_bank_in_place(self):
        game = self.build([("front_l", -30), ("front_r", 30)])
        host = host_for(game)
        bank = self.acquire(host, game)
        self.assertEqual(len(bank.sources), 2)
        self.assertEqual(bank.renderer.profile.name, "front_only")
        left = bank.slot_sources["front_l"]

        self.place(self.map, [("front_c", 0)])

        again = self.acquire(host, game)
        self.assertIs(again, bank)
        self.assertEqual(bank.renderer.profile.name, "front_stage")
        self.assertIs(bank.slot_sources["front_l"], left)
        self.assertIn("front_c", bank.slot_sources)

    def test_the_room_is_only_reshaped_when_it_actually_changed(self):
        game = self.build([("front_l", -30), ("front_r", 30)])
        host = host_for(game)
        bank = self.acquire(host, game)
        first_renderer = bank.renderer
        first_source = bank.slot_sources["front_l"]
        self.assertIs(self.acquire(host, game), bank)
        self.assertIs(bank.renderer, first_renderer)
        self.assertIs(bank.slot_sources["front_l"], first_source)

    def test_a_cabinet_anchor_is_read_from_the_map(self):
        from libs.audio.cinema import cabinet_anchor
        game = self.build([("front_l", -30), ("front_r", 30)])
        self.map.spawn_jukebox(minx=9, maxx=10, miny=19, maxy=20, minz=0, maxz=1,
                               id="j1")
        self.assertEqual(cabinet_anchor(game, "j1"), (9.5, 19.5, 0.5))
        self.assertIsNone(cabinet_anchor(game, "nope"))


class JukeboxLiveRoomTests(unittest.TestCase):
    """The cabinet's own playback follows the map while the song plays."""

    def build(self, entries):
        from math import cos, radians, sin
        game = FakeGame()
        set_enabled(game, True)
        map_obj = Map(game)
        for index, (channel, bearing) in enumerate(entries):
            angle = radians(bearing)
            x = ANCHOR[0] + sin(angle) * 8.0
            y = ANCHOR[1] + cos(angle) * 8.0
            map_obj.spawn_cinemaSpeaker(minx=x - 0.5, maxx=x + 0.5, miny=y - 0.5,
                                        maxy=y + 0.5, minz=0, maxz=1,
                                        id=f"spk{index}", channel=channel)
        map_obj.spawn_jukebox(minx=9, maxx=10, miny=19, maxy=20, minz=0, maxz=1,
                              id="j1")
        game.gameplay.map = map_obj
        self.map = map_obj
        player = jukebox.JukeboxPlayer(game)
        with mock.patch("libs.music_bot.AudioStreamer") as streamer:
            player.play("j1", 9.5, 19.5, 0.5, "Song", "http://example.com/a.mp3",
                        60, transport="direct")
        self.streamer = streamer
        return game, player

    def add(self, channel, bearing):
        from math import cos, radians, sin
        index = len(self.map.cinema_speaker_list)
        angle = radians(bearing)
        x = ANCHOR[0] + sin(angle) * 8.0
        y = ANCHOR[1] + cos(angle) * 8.0
        self.map.spawn_cinemaSpeaker(minx=x - 0.5, maxx=x + 0.5, miny=y - 0.5,
                                     maxy=y + 0.5, minz=0, maxz=1,
                                     id=f"spk{index}", channel=channel)

    def test_a_speaker_placed_mid_song_joins_without_restarting_it(self):
        game, player = self.build([("front_l", -30), ("front_r", 30)])
        entry = player.players["j1"]
        bank, streamer = entry["cinema"], entry["streamer"]
        self.assertEqual(len(bank.sources), 2)
        sources_before = len(game.audio_mngr.context.created)

        self.add("front_c", 0)
        player.refresh_cinema_rooms(time.monotonic() + 2.0)

        # The very same room and the very same stream: nothing was restarted.
        self.assertIs(player.players["j1"]["cinema"], bank)
        self.assertIs(player.players["j1"]["streamer"], streamer)
        self.assertEqual(len(game.audio_mngr.context.created), sources_before + 1)
        self.assertEqual(len(bank.sources), 3)
        self.assertEqual(bank.renderer.profile.name, "front_stage")

    def test_the_refresh_is_throttled(self):
        game, player = self.build([("front_l", -30), ("front_r", 30)])
        bank = player.players["j1"]["cinema"]
        now = time.monotonic()
        player._cinema_refresh_at = now          # as if it had just refreshed
        self.add("front_c", 0)
        self.assertEqual(player.refresh_cinema_rooms(now), 0)
        self.add("side_l", -90)
        self.add("side_r", 90)
        self.assertEqual(player.refresh_cinema_rooms(now + 0.2), 0)
        self.assertEqual(len(bank.sources), 2)
        # Once the window is over the room catches up on all of it at once.
        self.assertEqual(player.refresh_cinema_rooms(now + 5.0), 1)
        self.assertEqual(len(bank.sources), 5)

    def test_deleting_the_speakers_leaves_the_song_playing(self):
        game, player = self.build([("front_l", -30), ("front_r", 30)])
        bank = player.players["j1"]["cinema"]
        self.map.cinema_speaker_list = []
        player.refresh_cinema_rooms(time.monotonic() + 2.0)
        self.assertIs(player.players["j1"]["cinema"], bank)
        self.assertEqual(len(bank.sources), 2)
        self.assertFalse(bank._stopped)

    def test_the_update_loop_drives_the_refresh(self):
        game, player = self.build([("front_l", -30), ("front_r", 30)])
        bank = player.players["j1"]["cinema"]
        self.add("front_c", 0)
        player._cinema_refresh_at = time.monotonic() - 5.0
        player.update()
        self.assertEqual(len(bank.sources), 3)

    def test_a_map_reload_with_an_unchanged_room_builds_no_new_sources(self):
        game, player = self.build([("front_l", -30), ("front_r", 30)])
        bank = player.players["j1"]["cinema"]
        created = len(game.audio_mngr.context.created)
        player.refresh_cinema_rooms(time.monotonic() + 2.0)
        self.assertEqual(len(game.audio_mngr.context.created), created)
        self.assertIs(player.players["j1"]["cinema"], bank)


class PooledSource:
    """A speaker source that models OpenAL's processed-buffer queue."""

    def __init__(self, context):
        self.context = context
        self.position = None
        self.rolloff_factor = None
        self.reference_distance = None
        self.max_distance = None
        self.spatialize = None
        self.direct_channels = None
        self.direct_filter = None
        self.gain = 0.0
        self.state = cyal.SourceState.PLAYING
        self.deleted = False
        self._queued = []
        self._processed = []

    @property
    def buffers_queued(self):
        return len(self._queued)

    @property
    def buffers_processed(self):
        return len(self._processed)

    def queue_buffers(self, buffer):
        self._queued.append(buffer)

    def unqueue_buffers(self):
        if not self._processed:
            return None
        return [self._processed.pop(0)]

    def finish_one(self):
        """OpenAL finished consuming the oldest queued buffer."""
        if self._queued:
            self._processed.append(self._queued.pop(0))

    def play(self):
        self.state = cyal.SourceState.PLAYING

    def pause(self):
        self.state = cyal.SourceState.PAUSED

    def stop(self):
        self.state = cyal.SourceState.STOPPED

    def delete(self):
        self.deleted = True
        self.context.deleted.append(self)


class PooledContext:
    def __init__(self):
        self.created = []
        self.deleted = []

    def gen_source(self, **kwargs):
        source = PooledSource(self)
        self.created.append(source)
        return source

    def gen_buffer(self):
        return FakeBuffer()


class PooledAudio(FakeAudio):
    def __init__(self):
        super().__init__()
        self.context = PooledContext()


class RelayRoomReclaimTests(unittest.TestCase):
    """A relay-fed room must get its own buffers back from the relay pump.

    The receiver unqueues whatever OpenAL has finished and returns it to a
    pool. With a room set, the pools that matter are the room's per-speaker
    ones: unqueueing into the receiver's own pool drained the room instead,
    so it played the handful of frames it started with and then refused every
    later frame. Every packet-based check still called that "healthy" and the
    watchdog rebuilt the room every ~8s -- heard as a song that cuts in and
    out, on a map with a cinema cabinet only.
    """

    def build(self):
        game = FakeGame()
        game.audio_mngr = PooledAudio()
        set_enabled(game, True)
        renderer = CinemaRenderer(
            ANCHOR, "theatre",
            specs=specs(["front_l", "front_r", "rear_l", "rear_r"]))
        bank = CinemaSpeakerBank(game, renderer, volume=100, cabinet_volume=100,
                                 occlusion_provider=lambda *a: 0)
        receiver = jukebox_relay.JukeboxRelayReceiver(
            game, None, None, 100, 1, 2, 8.0, 40.0, box_pos=ANCHOR,
            player=SimpleNamespace(occlusion_tier=lambda *a: 0),
            cinema=bank, clock=time.monotonic)
        return bank, receiver

    def test_the_relay_pump_returns_the_rooms_buffers_to_the_room(self):
        bank, receiver = self.build()
        for index in range(bank.buffers_per_slot):
            self.assertTrue(bank.queue_frame(*frame(index)))
        # Every speaker's pool is now empty, and OpenAL has finished one
        # buffer on each -- exactly the state a pump cycle reclaims from.
        self.assertTrue(all(not pool for pool in bank._pools.values()))
        for source in bank.sources:
            source.finish_one()

        receiver._reclaim()

        for slot in bank.renderer.slots:
            self.assertEqual(len(bank._pools[slot]), 1,
                             f"{slot} did not get its buffer back")
        self.assertEqual(receiver._pool, [])
        self.assertIsNotNone(receiver.last_output_at)
        # The point of it all: the room can still be fed afterwards.
        self.assertTrue(bank.queue_frame(*frame(9)))

    def test_the_room_keeps_taking_frames_after_its_first_pool(self):
        """The whole song, not just the first few buffers of it."""
        bank, receiver = self.build()
        self.assertTrue(bank.queue_frame(*frame(0)))
        for index in range(bank.buffers_per_slot * 5):
            for source in bank.sources:
                source.finish_one()
            receiver._reclaim()
            self.assertTrue(bank.queue_frame(*frame(index % 200)),
                            f"the room refused frame {index}")


class RoomSwitchTests(unittest.TestCase):
    """Switching a cabinet's shape while it plays must not break the room.

    Reported from a live test room as "switching the mode is slow, and then
    the log fills with `cinema room was lost; awaiting recovery`":

    * the per-second map refresh re-resolved with the *newly picked* mode, so
      the running room was reshaped to a shape the server was about to
      replace it with -- churn in the window between the pick and the re-offer;
    * more seriously, a **relay** room was never marked as retiring (only the
      direct path marked it), so the replacement song was handed the very
      room being torn down; the receiver's cleanup then stopped and
      unregistered the room the new song was playing through, and every
      refresh after that reported the room lost, so the stream was silent
      until the watchdog rebuilt it.
    """

    ROOM = [("front_l", -30), ("front_r", 30), ("rear_l", 150), ("rear_r", 210)]

    def setUp(self):
        # The relay receiver's worker decodes in its own thread.
        self.pyogg_patch = mock.patch.dict(sys.modules, {
            "pyogg": SimpleNamespace(OpusDecoder=_FakeOpusDecoder),
        })
        self.pyogg_patch.start()
        self.receivers = []

    def tearDown(self):
        for receiver in self.receivers:
            try:
                receiver.stop()
            except Exception:
                pass
        self.pyogg_patch.stop()

    def build(self, transport="relay"):
        from math import cos, radians, sin
        game = FakeGame()
        set_enabled(game, True)
        map_obj = Map(game)
        for index, (channel, bearing) in enumerate(self.ROOM):
            angle = radians(bearing)
            x = ANCHOR[0] + sin(angle) * 8.0
            y = ANCHOR[1] + cos(angle) * 8.0
            map_obj.spawn_cinemaSpeaker(minx=x - 0.5, maxx=x + 0.5, miny=y - 0.5,
                                        maxy=y + 0.5, minz=0, maxz=1,
                                        id=f"spk{index}", channel=channel)
        map_obj.spawn_jukebox(minx=9, maxx=10, miny=19, maxy=20, minz=0, maxz=1,
                              id="j1")
        game.gameplay.map = map_obj
        player = jukebox.JukeboxPlayer(game)
        self.play(player, transport, "auto")
        return game, player

    def play(self, player, transport, mode):
        kwargs = {"transport": transport}
        if transport == "relay":
            kwargs.update(relay_id=1, stream_epoch=2)
        with mock.patch("libs.music_bot.AudioStreamer"):
            player.play("j1", 9.5, 19.5, 0.5, "Song", "http://example.com/a.mp3",
                        60, cinema_mode=mode, **kwargs)
        entry = player.players.get("j1") or {}
        if entry.get("streamer") is not None:
            self.receivers.append(entry["streamer"])

    def test_picking_a_shape_does_not_reshape_the_room_that_is_playing(self):
        game, player = self.build()
        room = player.players["j1"]["cinema"]
        self.assertEqual(room.renderer.profile.name, "theatre")

        player.set_local_cinema_mode("j1", "front_only")
        player.refresh_cinema_rooms(time.monotonic() + 10.0)

        # The pick is applied by the server's re-offer, not by reshaping the
        # running room into a shape it is about to be replaced with.
        self.assertEqual(room.renderer.profile.name, "theatre")
        self.assertEqual(len(room.sources), 4)

    def test_a_relay_song_that_replaces_a_room_gets_a_fresh_one(self):
        game, player = self.build(transport="relay")
        retiring = player.players["j1"]["cinema"]
        old_entry = dict(player.players["j1"])
        old_sources = list(retiring.sources)

        player.set_local_cinema_mode("j1", "front_only")
        self.play(player, "relay", "front_only")

        room = player.players["j1"]["cinema"]
        self.assertIsNot(room, retiring)
        self.assertTrue(retiring.spent)
        self.assertEqual(room.renderer.profile.name, "front_only")
        self.assertFalse(any(source.deleted for source in room.sources))

        # The old receiver's retire cleanup now runs, as the relay pump would.
        player._release_sources(old_sources)
        player._release_cinema("j1", old_entry)

        self.assertIs(host_for(game).bank("j1"), room)
        self.assertFalse(any(source.deleted for source in room.sources))
        self.assertFalse(room.spent)
        # And the room is not "lost": the refresh recognises it as its own.
        self.assertEqual(player.refresh_cinema_rooms(time.monotonic() + 30.0), 1)


class _FakeOpusDecoder:
    """Enough of pyogg's decoder for a receiver worker to come up."""

    def __init__(self):
        pass

    def set_channels(self, value):
        pass

    def set_sampling_frequency(self, value):
        pass

    def decode(self, payload):
        import struct
        return struct.pack("<hh", payload[0], -payload[0])


class RoomLifetimeTests(unittest.TestCase):
    """A room must be gone once its sources are, and keep its own trims.

    Two failures reported from a live test room, both heard as a song that
    "cuts in and out" on the map that has a cabinet with speakers, while a
    plain cabinet on the main map was fine:

    * a faded stop deleted the room's OpenAL sources but never handed the
      room back to the host, so the *next* song was given a bank whose every
      source had been deleted: no room audio at all, and a stream that failed
      and retried over and over (the dropouts);
    * the once-a-second map refresh re-acquired the room with default trims,
      resetting the cabinet's own volume to 100 and dropping the reverb and
      EQ the song started with.
    """

    def build(self, entries):
        from math import cos, radians, sin
        game = FakeGame()
        set_enabled(game, True)
        map_obj = Map(game)
        for index, (channel, bearing) in enumerate(entries):
            angle = radians(bearing)
            x = ANCHOR[0] + sin(angle) * 8.0
            y = ANCHOR[1] + cos(angle) * 8.0
            map_obj.spawn_cinemaSpeaker(minx=x - 0.5, maxx=x + 0.5, miny=y - 0.5,
                                        maxy=y + 0.5, minz=0, maxz=1,
                                        id=f"spk{index}", channel=channel)
        map_obj.spawn_jukebox(minx=9, maxx=10, miny=19, maxy=20, minz=0, maxz=1,
                              id="j1")
        game.gameplay.map = map_obj
        player = jukebox.JukeboxPlayer(game)
        with mock.patch("libs.music_bot.AudioStreamer"):
            player.play("j1", 9.5, 19.5, 0.5, "Song", "http://example.com/a.mp3",
                        60, transport="direct")
        return game, player

    ROOM = [("front_l", -30), ("front_r", 30), ("rear_l", 150), ("rear_r", 210)]

    def wait_for_release(self, game, jukebox_id="j1", timeout=3.0):
        """Wait for the fade worker to finish deleting the room's sources."""
        host = host_for(game)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if host is not None and host.bank(jukebox_id) is None:
                return True
            time.sleep(0.02)
        return host is not None and host.bank(jukebox_id) is None

    def test_a_faded_stop_gives_the_room_back(self):
        game, player = self.build(self.ROOM)
        bank = player.players["j1"]["cinema"]
        sources = list(bank.sources)

        player.stop("j1", fade=True)

        self.assertTrue(self.wait_for_release(game),
                        "the faded stop left the host holding the room")
        # The sources are gone and so is the room: nothing can hand the next
        # song a name the device has already deleted.
        self.assertTrue(all(source.deleted for source in sources))
        self.assertTrue(bank._stopped)

    def test_the_next_song_after_a_faded_stop_gets_live_speakers(self):
        game, player = self.build(self.ROOM)
        first = player.players["j1"]["cinema"]
        player.stop("j1", fade=True)
        self.assertTrue(self.wait_for_release(game))

        with mock.patch("libs.music_bot.AudioStreamer"):
            player.play("j1", 9.5, 19.5, 0.5, "Song 2",
                        "http://example.com/b.mp3", 60, transport="direct")

        second = player.players["j1"]["cinema"]
        self.assertIsNot(second, first)
        self.assertTrue(second.sources)
        self.assertFalse(any(source.deleted for source in second.sources),
                         "the next song was handed deleted OpenAL sources")

    def test_a_song_replacing_a_fading_room_gets_its_own(self):
        """The hard case: the next song starts before the fade has finished.

        The retired room still holds live sources for another half second, so
        it must be neither handed to the next song nor allowed to delete the
        next song's speakers when its fade ends.
        """
        game, player = self.build(self.ROOM)
        old = player.players["j1"]["cinema"]
        old_sources = list(old.sources)

        player.stop("j1", fade=True)
        with mock.patch("libs.music_bot.AudioStreamer"):
            player.play("j1", 9.5, 19.5, 0.5, "Song 2",
                        "http://example.com/b.mp3", 60, transport="direct")
        new = player.players["j1"]["cinema"]

        self.assertIsNot(new, old)
        self.assertTrue(old.spent)
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and not all(s.deleted for s in old_sources):
            time.sleep(0.02)
        # The old room's teardown finished its own job and nothing else's.
        self.assertTrue(all(source.deleted for source in old_sources))
        self.assertFalse(any(source.deleted for source in new.sources))
        self.assertIs(host_for(game).bank("j1"), new)
        self.assertFalse(new.spent)

    def re_offer(self, player, mode, title="Song"):
        """The server re-offering the song it is playing, as a map write does."""
        with mock.patch("libs.music_bot.AudioStreamer"):
            player.play("j1", 9.5, 19.5, 0.5, title, "http://example.com/a.mp3", 60,
                        transport="direct", cinema_mode=mode)

    def test_a_mode_change_re_tunes_the_song_that_is_already_playing(self):
        """Picking Off must be heard, not wait for the next track."""
        game, player = self.build(self.ROOM)
        room = player.players["j1"]["cinema"]
        self.assertIsNotNone(room)

        self.re_offer(player, "off")

        entry = player.players["j1"]
        self.assertIsNone(entry["cinema"])
        self.assertEqual(entry["cinema_mode"], "off")
        self.assertIsNotNone(entry["source"])
        self.assertIsNotNone(entry["secondary_source"])
        self.assertTrue(room.spent)

    def test_switching_back_into_a_room_re_tunes_again(self):
        game, player = self.build(self.ROOM)
        self.re_offer(player, "off")
        self.assertIsNone(player.players["j1"]["cinema"])

        self.re_offer(player, "auto")

        entry = player.players["j1"]
        self.assertIsNotNone(entry["cinema"])
        self.assertFalse(any(source.deleted for source in entry["cinema"].sources))

    def test_an_unchanged_re_offer_stays_seamless(self):
        """A map reload that changed nothing must not restart the song."""
        game, player = self.build(self.ROOM)
        room = player.players["j1"]["cinema"]
        streamer = player.players["j1"]["streamer"]

        self.re_offer(player, "auto")

        self.assertIs(player.players["j1"]["cinema"], room)
        self.assertIs(player.players["j1"]["streamer"], streamer)
        self.assertFalse(room.spent)

    def test_a_refresh_keeps_the_rooms_own_trims(self):
        game, player = self.build(self.ROOM)
        bank = player.players["j1"]["cinema"]
        bank.set_reverb(("reverb", "zone"))
        bank.set_eq_slot(("eq", "bass"))
        bank.set_cabinet_volume(40)

        player.refresh_cinema_rooms(time.monotonic() + 2.0)

        self.assertIs(player.players["j1"]["cinema"], bank)
        self.assertEqual(bank.reverb_slot, ("reverb", "zone"))
        self.assertEqual(bank.eq_slot, ("eq", "bass"))
        self.assertAlmostEqual(bank.cabinet_volume * 100.0, 40.0)


def make_bot(game, **attributes):
    bot = MapMusicBot.__new__(MapMusicBot)
    bot.game = game
    bot.volume = 50
    bot.enabled = True
    bot.streamer = None
    bot.stream_source = None
    bot.playing = False
    bot.paused = False
    bot.mode = "idle"
    bot.cinema_target = None
    bot.cinema_bank = None
    bot.cinema_bank_key = None
    bot.cinema_cabinet = None
    bot.duck_multiplier = 1.0
    bot.broadcast_to_megaphone = False
    bot._find_gameplay = lambda: game.gameplay
    for key, value in attributes.items():
        setattr(bot, key, value)
    return bot


class MusicBotLiveRoomTests(unittest.TestCase):
    """The bot's room follows the map too, without touching the stream."""

    def setUp(self):
        from math import cos, radians, sin
        self.game = FakeGame()
        map_obj = Map(self.game)
        for index, (channel, bearing) in enumerate([("front_l", -30), ("front_r", 30)]):
            angle = radians(bearing)
            x = ANCHOR[0] + sin(angle) * 8.0
            y = ANCHOR[1] + cos(angle) * 8.0
            map_obj.spawn_cinemaSpeaker(minx=x - 0.5, maxx=x + 0.5, miny=y - 0.5,
                                        maxy=y + 0.5, minz=0, maxz=1,
                                        id=f"spk{index}", channel=channel)
        map_obj.spawn_jukebox(minx=9, maxx=10, miny=19, maxy=20, minz=0, maxz=1,
                              id="box_a")
        self.game.gameplay.map = map_obj
        self.map = map_obj
        set_enabled(self.game, True)

    def add(self, channel, bearing):
        from math import cos, radians, sin
        index = len(self.map.cinema_speaker_list)
        angle = radians(bearing)
        x = ANCHOR[0] + sin(angle) * 8.0
        y = ANCHOR[1] + cos(angle) * 8.0
        self.map.spawn_cinemaSpeaker(minx=x - 0.5, maxx=x + 0.5, miny=y - 0.5,
                                     maxy=y + 0.5, minz=0, maxz=1,
                                     id=f"spk{index}", channel=channel)

    def routed(self):
        bot = make_bot(self.game)
        with mock.patch("libs.music_bot.controller.speak"), \
                mock.patch("libs.music_bot.controller.options.set"):
            bot.set_cinema_target("box_a")
        bot._create_stream_source()
        return bot

    def test_a_speaker_placed_mid_song_joins_the_bot_s_room(self):
        bot = self.routed()
        bank = bot.cinema_bank
        streamer = SimpleNamespace(cinema=bank, source=None)
        bot.streamer = streamer
        self.assertEqual(len(bank.sources), 2)

        self.add("front_c", 0)
        bot._update_cinema_output()

        self.assertIs(bot.cinema_bank, bank)
        self.assertIs(streamer.cinema, bank)
        self.assertEqual(len(bank.sources), 3)

    def test_the_bot_room_is_reshaped_at_most_once_a_second(self):
        bot = self.routed()
        bank = bot.cinema_bank
        bot.streamer = SimpleNamespace(cinema=bank, source=None)
        self.add("front_c", 0)
        bot._cinema_reshape_at = time.monotonic()
        bot._update_cinema_output()
        self.assertEqual(len(bank.sources), 2)

    def test_losing_the_room_while_playing_keeps_playing_it(self):
        bot = self.routed()
        bank = bot.cinema_bank
        bot.streamer = SimpleNamespace(cinema=bank, source=None)
        self.map.cinema_speaker_list = []
        bot._cinema_reshape_at = 0.0
        bot._update_cinema_output()
        self.assertIs(bot.cinema_bank, bank)
        self.assertFalse(bank._stopped)

    def streamer_for(self, bank):
        """The REAL streamer handed a room, not a stand-in with .cinema on it."""
        return AudioStreamer(self.game, "http://example.com/a.mp3", None,
                             volume=100, bot=SimpleNamespace(), cinema=bank)

    def test_the_stream_feeding_the_room_is_never_refused(self):
        """A stream that cannot queue reads as "stream produced no audio".

        The queue call is the only thing standing between a decoded frame and
        the room: if every frame were refused, the pre-buffer would come back
        empty and the track would be reported as unplayable (no silence, no
        error, nothing to point at) even though OpenAL was never the problem.
        """
        bot = self.routed()
        bank = bot.cinema_bank
        streamer = self.streamer_for(bank)
        chunk = bytes(streamer.SAMPLES_PER_BUFFER * streamer.channels * 2)

        for _ in range(streamer.PRE_BUFFER_COUNT):
            self.assertTrue(streamer._queue_local(chunk))

        for slot, source in bank.slot_sources.items():
            self.assertEqual(source.buffers_queued, streamer.PRE_BUFFER_COUNT, slot)

    def test_a_trimmed_room_still_takes_every_frame(self):
        """A trim makes one speaker start late -- never the stream or the room."""
        from libs.audio.cinema.layout import IDEAL_BEARING, CinemaSpeakerSpec
        from math import radians, sin, cos

        def positioned(channel, delay_ms):
            angle = radians(IDEAL_BEARING[channel])
            return CinemaSpeakerSpec(
                channel,
                (ANCHOR[0] + sin(angle) * 8.0, ANCHOR[1] + cos(angle) * 8.0,
                 ANCHOR[2]),
                delay_ms=delay_ms)

        game = FakeGame()
        renderer = CinemaRenderer(ANCHOR, "front_only", specs=[
            positioned("front_l", 0.0), positioned("front_r", 60.0)])
        bank = CinemaSpeakerBank(game, renderer, volume=100, cabinet_volume=100,
                                 occlusion_provider=lambda *a: 0)
        streamer = AudioStreamer(game, "http://example.com/a.mp3", None,
                                 volume=100, bot=SimpleNamespace(), cinema=bank)
        chunk = bytes(streamer.SAMPLES_PER_BUFFER * streamer.channels * 2)

        # 60 ms of trim is three 20 ms frames: the trimmed speaker is simply
        # not fed yet, and the stream must keep filling the room regardless.
        for _ in range(6):
            self.assertTrue(streamer._queue_local(chunk))
        self.assertEqual(bank.slot_sources["front_l"].buffers_queued, 6)
        self.assertEqual(bank.slot_sources["front_r"].buffers_queued, 3)

    def test_the_room_reports_the_audio_it_has_actually_been_fed(self):
        """A room's fed position is how far into the song the output has got.

        ``content_position()`` is what the Music Bot's seek and the Jukebox's
        end-of-song hand-over are measured from, and the room is the one
        output that queues through the bank instead of a source of its own.
        If handing it a frame does not move that position, a song played
        through a room reads as one that never started: a seek lands beside
        the intro no matter how long it has been playing, and a direct
        jukebox song never qualifies for its tail.
        """
        bot = self.routed()
        bank = bot.cinema_bank
        streamer = self.streamer_for(bank)
        chunk = bytes(streamer.SAMPLES_PER_BUFFER * streamer.channels * 2)

        self.assertAlmostEqual(streamer.content_position(), 0.0, places=6)
        for _ in range(5):
            self.assertTrue(streamer._queue_local(chunk))
        # Five 20 ms frames: a tenth of a second, not zero and not more.
        self.assertAlmostEqual(streamer.content_position(), 0.1, places=6)

    def test_a_frame_the_room_never_took_is_not_a_played_frame(self):
        """A refused frame must not advance the position it was refused for.

        The pre-buffer retries a frame until the room accepts it, so a room
        that is momentarily full (or already stopped) would otherwise be
        credited with audio nobody ever heard -- and the position would run
        ahead of the song by however many frames were dropped.
        """
        bot = self.routed()
        bank = bot.cinema_bank
        streamer = self.streamer_for(bank)
        chunk = bytes(streamer.SAMPLES_PER_BUFFER * streamer.channels * 2)
        bank.stop()

        self.assertFalse(streamer._queue_local(chunk))
        self.assertAlmostEqual(streamer.content_position(), 0.0, places=6)


class RoomFeedPositionTests(unittest.TestCase):
    """A room's fed position is read by more than the room itself.

    ``content_position()`` is how far into the song the output has got, and
    the Jukebox's end-of-song hand-over reads it to decide whether a song's
    last seconds are worth letting play out. A room queues through the bank
    rather than a source of its own, so a stream feeding a room that never
    advanced that position would look like one still at the intro: every
    direct song on a cinema map had its ending cut off at the packet, with a
    position that agreed the song had not been played yet.
    """

    ROOM = [("front_l", -30), ("front_r", 30)]

    def build(self):
        from math import cos, radians, sin
        game = FakeGame()
        game.audio_mngr = PooledAudio()     # models OpenAL consuming buffers
        set_enabled(game, True)
        map_obj = Map(game)
        for index, (channel, bearing) in enumerate(self.ROOM):
            angle = radians(bearing)
            x = ANCHOR[0] + sin(angle) * 8.0
            y = ANCHOR[1] + cos(angle) * 8.0
            map_obj.spawn_cinemaSpeaker(minx=x - 0.5, maxx=x + 0.5, miny=y - 0.5,
                                        maxy=y + 0.5, minz=0, maxz=1,
                                        id=f"spk{index}", channel=channel)
        map_obj.spawn_jukebox(minx=9, maxx=10, miny=19, maxy=20, minz=0, maxz=1,
                              id="j1")
        game.gameplay.map = map_obj
        player = jukebox.JukeboxPlayer(game)
        with mock.patch("libs.music_bot.AudioStreamer"):
            player.play("j1", 9.5, 19.5, 0.5, "Song", "http://example.com/a.mp3",
                        60, transport="direct")
        return game, player

    def play_through_room(self, streamer, bank, frames):
        """Feed the room the way the streaming loop does, OpenAL consuming."""
        chunk = bytes(streamer.SAMPLES_PER_BUFFER * streamer.channels * 2)
        for _ in range(frames):
            while not streamer._queue_local(chunk):
                for source in bank.slot_sources.values():
                    source.finish_one()
                bank.reclaim()

    def test_a_direct_song_through_a_room_still_gets_its_tail(self):
        game, player = self.build()
        entry = player.players["j1"]
        bank = entry["cinema"]
        streamer = AudioStreamer(game, "http://example.com/a.mp3", None,
                                 volume=60, cinema=bank)
        streamer.is_alive = lambda: True
        self.play_through_room(streamer, bank, 250)     # five seconds heard
        self.assertAlmostEqual(streamer.content_position(), 5.0, places=6)

        entry["streamer"] = streamer
        # Twelve seconds of song with three left: inside the hand-over budget
        # once the room's own position is counted, and far outside it if the
        # position is still sitting at the intro.
        entry["play_params"]["duration"] = 12.0
        calls = []
        player.stop = lambda jid, fade=False: calls.append((jid, fade)) or True
        player._retire_or_stop("j1")
        self.assertEqual(calls, [], "the song's ending was cut off instead")
        self.assertEqual(len(player._retiring_direct), 1)


class RoomShapeTests(unittest.TestCase):
    """Which speakers a room plays: the map's, not the profile's wish list."""

    def build(self, channels, **play_kwargs):
        from math import cos, radians, sin
        game = FakeGame()
        map_obj = Map(game)
        for index, (channel, bearing) in enumerate(channels):
            angle = radians(bearing)
            x = ANCHOR[0] + sin(angle) * 8.0
            y = ANCHOR[1] + cos(angle) * 8.0
            map_obj.spawn_cinemaSpeaker(minx=x - 0.5, maxx=x + 0.5, miny=y - 0.5,
                                        maxy=y + 0.5, minz=0, maxz=1,
                                        id=f"spk{index}", channel=channel)
        map_obj.spawn_jukebox(minx=9, maxx=10, miny=19, maxy=20, minz=0, maxz=1,
                              id="j1")
        game.gameplay.map = map_obj
        set_enabled(game, True)
        player = jukebox.JukeboxPlayer(game)
        with mock.patch("libs.music_bot.AudioStreamer") as streamer:
            player.play("j1", 9.5, 19.5, 0.5, "Song", "http://example.com/a.mp3",
                        60, transport="direct", **play_kwargs)
        return streamer.call_args.kwargs["cinema"]

    def test_a_front_and_rear_room_plays_exactly_those_four(self):
        bank = self.build([("front_l", -30), ("front_r", 30),
                           ("rear_l", -150), ("rear_r", 150)])
        self.assertEqual(bank.renderer.profile.name, "theatre")
        self.assertEqual(sorted(bank.slot_sources),
                         ["front_l", "front_r", "rear_l", "rear_r"])

    def test_the_map_speakers_keep_working_without_the_ring(self):
        bank = self.build([("front_l", -30), ("front_r", 30),
                           ("rear_l", -150), ("rear_r", 150)])
        self.assertTrue(bank.queue_frame(*frame(1)))
        for slot in ("front_l", "front_r", "rear_l", "rear_r"):
            self.assertEqual(bank.slot_sources[slot].buffers_queued, 1)

    def test_an_asked_for_profile_still_fills_the_gaps_it_needs(self):
        """The server naming a profile may pad it out; the map cannot."""
        bank = self.build([("front_l", -30), ("front_r", 30)],
                          cinema_profile="theatre")
        self.assertEqual(bank.renderer.profile.name, "theatre")
        self.assertEqual(len(bank.sources), 7)
        self.assertTrue(bank.renderer.layout.use_ring)

    def test_a_map_with_no_speakers_is_still_a_ring(self):
        bank = self.build([], cinema_profile="surround")
        self.assertEqual(len(bank.sources), 5)
        self.assertFalse(bank.renderer.layout._specs)


class WallOcclusionTests(unittest.TestCase):
    """Speakers behind a wall: still placed, still audible, but muffled.

    A builder rightly puts the side and rear speakers against (or behind) the
    room's own walls and pillars. Two things have to hold there: the resolver
    must still count those speakers as part of the room (a wall is not a
    reason to throw a speaker away), and a listener with a wall in between
    must hear that speaker *through* the wall -- quieter and duller, never
    gone, and never as clear as a speaker in the same room.
    """

    def walled(self, channels=None):
        """Cabinet and speakers, with a thick wall across the room's middle."""
        from math import cos, radians, sin
        game = FakeGame()
        map_obj = Map(game)
        for index, (channel, bearing) in enumerate(channels or [
                ("front_l", -30), ("front_r", 30),
                ("rear_l", -150), ("rear_r", 150)]):
            angle = radians(bearing)
            x = ANCHOR[0] + sin(angle) * 8.0
            y = ANCHOR[1] + cos(angle) * 8.0
            map_obj.spawn_cinemaSpeaker(minx=x - 0.5, maxx=x + 0.5, miny=y - 0.5,
                                        maxy=y + 0.5, minz=0, maxz=1,
                                        id=f"spk{index}", channel=channel)
        map_obj.spawn_jukebox(minx=9, maxx=10, miny=19, maxy=20, minz=0, maxz=1,
                              id="j1")
        # A four-block-thick wall between the cabinet and the back of the
        # room, so it stands between the listener and the rear pair only.
        map_obj.get_tile_at = lambda x, y, z=0: "wall1" if 14 <= y <= 17 else "floor"
        game.gameplay.map = map_obj
        self.map = map_obj
        return game

    def room_of(self, game):
        set_enabled(game, True)
        player = jukebox.JukeboxPlayer(game)
        with mock.patch("libs.music_bot.AudioStreamer") as streamer:
            player.play("j1", 9.5, 19.5, 0.5, "Song", "http://example.com/a.mp3",
                        60, transport="direct")
        bank = streamer.call_args.kwargs["cinema"]
        self.assertIsNotNone(bank)
        game.audio_mngr.position = (10.0, 20.0, 0.0)
        bank.update_output()
        return bank

    def test_a_wall_does_not_take_a_speaker_out_of_the_room(self):
        bank = self.room_of(self.walled())
        # Four placed speakers (the front pair and the rear pair behind the
        # wall) are the four speakers that play: a room read off the map never
        # invents a centre or a side speaker nobody placed.
        self.assertEqual(sorted(bank.slot_sources),
                         ["front_l", "front_r", "rear_l", "rear_r"])
        self.assertFalse(bank.renderer.layout.use_ring)

    def test_a_speaker_behind_a_wall_is_muffled_not_silenced(self):
        from libs.jukebox import wall_occlusion_tier
        game = self.walled()
        bank = self.room_of(game)
        listener = (10.0, 20.0, 0.0)
        rear = bank.slot_sources["rear_l"]
        front = bank.slot_sources["front_l"]

        tier = wall_occlusion_tier(self.map, rear.position, listener)
        self.assertEqual(tier, 2)                      # a thick wall
        # The screen wall is on a clear path and keeps its full brightness.
        self.assertEqual(getattr(front, "direct_filter", None), None)
        self.assertEqual(getattr(rear, "direct_filter", None),
                         ("filter", "LOWPASS", (("GAINHF", 0.05), ("GAIN", 0.22))))
        # Muffled, not muted: the wall is a lowpass with a big gain cut, and
        # the speaker still plays at full distance gain.
        self.assertGreater(rear.gain, 0.0)
        self.assertAlmostEqual(rear.gain, front.gain, places=6)

    def test_a_thin_pillar_only_lightly_muffles(self):
        game = self.walled()
        # One block of wall on the path: the light filter, not the heavy one.
        # (The tiles go in before the room is built -- the transport caches a
        # tier for a fraction of a second, so editing the map under a running
        # room is not what this is testing.)
        self.map.get_tile_at = lambda x, y, z=0: "wall1" if y == 16 else "floor"
        bank = self.room_of(game)
        self.assertEqual(getattr(bank.slot_sources["rear_l"], "direct_filter", None),
                         ("filter", "LOWPASS", (("GAINHF", 0.45), ("GAIN", 0.75))))

    def test_the_music_bot_room_hears_walls_too(self):
        """One output ignoring walls would be a room with no walls at all."""
        game = self.walled()
        set_enabled(game, True)
        bot = make_bot(game)
        with mock.patch("libs.music_bot.controller.speak"), \
                mock.patch("libs.music_bot.controller.options.set"):
            bot.set_cinema_target("j1")
        bot._create_stream_source()
        bank = bot.cinema_bank
        self.assertIsNotNone(bank.occlusion_provider)
        game.audio_mngr.position = (10.0, 20.0, 0.0)
        bank.update_output()
        self.assertIsNotNone(getattr(bank.slot_sources["rear_l"], "direct_filter", None))
        self.assertIsNone(getattr(bank.slot_sources["front_l"], "direct_filter", None))

    def test_the_bot_reuses_the_jukebox_ray_when_there_is_one(self):
        game = self.walled()
        bot = make_bot(game)
        game.gameplay.jukebox_player = SimpleNamespace(occlusion_tier=lambda *a: 1)
        self.assertEqual(bot._cinema_occlusion((0, 0, 0), (1, 1, 1), 40.0), 1)


class RearChannelTests(unittest.TestCase):
    """The rear pair: behind you, quieter, and not a copy of the screen wall."""

    def plan(self, channels):
        from math import cos, radians, sin
        from libs.audio.cinema.layout import CinemaLayout
        specs = []
        for channel, bearing in channels:
            angle = radians(bearing)
            specs.append(CinemaSpeakerSpec(
                channel, (sin(angle) * 8.0, cos(angle) * 8.0, 0.0)))
        layout = CinemaLayout((0.0, 0.0, 0.0), specs, use_ring=False)
        renderer = CinemaRenderer((0.0, 0.0, 0.0), "theatre", layout)
        return renderer, dict((slot, (gain_l, gain_r))
                              for slot, gain_l, gain_r in renderer.plan())

    def test_a_normal_song_plays_the_rear_pair_too(self):
        """Four placed speakers (front pair + rear pair) all play."""
        _renderer, plan = self.plan([("front_l", -30), ("front_r", 30),
                                     ("rear_l", -150), ("rear_r", 150)])
        self.assertEqual(sorted(plan), ["front_l", "front_r", "rear_l", "rear_r"])
        for slot in ("rear_l", "rear_r"):
            self.assertGreater(max(plan[slot]), 0.1)     # audible
            self.assertLess(max(plan[slot]), 0.5)        # and clearly ambience

    def test_the_front_pair_still_carries_the_real_channels(self):
        _renderer, plan = self.plan([("front_l", -30), ("front_r", 30),
                                     ("rear_l", -150), ("rear_r", 150)])
        self.assertEqual(plan["front_l"], (1.0, 0.0))
        self.assertEqual(plan["front_r"], (0.0, 1.0))

    def test_the_rear_pair_is_decorrelated_from_the_screen_wall(self):
        """Left rear leans right and vice versa: that is what stops the two
        correlated pairs from comb filtering across the room."""
        _renderer, plan = self.plan([("front_l", -30), ("front_r", 30),
                                     ("rear_l", -150), ("rear_r", 150)])
        self.assertGreater(plan["rear_l"][1], plan["rear_l"][0])
        self.assertGreater(plan["rear_r"][0], plan["rear_r"][1])

    def test_no_two_speakers_ever_share_weights(self):
        _renderer, plan = self.plan([("front_l", -30), ("front_c", 0),
                                     ("front_r", 30), ("side_l", -90),
                                     ("side_r", 90), ("rear_l", -150),
                                     ("rear_r", 150)])
        self.assertEqual(len(set(plan.values())), len(plan))

    def test_the_rears_get_louder_when_the_room_has_fewer_speakers(self):
        """The added speakers share one equal-power budget."""
        _four, four = self.plan([("front_l", -30), ("front_r", 30),
                                 ("rear_l", -150), ("rear_r", 150)])
        _seven, seven = self.plan([("front_l", -30), ("front_c", 0),
                                   ("front_r", 30), ("side_l", -90),
                                   ("side_r", 90), ("rear_l", -150),
                                   ("rear_r", 150)])
        self.assertGreater(max(four["rear_l"]), max(seven["rear_l"]))

    def test_a_mono_song_spreads_over_every_speaker(self):
        renderer, _plan = self.plan([("front_l", -30), ("front_r", 30),
                                     ("rear_l", -150), ("rear_r", 150)])
        mono = dict((slot, (gain_l, gain_r))
                    for slot, gain_l, gain_r in renderer.plan("mono"))
        self.assertEqual(set(mono), {"front_l", "front_r", "rear_l", "rear_r"})
        self.assertEqual(len(set(mono.values())), 1)     # one shared level
        self.assertGreater(max(mono["rear_l"]), 0.1)


class RoomCheckToolTests(unittest.TestCase):
    """The offline check answers "will it be heard" without entering the game."""

    @classmethod
    def setUpClass(cls):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "..", "tools", "cinema_room_check.py")
        spec = importlib.util.spec_from_file_location("cinema_room_check", path)
        cls.tool = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.tool)

    def test_a_speaker_reports_the_trim_it_carries(self):
        """A room that sounds different from its neighbour should say why."""
        def speaker(delay, level):
            return SimpleNamespace(
                spec=SimpleNamespace(delay_ms=delay, level=level))

        self.assertEqual(self.tool.speaker_note(speaker(25.0, 1.0)), "  [+25 ms]")
        self.assertEqual(self.tool.speaker_note(speaker(0.0, 0.4)),
                         "  [level 40%]")
        self.assertEqual(self.tool.speaker_note(speaker(30.0, 0.4)),
                         "  [+30 ms, level 40%]")
        # An untouched speaker reads exactly as it always did.
        self.assertEqual(self.tool.speaker_note(speaker(0.0, 1.0)), "")

    def test_a_ray_counts_the_wall_tiles_it_crosses(self):
        thin = [(0, 25, 15, 15, 0, 5)]
        thick = thin + [(0, 25, 16, 16, 0, 5), (0, 25, 17, 17, 0, 5)]
        room = (10.0, 5.0, 0.0)
        self.assertEqual(self.tool.wall_tiles(thin, room, (10.0, 20.0, 0.0)), 1)
        self.assertEqual(self.tool.wall_tiles(thick, room, (10.0, 20.0, 0.0)), 3)
        self.assertEqual(self.tool.wall_tiles(thick, room, (10.0, 12.0, 0.0)), 0)

    def test_a_wall_is_a_platform_whose_type_starts_with_wall(self):
        """Real maps build walls from ``<platform type="wall...">``, not <wall>."""
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "m.map")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(
                    '<map><body>'
                    '<platform bounds="0 10 0 10 0 5" type="grass" id="a"/>'
                    '<platform bounds="0 10 0 10 6 6" type="wallwood" id="b"/>'
                    '<platform bounds="0 10 0 10 7 7" type="wallglass" id="c"/>'
                    '<wall bounds="0 10 0 10 8 8" id="d"/>'
                    '</body></map>')
            self.assertEqual(len(self.tool.read_walls(path)), 3)

    def test_loudness_uses_the_rooms_own_distance_ramp(self):
        """Not the plain pair's narrower 8/40 -- a room reaches the back row."""
        def report(offset):
            class Speaker:
                position = (10.0, 5.0 + offset, 0.0)

            class Room:
                slots = ("front_c",)
                speakers = {"front_c": Speaker()}

            return self.tool.audible_lines(Room(), (10.0, 5.0, 0.0), [])

        # 40 m out is past the plain pair's silence, but a room that reaches
        # 60 plays it at a partial level (1 - 32/52) rather than nothing.
        self.assertGreater(self.tool.ROOM_MAX_DISTANCE, 40.0)
        text = report(40.0)
        self.assertIn("40.0m", text)
        self.assertNotIn("silent", text)
        self.assertIn("38% of full", text)
        # Past the room's own reach it is silent, and says so.
        self.assertIn("silent", report(70.0))

    def test_the_reported_feeds_are_the_speakers_the_map_has(self):
        """The tool must not report a room the game never plays.

        A room read off the map is only the speakers the map has (the game
        builds it with ``use_ring=False``); listing the feeds from the profile
        alone would invent a centre channel and a side pair nobody placed, and
        whoever tunes delays off that line tunes a room that does not exist.
        """
        from libs.audio.cinema import resolve_room
        # A four-speaker room resolves to the *theatre* profile, which wants a
        # centre and a side pair as well: the ring is exactly what must not
        # appear in the feeds.
        room = resolve_room(specs(["front_l", "front_r", "rear_l", "rear_r"]),
                            ANCHOR)
        self.assertEqual(room.profile_name, "theatre")
        line = [line for line in self.tool.describe(room, ANCHOR).splitlines()
                if line.strip().startswith("feeds")][0]
        self.assertIn("front_l", line)
        self.assertIn("front_r", line)
        self.assertIn("rear_l", line)
        self.assertNotIn("front_c", line)
        self.assertNotIn("side_", line)


class StreamPauseTests(unittest.TestCase):
    """A pause must hold the room, not one of its speakers."""

    def make_streamer(self, bank=None):
        streamer = AudioStreamer.__new__(AudioStreamer)
        streamer.cinema = bank
        streamer.paused = False
        streamer.source = None if bank is not None else FakeSource(FakeContext())
        streamer.network_queue = queue.Queue(maxsize=50)
        streamer._lock = threading.Lock()
        streamer.ready_event = threading.Event()
        return streamer

    def make_bank(self):
        game = FakeGame()
        renderer = CinemaRenderer(ANCHOR, "theatre", specs=specs(
            ["front_l", "front_c", "front_r", "side_l", "side_r"]))
        bank = CinemaSpeakerBank(game, renderer, volume=100, cabinet_volume=100,
                                 occlusion_provider=lambda *a: 0)
        for index in range(2):
            bank.queue_frame(*frame(index))
        bank.start_playback()
        return bank

    def test_pause_and_resume_touch_every_speaker(self):
        bank = self.make_bank()
        streamer = self.make_streamer(bank)

        streamer.set_pause(True)
        self.assertEqual({source.state for source in bank.sources},
                         {cyal.SourceState.PAUSED})

        streamer.set_pause(False)
        self.assertTrue(bank.playing())
        self.assertTrue(streamer.ready_event.is_set())

    def test_the_ear_source_still_pauses_without_a_room(self):
        streamer = self.make_streamer()
        streamer.set_pause(True)
        self.assertEqual(streamer.source.state, cyal.SourceState.PAUSED)
        streamer.set_pause(False)
        self.assertEqual(streamer.source.state, cyal.SourceState.PLAYING)


if __name__ == "__main__":
    unittest.main()
