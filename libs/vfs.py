"""Virtual file system for packaged client assets.

Two modes:
  * Source runs:  a local ``data/`` folder is used as-is (no decryption).
  * Compiled runs: ``sounds.dat`` holds a ZIP whose members are per-file
    XChaCha20-Poly1305 blobs (format ``BTX1``, see ``btx_encrypt``).
    Nothing is extracted up front: members are decrypted on demand into a
    bounded LRU temp cache, and the pack never lands on disk as plaintext
    in bulk.  Path-based consumers keep working unchanged because a small
    hook layer (``builtins.open``, ``os.path.exists/isfile/isdir``,
    ``os.path.getsize``, ``os.scandir``/``os.listdir`` and
    ``safe_vorbis.load_vorbis_pcm``) materializes pack members lazily.

Security note: the decryption key ships inside the client, so this raises
the bar against casual bulk extraction (there is no full plaintext dump)
but is not a cryptographic boundary against a determined reverse engineer.
"""

import atexit
import builtins
import json
import os
import shutil
import tempfile
import threading
import zipfile
import zlib

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

import libs.consts as consts

PAK_PATH = "sounds.dat"
SERVER_CONFIG_MEMBER = ".bt/server_endpoint.json"
PACK_META_MEMBER = ".bt/pack_meta.json"
FORMAT_MAGIC = b"BTX1"
FORMAT_NAME = "bt-aes256gcm-v1"
COMPRESSED_FLAG = 0x01

# Decryption key, hex-split to break raw string scans in the compiled binary.
# 32 bytes total; must stay in lockstep with client/tools/pack_data.py (which
# imports the helpers below, so there is a single source of truth).
_KEY_PARTS = (
    "58746675224e9e89",
    "9733eb0a5d112ef8",
    "fdac3ace46502b99",
    "fb7c39c9d28d529f",
)

TEMP_DIR = None
VFS_INITIALIZED = False
EMBEDDED_SERVER_CONFIG = None
_INSTANCE = None
_HOOKS_INSTALLED = False


def _pack_key():
    return bytes.fromhex("".join(_KEY_PARTS))


def btx_encrypt(data, key=None):
    """Encrypt one member: ``BTX1`` + flags + nonce(12) + ciphertext+tag.

    AES-256-GCM (authenticated; tamper-evident) with a fresh random nonce
    per member.  Payload is zlib-compressed first when that actually shrinks
    it (encrypted bytes are incompressible, so compression must happen
    before encryption).
    """
    if key is None:
        key = _pack_key()
    compressed = zlib.compress(data, level=6)
    if len(compressed) < len(data):
        payload = compressed
        flags = COMPRESSED_FLAG
    else:
        payload = data
        flags = 0
    nonce = os.urandom(12)
    ciphertext = AESGCM(key).encrypt(nonce, payload, None)
    return FORMAT_MAGIC + bytes([flags]) + nonce + ciphertext


def btx_decrypt(blob, key=None):
    """Inverse of :func:`btx_encrypt`; raises on bad format or tampering."""
    if key is None:
        key = _pack_key()
    if not blob.startswith(FORMAT_MAGIC) or len(blob) < 4 + 1 + 12 + 16:
        raise ValueError(
            "sounds.dat uses an unknown member format (expected BTX1). "
            "Rebuild it with tools/pack_data.py."
        )
    flags = blob[4]
    nonce = blob[5:17]
    ciphertext = blob[17:]
    payload = AESGCM(key).decrypt(nonce, ciphertext, None)
    if flags & COMPRESSED_FLAG:
        return zlib.decompress(payload)
    return payload


class _DirEntryShim:
    """Minimal stand-in for ``os.DirEntry`` for pack-only directory entries."""

    __slots__ = ("_name", "_path", "_is_dir", "_is_file")

    def __init__(self, name, path, is_dir, is_file):
        self._name = name
        self._path = path
        self._is_dir = is_dir
        self._is_file = is_file

    @property
    def name(self):
        return self._name

    @property
    def path(self):
        return self._path

    def is_dir(self, follow_symlinks=True):
        return self._is_dir

    def is_file(self, follow_symlinks=True):
        return self._is_file

    def is_symlink(self):
        return False


def _is_safe_member(name):
    """Reject absolute names and traversal outside the pack root."""
    if not name or name.startswith(("/", "\\")) or "\x00" in name:
        return False
    parts = name.replace("\\", "/").split("/")
    return ".." not in parts and "." not in parts and all(parts)


class PackVFS:
    """Lazy, bounded-cache reader for the encrypted sounds.dat pack."""

    def __init__(self, pak_path, cache_root, max_cache_bytes=None):
        self._zip = zipfile.ZipFile(pak_path)
        self.cache_root = os.path.abspath(cache_root)
        os.makedirs(self.cache_root, exist_ok=True)
        self._max_bytes = (
            max_cache_bytes
            if max_cache_bytes is not None
            else int(getattr(consts, "VFS_CACHE_MB", 512) * 1024 * 1024)
        )
        self._lock = threading.Lock()
        self._cached_bytes = 0
        self._lru = {}  # member name -> temp path (insertion order = LRU)
        self._members = set()
        self._dirs = set()
        # Windows paths are case-insensitive: the game frequently requests
        # normcase-lowercased paths (e.g. instrument samples) while the pack
        # preserves the original case of data/ folder names.  These indexes
        # fold case so both spellings resolve to the same member.
        self._member_by_lower = {}
        self._dir_by_lower = {}
        for info in self._zip.infolist():
            raw_name = info.filename.replace("\\", "/")
            if info.is_dir():
                key = raw_name.rstrip("/")
                self._dirs.add(key)
                self._dir_by_lower.setdefault(key.lower(), key)
                continue
            if not _is_safe_member(raw_name):
                raise ValueError(f"unsafe member name in pack: {raw_name!r}")
            self._members.add(raw_name)
            self._member_by_lower.setdefault(raw_name.lower(), raw_name)
            parent = os.path.dirname(raw_name)
            while parent:
                self._dirs.add(parent)
                self._dir_by_lower.setdefault(parent.lower(), parent)
                parent = os.path.dirname(parent)
        try:
            meta = json.loads(
                btx_decrypt(self._read_raw(PACK_META_MEMBER)).decode("utf-8")
            )
        except KeyError:
            raise ValueError(
                "sounds.dat has no pack metadata. Rebuild it with tools/pack_data.py."
            )
        if meta.get("format") != FORMAT_NAME:
            raise ValueError(
                f"sounds.dat pack format {meta.get('format')!r} is incompatible; "
                "rebuild it with tools/pack_data.py."
            )
        self._server_config = json.loads(
            btx_decrypt(self._read_raw(SERVER_CONFIG_MEMBER)).decode("utf-8")
        )

    @property
    def server_config(self):
        return dict(self._server_config)

    def _canonical_member(self, name):
        """Pack member name for *name* (case-insensitive), or None."""
        if name in self._members:
            return name
        return self._member_by_lower.get(name.lower())

    def _canonical_dir(self, name):
        """Pack dir name for *name* (case-insensitive), or None."""
        if name in self._dirs:
            return name
        return self._dir_by_lower.get(name.lower())

    def _read_raw(self, name):
        with self._zip.open(name) as handle:
            return handle.read()

    def read_member(self, name):
        """Decrypt one member fully in memory (no disk write)."""
        canonical = self._canonical_member(name)
        if canonical is None:
            raise KeyError(name)
        return btx_decrypt(self._read_raw(canonical), _pack_key())

    def member_exists(self, name):
        return self._canonical_member(name) is not None

    def member_is_dir(self, name):
        return self._canonical_dir(name) is not None

    def list_members(self, rel_dir):
        """``(name, is_dir)`` pairs directly inside ``rel_dir`` (pack index).

        Internal ``.bt/`` build members are hidden from listings.
        ``rel_dir`` may use any case (Windows normcase paths); the canonical
        pack spelling is resolved first.
        """
        rel_dir = rel_dir.replace("\\", "/").strip("/")
        canonical = self._canonical_dir(rel_dir)
        if canonical is None and rel_dir:
            return []
        rel_dir = canonical if canonical is not None else rel_dir
        prefix = rel_dir + "/" if rel_dir else ""
        prefix_lower = prefix.lower()
        found = {}
        for name in self._members:
            if not name.startswith(".bt/") and name.lower().startswith(prefix_lower):
                rest = name[len(prefix):]
                if "/" in rest:
                    found.setdefault(rest.split("/", 1)[0], True)
                else:
                    found[rest] = False
        for name in self._dirs:
            if not name.startswith(".bt") and name.lower().startswith(prefix_lower):
                rest = name[len(prefix):]
                if rest and "/" not in rest:
                    found[rest] = True
        return sorted((name, is_dir) for name, is_dir in found.items())

    def materialize(self, name):
        """Ensure the member exists on disk (LRU-bounded) and return its path."""
        if not _is_safe_member(name):
            raise ValueError(f"unsafe member name: {name!r}")
        canonical = self._canonical_member(name)
        if canonical is None:
            raise KeyError(name)
        with self._lock:
            cached = self._lru.get(canonical)
            if cached is not None:
                # Refresh LRU position.
                del self._lru[canonical]
                self._lru[canonical] = cached
                return cached
            data = btx_decrypt(self._read_raw(canonical), _pack_key())
            target = os.path.join(self.cache_root, *canonical.split("/"))
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, "wb") as handle:
                handle.write(data)
            self._lru[canonical] = target
            self._cached_bytes += len(data)
            self._evict_locked()
            return target

    def _evict_locked(self):
        while self._cached_bytes > self._max_bytes and len(self._lru) > 1:
            oldest_name, oldest_path = next(iter(self._lru.items()))
            del self._lru[oldest_name]
            try:
                size = os.path.getsize(oldest_path)
            except OSError:
                size = 0
            self._cached_bytes -= size
            try:
                os.unlink(oldest_path)
            except OSError:
                pass  # In use (open handle) or already gone; temp dir cleans up.

    def resolve_path(self, path):
        """If *path* refers to a pack member, materialize it; else return None."""
        root = os.path.abspath(self.cache_root)
        candidate = os.path.abspath(os.path.normpath(path))
        try:
            rooted = os.path.commonpath((root, candidate)) == root
        except ValueError:
            rooted = False
        if not rooted:
            # Relative path (consumers sometimes pass relpaths to C fopen).
            candidate = os.path.abspath(os.path.join(root, path))
            try:
                rooted = os.path.commonpath((root, candidate)) == root
            except ValueError:
                return None
        if not rooted:
            return None
        rel = os.path.relpath(candidate, root).replace("\\", "/")
        if rel == "." or rel.startswith("."):
            return None
        if not self.member_exists(rel):
            return None
        try:
            return self.materialize(rel)
        except (KeyError, ValueError):
            return None

    def close(self):
        try:
            self._zip.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Path hook layer (compiled mode only; everything delegates when not mounted)
# ---------------------------------------------------------------------------

_ORIGINALS = {}


def _under_root(path):
    root = os.path.abspath(consts.SOUNDPREPEND).rstrip("/\\")
    candidate = os.path.abspath(os.path.normpath(path))
    try:
        return os.path.commonpath((root, candidate)) == root
    except ValueError:
        return False


def _hooked_open(file, mode="r", *args, **kwargs):
    if _INSTANCE is not None and mode in ("r", "rb", "r+", "rb+"):
        if isinstance(file, (str, os.PathLike)) and _under_root(file):
            materialized = _INSTANCE.resolve_path(file)
            if materialized is not None:
                return _ORIGINALS["open"](materialized, mode, *args, **kwargs)
    return _ORIGINALS["open"](file, mode, *args, **kwargs)


def _hooked_exists(path):
    if _INSTANCE is not None and isinstance(path, (str, os.PathLike)) and _under_root(path):
        rel = os.path.relpath(
            os.path.abspath(os.path.normpath(path)),
            os.path.abspath(consts.SOUNDPREPEND).rstrip("/\\"),
        ).replace("\\", "/")
        if _INSTANCE.member_exists(rel):
            _INSTANCE.materialize(rel)
            return True
    return _ORIGINALS["exists"](path)


def _hooked_isfile(path):
    if _INSTANCE is not None and isinstance(path, (str, os.PathLike)) and _under_root(path):
        rel = os.path.relpath(
            os.path.abspath(os.path.normpath(path)),
            os.path.abspath(consts.SOUNDPREPEND).rstrip("/\\"),
        ).replace("\\", "/")
        if _INSTANCE.member_exists(rel):
            _INSTANCE.materialize(rel)
            return True
    return _ORIGINALS["isfile"](path)


def _hooked_isdir(path):
    if _INSTANCE is not None and isinstance(path, (str, os.PathLike)) and _under_root(path):
        rel = os.path.relpath(
            os.path.abspath(os.path.normpath(path)),
            os.path.abspath(consts.SOUNDPREPEND).rstrip("/\\"),
        ).replace("\\", "/")
        if _INSTANCE.member_is_dir(rel):
            return True
    return _ORIGINALS["isdir"](path)


def _hooked_getsize(path):
    if _INSTANCE is not None and isinstance(path, (str, os.PathLike)) and _under_root(path):
        rel = os.path.relpath(
            os.path.abspath(os.path.normpath(path)),
            os.path.abspath(consts.SOUNDPREPEND).rstrip("/\\"),
        ).replace("\\", "/")
        if _INSTANCE.member_exists(rel):
            _INSTANCE.materialize(rel)
    return _ORIGINALS["getsize"](path)


def _real_listdir(path):
    """Real os.listdir; a missing physical dir (virtual pack dir) is empty."""
    try:
        return _ORIGINALS["listdir"](path)
    except (FileNotFoundError, NotADirectoryError):
        return []


def _real_scandir(path):
    """Real os.scandir; a missing physical dir (virtual pack dir) is empty."""
    try:
        return list(_ORIGINALS["scandir"](path))
    except (FileNotFoundError, NotADirectoryError):
        return []


def _hooked_listdir(path="."):
    if _INSTANCE is not None and _under_root(path):
        rel = os.path.relpath(
            os.path.abspath(os.path.normpath(path)),
            os.path.abspath(consts.SOUNDPREPEND).rstrip("/\\"),
        ).replace("\\", "/")
        names = set(_real_listdir(path))
        names.update(name for name, _is_dir in _INSTANCE.list_members(rel if rel != "." else ""))
        return sorted(names)
    return _ORIGINALS["listdir"](path)


class _ScandirResult:
    """Context-manager-compatible wrapper around a list of DirEntry shims.

    ``os.scandir`` normally returns an iterator that also supports ``with``
    (pathlib and other consumers rely on that), so a bare list would break
    them once hooks are installed.
    """

    __slots__ = ("_entries",)

    def __init__(self, entries):
        self._entries = entries

    def __iter__(self):
        return iter(self._entries)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False


def _hooked_scandir(path="."):
    if _INSTANCE is not None and _under_root(path):
        root = os.path.abspath(consts.SOUNDPREPEND).rstrip("/\\")
        rel = os.path.relpath(
            os.path.abspath(os.path.normpath(path)), root
        ).replace("\\", "/")
        rel = rel if rel != "." else ""
        real_entries = _real_scandir(path)
        existing = {entry.name for entry in real_entries}
        shims = []
        for item, is_dir in _INSTANCE.list_members(rel):
            if item not in existing:
                shims.append(
                    _DirEntryShim(item, os.path.join(path, item), is_dir, not is_dir)
                )
        return _ScandirResult(real_entries + shims)
    return _ORIGINALS["scandir"](path)


def _hooked_load_vorbis_pcm(path, *args, **kwargs):
    if _INSTANCE is not None:
        materialized = _INSTANCE.resolve_path(path)
        if materialized is not None:
            path = materialized
    return _ORIGINALS["load_vorbis_pcm"](path, *args, **kwargs)


def install_hooks():
    """Patch file-access entry points so pack members materialize lazily."""
    global _HOOKS_INSTALLED
    if _HOOKS_INSTALLED:
        return
    _ORIGINALS["open"] = builtins.open
    _ORIGINALS["exists"] = os.path.exists
    _ORIGINALS["isfile"] = os.path.isfile
    _ORIGINALS["isdir"] = os.path.isdir
    _ORIGINALS["getsize"] = os.path.getsize
    _ORIGINALS["listdir"] = os.listdir
    _ORIGINALS["scandir"] = os.scandir
    builtins.open = _hooked_open
    os.path.exists = _hooked_exists
    os.path.isfile = _hooked_isfile
    os.path.isdir = _hooked_isdir
    os.path.getsize = _hooked_getsize
    os.listdir = _hooked_listdir
    os.scandir = _hooked_scandir
    try:
        from . import safe_vorbis
    except ImportError:
        from libs import safe_vorbis
    try:
        _ORIGINALS["load_vorbis_pcm"] = safe_vorbis.load_vorbis_pcm
        safe_vorbis.load_vorbis_pcm = _hooked_load_vorbis_pcm
    except Exception:
        _ORIGINALS["load_vorbis_pcm"] = None
    _HOOKS_INSTALLED = True


def uninstall_hooks():
    """Restore original functions; safe to call multiple times."""
    global _HOOKS_INSTALLED
    if not _HOOKS_INSTALLED:
        return
    builtins.open = _ORIGINALS["open"]
    os.path.exists = _ORIGINALS["exists"]
    os.path.isfile = _ORIGINALS["isfile"]
    os.path.isdir = _ORIGINALS["isdir"]
    os.path.getsize = _ORIGINALS["getsize"]
    os.listdir = _ORIGINALS["listdir"]
    os.scandir = _ORIGINALS["scandir"]
    if _ORIGINALS.get("load_vorbis_pcm"):
        try:
            from . import safe_vorbis
        except ImportError:
            from libs import safe_vorbis
        try:
            safe_vorbis.load_vorbis_pcm = _ORIGINALS["load_vorbis_pcm"]
        except Exception:
            pass
    _ORIGINALS.clear()
    _HOOKS_INSTALLED = False


def get_embedded_server_config():
    """Return a copy of the production endpoint loaded from the VFS package."""
    if EMBEDDED_SERVER_CONFIG is None:
        return None
    return dict(EMBEDDED_SERVER_CONFIG)


def init_vfs():
    global TEMP_DIR, VFS_INITIALIZED, EMBEDDED_SERVER_CONFIG, _INSTANCE

    if VFS_INITIALIZED:
        return

    if not os.path.exists(PAK_PATH):
        if os.path.exists("data"):
            print("VFS: Running from source (data folder found), skipping decryption.")
            consts.SOUNDPREPEND = "data/"
            consts.SOUNDSPREPEND = "/data/"
            EMBEDDED_SERVER_CONFIG = None
            VFS_INITIALIZED = True
            return
        print(f"VFS Error: Missing both {PAK_PATH} and data/ folder!")
        raise SystemExit(1)

    print("VFS: Initializing Virtual File System from %s..." % PAK_PATH)
    # Secure temporary directory; holds only lazily materialized members.
    TEMP_DIR = tempfile.mkdtemp(prefix=".bt_cache_")
    atexit.register(cleanup_vfs)

    try:
        _INSTANCE = PackVFS(PAK_PATH, TEMP_DIR)
        EMBEDDED_SERVER_CONFIG = _INSTANCE.server_config
        consts.SOUNDPREPEND = TEMP_DIR.replace("\\", "/")
        if not consts.SOUNDPREPEND.endswith("/"):
            consts.SOUNDPREPEND += "/"
        consts.SOUNDSPREPEND = "/" + consts.SOUNDPREPEND
        install_hooks()
        print("VFS: Assets mounted (lazy decryption, %d members)." % len(_INSTANCE._members))
        VFS_INITIALIZED = True
    except Exception as error:
        print("VFS Decryption Error: %s" % error)
        cleanup_vfs()
        raise SystemExit(1)


def cleanup_vfs():
    global TEMP_DIR, EMBEDDED_SERVER_CONFIG, _INSTANCE, VFS_INITIALIZED
    if _INSTANCE is not None:
        try:
            _INSTANCE.close()
        except Exception:
            pass
        _INSTANCE = None
    uninstall_hooks()
    if TEMP_DIR and os.path.exists(TEMP_DIR):
        try:
            shutil.rmtree(TEMP_DIR, ignore_errors=True)
        except Exception:
            pass
    TEMP_DIR = None
    EMBEDDED_SERVER_CONFIG = None
    VFS_INITIALIZED = False


def _reset_for_tests():
    """Reset module state so tests can re-run init_vfs against fixtures."""
    cleanup_vfs()
    global VFS_INITIALIZED
    VFS_INITIALIZED = False