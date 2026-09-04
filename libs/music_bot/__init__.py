"""Music Bot subsystem - package split of the former libs/music_bot.py.

Re-exports the old module's full public surface so existing imports keep
working unchanged, e.g. ``from libs import music_bot`` then
``music_bot.MapMusicBot``, or ``from libs.music_bot import AudioStreamer``.
"""

from .media import (DEFAULT_MAP_MUSIC, FALLBACK_PLAYLIST, FFMPEG_PATH,
                    YouTubeSearcher, _find_ffmpeg, _is_youtube_watch_url,
                    clamp_seek_position, format_track_position)
from .streaming import AudioStreamer, LiveRelayStreamer
from .controller import MapMusicBot

