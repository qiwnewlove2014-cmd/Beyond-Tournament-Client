"""Drive the real map-audio lifecycle through many loads, with faults.

The reported bug is not reproducible on demand in a live match, so this harness
reproduces the *lifecycle* instead: a real ``Map``, the real ``Map_parser``,
the real ``Reverb`` zones and effect-slot pool, the real ``Entity.sync_reverb``,
the real ``Camera.apply_listener_state`` and the real
``EventHandeler._begin/_finish_map_audio_reload`` -- all against a fake OpenAL
device that models the two driver behaviours that matter:

* a slot carries whichever effect was attached to it last, and
* ``efx.send`` raises when the source name is no longer valid.

Nothing is played and no device is opened, so the whole run is deterministic
and silent. What is *approximated* (and marked here, not hidden):

* ``Entity.destroy``'s audio half is done by ``release_entity`` -- detach the
  sends, destroy the group, return the two voice-effect slots. Constructing a
  real player entity would drag voice-chat compression into a harness that does
  not test it.
* One-shot sounds are not played, so a group's routing is checked through its
  own send table (``sends[0]``) rather than through per-source sends. The
  per-source path is pinned by ``tests/test_audio_routing_faults.py``.

Usage (from ``client/``):

    python tools/audio_lifecycle_sim.py
    python tools/audio_lifecycle_sim.py --loads 30 --entities 8
    python tools/audio_lifecycle_sim.py --map ../server/maps/audio_stress.map

Exit codes: 0 every invariant held, 1 an unexpected invariant failed,
2 the harness could not start.
"""

import argparse
import contextlib
import io
import os
import sys
import traceback
import xml.etree.ElementTree as ElementTree
from contextlib import nullcontext
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from libs import audio_health  # noqa: E402
from libs.audio_health import AudioRecovery, GameAudioView  # noqa: E402
from libs import camera as camera_module  # noqa: E402
from libs.audio_manager import AudioManager  # noqa: E402
from libs.camera import Camera  # noqa: E402
from libs.event_handeler import EventHandeler  # noqa: E402
from libs.map import Map_parser  # noqa: E402
from libs.objects.entity import Entity  # noqa: E402
from libs.world_map import Map  # noqa: E402

# A zone change speaks its name for a blind player; the harness is a log, not a
# screen reader.
camera_module.speak = lambda *_args, **_kwargs: None


# --------------------------------------------------------------------- driver


class DeadHandle(Exception):
    """Stands in for cyal's InvalidNameError: the source name is gone."""


class FakeEffect:
    def __init__(self, kind):
        self.kind = kind
        self.params = {}

    def set(self, name, value):
        self.params[name] = value


class FakeSlot:
    def __init__(self, index):
        self.index = index
        self.effect = None
        self.gain = 1.0
        self.invalid = False

    def unload(self):
        """``release_effect_slot`` detaches before the slot is reused."""
        self.effect = None


class FakeSource:
    def __init__(self, name):
        self.name = name
        self.position = (0.0, 0.0, 0.0)

    def __repr__(self):
        return f"<source {self.name}>"


class FakeDevice:
    def __init__(self, context):
        self.context = context
        self.name = "Fake OpenAL Soft"
        self.reopens = 0

    def reopen(self, name=None, **_kwargs):
        self.reopens += 1
        self.context.after_reopen()


class FakeContext:
    """The driver state a room can break: routing and slot contents."""

    def __init__(self, *, slots=64):
        self.slots = slots
        self.dead_sources = set()
        self.efx = FakeEfx(self)
        self.device = FakeDevice(self)
        self.is_connected = True
        self.listener = SimpleNamespace(position=(0.0, 0.0, 0.0))
        self._serial = 0

    def batch(self):
        return nullcontext()

    def gen_source(self, name=None, **_kwargs):
        self._serial += 1
        return FakeSource(name or f"src{self._serial}")

    def gen_sources(self, count):
        return [self.gen_source() for _ in range(count)]

    def kill(self, source):
        self.dead_sources.add(source)

    def after_reopen(self):
        """What the measured driver did: nothing. ``hostile`` changes that."""
        if self.efx.hostile_reopen:
            self.efx.lose_routing()
            for slot in self.efx.slots:
                slot.effect = None


class FakeEfx:
    def __init__(self, context):
        self.context = context
        self.slots = []
        self.routing = {}
        self.hostile_reopen = False

    # -- the AL surface AudioManager uses ----------------------------------
    def gen_auxiliary_effect_slot(self):
        if len(self.slots) >= self.context.slots:
            raise MemoryError("auxiliary effect slot limit reached")
        slot = FakeSlot(len(self.slots) + 1)
        self.slots.append(slot)
        return slot

    def gen_effect(self, type, **_kwargs):
        return FakeEffect(type)

    def send(self, source, sendnum, slot, filter=None):
        if source is None:
            raise DeadHandle("no source")
        if source in self.context.dead_sources:
            raise DeadHandle(f"{source!r} was deleted")
        if slot is not None and slot.invalid:
            raise DeadHandle("slot is not a valid effect slot")
        self.routing[(source, sendnum)] = slot

    # -- the harness's own view -------------------------------------------
    def effect_of(self, slot):
        return None if slot is None else slot.effect

    def forget(self, source):
        for key in [key for key in self.routing if key[0] is source]:
            self.routing.pop(key, None)

    def lose_routing(self):
        self.routing.clear()


class SimAudio(AudioManager):
    """The real pool arithmetic; only the device calls are fake."""

    def _armor_filter(self, filter_obj, site, label="filter"):
        return None


def build_audio(slots=64, hostile_reopen=False):
    audio = SimAudio.__new__(SimAudio)
    context = FakeContext(slots=slots)
    context.efx.hostile_reopen = hostile_reopen
    audio.context = context
    audio.efx = context.efx
    audio.sends = [None, None, None, None]
    audio.listener = context.listener
    audio.filter = []
    audio.soundgroups = set()
    audio.unbound_sources = []
    audio._filter_pool = []
    audio._slot_pool = []
    audio._slot_in_use = []
    audio._slot_pool_size = 0
    audio._slot_hold = {}
    audio._effect_leases = {}
    audio._reclaimed_slots = 0
    audio._exhaustion_reported = False
    audio._lease_serial = 0
    audio.muted = False
    audio.max_distance = 59
    audio.position = (0.0, 0.0, 0.0)
    with contextlib.redirect_stdout(io.StringIO()):
        audio._init_slot_pool()
    return audio, context


# ---------------------------------------------------------------------- maps


def _number(text):
    try:
        return int(text)
    except (TypeError, ValueError):
        pass
    try:
        return float(text)
    except (TypeError, ValueError):
        return text


def read_map(path, *, decay_scale=1.0, drop_every=0):
    """A map file as the payload ``Map_parser.load`` consumes.

    ``bounds="minx maxx miny maxy minz maxz"`` is expanded exactly as the
    Server writes it; every other attribute is passed through and the spawn
    signature keeps what it knows (each ``spawn_*`` accepts ``**kwargs``).
    """
    root = ElementTree.parse(path).getroot()
    bounds = [int(value) for value in root.get("bounds").split()]
    elements = []
    rooms = []
    seen_reverb = 0
    for node in root.iter():
        if node.tag in ("map", "head", "body", "public",
                        "hidden_from_matchmaking"):
            continue
        data = {key: _number(value) for key, value in node.attrib.items()}
        raw_bounds = data.pop("bounds", None)
        if raw_bounds is None:
            continue
        data.update(zip(("minx", "maxx", "miny", "maxy", "minz", "maxz"),
                        [int(value) for value in raw_bounds.split()]))
        if node.tag == "zone":
            data["innerText"] = (node.text or "").strip()
        if node.tag == "reverb":
            if decay_scale != 1.0 and "decayTime" in data:
                data["decayTime"] = round(float(data["decayTime"]) * decay_scale, 3)
            seen_reverb += 1
            if drop_every and seen_reverb % drop_every == 0:
                continue
            rooms.append((data.get("id"), (data["minx"], data["maxx"],
                                           data["miny"], data["maxy"])))
        if node.tag == "megaphoneSpeaker" and decay_scale != 1.0:
            data["reverb_decay"] = round(float(data.get("reverb_decay", 2.0))
                                         * decay_scale, 3)
        elements.append({"type": node.tag, "data": data})
    payload = {
        "minx": bounds[0], "maxx": bounds[1],
        "miny": bounds[2], "maxy": bounds[3],
        "minz": bounds[4], "maxz": bounds[5],
        "elements": elements,
    }
    return payload, rooms


def room_center(bounds):
    minx, maxx, miny, maxy = bounds
    return ((minx + maxx) / 2.0, (miny + maxy) / 2.0, 1.0)


# --------------------------------------------------------------------- world


class World:
    """One client's audio, driven through the real lifecycle."""

    def __init__(self, *, slots=64, hostile_reopen=False, verbose=False):
        self.audio, self.context = build_audio(slots=slots,
                                               hostile_reopen=hostile_reopen)
        self.driver = self.context.efx
        self.clock = SimpleNamespace(new_clock=lambda: SimpleNamespace(
            restart=lambda: None, elapsed=0.0))

        self.game = SimpleNamespace(
            audio_mngr=self.audio,
            new_clock=self.clock.new_clock,
            automations=[],
            exclude_water=set(),
        )
        self.map_obj = Map(self.game)
        self.parser = Map_parser(self.game, self.map_obj)
        self.game.gameplay = SimpleNamespace(map=self.map_obj, map_name="")

        self.player = Entity(self.game, self.map_obj, 0.0, 0.0, 0.0, 100,
                             name="listener")
        self.player._player = True
        self.player.vc_source = self.context.gen_source("listener:vc")
        self.player.music_source = self.context.gen_source("listener:music")

        self.camera = Camera.__new__(Camera)
        self.camera.game = self.game
        self.camera.focus_object = self.player
        self.camera.soundgroup = self.player.soundgroup
        self.camera.x = self.camera.y = self.camera.z = 0.0
        self.camera.reverb = None
        self.camera.currentzone = ""

        self.handler = EventHandeler.__new__(EventHandeler)
        self.handler.game = self.game
        self.handler.gameplay = SimpleNamespace(
            map=self.map_obj, map_name="", player=self.player,
            jukebox_player=None, music_bot=None, megaphone=None,
            spectator_mode=False, camera=self.camera,
        )
        self.game.gameplay = self.handler.gameplay

        self.entities = []
        self.rooms = []
        self.loads = 0
        self.verbose = verbose

    # -- lifecycle ---------------------------------------------------------

    def load(self, path, *, decay_scale=1.0, drop_every=0):
        payload, rooms = read_map(path, decay_scale=decay_scale,
                                  drop_every=drop_every)
        self.handler._begin_map_audio_reload()
        self.parser.load(payload, destroy_entities=False)
        self.rooms = rooms
        self.handler.gameplay.map_name = os.path.basename(path)
        self.loads += 1
        self._place_player()
        self.handler._finish_map_audio_reload()
        # Entities are re-spawned and re-synced by the packets that follow a
        # rebuild; a harness has to say that explicitly.
        self.sync_entities()
        return payload

    def _place_player(self):
        if not self.rooms:
            return
        x, y, z = room_center(self.rooms[0][1])
        self.player.x, self.player.y, self.player.z = x, y, z
        self.player.soundgroup.position = (x, y, z)
        self.audio.position = (x, y, z)
        self.camera.x, self.camera.y, self.camera.z = x, y, z
        self.camera.apply_listener_state(x, y, z)

    def walk_to(self, room_index):
        room_id, bounds = self.rooms[room_index % len(self.rooms)]
        x, y, z = room_center(bounds)
        self.player.x, self.player.y, self.player.z = x, y, z
        self.player.soundgroup.position = (x, y, z)
        self.audio.position = (x, y, z)
        self.camera.x, self.camera.y, self.camera.z = x, y, z
        self.camera.apply_listener_state(x, y, z)
        return room_id

    def sync_entities(self):
        for entity in self.entities:
            entity.sync_reverb()

    # -- one-shot sounds (steps, shots, growls) ---------------------------

    def add_oneshot(self, entity, label):
        """A sound playing at a peer, routed the way ``SoundGroup.play`` does.

        Decoding the Ogg is what this harness does not do; the per-source send
        loop, the sends it inherits and the churn of sources starting and
        finishing are the real thing, and they are what a busy fight stresses.
        """
        source = self.context.gen_source(f"{entity.name}:{label}")
        entity.soundgroup.labeled_sources[label] = SimpleNamespace(source=source)
        for index, slot in enumerate(entity.soundgroup.sends):
            try:
                self.audio.efx.send(source, index, slot)
            except Exception as error:
                audio_health.note_send_failure("group", type(error).__name__)
        return source

    def clear_oneshots(self, entity):
        for label, sound in list(entity.soundgroup.labeled_sources.items()):
            if getattr(sound, "source", None) is not None:
                self.driver.forget(sound.source)
            del entity.soundgroup.labeled_sources[label]

    def let_cooldown_expire(self):
        """Stand still for longer than a zone's own retry cooldown (1.5 s).

        ``Reverb.ensure_slot`` backs off after a failed borrow so a step through
        a starved room cannot hammer the pool. Real players spend seconds in a
        room; a harness spends microseconds, so the wait is said explicitly
        instead of slept.
        """
        for zone in self.map_obj.reverb_list:
            zone._retry_at = 0.0

    def spawn_entities(self, count):
        released = self.release_entities()
        for index in range(count):
            if self.rooms:
                room_id, bounds = self.rooms[index % len(self.rooms)]
                x, y, z = room_center(bounds)
            else:
                x, y, z = float(index), 0.0, 1.0
            entity = Entity(self.game, self.map_obj, x, y, z, 100,
                            name=f"peer{index}")
            entity._player = True
            entity.vc_source = self.context.gen_source(f"peer{index}:vc")
            entity.music_source = self.context.gen_source(f"peer{index}:music")
            entity.eq_slot = self.audio.gen_effect(
                "EQUALIZER", ("low_gain", 0.126), ("low_cutoff", 400.0),
                hold=("voice_effects", f"peer{index}:eq", None),
            )
            entity.distortion_slot = self.audio.gen_effect(
                "DISTORTION", ("edge", 0.2),
                hold=("voice_effects", f"peer{index}:radio", None),
            )
            entity.radio_source = self.context.gen_source(f"peer{index}:radio")
            if entity.distortion_slot is not None:
                try:
                    self.audio.efx.send(entity.radio_source, 1,
                                        entity.distortion_slot)
                except Exception as error:
                    audio_health.note_send_failure(
                        "entity-radio", type(error).__name__)
            entity.sync_reverb()
            self.entities.append(entity)
        return released

    def release_entities(self):
        count = len(self.entities)
        for entity in self.entities:
            self.clear_oneshots(entity)
            entity.detach_environment_effects()
            for source in (entity.vc_source, entity.music_source,
                           getattr(entity, "radio_source", None)):
                self.driver.forget(source)
            entity.soundgroup.destroy()
            for slot in (entity.distortion_slot, entity.eq_slot):
                if slot is not None:
                    self.audio.release_effect_slot(slot)
        self.entities = []
        return count

    # -- invariants --------------------------------------------------------

    def check(self):
        """Every way a room can be wrong, as a list of sentences."""
        problems = []
        in_use = self.audio._slot_in_use
        pool = self.audio._slot_pool
        if len(set(map(id, in_use))) != len(in_use):
            problems.append("a slot is in use twice")
        if set(map(id, in_use)) & set(map(id, pool)):
            problems.append("a slot is in use and in the pool at once")
        if len(in_use) + len(pool) != self.audio._slot_pool_size:
            problems.append(
                f"slots lost: {len(in_use)} in use + {len(pool)} free "
                f"!= {self.audio._slot_pool_size}")

        for zone in self.map_obj.reverb_list:
            if zone.reverb is None:
                continue
            if zone.reverb not in in_use:
                problems.append(f"room {zone.id} holds a slot that is not in use")
                continue
            effect = self.driver.effect_of(zone.reverb)
            if effect is None:
                problems.append(f"room {zone.id} holds a slot with no effect")
            elif effect.params.get("decay_time") != float(zone.decay_time):
                problems.append(
                    f"room {zone.id} plays another room's decay "
                    f"({effect.params.get('decay_time')} != {zone.decay_time})")

        # The ears, not the body: while spectating they are two different places,
        # and the group they route through is the followed object's.
        room = self.map_obj.get_reverb_at(self.camera.x, self.camera.y, self.camera.z)
        expected = room.reverb if room is not None else None
        ears = getattr(self.camera.focus_object, "soundgroup", None)
        if ears is not None and ears.sends[0] is not expected:
            problems.append(
                f"listener send 0 is {self.player.soundgroup.sends[0]!r}, "
                f"expected {expected!r}")

        for entity in self.entities:
            room = self.map_obj.get_reverb_at(entity.x, entity.y, entity.z)
            expected = room.reverb if room is not None else None
            for source in (entity.vc_source, entity.music_source):
                actual = self.driver.routing.get((source, 0))
                if actual is not expected:
                    problems.append(
                        f"{entity.name} {source.name} send 0 is {actual!r}, "
                        f"expected {expected!r}")

        for (source, index), slot in self.driver.routing.items():
            if slot is None:
                continue
            if slot not in in_use:
                problems.append(
                    f"{getattr(source, 'name', source)!r} send {index} points at "
                    f"a slot that was returned to the pool")
                continue
            effect = self.driver.effect_of(slot)
            if effect is None:
                problems.append(
                    f"{getattr(source, 'name', source)!r} send {index} plays "
                    f"through a slot with no effect")
        return problems

    def dry_rooms(self):
        return sum(1 for zone in self.map_obj.reverb_list if zone.reverb is None)

    def report(self):
        return self.audio.slot_report()


class LegacyCamera(Camera):
    """The listener policy before the fix, for an A/B on the same run."""

    def _sync_listener_reverb(self, reverb):
        if reverb != self.reverb:
            self.reverb = reverb
            if reverb is None:
                self.focus_object.soundgroup.apply_effect(None, 0)
            else:
                slot = reverb.reverb
                if slot is None and hasattr(reverb, "ensure_slot"):
                    slot = reverb.ensure_slot()
                self.focus_object.soundgroup.apply_effect(slot, 0)


# ----------------------------------------------------------------- scenarios

# The tags the Server's world_map/elements/index.ts knows. A typo here would
# compile into a map the server silently drops an element from.
SERVER_MAP_TAGS = frozenset((
    "platform", "zone", "ambience", "soundSource", "door", "wallbuy",
    "interactable", "window", "perkMachine", "playerSpawn", "zombieSpawn",
    "guardianFort", "reverb", "powerSwitch", "music", "radio",
    "megaphoneSpeaker", "minigameTable", "instrument", "animalPen",
    "jukebox", "cinemaSpeaker", "travel_point", "warp",
))
CINEMA_CHANNELS = frozenset(("front_l", "front_c", "front_r", "side_l",
                             "side_r", "rear_l", "rear_c", "rear_r"))


class Result:
    def __init__(self, name):
        self.name = name
        self.lines = []
        self.problems = []
        self.expected_breaches = []

    def note(self, text):
        self.lines.append(text)

    def check(self, world, label=""):
        problems = world.check()
        if problems:
            self.problems.extend(f"{label}{problem}" for problem in problems)
        return problems

    @property
    def ok(self):
        return not self.problems


def scenario_rotate(path, *, loads=20):
    """Map A/B/C/A ...: a room must sound like itself on every return."""
    result = Result("rotate")
    world = World()
    world.load(path)
    base_slots = world.report()["used"]
    for index in range(loads):
        variant = index % 3
        world.load(path, decay_scale=(1.0, 1.5, 1.0)[variant],
                   drop_every=(0, 0, 2)[variant])
        world.spawn_entities(2)
        for step in range(3):
            world.walk_to(step)
            result.check(world, f"load {index + 1} step {step}: ")
        result.note(f"load {index + 1}: rooms={len(world.map_obj.reverb_list)} "
                    f"used={world.report()['used']}/"
                    f"{world.report()['total']} dry={world.dry_rooms()}")
    world.release_entities()
    final = world.report()
    if final["used"] > base_slots:
        result.problems.append(
            f"slot count drifted: {base_slots} -> {final['used']} after {loads} loads")
    return result, world


def scenario_long(path, *, loads=20):
    """The same map loaded again and again: no leak, no drift."""
    result = Result("long")
    world = World()
    world.load(path)
    baseline = world.report()["used"]
    for _ in range(loads):
        world.load(path)
        world.spawn_entities(2)
        world.walk_to(0)
        result.check(world)
    world.release_entities()
    final = world.report()
    result.note(f"baseline used={baseline}, after {loads} loads used={final['used']}")
    if final["used"] != baseline:
        result.problems.append(
            f"leak: {baseline} -> {final['used']} slots after {loads} identical loads")
    return result, world


def scenario_dead_source(path, *, entities=2):
    """A dead handle mid-loop must not leave the listener dry."""
    result = Result("dead source")
    world = World()
    world.load(path)
    world.spawn_entities(entities)
    # A looping map sound inside the listener's own group whose handle died.
    dead = world.context.gen_source("a map loop that died")
    world.context.kill(dead)
    world.player.soundgroup.labeled_sources["map_loop"] = SimpleNamespace(
        source=dead)

    before = world.walk_to(0)
    failures_before = audio_health.probe.total("send_failure")
    after = world.walk_to(1)
    failures_after = audio_health.probe.total("send_failure")

    result.note(f"room {before} -> {after}; send failures "
                f"{failures_before} -> {failures_after}")
    if failures_after <= failures_before:
        result.problems.append("the dead source was never reported")
    result.check(world, "after the dead handle: ")
    return result, world


def scenario_exhaust(path, *, entities=40):
    """Full lobby first, then a map load: rooms wait for a slot, then recover."""
    result = Result("exhaustion")
    world = World()
    _, rooms = read_map(path)
    world.rooms = rooms
    world.spawn_entities(entities)
    result.note(f"{entities} peers hold {len(world.audio._slot_in_use)}/"
                f"{world.audio._slot_pool_size} slots before the load")
    world.load(path)
    world.sync_entities()
    result.check(world, "after an exhausted load: ")
    dried = world.dry_rooms()
    result.note(f"rooms={len(world.map_obj.reverb_list)} dry={dried} "
                f"used={world.report()['used']}/{world.report()['total']}")

    released = world.release_entities()
    recovered = None
    for step in range(len(world.rooms)):
        world.let_cooldown_expire()
        world.walk_to(step)
        dried_now = world.dry_rooms()
        result.note(f"step {step + 1} after releasing {released} peers: dry={dried_now}")
        if dried_now == 0 and recovered is None:
            recovered = step + 1
    result.check(world, "after peers left: ")
    if recovered is None:
        result.problems.append("the rooms never recovered after slots were freed")
    return result, world


def scenario_recovery(path, *, entities=40):
    """The same exhausted map, healed by the recovery with nobody walking.

    ``scenario_exhaust`` recovers by walking the rooms, which is what a player
    does; this one stands still.  The only thing that asks a dry room for a
    slot is ``AudioRecovery`` -- the real policy, the real ``GameAudioView``,
    the real ``ensure_slot`` and the real lease -- so this is the measurement
    of what the automatic repair actually buys.
    """
    result = Result("recovery")
    world = World()
    _, rooms = read_map(path)
    world.rooms = rooms
    world.spawn_entities(entities)
    world.load(path)
    world.sync_entities()
    dried = world.dry_rooms()
    result.note(f"exhausted load: rooms={len(world.map_obj.reverb_list)} "
                f"dry={dried} (nobody has walked since)")
    world.release_entities()
    result.note(f"peers left: free slots={len(world.audio._slot_pool)}")

    now = [0.0]
    recovery = AudioRecovery(health=None, clock=lambda: now[0],
                             emit=result.note, enabled=True)
    view = GameAudioView(world.game)
    healed_at = None
    for step in range(1, 13):
        now[0] += 1.0
        recovery.tick(view)
        if healed_at is None and world.dry_rooms() == 0:
            healed_at = step
    result.note(f"after {int(now[0])}s of recovery: dry={world.dry_rooms()} "
                f"repairs={recovery.repairs} state={recovery.state}")
    if healed_at is None:
        result.problems.append("the recovery never put the dry rooms back")
    else:
        result.note(f"every room was wet again after {healed_at}s, no walk")
    result.check(world, "after the recovery: ")
    return result, world


def scenario_legacy_exhaust(path, *, entities=40):
    """The same run under the old listener policy, for the A/B line."""
    result = Result("exhaustion (old policy)")
    world = World()
    world.camera = LegacyCamera.__new__(LegacyCamera)
    world.camera.game = world.game
    world.camera.focus_object = world.player
    world.camera.soundgroup = world.player.soundgroup
    world.camera.x = world.camera.y = world.camera.z = 0.0
    world.camera.reverb = None
    world.camera.currentzone = ""
    _, rooms = read_map(path)
    world.rooms = rooms
    world.spawn_entities(entities)
    world.load(path)
    world.sync_entities()
    dried = world.dry_rooms()
    world.release_entities()
    for step in range(len(world.rooms)):
        world.let_cooldown_expire()
        world.walk_to(step)
    result.note(f"rooms={len(world.map_obj.reverb_list)} dry after peers left="
                f"{world.dry_rooms()} (was {dried} on load)")
    return result, world


def scenario_reopen(path, *, hostile=False):
    """Device reopen: the real driver lost nothing; a hostile one loses EFX."""
    result = Result("device reopen" + (" (hostile)" if hostile else ""))
    world = World(hostile_reopen=hostile)
    world.load(path)
    world.spawn_entities(3)
    world.context.device.reopen()
    world.sync_entities()
    world.walk_to(0)
    problems = world.check()
    if hostile:
        result.expected_breaches.extend(problems)
        result.note(f"a hostile reopen leaves {len(problems)} routing breach(es); "
                    "the recovery can re-assert a route but cannot rebuild the "
                    "driver, which is exactly H6")
        world.sync_entities()
        for index in range(len(world.map_obj.reverb_list)):
            world.walk_to(index)
        result.note(f"after a full walk: {len(world.check())} breach(es) remain")
    else:
        result.problems.extend(problems)
        result.note("EFX intact across reopen (matches tools/audio_driver_probe.py)")
    return result, world


def scenario_map_structure(path):
    """The map itself: tags the server knows, ids unique, bounds sane."""
    result = Result("map structure")
    root = ElementTree.parse(path).getroot()
    if root.tag != "map":
        result.problems.append(f"root element is <{root.tag}>, not <map>")
        return result, None
    try:
        edges = [int(value) for value in root.get("bounds").split()]
        if len(edges) != 6:
            raise ValueError
    except (AttributeError, ValueError):
        result.problems.append("<map> needs bounds=\"minx maxx miny maxy minz maxz\"")
        return result, None

    body = root.find("body")
    if body is None:
        result.problems.append("<map> has no <body>")
        return result, None

    seen_ids = set()
    counts = {}
    settings = set()
    for node in body:
        if not isinstance(node.tag, str):
            continue
        counts[node.tag] = counts.get(node.tag, 0) + 1
        if node.tag not in SERVER_MAP_TAGS:
            result.problems.append(f"unknown element <{node.tag}>")
            continue
        raw = node.get("bounds")
        try:
            box = [int(value) for value in (raw or "").split()]
            if len(box) != 6:
                raise ValueError
        except ValueError:
            result.problems.append(f"<{node.tag} id={node.get('id')}> has no 6-number bounds")
            continue
        if any(box[index] > box[index + 1] for index in (0, 2, 4)):
            result.problems.append(f"<{node.tag} id={node.get('id')}> has min > max")
        if (box[0] < edges[0] or box[1] > edges[1] or box[2] < edges[2]
                or box[3] > edges[3] or box[4] < edges[4] or box[5] > edges[5]):
            result.problems.append(f"<{node.tag} id={node.get('id')}> is outside the map bounds")
        element_id = node.get("id")
        if not element_id:
            result.problems.append(f"<{node.tag}> has no id")
        elif element_id in seen_ids:
            result.problems.append(f"duplicate id {element_id}")
        else:
            seen_ids.add(element_id)
        if node.tag == "reverb":
            try:
                decay = float(node.get("decayTime"))
                if decay <= 0:
                    raise ValueError
            except (TypeError, ValueError):
                result.problems.append(f"room {element_id} has no positive decayTime")
            diffusion = node.get("diffusion")
            if diffusion is not None:
                try:
                    if not 0.0 <= float(diffusion) <= 1.0:
                        raise ValueError
                except ValueError:
                    result.problems.append(f"room {element_id} diffusion must be 0..1")
            settings.add((node.get("decayTime"), diffusion))
        elif node.tag == "megaphoneSpeaker":
            for axis in ("x", "y", "z"):
                if node.get(axis) is None:
                    result.problems.append(f"speaker {element_id} has no {axis}")
            try:
                if float(node.get("hearing_range", "0")) <= 0:
                    raise ValueError
            except ValueError:
                result.problems.append(f"speaker {element_id} hearing_range must be > 0")
        elif node.tag == "cinemaSpeaker":
            channel = node.get("channel")
            if channel not in CINEMA_CHANNELS:
                result.problems.append(f"cinema speaker {element_id} has channel={channel!r}")

    if not counts.get("playerSpawn"):
        result.problems.append("the map has no <playerSpawn>")
    result.note(f"tags: " + ", ".join(f"{tag}={count}"
                                        for tag, count in sorted(counts.items())))
    result.note(f"{counts.get('reverb', 0)} rooms forming {len(settings)} "
                f"distinct reverb setting(s)")
    return result, None


def scenario_selfcheck(path):
    """Break things on purpose: the checker has to name both faults."""
    result = Result("self-check of the checker")
    world = World()
    world.load(path)
    world.spawn_entities(2)

    room = next(zone for zone in world.map_obj.reverb_list
                if zone.reverb is not None)
    foreign = FakeEffect("REVERB")
    foreign.set("decay_time", float(room.decay_time) + 5.0)
    room.reverb.effect = foreign
    caught_decay = any("another room's decay" in problem
                       for problem in world.check())

    peer = world.entities[0]
    orphan = world.audio._slot_pool[0]
    world.driver.routing[(peer.vc_source, 0)] = orphan
    caught_orphan = any("returned to the pool" in problem
                        for problem in world.check())

    result.note(f"detected decay swap: {caught_decay}; "
                f"detected orphaned send: {caught_orphan}")
    for label, caught in (("a room playing another room's decay", caught_decay),
                          ("a send pointing at a recycled slot", caught_orphan)):
        if not caught:
            result.problems.append(f"the checker missed {label}")
    return result, world


def scenario_pool_churn(path, *, loads=10):
    """A pool barely big enough: every slot changes owner many times.

    This is the state the reported bug lived in -- a slot that had belonged to
    one room handed to the next map's room. A room that comes back must carry
    its *own* decay, every time, however often the pool recycled the slot.
    """
    result = Result("pool churn")
    world = World(slots=14)
    world.load(path)
    world.spawn_entities(2)
    for index in range(loads):
        variant = index % 3
        world.load(path, decay_scale=(1.0, 1.5, 1.0)[variant],
                   drop_every=(0, 0, 2)[variant])
        world.spawn_entities(2)
        world.let_cooldown_expire()
        for step in range(len(world.rooms)):
            world.walk_to(step)
            result.check(world, f"load {index + 1} room {step}: ")
    result.note(f"{loads} loads through 14 slots, "
                f"used={world.report()['used']}/14 dry={world.dry_rooms()}")
    return result, world


def scenario_spectator(path):
    """A map change while the ears follow somebody else's body."""
    result = Result("spectator map change")
    world = World()
    world.load(path)
    world.spawn_entities(1)
    peer = world.entities[0]
    world.camera.focus_object = peer
    world.camera.spectator_cam_mode = "follow"

    world.load(path, drop_every=2)
    world.camera.refresh_listener(peer.x, peer.y, peer.z)
    room = world.map_obj.get_reverb_at(peer.x, peer.y, peer.z)
    result.note(f"following a peer in {getattr(room, 'id', None) or 'no room'}; "
                f"ears at {world.camera.x:.1f},{world.camera.y:.1f}")
    result.check(world, "after the map change: ")
    ears = world.camera.focus_object.soundgroup
    if room is not None and ears.sends[0] is not room.reverb:
        result.problems.append("the spectator's ears stayed on the old map's room")
    return result, world


def _lerp(start, end, t):
    return tuple(start[index] + (end[index] - start[index]) * t for index in range(3))


def scenario_walk(path, *, peers=24, steps_per_leg=25):
    """A lobby on the move: listener and 24 peers walk room to room.

    This is the shape of a normal match -- people crossing room boundaries,
    several of them at once, with the map reloaded underneath them -- without
    playing a single sample. What it checks is what a walk can break: routing
    per step, a room that goes dry while slots exist, and sends left on the map
    that just went away. One-shot sounds (footsteps, a shot) are not played, so
    their distance fade and occlusion stay with the tests that own them.
    """
    result = Result(f"walk (listener + {peers} peers)")
    world = World()
    world.load(path)
    world.spawn_entities(peers)
    route = [room_center(bounds) for _, bounds in world.rooms]
    route = route + list(reversed(route[:-1]))
    visited = set()
    step = 0

    def place_listener(point):
        x, y, z = point
        world.player.x, world.player.y, world.player.z = x, y, z
        world.player.soundgroup.position = (x, y, z)
        world.audio.position = (x, y, z)
        world.camera.x, world.camera.y, world.camera.z = x, y, z
        world.camera.apply_listener_state(x, y, z)

    def place_peer(entity, point):
        x, y, z = point
        entity.x, entity.y, entity.z = x, y, z
        entity.soundgroup.position = (x, y, z)
        entity.sync_reverb()

    for leg in range(len(route) - 1):
        for tick in range(steps_per_leg):
            t = tick / float(steps_per_leg)
            here = _lerp(route[leg], route[leg + 1], t)
            place_listener(here)
            for index, entity in enumerate(world.entities):
                phase = (t + index / float(max(1, len(world.entities)))) % 1.0
                place_peer(entity, _lerp(route[leg], route[leg + 1], phase))
            if step % 30 == 0:
                # 48 sounds in flight across the lobby (two per peer), replaced
                # as they finish -- the churn a fight produces, without audio.
                for entity in world.entities:
                    world.clear_oneshots(entity)
                    world.add_oneshot(entity, f"step{step}a")
                    world.add_oneshot(entity, f"step{step}b")
            room = world.map_obj.get_reverb_at(here[0], here[1], here[2])
            if room is not None:
                visited.add(room.id)
            result.check(world, f"step {step}: ")
            dry = world.dry_rooms()
            if dry:
                result.problems.append(f"step {step}: {dry} room(s) dry")
            step += 1
            if step % 120 == 0:
                # A map change in the middle of the walk: the ears and every
                # peer stay where they are, exactly as a reload leaves them.
                x, y, z = world.player.x, world.player.y, world.player.z
                world.load(path,
                           decay_scale=1.5 if (step // 120) % 2 else 1.0,
                           drop_every=2 if (step // 120) % 3 == 0 else 0)
                place_listener((x, y, z))
                for entity in world.entities:
                    place_peer(entity, (entity.x, entity.y, entity.z))

    live = sum(len(entity.soundgroup.labeled_sources)
               for entity in world.entities)
    result.note(f"{step} steps, {len(world.entities)} peers, {live} one-shot "
                f"source(s) in flight, rooms visited "
                f"{len(visited)}/{len(world.rooms)}, map rebuilt "
                f"{step // 120} time(s) mid-walk")
    if len(visited) != len(world.rooms):
        result.problems.append(
            f"the walk crossed {len(visited)} of {len(world.rooms)} rooms")
    world.release_entities()
    return result, world


def scenario_control(path, *, entities=4):
    """No fault at all: nothing may be reported and nothing may be repaired."""
    result = Result("control")
    world = World()
    world.load(path)
    world.spawn_entities(entities)
    for index in range(min(6, len(world.rooms))):
        world.walk_to(index)
        world.sync_entities()
        result.check(world, f"walk {index}: ")
    faults = sum(audio_health.probe.total(kind) for kind in
                 ("send_failure", "swallowed", "loop_fault"))
    result.note(f"faults counted: {faults}")
    if faults:
        result.problems.append(f"a healthy run reported {faults} fault(s)")
    return result, world


# --------------------------------------------------------------------- entry


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--map", default=os.path.join(
        os.path.dirname(__file__), "..", "..", "server", "maps", "audio_stress.map"))
    parser.add_argument("--loads", type=int, default=20)
    parser.add_argument("--entities", type=int, default=6)
    parser.add_argument("--hostile-reopen", action="store_true",
                        help="model a driver where reopen loses routing and EFX")
    args = parser.parse_args(argv)

    path = os.path.abspath(args.map)
    if not os.path.exists(path):
        print(f"map not found: {path}")
        return 2

    lines = []
    audio_health.probe.verbose = True
    audio_health.probe.reset()
    audio_health.probe._sink = lines.append

    results = []
    failures = []
    report = []

    def emit(text=""):
        report.append(text)

    # The client's own diagnostics (pool exhaustion, tool chatter) are noise
    # here: they are captured, and quoted only when something failed.
    captured = io.StringIO()
    try:
      with contextlib.redirect_stdout(captured):
        for name, runner in (
            ("map structure", lambda: scenario_map_structure(path)),
            ("self-check of the checker",
             lambda: scenario_selfcheck(path)),
            ("control", lambda: scenario_control(path, entities=args.entities)),
            ("rotate", lambda: scenario_rotate(path, loads=args.loads)),
            ("long", lambda: scenario_long(path, loads=args.loads)),
            ("dead source", lambda: scenario_dead_source(path)),
            ("pool churn", lambda: scenario_pool_churn(
                path, loads=max(3, args.loads // 2))),
            ("spectator map change", lambda: scenario_spectator(path)),
            ("walk", lambda: scenario_walk(path,
                                           peers=max(4, args.entities * 4))),
            ("exhaustion", lambda: scenario_exhaust(path)),
            ("recovery", lambda: scenario_recovery(path)),
            ("exhaustion (old policy)",
             lambda: scenario_legacy_exhaust(path)),
            ("device reopen",
             lambda: scenario_reopen(path, hostile=args.hostile_reopen)),
        ):
            try:
                result, world = runner()
            except Exception:
                traceback.print_exc()
                failures.append(name)
                continue
            results.append(result)
            emit(f"\n### {result.name}")
            for line in result.lines:
                emit(f"  {line}")
            for breach in result.expected_breaches[:4]:
                emit(f"  expected breach: {breach}")
            for problem in result.problems[:8]:
                emit(f"  FAIL: {problem}")
            if len(result.problems) > 8:
                emit(f"  ... and {len(result.problems) - 8} more")
            if not result.ok:
                failures.append(result.name)
            # Damage from a hostile driver is reported, never repaired, so the
            # next scenario starts from a fresh world -- each scenario builds
            # its own.
    except Exception:
        traceback.print_exc()
        return 2

    for line in report:
        print(line)
    if failures:
        quoted = captured.getvalue().strip().splitlines()
        if quoted:
            print("\n--- client output (last lines) ---")
            for line in quoted[-12:]:
                print(f"  {line}")

    print("\n=== audio health lines (the log a tester would send) ===")
    health_lines = [line for line in lines if line.startswith("[AUDIO HEALTH]")]
    for line in health_lines[:12]:
        print(f"  {line}")
    if len(health_lines) > 12:
        print(f"  ... and {len(health_lines) - 12} more")
    print(f"  counters: {audio_health.probe.snapshot_line()}")

    print("\n=== result ===")
    for result in results:
        mark = "PASS" if result.ok else "FAIL"
        print(f"  {mark}  {result.name}")
    if failures:
        print(f"\nFAILED: {', '.join(failures)}")
        return 1
    print("\nAll invariants held.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
