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

from .bank import CinemaSpeakerBank
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
        self._banks = {}

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

    def acquire_bank(self, jukebox_id, anchor, **options):
        """Acquire the OpenAL speaker bank for this cabinet, or None when off.

        Re-acquiring the same room (a map reload re-offering the same song)
        keeps the existing bank and just refreshes its settings: rebuilding it
        would create new sources and interrupt audio that is already flowing.
        """
        volume = options.pop("volume", 100)
        cabinet_volume = options.pop("cabinet_volume", 100)
        reference_distance = options.pop("reference_distance", 8.0)
        max_distance = options.pop("max_distance", 40.0)
        occlusion_provider = options.pop("occlusion_provider", None)
        reverb_slot = options.pop("reverb_slot", None)
        eq_slot = options.pop("eq_slot", None)
        renderer = self.acquire(jukebox_id, anchor, **options)
        if renderer is None:
            return None
        key = self._key(jukebox_id)
        bank = self._banks.get(key)
        if bank is not None and bank.renderer is renderer and bank.sources:
            bank.set_volume(volume)
            bank.set_cabinet_volume(cabinet_volume)
            bank.set_reverb(reverb_slot)
            bank.set_eq_slot(eq_slot)
            return bank
        bank = CinemaSpeakerBank(
            self.game, renderer, volume=volume, cabinet_volume=cabinet_volume,
            reference_distance=reference_distance, max_distance=max_distance,
            occlusion_provider=occlusion_provider, reverb_slot=reverb_slot,
            eq_slot=eq_slot,
        )
        self._banks[key] = bank
        return bank

    def renderer(self, jukebox_id):
        return self._renderers.get(self._key(jukebox_id))

    def bank(self, jukebox_id):
        return self._banks.get(self._key(jukebox_id))

    def release(self, jukebox_id):
        key = self._key(jukebox_id)
        bank = self._banks.pop(key, None)
        if bank is not None:
            bank.stop()
        renderer = self._renderers.pop(key, None)
        if renderer is not None:
            renderer.stop()
        return bank if bank is not None else renderer

    def release_all(self):
        banks = list(self._banks.values())
        self._banks.clear()
        for bank in banks:
            bank.stop()
        renderers = list(self._renderers.values())
        self._renderers.clear()
        for renderer in renderers:
            renderer.stop()
        return len(renderers)

    @property
    def renderers(self):
        return dict(self._renderers)

    @property
    def banks(self):
        return dict(self._banks)

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


def _active_host(game):
    """The host when cinema is actually in use, else None.

    A host is only attached to the game object once something has turned the
    feature on, so a player who never uses cinema keeps an untouched game
    object and the caller keeps its original code path.
    """
    host = host_for(game, create=False)
    if host is not None:
        return host if host.enabled else None
    if not _option_enabled():
        return None
    return host_for(game)


def acquire_renderer(game, jukebox_id, anchor, **options):
    """Single entry point for a source that wants to play through speakers."""
    host = _active_host(game)
    if host is None:
        return None
    return host.acquire(jukebox_id, anchor, **options)


def acquire_bank(game, jukebox_id, anchor, **options):
    """Single entry point for a source that wants an OpenAL room."""
    host = _active_host(game)
    if host is None:
        return None
    return host.acquire_bank(jukebox_id, anchor, **options)


def release_renderer(game, jukebox_id):
    """Release a cabinet's room; its OpenAL sources are the caller's to delete."""
    host = host_for(game, create=False)
    return None if host is None else host.release(jukebox_id)


def release_all(game):
    host = host_for(game, create=False)
    return 0 if host is None else host.release_all()
