"""Which pasted page links this client accepts, and what site they name.

The Music Bot's "Play a YouTube or Facebook link" line and the jukebox's URL
line both take the page a person already has in hand and both hand it to
yt-dlp. They must agree on what is accepted, so the host rule lives here once.

Only a normal https page on a known host is accepted. The path is yt-dlp's
business: a Facebook video, a reel and a live share the host and differ in the
path, and each one is a page yt-dlp reads. A caller gives a bare host its
scheme and upgrades ``http`` first, exactly as it already does for ``youtu.be``.
"""

from urllib.parse import urlsplit


YOUTUBE_HOSTS = ("youtube.com", "youtu.be")
FACEBOOK_HOSTS = ("facebook.com", "fb.watch")


def _host(value):
    try:
        return (urlsplit(str(value).strip()).hostname or "").lower()
    except (ValueError, UnicodeError):
        return ""


def _on_host(host, bases):
    return bool(host) and any(
        host == base or host.endswith("." + base) for base in bases
    )


def is_youtube_url(value):
    """True for a youtube.com page or a youtu.be short link."""
    return _on_host(_host(value), YOUTUBE_HOSTS)


def is_facebook_url(value):
    """True for a facebook.com page (video, reel or live) or an fb.watch link."""
    return _on_host(_host(value), FACEBOOK_HOSTS)


def is_supported_page_url(value):
    """True for an https YouTube or Facebook page a pasted link may name."""
    return str(value).strip().startswith("https://") and (
        is_youtube_url(value) or is_facebook_url(value)
    )


def site_name(value):
    """The name a spoken line or a title fallback uses for this link."""
    return "Facebook" if is_facebook_url(value) else "YouTube"
