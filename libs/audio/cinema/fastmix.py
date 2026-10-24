"""The room's per-speaker mix, vectorised, handed to the stdlib-only core.

`channel.mix_samples` is called once per fed speaker per frame, and it is the
whole cost of a room: a twelve-speaker room at 1920-sample (40 ms) relay frames
paid **5.8 ms of Python per frame** (12 x 0.49 ms), 14.5% of the game's frame
budget, every frame, for as long as a cabinet played. On a slower machine that
is the game stuttering whenever a song comes out of a room with a lot of
speakers -- and it is paid on the transport's thread, so it holds the
interpreter while the frame is being drawn.

The six modules of `libs/audio/cinema` may import the standard library only
(`test_cinema_portability`), so the core does not import numpy itself: it takes
a mixer through `channel.set_vector_mix`, and this module is the engine-side
half that hands it one. numpy makes the same bytes in **0.027 ms** a mix -- the
room drops to 0.28 ms a frame, 0.7% -- because it is the same arithmetic
(``(L * scaled_l + R * scaled_r) >> 15`` with the int16 clamp), not an
approximation: ``>>`` on a signed integer array is the floor shift Python's
``>>`` is, and the arrays are native int16 exactly as ``array('h')`` is.
`test_cinema_speaker.FastMixTests` pins that against the reference loop.

Importing this module installs the mixer. A machine with no numpy keeps the
reference loop: it is slower, and it still plays.
"""

from . import channel

try:                                    # numpy is optional, never fatal
    import numpy as _np
except Exception:                       # pragma: no cover - a machine without it
    _np = None


def numpy_mix(left_samples, right_samples, count, scaled_l, scaled_r):
    """One MONO int16 feed, mixed by numpy exactly as the reference loop does."""
    left = _np.frombuffer(left_samples, dtype=_np.int16, count=count)
    right = _np.frombuffer(right_samples, dtype=_np.int16, count=count)
    mixed = (left.astype(_np.int32) * scaled_l
             + right.astype(_np.int32) * scaled_r) >> 15
    _np.clip(mixed, channel._INT16_MIN, channel._INT16_MAX, out=mixed)
    return mixed.astype(_np.int16).tobytes()


def install():
    """Hand the core the numpy mixer, or leave its reference loop in place."""
    if _np is None:
        channel.set_vector_mix(None)
        return False
    channel.set_vector_mix(numpy_mix)
    return True


installed = install()
