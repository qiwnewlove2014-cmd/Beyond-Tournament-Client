"""The OpenAL output pool of a music-bot stream.

One place for the question "where do this stream's frames play, and is there a
buffer free": the sources a stream feeds (one, a spatial pair, or a cinema
room's speaker bank), the pre-allocated buffer ring, reclamation of played
buffers, queue depth and the start gate. The queueing policy itself -- which
frame goes to which source, the room hand-over, the transposer -- stays on
``AudioStreamer``; these functions only touch the pool and its sources.

Every function takes the stream and reads/writes the fields it names, so the
existing one-line methods on ``AudioStreamer`` stay the patch seams tests use
(``stream._reclaim_processed``, ``stream._init_buffer_pool``).
"""

import time

import cyal
import cyal.exceptions

from .. import logger


def _sources(stream):
    """All OpenAL sources this stream feeds (1 normal, 2 spatial, N cinema)."""
    if stream.cinema is not None:
        return stream.cinema.sources
    if stream.spatial_pair:
        return (stream.spatial_src_l, stream.spatial_src_r)
    return (stream.source,)


def all_sources(stream):
    """All OpenAL sources this stream feeds (see ``_sources``)."""
    return _sources(stream)


def play_all(stream):
    for src in stream._all_sources():
        try:
            src.play()
        except Exception:
            pass


def all_playing(stream):
    if stream.remote is not None:
        # The child starts its own source when it has frames queued; a
        # silent slot here is not a stalled output.
        return True
    for src in _sources(stream):
        try:
            if src.state != cyal.SourceState.PLAYING:
                return False
        except Exception:
            return False
    return True


def start_output_playing(stream):
    """Start whichever sources are not playing yet, as one output.

    In a cinema room the bank owns that decision, because starting a speaker
    on its own is what makes one of them permanently miss the room's beat: the
    bank puts a speaker that fell behind back on the room's content instant
    first (``CinemaSpeakerBank.realign``) and then starts the room together.
    """
    if stream.cinema is not None:
        try:
            stream.cinema.start_playback()
            return
        except Exception:
            pass
    play_all(stream)


def buffers_queued(stream):
    if stream.remote is not None:
        return stream.remote.pending_frames()
    total = 0
    for src in stream._all_sources():
        try:
            total += src.buffers_queued
        except Exception:
            pass
    return total


def split_stereo_16(data):
    """Split interleaved s16le stereo bytes into (left, right) mono bytes."""
    import array
    samples = array.array('h')
    samples.frombytes(data)
    return samples[0::2].tobytes(), samples[1::2].tobytes()


def init_buffer_pool(stream):
    """Pre-allocate OpenAL buffers for reuse."""
    if stream.remote is not None:
        # The other device's process owns its own buffers.
        return
    if stream.cinema is not None:
        # A cinema room allocates its own per-speaker pools: a single
        # shared pool could never hold N frames the pre-buffer needs.
        return
    for _ in range(stream.NUM_BUFFERS):
        try:
            buf = stream.game.audio_mngr.context.gen_buffer()
            stream._buffer_pool.append(buf)
        except cyal.exceptions.InvalidOperationError:
            # The project audio backend can surface one stale OpenAL error
            # before succeeding; other buffer loaders use the same retry.
            try:
                buf = stream.game.audio_mngr.context.gen_buffer()
                stream._buffer_pool.append(buf)
            except Exception as ex:
                logger.log_exception(ex, "AudioStreamer._init_buffer_pool retry")
                break
        except Exception:
            break


def get_buffer(stream):
    """Get a reclaimed buffer without growing the OpenAL pool.

    Streaming must apply backpressure when every pre-allocated buffer is in
    flight; allocating another hardware buffer here lets a fast decoder queue
    an entire song and can exhaust the shared audio device.
    """
    stream._reclaim_processed()

    if not stream._buffer_pool and stream._pool_swap_armed:
        # A stream handed from a room to the plain pair never built a pool of
        # its own (a room allocates per speaker), and it is being fed again
        # before the pair's queued frames have recycled a buffer. Build the
        # pool here, on the thread that already owns this stream's buffers.
        stream._pool_swap_armed = False
        stream._init_buffer_pool()
    if stream._buffer_pool:
        return stream._buffer_pool.pop(0)
    return None


def reclaim_processed(stream):
    """Return processed buffers to the pool for reuse.

    CRITICAL: cyal's unqueue_buffers() returns a SINGLE Buffer object by
    default, not a list - handle both cases robustly. Spatial pairs drain both
    sources.
    """
    if stream.remote is not None:
        # This stream owns no buffers here: the child's process recycles its
        # own, and the parent only counts what it has not written yet.
        return
    if stream.cinema is not None:
        # The speaker bank owns its per-speaker pools, so it owns the
        # reclamation too -- but the watchdog's audible-progress stamp is
        # this stream's contract, so it is still stamped here.
        reclaimed = False
        try:
            reclaimed = stream.cinema.reclaim()
        except Exception:
            reclaimed = False
        if reclaimed and getattr(stream, "jukebox_player", None) is not None:
            stream.last_output_at = time.monotonic()
        return
    reclaimed = False
    try:
        for src in stream._all_sources():
            while src.buffers_processed > 0:
                result = src.unqueue_buffers()
                if result is not None:
                    reclaimed = True
                    try:
                        for buf in result:
                            stream._buffer_pool.append(buf)
                    except TypeError:
                        # Not iterable - single buffer object (cyal default)
                        stream._buffer_pool.append(result)
    except Exception:
        pass
    if reclaimed and getattr(stream, "jukebox_player", None) is not None:
        # A buffer finished on the speaker: audible progress. Only jukebox
        # direct streams feed the jukebox watchdog (jukebox/player.py reads
        # this stamp); personal Music Bot streams never set jukebox_player, so
        # their output is not watched here.
        stream.last_output_at = time.monotonic()
