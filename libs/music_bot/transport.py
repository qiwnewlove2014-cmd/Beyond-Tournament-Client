"""Input resolution and ffmpeg command building for the music bot.

These are the steps before a flight can run: turn the page URL into a fresh
stream URL and headers, then assemble the argv that reads it. They live here
instead of on ``AudioStreamer`` so they can be exercised with a small fake
stream; the thread keeps thin delegating methods, and every field they read or
write on that stream is named below.
"""

import time

from .. import logger
from .media import FFMPEG_PATH, YouTubeSearcher


def resolve_playback_info(stream, canonical_url, *, use_cache=False):
    """Resolve ``canonical_url`` through the isolated yt-dlp helper.

    Cached entries are used only when the caller allows it; a failed/expired
    entry is invalidated and re-resolved by the flight loop.
    """
    stream._media_cache_key = canonical_url
    stream._media_cache_entry = None
    stream._media_cache_candidate = None
    if not stream.running:
        return None
    if use_cache and stream._media_cache is not None:
        entry = stream._media_cache.get(canonical_url)
        if entry is not None:
            stream._media_cache_entry = entry
            return entry.info()
    # Retries bypass the cache, retaining the original isolated resolver
    # and cancellation contract. Only successful prebuffering promotes it.
    fresh = stream._diagnostic_startup_call(
        "direct.resolve", YouTubeSearcher.get_stream_info,
        canonical_url, cancelled=lambda: not stream.running)
    if stream.running and stream._media_cache is not None:
        stream._media_cache_candidate = fresh
    return fresh


def ffmpeg_header_block(headers):
    """Build an injection-safe CRLF header block for ffmpeg's input."""
    lines = []
    token_chars = "!#$%&'*+-.^_`|~"
    for raw_name, raw_value in (headers or {}).items():
        name = str(raw_name).strip()
        value = str(raw_value).strip()
        if not name or not value:
            continue
        if any(ch in name or ch in value for ch in ('\r', '\n')):
            continue
        if not all(ch.isalnum() or ch in token_chars for ch in name):
            continue
        lines.append(f"{name}: {value}\r\n")
    return "".join(lines)


def build_cmd(stream, target_url, input_headers):
    """The ffmpeg argv for one flight of ``stream``.

    ``target_url``/``input_headers`` are whatever the last resolve produced, so
    a command rebuilt after a re-resolve reads the fresh signed URL. Reads the
    stream's channel count, offset/anchor fields and seek maths; writes
    ``stream._direct_seek_to``.
    """
    cmd = [FFMPEG_PATH]
    if target_url.startswith(("http://", "https://")):
        # Reconnect on mid-stream drops for EVERY network source, including googlevideo: a CDN connection
        # close near the end of a song used to kill ffmpeg outright (no reconnect flags) and the song ended
        # early - verified locally: ffmpeg exits with an I/O error on a dropped connection, but with
        # reconnect flags it reconnects at the last byte offset (range requests) and the full song plays out.
        if "googlevideo.com" in target_url.lower():
            # Shorter budget: the startup 403 path (stale signed URL)
            # must re-resolve a fresh URL quickly instead of burning
            # the full backoff window before giving up.
            cmd.extend([
                '-reconnect', '1',
                '-reconnect_streamed', '1',
                '-reconnect_on_http_error', '403,429,5xx',
                '-reconnect_delay_max', '2',
                '-reconnect_delay_total_max', '6',
            ])
        else:
            cmd.extend([
                '-reconnect', '1',
                '-reconnect_streamed', '1',
                '-reconnect_on_http_error', '403,429,5xx',
                '-reconnect_delay_max', '5',
                '-reconnect_delay_total_max', '12',
            ])
    user_agent = next(
        (str(value).strip() for name, value in input_headers.items()
         if str(name).lower() == 'user-agent'),
        '',
    )
    if user_agent and '\r' not in user_agent and '\n' not in user_agent:
        # ffmpeg has a dedicated option for User-Agent. Using it avoids
        # a built-in Lavf agent overriding the yt-dlp authorization UA.
        cmd.extend(['-user_agent', user_agent])
    header_block = ffmpeg_header_block({
        name: value for name, value in input_headers.items()
        if str(name).lower() != 'user-agent'
    })
    if header_block:
        cmd.extend(['-headers', header_block])
    effective_offset = getattr(stream, "start_offset", 0.0)
    # Timeline anchoring (jukebox direct only): a fresh song keeps position 0 and waits out
    # the shared lead-in (adding resolve time here is what used to skip the intro), while a
    # mid-song join seeks PAST the projected audible start and waits the exact residual at
    # prebuffer-complete. Non-anchored workers (personal music bot, livestreams) keep legacy.
    if stream.start_offset_received_at is not None:
        if stream._join_playing_room:
            # Joining a room that is already playing (the per-listener direct fallback, or this
            # stream's own one catch-up after a late start): always seek. The seek formula cancels
            # the offset entirely and lands this machine on the room's clock.
            effective_offset = max(0.001, effective_offset)
            effective_offset = stream.direct_seek_seconds(
                effective_offset, stream.start_offset_received_at, time.monotonic(),
                aim_ahead_s=stream._join_aim_ahead_s,
                lead_in=stream.room_lead_in_s)
        elif effective_offset > 0.0:
            if stream._direct_anchor and effective_offset <= stream.DIRECT_FRESH_MAX_S:
                effective_offset = 0.0
            elif stream._direct_anchor:
                effective_offset = stream.direct_seek_seconds(
                    effective_offset, stream.start_offset_received_at, time.monotonic())
            else:
                # Only compensate the client's resolve delay when RESUMING
                # mid-song (start_offset > 0). A fresh song must start at 0:
                # adding the yt-dlp resolve time here made the jukebox skip
                # past the intro every time the direct fallback was used.
                effective_offset += max(0.0, time.monotonic() - stream.start_offset_received_at)
    stream._direct_seek_to = effective_offset if effective_offset > 0.5 else 0.0
    # Input authorization must precede seek. YouTube's signed range
    # request can return 403 when -ss is placed before these headers.
    if effective_offset > 0.5:
        cmd.extend(['-ss', f"{effective_offset:.2f}"])
    cmd.extend([
        '-re',  # Read/decode at media rate; network and OpenAL consume 20 ms frames.
        '-i', target_url,
        '-vn',  # Video containers (mp4/mkv/...) must decode audio only.
        '-f', 's16le',
        '-ar', '48000',
        '-ac', str(stream.channels),
        '-loglevel', 'error',
        'pipe:1'
    ])
    return cmd


def resolve_input(stream):
    """The direct stream URL and the headers to read it with.

    Returns ``(target_url, input_headers, canonical_url)``, or ``None`` when
    the worker was cancelled or the link could not be resolved -- in which case
    it has already cleaned itself up and ``run`` returns.
    """
    canonical_url = stream.canonical_url
    target_url = stream.audio_url
    input_headers = dict(stream.http_headers)
    if "youtube.com" in target_url or "youtu.be" in target_url:
        canonical_url = target_url
        target_url = ""
    if canonical_url and not target_url.startswith(("http://", "https://")):
        fresh = resolve_playback_info(stream, canonical_url, use_cache=True)
        if not stream.running:
            stream._cleanup()
            return None
        if fresh:
            target_url = fresh['url']
            input_headers = fresh.get('http_headers') or {}
        else:
            # A failed/timed-out helper must not fall back to in-process
            # extraction or launch ffmpeg with an empty input URL.
            stream.failure_reason = "audio link resolution failed"
            stream._cleanup()
            return None
    return target_url, input_headers, canonical_url
