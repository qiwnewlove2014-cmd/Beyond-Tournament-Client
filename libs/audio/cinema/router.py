"""Speaker distribution: one stereo frame in, one mono feed per speaker out.

A stereo frame is spread with the profile's Mid/Side weights so the front pair
rebuilds the original image and the rest of the room carries the difference; a
mono frame takes the mono path instead.

Mono content must never go through the stereo profile. With identical channels
the front, centre and the sides all collapse to the same samples (the sides
collapse to silence), which is both a destroyed image and two or three speakers
playing identical content -- the comb-filtering case. A mono programme therefore
always renders through the mono-spread weights, where every speaker gets the
programme itself: the one automatic decision the renderer makes, and the reason
the source layout has to be detected at all.

No OpenAL here by design: this class only answers "what should each speaker be
fed right now", and the transport that already owns the sources stays on the
audio owner thread.
"""

from math import sqrt

from .channel import (AUTO, MONO, STEREO, ChannelAnalyzer, mix_samples,
                      source_layout, to_samples)
from .layout import LAYER_SLOTS, SLOT_PAIRS, CinemaLayout
from .profiles import IMAGE_SLOTS, LAYER_POWER_SHARE, get_profile

# The plain two-source jukebox, expressed as a plan. Matching this exactly
# lets the renderer hand back the untouched source frames, which is what
# makes front-only playback bit-identical to the audio it replaces.
PARITY_PLAN = (("front_l", 1.0, 0.0), ("front_r", 0.0, 1.0))

MONO_PROFILE = "mono_spread"


def ordered_edges(low, high):
    """A slot's two raw crossover edges, rounded and in order.

    The edges are carried *as the map wrote them* -- this module may not import the
    reader that decides what a mark means (it is part of the portable core, which
    imports nothing but the standard library and its five siblings) -- but they are
    ordered here, because a band written the other way round is the same band and
    must not read as a different room. Ordering is pure arithmetic; the vocabulary
    each edge is settled into is the reader's.
    """
    first = round(float(low or 0.0), 4)
    second = round(float(high or 0.0), 4)
    return (first, second) if first <= second else (second, first)


class CinemaRenderer:
    """Per-jukebox renderer: stereo PCM in, per-slot mono PCM out.

    ``slots`` is the union of every slot any plan can drive, so the transport can
    create one OpenAL source per slot once and then queue only what the active plan
    returns: a verdict change mid-song (a mono intro, a stereo chorus) never needs a
    source created while audio is already flowing.
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
        # The frame this renderer last decided, and the last window it decoded:
        # (left, right, verdict, plan) and (left, right, l_samples, r_samples). The room
        # feeds one frame in several *groups* -- one per distinct crossover and delay
        # trim -- and each group asks for the slots that belong to it, so a frame must be
        # judged once (the layout detector counts one vote per frame) and a window
        # decoded once however many groups are cut out of it. Told apart by identity: the
        # transport hands the same objects to every group of one frame and new objects
        # for the next.
        self._frame = None
        self._window = None
        self._build_plans()

    # ---------------------------------------------------------------- plans

    def _plan_for(self, profile, only=None):
        """Resolve a profile into concrete ``(slot, gain_l, gain_r)`` terms.

        ``only`` restricts the plan to a given speaker set, which is how a mono passage
        is spread across the speakers the room actually has instead of conjuring extra
        ones. Slots are emitted in room order, so truncating to ``max_speakers`` drops
        the back of the room before the screen wall.
        """
        available = self.layout.slots
        allowed = [slot for slot in profile.slots
                   if slot in available and (only is None or slot in only)]
        slots = allowed[:self.max_speakers]
        # A cut can land inside a mirrored pair -- a roof, a wide pair, a rear
        # pair -- and half a pair is a steering bias rather than a cheaper
        # room, so the pair goes together: the half that survived is dropped
        # with it. Only a pair *the cut* separated is touched: a pair the map
        # never had (a custom room's lone wall speaker, which is that mode's
        # whole point) is left exactly as the resolver built it.
        cut = set(allowed[len(slots):])
        if cut:
            present = set(slots)
            for left, right in SLOT_PAIRS:
                if (left in present) == (right in present):
                    continue
                lonely = left if left in present else right
                partner = right if lonely == left else left
                if partner in cut:
                    slots = [slot for slot in slots if slot != lonely]
                    present.discard(lonely)
        # A squeeze down to two speakers is the *stereo* pair, not the left
        # speaker and the centre: the cut walks room order, where ``front_c``
        # stands between the pair, so the two survivors would otherwise both
        # carry the left of the image and the room would lean. The pair is the
        # floor ``plugin.MIN_BUDGETED_SPEAKERS`` promises, and it is why a
        # listener holding two full rooms still hears the third in stereo
        # rather than in a corner.
        if len(slots) == 2:
            stereo = [slot for slot in allowed if slot in IMAGE_SLOTS]
            if len(stereo) == 2:
                slots = stereo
        # The shape's equal-power share is computed over the *shape* speakers
        # this plan will actually feed, so trimming a room also raises each
        # speaker's share instead of leaving the whole room quiet.
        #
        # A **layer** is not part of that share. The optional wide pair and
        # overhead layer are added *over* the room, so counting them in the
        # shape's divisor turns the centre and the walls down the moment somebody
        # places a roof: measured on the twelve-speaker theatre, every wall fell
        # from 1/sqrt(5) to 1/sqrt(10) and the room's difference signal dropped
        # 17% -- the new width was paid for out of the walls rather than added to
        # them, which is the opposite of what a layer is for. So the shape keeps
        # exactly the level it was authored at (identical to the room without
        # layers, term for term, which ``tests/test_cinema_layer_balance.py``
        # holds) and the layers get their own share, ``LAYER_POWER_SHARE`` of the
        # shape's power between them.
        layer_slots = [slot for slot in slots if slot in LAYER_SLOTS]
        shape_slots = [slot for slot in slots if slot not in LAYER_SLOTS]
        if not profile.equal_power_surrounds:
            shared = []
        elif profile.protect_image:
            shared = [slot for slot in shape_slots if slot not in IMAGE_SLOTS]
        else:
            # No image to protect: the whole room is one budget, layers and all.
            # A mono room is every speaker playing the same middle of the song,
            # and two levels in it would be a stereo difference nobody asked
            # for (``mono_spread``'s own rule).
            shared = shape_slots + layer_slots
        scale = 1.0 / sqrt(len(shared)) if shared else 1.0
        layer_scale = scale
        if shared and layer_slots and profile.protect_image:
            layer_scale = scale * sqrt(LAYER_POWER_SHARE * len(shared) / len(layer_slots))
        plan = []
        for slot in slots:
            weight = profile.weight(slot)
            if weight is None:
                continue
            if slot in shared:
                factor = scale
            elif slot in layer_slots:
                factor = layer_scale
            else:
                factor = 1.0
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

    def _prepared(self, left, right, live=None):
        """``(verdict, plan)`` for the frame ``left``/``right`` belong to.

        The verdict is the frame's own evidence -- one vote per frame -- so the second
        window of a frame gets the first window's answer rather than a second opinion.

        ``live`` is that frame when ``left``/``right`` are a *window* cut out of it: a
        speaker carrying a delay trim is fed the same programme a few milliseconds late,
        and that window is the room's audio, not a frame of its own. Judging the window
        would count one frame of the song as several votes of layout evidence. ``None``
        means the bytes being fed are the frame, which is what a caller reading or
        replaying the room hands in.
        """
        subject_l, subject_r = (left, right) if live is None else live
        cached = self._frame
        if (cached is not None and cached[0] is subject_l
                and cached[1] is subject_r):
            return cached[2], cached[3]
        stereo = self._plans.get(STEREO) or ()
        mono = self._plans.get(MONO) or ()
        verdict = self._verdict(subject_l, subject_r)
        self.active_kind = verdict
        plan = mono if verdict == MONO and mono else stereo
        self._frame = (subject_l, subject_r, verdict, plan)
        return verdict, plan

    def _samples_for(self, left, right):
        """The two channels of this *window* as sample arrays, decoded once.

        Decoding is only needed for the blends (a whole-channel slot is handed the source
        bytes), so a room whose plan is only the front pair never decodes at all. A
        window fed twice -- a refused frame offered again -- is not decoded twice either.
        """
        cached = self._window
        if cached is not None and cached[0] is left and cached[1] is right:
            return cached[2], cached[3]
        left_samples = to_samples(left)
        right_samples = to_samples(right)
        self._window = (left, right, left_samples, right_samples)
        return left_samples, right_samples

    def render(self, left, right, only=None, live=None):
        """Return ``[(slot, mono_pcm16), ...]`` for this frame.

        Slots the active plan does not use are simply absent, so an idle speaker is never
        fed silence buffers. ``only`` is the set of slots the caller is about to feed this
        frame: the room renders one frame in several groups -- one per distinct crossover
        and delay trim -- and a group that asked for the whole room would mix every
        speaker once per group, so each group asks for its own slots and the room mixes
        each fed speaker exactly once. ``None`` means the whole plan, which is what a
        caller reading the room (a test, a read-out) wants.

        ``live`` is the frame these bytes are cut from, for a caller feeding a delayed
        window of it (see ``_prepared``); the verdict is that frame's.

        # An empty frame means nothing arrived; queueing a silent buffer for every
        # speaker would burn the transport's pool for no audio.
        """
        if not left and not right:
            self._frame = self._window = None
            return []
        verdict, plan = self._prepared(left, right, live)
        if not plan:
            return []
        wanted = None if only is None else set(only)
        if self._parity and verdict != MONO and len(plan) == len(PARITY_PLAN):
            # Zero-copy and byte-exact: this is the plain jukebox pair.
            feeds = [(PARITY_PLAN[0][0], left), (PARITY_PLAN[1][0], right)]
            return feeds if wanted is None else [feed for feed in feeds
                                                 if feed[0] in wanted]
        left_samples = right_samples = None
        feeds = []
        for slot, gain_l, gain_r in plan:
            if wanted is not None and slot not in wanted:
                continue
            if gain_l == 1.0 and gain_r == 0.0:
                # Exactly the source channel (``_GAIN_SCALE`` is 1 << 15, so
                # ``sample * 32768 >> 15`` is the sample): handed over as the
                # bytes it already is instead of mixed back to itself.
                feeds.append((slot, left))
                continue
            if gain_l == 0.0 and gain_r == 1.0:
                feeds.append((slot, right))
                continue
            if left_samples is None:
                left_samples, right_samples = self._samples_for(left, right)
            feeds.append((slot, mix_samples(left_samples, right_samples,
                                            gain_l, gain_r)))
        return feeds

    # ----------------------------------------------------------- inspection

    @property
    def slots(self):
        """Every slot any plan can drive, in room order."""
        return self._slots

    @property
    def signature(self):
        """A stable description of the room's shape, for reuse decisions.

        Two renderers built from the same speakers, profile and trims are the same room
        even though they are different objects, so a caller that re-offers a song on a
        map reload keeps the room it already has. A speaker set, position, level, voicing,
        crossover or aim that differs is a *different* room: treating those as equal is
        what made a speaker a builder placed mid-song inaudible until the feature was
        toggled, and what left a bass cabinet playing full range until the next track.

        Every number a builder can change while a song plays belongs here, because a
        field left out of this tuple is a change that never reaches the bank that holds
        the room at all (``plugin.acquire`` hands back the renderer it already has, so
        nothing re-reads the map). A crossover's *sign* makes it one mark or the other,
        and **both** edges are carried, because a speaker that grew a second one became a
        *band* -- read as the map wrote them, not composed into a mark, since this module
        is part of the portable core and may not import the reader that composes one.
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

        The jam-note sync measures the jukebox staging queue so remote instruments land on
        the beat; a per-speaker delay is latency the song gains but that measurement
        cannot see, so it is reported here for the sync to subtract.
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

    ``fill`` is the only difference between a room read off the map and one a builder
    asked for by name: a requested shape may pad itself out with the geometric ring
    for the slots the map does not have. Playback (``acquire_bank``) and the read-outs
    (a cabinet's menu, which must answer *before* anything is acquired) both build
    their room here, so a menu can never describe a room that would play differently.
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
