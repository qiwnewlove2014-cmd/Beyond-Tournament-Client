import concurrent.futures
import hashlib
import ipaddress
import os
import re
import socket
import threading
from urllib.parse import urlparse

import requests

from . import buffer, options
from .speech import speak


MAX_SOUND_BYTES = 512 * 1024
MAX_CACHE_BYTES = 50 * 1024 * 1024
DOWNLOAD_GRACE_SECONDS = 1.0
SOUND_ID_RE = re.compile(r"^[a-f0-9]{64}$")
RADMIN_VPN_NETWORK = ipaddress.ip_network("26.0.0.0/8")
TRANSPORT_ERROR = (
    "Custom sound changes require HTTPS, localhost, or an active Radmin VPN connection."
)


def _has_local_radmin_vpn_address():
    """Confirm this client is actually attached to Radmin's 26.0.0.0/8 network."""
    try:
        import psutil
    except ImportError:
        return False

    try:
        for addresses in psutil.net_if_addrs().values():
            for address in addresses:
                if address.family != socket.AF_INET:
                    continue
                try:
                    if ipaddress.ip_address(address.address) in RADMIN_VPN_NETWORK:
                        return True
                except ValueError:
                    continue
    except (OSError, psutil.Error):
        return False
    return False


def _upload_transport_allowed(base_url):
    """Allow TLS, loopback development, or HTTP carried inside Radmin VPN."""
    parsed = urlparse(str(base_url or ""))
    if parsed.scheme.lower() == "https":
        return True
    if parsed.scheme.lower() != "http" or not parsed.hostname:
        return False

    hostname = parsed.hostname.lower()
    if hostname == "localhost":
        return True
    try:
        target_ip = ipaddress.ip_address(hostname)
    except ValueError:
        return False
    if target_ip.is_loopback:
        return True
    return target_ip in RADMIN_VPN_NETWORK and _has_local_radmin_vpn_address()


class PresenceSoundManager:
    """Downloads files off-thread; OpenAL playback remains owned by the game thread."""

    def __init__(self, game):
        self.game = game
        self.base_url = ""
        self.upload_token = ""
        self.own_sound_ids = {"online": "", "offline": ""}
        self.cache_dir = os.path.join(options.config_dirs.user_cache_dir, "presence_sounds")
        os.makedirs(self.cache_dir, exist_ok=True)
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="presence-sound"
        )
        self._inflight = {}
        self._lock = threading.RLock()
        self._timers = set()
        self._closing = False

    def configure(self, data):
        self.upload_token = str(data.get("presence_upload_token", ""))
        self.own_sound_ids["online"] = str(data.get("online_presence_sound_id", ""))
        self.own_sound_ids["offline"] = str(data.get("offline_presence_sound_id", ""))
        configured_url = str(data.get("presence_sound_base_url", "")).strip().rstrip("/")
        if configured_url:
            self.base_url = configured_url
            return

        host = str(options.get("host", "localhost")).strip()
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        default_port = int(options.get("port", 13000)) + 1
        port = int(data.get("presence_sound_http_port", default_port))
        self.base_url = f"http://{host}:{port}"

    def shutdown(self):
        with self._lock:
            self._closing = True
            for timer in self._timers:
                timer.cancel()
            self._timers.clear()
        self._executor.shutdown(wait=False, cancel_futures=True)

    def _players_buffer_muted(self):
        return any(item.name == "players" and item.muted for item in buffer.buffers)

    def _default_sound(self, kind):
        return f"ui/{kind}.ogg"

    def _cache_path(self, sound_id):
        return os.path.join(self.cache_dir, f"{sound_id}.ogg")

    def _play_on_game_thread(self, path, fallback_kind=None):
        if self._closing or self._players_buffer_muted():
            return
        played = self.game.direct_soundgroup.play(path)
        if played is None and fallback_kind:
            try:
                if os.path.commonpath((self.cache_dir, os.path.abspath(path))) == os.path.abspath(self.cache_dir):
                    os.unlink(path)
            except (OSError, ValueError):
                pass
            self.game.direct_soundgroup.play(self._default_sound(fallback_kind))

    def _queue_play(self, path, fallback_kind=None):
        self.game.put(lambda: self._play_on_game_thread(path, fallback_kind))

    def notify(self, kind, sound_id):
        if kind not in ("online", "offline") or self._players_buffer_muted():
            return
        sound_id = str(sound_id or "").lower()
        if not SOUND_ID_RE.fullmatch(sound_id) or not self.base_url:
            self._queue_play(self._default_sound(kind))
            return

        cache_path = self._cache_path(sound_id)
        if os.path.isfile(cache_path):
            self._queue_play(cache_path, kind)
            return

        state = {"settled": False, "lock": threading.Lock()}

        def fallback():
            with state["lock"]:
                if state["settled"]:
                    return
                state["settled"] = True
            with self._lock:
                self._timers.discard(timer)
            self._queue_play(self._default_sound(kind))

        timer = threading.Timer(DOWNLOAD_GRACE_SECONDS, fallback)
        timer.daemon = True
        with self._lock:
            if self._closing:
                return
            self._timers.add(timer)
        timer.start()

        future = self._download(sound_id)

        def downloaded(done):
            try:
                downloaded_path = done.result()
            except Exception:
                downloaded_path = None
            if not downloaded_path:
                return
            with state["lock"]:
                if state["settled"]:
                    return
                state["settled"] = True
            timer.cancel()
            with self._lock:
                self._timers.discard(timer)
            self._queue_play(downloaded_path, kind)

        future.add_done_callback(downloaded)

    def _download(self, sound_id):
        with self._lock:
            existing = self._inflight.get(sound_id)
            if existing is not None:
                return existing
            future = self._executor.submit(self._download_file, sound_id)
            self._inflight[sound_id] = future

        def remove_inflight(_):
            with self._lock:
                if self._inflight.get(sound_id) is future:
                    self._inflight.pop(sound_id, None)

        future.add_done_callback(remove_inflight)
        return future

    def _download_file(self, sound_id):
        destination = self._cache_path(sound_id)
        temp_path = f"{destination}.{threading.get_ident()}.tmp"
        digest = hashlib.sha256()
        total = 0
        try:
            with requests.get(
                f"{self.base_url}/presence-sounds/{sound_id}.ogg",
                stream=True,
                timeout=(3, 7),
            ) as response:
                response.raise_for_status()
                content_type = response.headers.get("Content-Type", "").split(";", 1)[0].lower()
                if content_type not in ("audio/ogg", "application/ogg"):
                    return None
                length = int(response.headers.get("Content-Length", "0") or 0)
                if length <= 0 or length > MAX_SOUND_BYTES:
                    return None
                with open(temp_path, "xb") as output:
                    for chunk in response.iter_content(16 * 1024):
                        if not chunk:
                            continue
                        total += len(chunk)
                        if total > MAX_SOUND_BYTES:
                            raise ValueError("presence sound exceeded size limit")
                        digest.update(chunk)
                        output.write(chunk)
            if total != length or digest.hexdigest() != sound_id:
                return None
            with open(temp_path, "rb") as uploaded:
                if uploaded.read(4) != b"OggS":
                    return None
            os.replace(temp_path, destination)
            self._trim_cache()
            return destination
        except (OSError, requests.RequestException, ValueError):
            return None
        finally:
            try:
                if os.path.exists(temp_path):
                    os.unlink(temp_path)
            except OSError:
                pass

    def _trim_cache(self):
        try:
            files = []
            total = 0
            for name in os.listdir(self.cache_dir):
                if not SOUND_ID_RE.fullmatch(os.path.splitext(name)[0]) or not name.endswith(".ogg"):
                    continue
                full_path = os.path.join(self.cache_dir, name)
                stat = os.stat(full_path)
                files.append((stat.st_mtime, stat.st_size, full_path))
                total += stat.st_size
            for _, size, full_path in sorted(files):
                if total <= MAX_CACHE_BYTES:
                    break
                os.unlink(full_path)
                total -= size
        except OSError:
            pass

    def choose_and_upload(self, kind):
        if kind not in ("online", "offline"):
            return
        if not self.upload_token or not self.base_url:
            speak("You must be connected to a compatible server.")
            return
        if not _upload_transport_allowed(self.base_url):
            speak(TRANSPORT_ERROR)
            return

        def choose_file():
            root = None
            try:
                import tkinter as tk
                from tkinter import filedialog
                root = tk.Tk()
                root.withdraw()
                root.attributes("-topmost", True)
                file_path = filedialog.askopenfilename(
                    title=f"Select {kind} presence sound",
                    filetypes=[("OGG Vorbis", "*.ogg")],
                )
                if file_path:
                    self.game.put(lambda: speak("Uploading and validating sound."))
                    self._executor.submit(self._upload_file, kind, file_path)
                else:
                    self.game.put(lambda: speak("No file selected."))
            except Exception:
                self.game.put(lambda: speak("Could not open the file selector."))
            finally:
                if root is not None:
                    try:
                        root.destroy()
                    except Exception:
                        pass

        thread = threading.Thread(target=choose_file, name="presence-file-dialog", daemon=True)
        thread.start()
        speak("Opening file explorer.")

    def _upload_file(self, kind, file_path):
        try:
            if not _upload_transport_allowed(self.base_url):
                raise ValueError(TRANSPORT_ERROR)
            if not file_path.lower().endswith(".ogg"):
                raise ValueError("Only OGG Vorbis files are accepted.")
            size = os.path.getsize(file_path)
            if size <= 0 or size > MAX_SOUND_BYTES:
                raise ValueError("The sound must be no larger than 512 KB.")
            with open(file_path, "rb") as selected:
                data = selected.read(MAX_SOUND_BYTES + 1)
            if len(data) != size or data[:4] != b"OggS":
                raise ValueError("The selected file is not a valid OGG file.")
            response = requests.post(
                f"{self.base_url}/api/presence-sounds/{kind}",
                data=data,
                headers={
                    "Authorization": f"Bearer {self.upload_token}",
                    "Content-Type": "audio/ogg",
                },
                timeout=(3, 10),
            )
            payload = response.json()
            if not response.ok:
                raise ValueError(str(payload.get("error", "Upload failed.")))
            sound_id = str(payload.get("sound_id", ""))
            if not SOUND_ID_RE.fullmatch(sound_id):
                raise ValueError("Server returned an invalid sound identifier.")
            self.own_sound_ids[kind] = sound_id
            duration = float(payload.get("duration", 0))
            self.game.put(lambda: speak(f"{kind} sound uploaded successfully. Duration {duration:.2f} seconds."))
        except (OSError, requests.RequestException, ValueError, TypeError) as error:
            message = str(error) or "Upload failed."
            self.game.put(lambda message=message: speak(f"Could not upload sound. {message}"))

    def clear(self, kind):
        if kind not in ("online", "offline") or not self.upload_token or not self.base_url:
            speak("You must be connected to a compatible server.")
            return
        if not _upload_transport_allowed(self.base_url):
            speak(TRANSPORT_ERROR)
            return
        speak(f"Removing custom {kind} sound.")
        self._executor.submit(self._clear_remote, kind)

    def _clear_remote(self, kind):
        try:
            if not _upload_transport_allowed(self.base_url):
                raise ValueError(TRANSPORT_ERROR)
            response = requests.delete(
                f"{self.base_url}/api/presence-sounds/{kind}",
                headers={"Authorization": f"Bearer {self.upload_token}"},
                timeout=(3, 7),
            )
            payload = response.json()
            if not response.ok:
                raise ValueError(str(payload.get("error", "Request failed.")))
            self.own_sound_ids[kind] = ""
            self.game.put(lambda: speak(f"Custom {kind} sound removed. The default sound will be used."))
        except (requests.RequestException, ValueError) as error:
            message = str(error) or "Request failed."
            self.game.put(lambda message=message: speak(f"Could not remove sound. {message}"))
