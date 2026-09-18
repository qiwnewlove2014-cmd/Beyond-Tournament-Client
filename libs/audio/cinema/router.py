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

from math import sqrt

from .channel import (AUTO, MONO, STEREO, ChannelAnalyzer, mix_samples,
                      source_layout, to_samples)
from .layout import CinemaLayout
from .profiles import IMAGE_SLOTS, get_profile

# The plain two-source jukebox, expressed as a plan. Matching this exactly
# lets the renderer hand back the untouched source frames, which is what
# makes front-only playback bit-identical to the audio it replaces.
PARITY_PLAN = (("front_l", 1.0, 0.0), ("front_r", 0.0, 1.0))

MONO_PROFILE = "mono_spread"


def ordered_edges(low, high):
    """A slot's two raw crossover edges, rounded and in order.

    The edges are carried *as the map wrote them* -- this module may not import
    the reader that decides what a mark means (it is part of the portable
    core, which imports nothing but the standard library and its five
    siblings) -- but they are ordered here, because a band written the other
    way round is the same band and must not read as a different room. Ordering
    is pure arithmetic; the vocabulary each edge is settled into is the
    reader's, and a change only it would make is a change the room re-reads
    anyway.
    """
    first = round(float(low or 0.0), 4)
    second = round(float(high or 0.0), 4)
    return (first, second) if first <= second else (second, first)


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

    def _plan_for(self, profile, only=None):
        """Resolve a profile into concrete ``(slot, gain_l, gain_r)`` terms.

        ``only`` restricts the plan to a given speaker set, which is how a
        mono passage is spread across the speakers the room actually has
        instead of conjuring extra ones. Slots are emitted in room order, so
        truncating to ``max_speakers`` drops the back of the room before the
        screen wall -- the centre and front pair are the last things a
        constrained room should lose.
        """
        available = self.layout.slots
        slots = [slot for slot in profile.slots
                 if slot in available and (only is None or slot in only)]
        slots = slots[:self.max_speakers]
        # The equal-power share is computed over the speakers this plan will
        # actually feed, so trimming the room also raises each speaker's
        # share instead of leaving the whole room quiet.
        if profile.equal_power_surrounds:
            shared = [slot for slot in slots
                      if not profile.protect_image or slot not in IMAGE_SLOTS]
        else:
            shared = []
        scale = 1.0 / sqrt(len(shared)) if shared else 1.0
        plan = []
        for slot in slots:
            weight = profile.weight(slot)
            if weight is None:
                continue
            factor = scale if slot in shared else 1.0
            level = self.layout.level(slot)
            plan.append((slot, weight[0] * level * factor, weight[1] * level * factor))
        return tuple(plan)

    def _build_plans(self):
        stereo = self._plan_for(self.profile)
        if self.profile.name != MONO_PROFILE:
            mono = self._plan_for(get_profile(MONO_PROFILE),
                                  only=[term[0] for term in stereo])
        else:
            mono = stereo
        self._plans = {STEREO: stereo, MONO: mono}
        # The room's speaker set is the profile's, so the mono plan can never
        # need a source the stereo plan has not already created.
        self._slots = tuple(term[0] for term in stereo)
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
    def signature(self):
        """A stable description of the room's shape, for reuse decisions.

        Two renderers built from the same speakers, profile and trims are the
        same room even though they are different objects, so a caller that
        re-offers a song on a map reload keeps the room it already has. A
        renderer whose speaker set, positions, levels, voicings, crossovers or
        aims differ is a *different* room: treating those as equal is what made
        a speaker a builder placed mid-song inaudible until the feature was
        toggled, and what left a bass cabinet playing full range until the next
        track after the builder dialled its crossover (the room was
        "unchanged", so the bank that holds the room was never re-shaped).

        Every number a builder can change while a song plays belongs here. A
        mark or a voicing is not a *shape*, but it is a property of the room a
        bank is playing, and a field left out of this tuple is a change that
        never reaches that bank at all -- plugin.acquire hands back the
        renderer it already has, so nothing re-reads the map. A crossover's
        *sign* is what makes it one mark or the other (a bass cabinet below a
        frequency, a tweeter above it), so it is carried as it was written and
        a room that swaps one for the other is a different room. **Both**
        edges are carried, because a speaker that grew a second one became a
        *band* rather than a two-way split: a mid cabinet's upper edge is as
        much a property of the room as its lower one, and a builder who dials
        200-3 kHz onto a speaker already set to 200-1.5 kHz must get a room
        that re-cuts -- the two numbers are read as the map wrote them here,
        not composed into a mark, because this module is part of the portable
        core and may not import the reader that composes one.
        """
        slots = []
        for slot in self._slots:
            position = self.layout.position(slot)
            spec = self.layout.spec(slot)
            slots.append((
                slot,
                None if position is None else tuple(round(float(value), 3)
                                                    for value in position),
                round(float(self.layout.level(slot)), 4),
                round(float(self.layout.delay_ms(slot)), 4),
                round(float(self.layout.tone(slot)), 4),
                ordered_edges(self.layout.crossover(slot),
                              self.layout.crossover_high(slot)),
                None if spec is None or spec.aim_yaw is None else round(float(spec.aim_yaw), 3),
                False if spec is None else bool(spec.has_cone),
            ))
        return (self.profile.name, self.max_speakers, tuple(slots))

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


def renderer_for(anchor, profile, *, specs=None, fill=True, layout=None,
                 max_speakers=None, detect_channels=True, declared_layout=AUTO):
    """The renderer a room asks for, ring and all -- one rule, two callers.

    ``fill`` is the only difference between a room read off the map and one a
    builder asked for by name: a requested shape may pad itself out with the
    geometric ring for the slots the map does not have, while a room read off
    the map is exactly the speakers someone placed. Playback
    (``plugin.CinemaSpeakerHost.acquire_bank``) and the read-outs (a cabinet's
    own menu, which has to answer *before* anything is acquired) both build
    their room here, so a menu can never describe a room that would play
    differently -- which is the whole point of being able to ask.
    """
    if layout is None and not fill and specs:
        layout = CinemaLayout(anchor, specs, use_ring=False)
        specs = None
    if layout is not None:
        specs = None
    return CinemaRenderer(anchor, profile, layout, specs=specs,
                          max_speakers=max_speakers,
                          detect_channels=detect_channels,
                          declared_layout=declared_layout)
