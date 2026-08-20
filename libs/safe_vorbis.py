"""Overflow-safe Ogg Vorbis PCM loader.

pyogg 0.7's ``VorbisFile`` allocates one destination buffer sized from
``ov_pcm_total()`` and lets libvorbisfile's ``ov_read()`` decode into it
with a shrinking byte budget. On files whose real PCM length exceeds the
granulepos estimate (metadata under-reporting), ``ov_read`` writes past the
end of the ctypes buffer — observed live with ``PYTHONMALLOC=debug`` as
eight zero bytes written past an 832-byte allocation ("bad trailing pad
byte", allocation traced to pyogg/vorbis_file.py line 51). That silently
corrupts the CPython heap; the garbage collector then dies with an access
violation (0xC0000005) seconds or minutes later, far away from the actual
write. This was the root cause of the hard client crashes during zombie
rounds, which mass-load footstep/water/ambience OGG files.

This loader never trusts ``ov_pcm_total()``: it decodes in fixed chunks
into a reusable ctypes buffer and accumulates into a bytearray. An
overflow is impossible by construction — every ``ov_read()`` is bounded by
the chunk buffer's exact size, and only the returned byte count is copied.
"""

import ctypes
from types import SimpleNamespace

from pyogg import vorbis as _vorbis

_CHUNK_BYTES = 16384

# pyogg's OggVorbis_File declaration is 832 bytes, but the libvorbisfile.dll
# shipped with pyogg writes (at least) 8 zero bytes PAST that size while
# opening/decoding — observed live with PYTHONMALLOC=debug ("bad trailing
# pad byte" on an 832-byte allocation, the writes are the tail fields of the
# DLL's slightly larger real structure). Those stray writes silently
# corrupted the CPython heap and crashed the game long afterwards in the
# garbage collector. Allocating the struct with zeroed trailing slack
# absorbs them completely.
_VF_PAD_BYTES = 512


class SafeVorbisError(Exception):
    pass


def _alloc_vorbis_file():
    """Allocate an OggVorbis_File with zeroed trailing padding.

    Returns (vf, raw); ``raw`` must stay referenced for as long as ``vf``
    is in use — it owns the memory ``vf`` points into.
    """
    raw = ctypes.create_string_buffer(
        ctypes.sizeof(_vorbis.OggVorbis_File) + _VF_PAD_BYTES
    )
    vf = ctypes.cast(raw, ctypes.POINTER(_vorbis.OggVorbis_File)).contents
    return vf, raw


def load_vorbis_pcm(path, bytes_per_sample=2, signed=True):
    """Decode an Ogg Vorbis file to 16-bit PCM.

    Returns an object with the same interface the game uses from
    pyogg.VorbisFile: ``.buffer`` (bytes), ``.channels`` (int) and
    ``.frequency`` (int).
    """
    vf, vf_raw = _alloc_vorbis_file()
    if _vorbis.libvorbisfile.ov_fopen(
        _vorbis.to_char_p(path), ctypes.byref(vf)
    ) != 0:
        raise SafeVorbisError(f"could not open vorbis file: {path}")
    try:
        info = _vorbis.libvorbisfile.ov_info(ctypes.byref(vf), -1)
        if not info:
            raise SafeVorbisError(f"could not read vorbis info: {path}")
        channels = int(info.contents.channels)
        frequency = int(info.contents.rate)

        chunk = ctypes.create_string_buffer(_CHUNK_BYTES)
        chunk_ptr = ctypes.cast(chunk, ctypes.POINTER(ctypes.c_char))
        bitstream = ctypes.c_int()
        bitstream_previous = None
        pcm = bytearray()
        while True:
            result = _vorbis.libvorbisfile.ov_read(
                ctypes.byref(vf),
                chunk_ptr,
                _CHUNK_BYTES,
                0,  # little endian
                bytes_per_sample,
                int(signed),
                ctypes.byref(bitstream),
            )
            if result == 0:
                break  # end of file
            if result < 0:
                raise SafeVorbisError(
                    f"vorbis decode error {result} in {path}"
                )
            if bitstream_previous is None:
                bitstream_previous = bitstream.value
            elif bitstream.value != bitstream_previous:
                raise SafeVorbisError(
                    f"{path}: multiple logical bitstreams are not supported"
                )
            # Only copy what ov_read reported — never more than the chunk.
            pcm += chunk.raw[:result]

        return SimpleNamespace(
            buffer=bytes(pcm), channels=channels, frequency=frequency
        )
    finally:
        _vorbis.libvorbisfile.ov_clear(ctypes.byref(vf))
