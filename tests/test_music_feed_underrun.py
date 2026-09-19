"""A feed that stopped mid-song restarts its pre-buffer (and its own clock).

``MusicCompression._play_music_frame`` has to flush the queue and restart
pre-buffering when a source it was playing has STOPPED under it: OpenAL reports
every buffer a source that is not playing holds as *processed*, so the recycle
path alone hands the whole queue back and can never rebuild the pre-buffer the
start condition waits for -- the feed would stay silent until the song's packets
gapped long enough for the session reset to do the same thing.

That suite was indented under a comment, and CPython compiles a block indented
under nothing as code that never runs -- so the flush was dead, and the recovery
it describes never happened on an underrun.

This file pins the half that is easy to lose again: the flush runs when the source
has stopped, it does not run while the song is playing, and it starts the feed's
own clock over with it (``libs/jukebox_clock.py``).
"""

import os
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import cyal  # noqa: E402

from libs.voice_chat import MusicCompression  # noqa: E402

from test_cinema_peer_room import (FRAME, FakeContext, FakeSource,  # noqa: E402
                                   make_compression)

PRE_BUFFER = MusicCompression.PRE_BUFFER_FRAMES


def make_game():
    return SimpleNamespace(
        audio_mngr=SimpleNamespace(context=FakeContext()),
        gameplay=SimpleNamespace(player=SimpleNamespace(dead=False)),
    )


def play(compression, source, game, frames=1, epoch=None, frame_seq=None):
    for _ in range(frames):
        compression._play_music_frame(source, FRAME, epoch, frame_seq,
                                      game.gameplay)


class AStoppedSourceRestartsThePreBufferTests(unittest.TestCase):
    def _playing(self, queued=3):
        """A song that is playing, with its queue and its anchors where they are."""
        game = make_game()
        compression = make_compression(game)
        source = FakeSource(game.audio_mngr.context)
        play(compression, source, game, queued)
        source.play()
        compression._has_started = True
        compression._timeline_anchor_seq = 400
        compression._timeline_anchor_time = 0.0
        compression.pair_frames_fed = queued
        compression._pair_source = source
        return game, compression, source

    def test_the_underrun_flushes_and_starts_over(self):
        game, compression, source = self._playing()
        source.stop()                       # the underrun: it stopped under us
        play(compression, source, game)
        # The queue the stopped source reported as processed is gone, and the
        # feed is back in its pre-buffering phase instead of draining itself.
        self.assertEqual(source.buffers_queued, 1)
        self.assertFalse(compression._has_started)
        # The song's own anchors describe a queue that no longer exists: with
        # them gone ``_audible_timeline_seq`` says nothing, so a note held on the
        # timeline cannot be placed against a stale position.
        self.assertIsNone(compression._timeline_anchor_seq)
        self.assertIsNone(compression._audible_timeline_seq())
        # And the clock a jam note waits on starts over with the song.
        self.assertEqual(compression.pair_frames_fed, 1)

    def test_a_playing_song_is_never_flushed(self):
        game, compression, source = self._playing()
        play(compression, source, game)
        self.assertEqual(source.buffers_queued, 4)
        self.assertTrue(compression._has_started)
        self.assertEqual(compression._timeline_anchor_seq, 400)
        self.assertEqual(compression.pair_frames_fed, 4)

    def test_the_feed_plays_again_once_the_pre_buffer_is_back(self):
        game, compression, source = self._playing()
        source.stop()
        play(compression, source, game, PRE_BUFFER - 1)
        self.assertFalse(compression._has_started)
        self.assertEqual(source.state, cyal.SourceState.STOPPED)
        play(compression, source, game, 1)
        self.assertTrue(compression._has_started)
        self.assertEqual(source.state, cyal.SourceState.PLAYING)


if __name__ == "__main__":
    unittest.main()
