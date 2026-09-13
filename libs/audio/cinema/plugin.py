"""The plugin seam between a playing source and the room's speakers.

This module is the only part of the Cinema Speaker System the Jukebox ever
touches, and it is deliberately tiny: ask for a renderer, hand over PCM
frames, give the renderer back when the song ends. Everything else -- the
queue, the relay, the direct ffmpeg stream, the timeline alignment, the
recovery watches -- keeps working exactly as it does today.

The host is **inert by default**. When cinema is off, or when a cabinet has
no cinema profile, :func:`acquire_renderer` returns None and the caller runs
its original two-source code path untouched. That is the whole safety
argument for this design: the default value of the feature is "the code you
already shipped".
"""

from .channel import AUTO
from .profiles import DEFAULT_PROFILE, get_profile
from .router import CinemaRenderer

# Player preference. The per-cabinet decision is made by the server (the
# jukebox element's cinema profile), so this is only a local master switch
# for players who want the plain jukebox back.
OPTION_ENABLED = "cinema_speakers"

# Attribute the host attaches itself to on the game object.
HOST_ATTRIBUTE = "cinema_speakers"

DEFAULT_ENABLED = False


def _option_enabled():
    try:
        from ... import options
        return bool(options.get(OPTION_ENABLED, DEFAULT_ENABLED))
    except Exception:
        # A missing settings back end must never stop the game from starting;
        # staying in the pre-cinema behaviour is always the safe answer.
        return DEFAULT_ENABLED


class CinemaSpeakerHost:
    """One renderer per playing jukebox, keyed by jukebox id."""

    def __init__(self, game=None, *, enabled=None, profile=None, max_speakers=None):
        self.game = game
        self._enabled = bool(_option_enabled() if enabled is None else enabled)
        self.profile = get_profile(profile or DEFAULT_PROFILE).name
        self.max_speakers = max_speakers
        self._renderers = {}

    @property
    def enabled(self):
        return self._enabled

    def set_enabled(self, enabled):
        """Turn the whole feature on or off; off also releases every room."""
        enabled = bool(enabled)
        if not enabled:
            self.release_all()
        self._enabled = enabled
        return self._enabled

    @staticmethod
    def _key(jukebox_id):
        return str(jukebox_id)

    def acquire(self, jukebox_id, anchor, **options):
        """Return the renderer for this cabinet, or None when cinema is off.

        A re-offered play event for the same song (a map reload) keeps the
        existing renderer, and a reload that moved the cabinet rebuilds it
        while carrying the channel verdict across: re-analysing from scratch
        would flip a mono stream back to stereo for a second or two and feed
        two speakers the same content in the meantime.
        """
        if not self._enabled:
            return None
        key = self._key(jukebox_id)
        profile_name = get_profile(options.pop("profile", None) or self.profile).name
        anchor = tuple(float(value) for value in anchor)
        current = self._renderers.get(key)
        if current is not None and current.layout.anchor == anchor and current.profile.name == profile_name:
            return current
        renderer = CinemaRenderer(
            anchor,
            profile_name,
            options.pop("layout", None),
            specs=options.pop("specs", None),
            max_speakers=self.max_speakers,
            detect_channels=options.pop("detect_channels", True),
            declared_layout=options.pop("declared_layout", AUTO),
        )
        if current is not None:
            renderer.channel = current.channel
        self._renderers[key] = renderer
        return renderer

    def renderer(self, jukebox_id):
        return self._renderers.get(self._key(jukebox_id))

    def release(self, jukebox_id):
        renderer = self._renderers.pop(self._key(jukebox_id), None)
        if renderer is not None:
            renderer.stop()
        return renderer

    def release_all(self):
        renderers = list(self._renderers.values())
        self._renderers.clear()
        for renderer in renderers:
            renderer.stop()
        return len(renderers)

    @property
    def renderers(self):
        return dict(self._renderers)

    def __len__(self):
        return len(self._renderers)

    def __repr__(self):
        state = "on" if self._enabled else "off"
        return f"CinemaSpeakerHost({state}, rooms={len(self._renderers)})"


def host_for(game, *, create=True):
    """The host attached to a game object, created on first use."""
    if game is None:
        return None
    host = getattr(game, HOST_ATTRIBUTE, None)
    if host is None and create:
        host = CinemaSpeakerHost(game)
        try:
            setattr(game, HOST_ATTRIBUTE, host)
        except Exception:
            return host
    return host


def set_enabled(game, enabled):
    """Toggle cinema for a game, returning the resulting state."""
    host = host_for(game)
    return None if host is None else host.set_enabled(enabled)


def acquire_renderer(game, jukebox_id, anchor, **options):
    """Single entry point for a source that wants to play through speakers."""
    host = host_for(game)
    if host is None:
        return None
    return host.acquire(jukebox_id, anchor, **options)


def release_renderer(game, jukebox_id):
    host = host_for(game, create=False)
    return None if host is None else host.release(jukebox_id)


def release_all(game):
    host = host_for(game, create=False)
    return 0 if host is None else host.release_all()
