"""Speaker distribution: one stereo frame in, one mono feed per speaker out.

The renderer is where the two halves of the problem meet. A stereo frame is
spread with the profile's Mid/Side weights so the front pair rebuilds the
original image and the rest of the room carries the difference; a mono frame
takes the mono path instead.

Mono content must never go through the stereo profile. With identical
channels the front, centre and the sides all collapse to the same samples
(the sides collapse to silence), which is both a destroyed image and two or
three speakers playing identical content -- the comb-filtering case. A mono
programme therefore always renders through the mono-spread weights, where
every speaker gets the programme itself. This is the one automatic decision
the renderer makes, and it is the reason the source layout has to be
detected at all.

No OpenAL here by design. Sources, buffers, upload and queueing stay with
the transport that already owns them and on the audio owner thread; this
class only answers "what should each speaker be fed right now".
"""

from .channel import (AUTO, MONO, STEREO, ChannelAnalyzer, mix_samples,
                      source_layout, to_samples)
from .layout import SLOT_ORDER, CinemaLayout
from .profiles import IMAGE_SLOTS, get_profile

# The plain two-source jukebox, expressed as a plan. Matching this exactly
# lets the renderer hand back the untouched source frames, which is what
# makes front-only playback bit-identical to the audio it replaces.
PARITY_PLAN = (("front_l", 1.0, 0.0), ("front_r", 0.0, 1.0))

MONO_PROFILE = "mono_spread"


class CinemaRenderer:
    """Per-jukebox renderer: stereo PCM in, per-slot mono PCM out.

    ``slots`` is the union of every slot any plan can drive, so the transport
    can create one OpenAL source per slot once and then queue only what the
    active plan returns. A verdict change mid-song (a mono intro, a stereo
    chorus) therefore never needs a source to be created while audio is
    already flowing.
    """

    MAX_SPEAKERS = 12

    def __init__(self, anchor, profile=None, layout=None, *, specs=None,
                 max_speakers=None, detect_channels=True, declared_layout=AUTO):
        self.profile = get_profile(profile)
        self.declared_layout = source_layout(declared_layout)
        self.detect_channels = bool(detect_channels)
        self.max_speakers = max(1, int(max_speakers or self.MAX_SPEAKERS))
        self.layout = layout if layout is not None else CinemaLayout(anchor, specs)
        self.channel = ChannelAnalyzer()
        self.active_kind = STEREO
        self._plans = {}
        self._slots = ()
        self._build_plans()

    # ---------------------------------------------------------------- plans

    def _plan_for(self, profile):
        """Resolve a profile into concrete ``(slot, gain_l, gain_r)`` terms.

        Slots are emitted in room order, so truncating to ``max_speakers``
        drops the back of the room before the screen wall -- the centre and
        front pair are the last things a constrained room should lose.
        """
        available = self.layout.slots
        scale = profile.surround_scale()
        plan = []
        for slot in profile.slots:
            if slot not in available:
                continue
            weight = profile.weight(slot)
            if weight is None:
                continue
            factor = scale if (profile.equal_power_surrounds and slot not in IMAGE_SLOTS) else 1.0
            level = self.layout.level(slot)
            plan.append((slot, weight[0] * level * factor, weight[1] * level * factor))
        return tuple(plan[:self.max_speakers])

    def _build_plans(self):
        stereo = self._plan_for(self.profile)
        if self.profile.name != MONO_PROFILE:
            mono = self._plan_for(get_profile(MONO_PROFILE))
        else:
            mono = stereo
        self._plans = {STEREO: stereo, MONO: mono}
        self._slots = tuple(slot for slot in SLOT_ORDER
                            if any(slot == term[0] for term in stereo + mono))
        # Parity only exists when nothing trims or rescales the front pair.
        self._parity = stereo == PARITY_PLAN

    def set_profile(self, profile):
        """Swap the room's profile live; the mono plan is rebuilt with it."""
        self.profile = get_profile(profile)
        self._build_plans()

    # --------------------------------------------------------------- render

    def _verdict(self, left, right):
        if self.declared_layout != AUTO:
            return self.declared_layout
        if not self.detect_channels:
            return STEREO
        return self.channel.observe(left, right)

    def render(self, left, right):
        """Return ``[(slot, mono_pcm16), ...]`` for this frame.

        Slots the active plan does not use are simply absent, so an idle
        speaker is never fed silence buffers.
        """
        # An empty frame means nothing arrived; queueing a silent buffer for
        # every speaker would burn the transport's pool for no audio.
        if not left and not right:
            return []
        stereo = self._plans.get(STEREO) or ()
        mono = self._plans.get(MONO) or ()
        if not stereo and not mono:
            return []
        verdict = self._verdict(left, right)
        self.active_kind = verdict
        if verdict == MONO and mono:
            plan = mono
        else:
            plan = stereo
        if not plan:
            return []
        if self._parity and verdict != MONO and len(plan) == len(PARITY_PLAN):
            # Zero-copy and byte-exact: this is the plain jukebox pair.
            return [(PARITY_PLAN[0][0], left), (PARITY_PLAN[1][0], right)]
        left_samples = to_samples(left)
        right_samples = to_samples(right)
        return [(slot, mix_samples(left_samples, right_samples, gain_l, gain_r))
                for slot, gain_l, gain_r in plan]

    # ----------------------------------------------------------- inspection

    @property
    def slots(self):
        """Every slot any plan can drive, in room order."""
        return self._slots

    @property
    def extra_latency_s(self):
        """Latency this room adds beyond the transport's own buffering.

        The jam-note sync measures the jukebox staging queue so remote
        instruments land on the beat; a per-speaker delay is latency the
        song gains but that measurement cannot see, so it is reported here
        for the sync to subtract.
        """
        return self.layout.max_delay_s

    def plan(self, kind=STEREO):
        return self._plans.get(kind, ())

    def __repr__(self):
        return (f"CinemaRenderer({self.profile.name!r}, slots={list(self._slots)}, "
                f"max={self.max_speakers})")

    def stop(self):
        """Forget the running channel evidence; nothing native to release."""
        self.channel.reset()
        self.active_kind = STEREO
