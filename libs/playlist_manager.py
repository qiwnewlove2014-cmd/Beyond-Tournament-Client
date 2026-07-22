import os
import json
import shutil
from . import logger

def get_default_playlist_path():
    """Get persistent storage path for playlists in %APPDATA%/Beyond Tournament.
    Auto-migrates existing data/my_playlists.json if present.
    """
    appdata = os.getenv('APPDATA')
    if appdata:
        appdata_dir = os.path.join(appdata, 'Beyond Tournament')
        try:
            os.makedirs(appdata_dir, exist_ok=True)
        except Exception:
            pass
        appdata_path = os.path.join(appdata_dir, 'my_playlists.json')

        # Auto-migrate legacy file if it exists
        legacy_path = os.path.join('data', 'my_playlists.json')
        if os.path.exists(legacy_path) and not os.path.exists(appdata_path):
            try:
                shutil.move(legacy_path, appdata_path)
                logger.log(f"[PlaylistManager] Migrated legacy playlists from {legacy_path} to {appdata_path}")
            except Exception as e:
                logger.log(f"[PlaylistManager] Migration failed: {e}")
        return appdata_path

    return os.path.join('data', 'my_playlists.json')


class PlaylistManager:
    """Manages player's local personal playlists and favorite tracks.
    Persists data cleanly to %APPDATA%/Beyond Tournament/my_playlists.json on the client.
    """

    def __init__(self, filepath=None):
        if filepath is None:
            filepath = get_default_playlist_path()
        self.filepath = filepath
        self.favorites = []
        self.playlists = {}
        self.load()

    def load(self):
        """Load playlists and favorites from client JSON file."""
        if not os.path.exists(self.filepath):
            self.favorites = []
            self.playlists = {}
            return

        try:
            with open(self.filepath, "r", encoding="utf-8") as f:
                data = json.load(f)
                self.favorites = data.get("favorites", [])
                self.playlists = data.get("playlists", {})
        except Exception as e:
            logger.log(f"[PlaylistManager] Error loading playlists: {e}")
            self.favorites = []
            self.playlists = {}

    def save(self):
        """Save playlists and favorites to client JSON file."""
        try:
            # Ensure folder exists
            dirname = os.path.dirname(self.filepath)
            if dirname and not os.path.exists(dirname):
                os.makedirs(dirname, exist_ok=True)

            data = {
                "favorites": self.favorites,
                "playlists": self.playlists
            }
            with open(self.filepath, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            return True
        except Exception as e:
            logger.log(f"[PlaylistManager] Error saving playlists: {e}")
            return False

    def add_favorite(self, title, target, source="youtube"):
        """Add a track to Favorites. Returns True if added, False if duplicate."""
        # Avoid duplicate targets
        for track in self.favorites:
            if track.get("target") == target:
                return False

        item = {
            "title": title or "Unknown Title",
            "target": target,
            "source": source
        }
        self.favorites.append(item)
        self.save()
        return True

    def remove_favorite(self, target):
        """Remove a track from Favorites by target URL/path."""
        self.favorites = [t for t in self.favorites if t.get("target") != target]
        self.save()

    def create_playlist(self, name):
        """Create a new empty custom playlist."""
        name = name.strip()
        if not name or name in self.playlists:
            return False
        self.playlists[name] = []
        self.save()
        return True

    def delete_playlist(self, name):
        """Delete an entire custom playlist."""
        if name in self.playlists:
            del self.playlists[name]
            self.save()
            return True
        return False

    def add_to_playlist(self, playlist_name, title, target, source="youtube"):
        """Add a track to a custom playlist."""
        if playlist_name not in self.playlists:
            self.playlists[playlist_name] = []

        tracks = self.playlists[playlist_name]
        for t in tracks:
            if t.get("target") == target:
                return False

        item = {
            "title": title or "Unknown Title",
            "target": target,
            "source": source
        }
        tracks.append(item)
        self.save()
        return True

    def remove_from_playlist(self, playlist_name, target):
        """Remove a track from a custom playlist."""
        if playlist_name in self.playlists:
            self.playlists[playlist_name] = [
                t for t in self.playlists[playlist_name] if t.get("target") != target
            ]
            self.save()

    def get_favorites(self):
        """Get all favorite tracks."""
        return self.favorites

    def get_playlist_names(self):
        """Get list of custom playlist names."""
        return list(self.playlists.keys())

    def get_playlist_tracks(self, playlist_name):
        """Get all tracks inside a custom playlist."""
        return self.playlists.get(playlist_name, [])
