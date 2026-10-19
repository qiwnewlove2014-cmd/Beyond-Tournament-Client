"""Opt-in evidence for the room a listener actually hears.

A reverb that comes back changed, dry, or "different in the map it was in"
used to leave no trace at all: the failure path was an ``except`` that threw
the error away, and the pool's own numbers were only ever spoken on demand.
This module is the smallest thing that makes that speak.

Two things are counted, always, because a dropped error is how the reports
disappeared in the first place:

* a send that failed (``phase`` names where it was attempted, ``error`` the
  exception class -- never a message, a path, PCM or a game object), said once
  per phase/class and then only counted;
* a routing step whose owner swallowed an exception (``note_swallowed``).

The per-map-load line is **opt-in** (``BT_AUDIO_HEALTH=1``): it is written at
every rebuild and is the line a tester's log needs to be read against the map
they were standing on. Fault lines are not gated -- they are rare and they are
the evidence.

The second half is ``AudioRecovery``: the same counts, acted on.  It puts a dry
or dead room back with the game's own ``ensure_slot`` / ``slot.reload`` /
``sync_reverb``, at most three times a minute, and it says out loud when it
stops trying (``BT_AUDIO_RECOVERY=0`` turns it off).  Policy lives here; the
only code that touches a game object is ``GameAudioView`` at the bottom, which
is why every rule of the recovery is tested with a fake clock and a fake view.

Everything here is bounded and main-thread only: a 256-entry ring of short
primitives, no I/O on any path, and the sink is the deferred logger except for
the map-load line, which uses the persistent one (a load is a rare event and
the line must survive the crash it describes).
"""

import os
import threading
import time
from collections import deque

COUNTER_LIMIT = 1_000_000_000
COUNTER_KEY_LIMIT = 64
ENTRY_LIMIT = 256
TEXT_LIMIT = 200

FAULT_KINDS = ("send_failure", "swallowed", "loop_fault")

# How many occurrences of one fault class are worth a line: the first, because
# it is the news, and then powers of ten, because a number nobody sees is not a
# report either. The map-load line carries the exact delta between loads.
_MILESTONES = frozenset((1, 10, 100, 1000, 10000))


def _safe_label(value, limit=48):
    """A map name as a log field: printable ASCII, no quotes, never a guess."""
    if not isinstance(value, str):
        return "?"
    kept = "".join(character for character in value[:limit]
                   if 32 <= ord(character) < 127 and character not in "\"'\\")
    return kept or "?"


class AudioHealth:
    """Counters and a bounded ring; nothing here performs I/O on a frame path."""

    def __init__(self, *, verbose=None, clock=None, sink=None,
                 max_entries=ENTRY_LIMIT):
        if type(max_entries) is not int or not 16 <= max_entries <= 4096:
            raise ValueError("max_entries must be between 16 and 4096")
        self.verbose = (os.environ.get("BT_AUDIO_HEALTH", "0") == "1"
                        if verbose is None else bool(verbose))
        self._clock = clock or time.monotonic
        self._sink = sink if sink is not None else self._default_sink
        self._lock = threading.Lock()
        self._entries = deque(maxlen=max_entries)
        self._counts = {}
        self._loads = 0
        self._seen = {}
        self._origin = None

    # ------------------------------------------------------------------ sink

    @staticmethod
    def _default_sink(line):
        try:
            from .logger import log
            log(line)
            return True
        except Exception:
            return False

    def _emit(self, line):
        try:
            self._sink(line[:2048])
        except Exception:
            pass

    def _record(self, kind, text):
        try:
            now = self._clock()
            with self._lock:
                if self._origin is None:
                    self._origin = now
                millis = int(max(0.0, now - self._origin) * 1000)
                self._entries.append((millis, kind, str(text)[:TEXT_LIMIT]))
        except Exception:
            pass

    # -------------------------------------------------------------- counters

    def _bump(self, kind, key=""):
        try:
            with self._lock:
                entry = (kind, _safe_label(key))
                if len(self._counts) >= COUNTER_KEY_LIMIT and entry not in self._counts:
                    entry = (kind, "other")
                count = min(COUNTER_LIMIT, self._counts.get(entry, 0) + 1)
                self._counts[entry] = count
                return count
        except Exception:
            return 1

    def total(self, kind):
        try:
            with self._lock:
                return sum(count for (name, _key), count
                           in self._counts.items() if name == kind)
        except Exception:
            return 0

    # ---------------------------------------------------------------- events

    def note_send_failure(self, phase, error_name):
        """One aux send that OpenAL refused (a dead handle, a bad slot)."""
        count = self._bump("send_failure", f"{phase}:{error_name}")
        if count in _MILESTONES:
            self._record("send_failure", f"phase={_safe_label(phase)} "
                                         f"error={_safe_label(error_name)} "
                                         f"occurrences={count}")
            self._emit(f"[AUDIO HEALTH] send_failure phase={_safe_label(phase)} "
                       f"error={_safe_label(error_name)} occurrences={count}")

    def note_swallowed(self, where, error_name):
        """A routing step that failed and used to be discarded silently."""
        count = self._bump("swallowed", f"{where}:{error_name}")
        if count in _MILESTONES:
            self._record("swallowed", f"where={_safe_label(where)} "
                                      f"error={_safe_label(error_name)} "
                                      f"occurrences={count}")
            self._emit(f"[AUDIO HEALTH] swallowed where={_safe_label(where)} "
                       f"error={_safe_label(error_name)} occurrences={count}")

    def note_loop_fault(self, error_name):
        """The frame's audio batch raised; the frame now continues without it."""
        count = self._bump("loop_fault", error_name)
        if count in _MILESTONES:
            self._record("loop_fault", f"error={_safe_label(error_name)} "
                                       f"occurrences={count}")
            self._emit(f"[AUDIO HEALTH] loop_fault error={_safe_label(error_name)} "
                       f"occurrences={count}")

    def note_recovery(self, action, detail):
        """A repair the recovery performed -- or the incident it gave up on.

        Always spoken: a repair is news, and is the only line that says a room
        came *back* rather than that something failed.
        """
        self._bump("recovery", action)
        text = f"action={_safe_label(action)} {str(detail)[:TEXT_LIMIT]}"
        self._record("recovery", text)
        self._emit(f"[AUDIO RECOVERY] {_safe_label(action)} {str(detail)[:TEXT_LIMIT]}")

    def note_device_reopen(self, reason, connected, slots_line=None):
        """An output device was reopened (the H6 question: does EFX survive it)."""
        self._bump("reopen", reason)
        detail = (f"reason={_safe_label(reason)} connected={bool(connected)} "
                  f"slots={_safe_label(slots_line or '?', 120)}")
        self._record("device_reopen", detail)
        self._emit(f"[AUDIO HEALTH] device_reopen {detail}")

    # -------------------------------------------------------------- map load

    def map_load(self, *, name=None, rooms=0, dry_rooms=0, slots_used=0,
                 slots_total=0, leases=0):
        """One line per rebuild: what the map asked for, what the pool said.

        Returns the line (or ``""`` when the per-load read-out is off), so a
        caller can hand it to a test without reaching into the sink.
        """
        deltas = {}
        for kind in FAULT_KINDS:
            current = self.total(kind)
            deltas[kind] = max(0, current - self._seen.get(kind, 0))
            self._seen[kind] = current
        with self._lock:
            self._loads += 1
            number = self._loads
        line = (f"[AUDIO HEALTH] load={number} map={_safe_label(name)} "
                f"rooms={int(rooms)} dry={int(dry_rooms)} "
                f"slots={int(slots_used)}/{int(slots_total)} "
                f"leases={int(leases)} "
                f"send_failures=+{deltas['send_failure']} "
                f"swallowed=+{deltas['swallowed']} "
                f"loop_faults=+{deltas['loop_fault']}")
        self._record("map_load", line[len("[AUDIO HEALTH] "):])
        if not self.verbose:
            return ""
        self._emit(line)
        return line

    # ------------------------------------------------------------- read-outs

    def snapshot_line(self):
        """One line a staff read-out can speak, whatever the counters hold."""
        try:
            with self._lock:
                loads = self._loads
                counts = dict(self._counts)
            parts = []
            for kind in FAULT_KINDS:
                for (name, key), count in sorted(counts.items()):
                    if name == kind:
                        parts.append(f"{kind}:{key}={count}")
            tail = " ".join(parts) or "none"
            line = (f"[AUDIO HEALTH] loads={loads} faults {tail}")
            return line[:512]
        except Exception:
            return "[AUDIO HEALTH] unavailable."

    def dump_lines(self, limit=50):
        """The recent history, oldest first -- what a tester sends instead of a repro."""
        try:
            limit = max(0, min(int(limit), ENTRY_LIMIT))
            with self._lock:
                entries = list(self._entries)[-limit:] if limit else []
            return [f"[AUDIO HEALTH] +{millis}ms {kind} {text}"
                    for millis, kind, text in entries]
        except Exception:
            return []

    def reset(self):
        with self._lock:
            self._entries.clear()
            self._counts.clear()
            self._seen.clear()
            self._loads = 0
            self._origin = None


# ---------------------------------------------------------------- recovery

RECOVERY_DRY = "dry"
RECOVERY_DEAD = "dead"
RECOVERY_ROUTE = "route"


class AudioRecovery:
    """What to do about a room that stopped reaching a sound, and when to stop.

    Policy only: it asks a *view* for the three things it may know and tells
    that view to repair.  It never touches OpenAL, a map or a source, so every
    rule below is tested with a fake clock and a fake view.

    Exactly three faults are known, and each says how it was learned:

    * ``dry`` -- the zone holds no slot at all. Read from the map.
    * ``dead`` -- the zone's slot handle no longer answers a real ``slot.gain``
      read. That is the only driver-proven check there is (cyal's
      ``slot.effect`` is a Python shadow and proves bookkeeping, not a driver).
    * ``route`` -- a send failed, because ``AudioHealth`` counted it. A source's
      aux send cannot be read back (cyal keeps its ledger private), so this is
      the one fault known by having raised, never by inspecting it; the repair
      re-asserts the owners' routes and the next count says whether it held.

    A fault must persist for ``confirm_seconds`` before it is repaired (a room
    being re-leased during a rebuild is not a fault).  Every repair is bounded
    per fault: the cooldown doubles per attempt and ``max_attempts`` of them is
    all a fault ever gets on one map, after which ``gave_up`` is terminal until
    the next map load or device reopen calls ``reset_incident``.

    Only the audible repair also has a budget -- ``max_repairs`` of
    ``window`` seconds -- and that is the route re-assert: it re-applies a room
    to sources that are already sounding.  A room's ``ensure_slot`` or a
    slot's ``reload`` cannot be heard (a dry room is dry, and a slot that
    answers again is presumably the room it was), so those are limited per
    room and not per minute: an exhausted map with twelve dry rooms has to be
    able to take twelve freed slots as they come free, which a three-a-minute
    budget starved for four minutes in ``audio_lifecycle_sim``'s exhaustion
    scenario.  That measurement is why the budget is shaped this way.

    A repair is only ever *reported recovered* once the fault has stayed away
    for ``verify_seconds``; until then it is unconfirmed.  Nothing escalates.
    """

    HEALTHY = "healthy"
    SUSPECT = "suspect"
    VERIFYING = "verifying"
    DEGRADED = "degraded"
    GAVE_UP = "gave_up"
    DISABLED = "disabled"

    def __init__(self, *, health=None, clock=None, enabled=None, emit=None,
                 confirm_seconds=2.0, cooldown=5.0, window=60.0,
                 max_repairs=3, verify_seconds=3.0, max_attempts=3):
        self.enabled = (os.environ.get("BT_AUDIO_RECOVERY", "1") != "0"
                        if enabled is None else bool(enabled))
        self._health = health
        self._clock = clock or time.monotonic
        self._emit = emit
        self.confirm_seconds = float(confirm_seconds)
        self.cooldown = float(cooldown)
        self.window = float(window)
        self.max_repairs = int(max_repairs)
        self.verify_seconds = float(verify_seconds)
        self.max_attempts = int(max_attempts)
        self.state = self.HEALTHY
        self.repairs = 0
        self.recovered = 0
        self.gave_up = 0
        self._seen_at = {}
        self._last_attempt = {}
        self._attempts = {}
        self._terminal = set()
        self._repair_times = []
        self._recovering = {}
        self._route_suspect = False
        self._sends_seen = self._send_total()

    # --------------------------------------------------------------- inputs

    def _send_total(self):
        if self._health is None:
            return 0
        try:
            return int(self._health.total("send_failure"))
        except Exception:
            return 0

    def _collect(self, view):
        """Every room that is dry or whose handle no longer answers."""
        found = {}
        try:
            rooms = list(view.rooms())
        except Exception:
            return found
        for room in rooms:
            try:
                label = view.room_label(room)
                slot = view.room_slot(room)
                if slot is None:
                    found[f"{RECOVERY_DRY}:{label}"] = (RECOVERY_DRY, room, label)
                    continue
                live = view.slot_live(slot)
            except Exception:
                continue
            if live is False:
                found[f"{RECOVERY_DEAD}:{label}"] = (RECOVERY_DEAD, room, label)
        return found

    # ---------------------------------------------------------------- policy

    def _rate_limited(self, now):
        """The audible repair's own budget; room repairs are bounded per room."""
        self._repair_times = [when for when in self._repair_times
                              if now - when <= self.window]
        return len(self._repair_times) >= self.max_repairs

    def _note(self, action, detail):
        try:
            if self._emit is not None:
                self._emit(f"[AUDIO RECOVERY] {action} {detail}")
            elif self._health is not None:
                self._health.note_recovery(action, detail)
        except Exception:
            pass

    def tick(self, view, now=None):
        """One pass; call at most once a second, on the owner thread.

        Returns ``{"state", "repaired", "gave_up"}`` -- the same answer the log
        line carries, so a test does not have to read a sink.
        """
        if not self.enabled:
            return {"state": self.DISABLED, "repaired": [], "gave_up": []}
        now = self._clock() if now is None else float(now)
        problems = self._collect(view)

        # The only way to know a route is wrong is that a send raised.
        # The counter only moves at the moment of the failure, so the suspicion
        # has to outlive it: waiting for a *second* failure to confirm the first
        # is how a one-off dead handle would never be repaired at all.
        total = self._send_total()
        if total > self._sends_seen:
            self._sends_seen = total
            self._route_suspect = True
        if self._route_suspect:
            problems[RECOVERY_ROUTE] = (RECOVERY_ROUTE, None, "owners")
        else:
            self._seen_at.pop("route:owners", None)
        for key in list(self._seen_at):
            if key not in problems:
                self._seen_at.pop(key, None)

        # Verification: a fault that stayed away for the window is recovered;
        # one that is back is unconfirmed and may be retried (with backoff).
        for key, (kind, label, at) in list(self._recovering.items()):
            if now - at < self.verify_seconds:
                continue
            if key in problems:
                self._note("unconfirmed", f"{kind} room={label} "
                           f"after={int(now - at)}s")
            else:
                self.recovered += 1
                self._note("recovered", f"{kind} room={label} "
                           f"after={int(now - at)}s")
            self._recovering.pop(key, None)

        repaired = []
        gave_up = []
        for key in sorted(problems):
            kind, room, label = problems[key]
            if key in self._terminal:
                continue
            first = self._seen_at.setdefault(key, now)
            if now - first < self.confirm_seconds:
                continue
            attempts = self._attempts.get(key, 0)
            last = self._last_attempt.get(key)
            wait = self.cooldown * (2 ** max(0, attempts - 1))
            if last is not None and now - last < wait:
                continue
            if key == RECOVERY_ROUTE and self._rate_limited(now):
                self._route_suspect = False
                self._terminal.add(key)
                self.gave_up += 1
                gave_up.append(key)
                self._note("gave_up", f"{kind} room={label} reason=rate "
                           f"attempts={attempts}")
                continue
            if key == RECOVERY_ROUTE:
                self._repair_times.append(now)
            self._last_attempt[key] = now
            self._attempts[key] = attempts + 1
            self.repairs += 1
            try:
                ok = bool(view.repair(kind, room))
            except Exception:
                ok = False
            repaired.append((kind, label, ok))
            if kind == RECOVERY_ROUTE:
                # One re-assert per counted incident; a failure that repeats sets
                # it again, which is also what keeps a broken owner from being
                # re-asserted forever.
                self._route_suspect = False
            self._note("repair", f"{kind} room={label} ok={ok} "
                       f"attempt={attempts + 1}")
            # Only a direct check (a dry or dead room) can be verified later;
            # a route is repaired on a counted failure and has nothing to
            # re-inspect, so claiming it "recovered" would be a guess.
            if ok and key != RECOVERY_ROUTE:
                self._recovering[key] = (kind, label, now)
            if not ok and attempts + 1 >= self.max_attempts:
                self._terminal.add(key)
                self.gave_up += 1
                gave_up.append(key)
                self._note("gave_up", f"{kind} room={label} "
                           f"attempts={attempts + 1}")

        self.state = self._state_for(problems, repaired)
        return {"state": self.state, "repaired": repaired, "gave_up": gave_up}

    def _state_for(self, problems, repaired):
        if repaired:
            return self.VERIFYING
        if not problems:
            return self.VERIFYING if self._recovering else self.HEALTHY
        active = [key for key in problems if key not in self._terminal]
        if not active:
            return self.GAVE_UP
        if len(active) != len(problems):
            return self.DEGRADED
        if any(self._attempts.get(key) for key in active):
            return self.DEGRADED      # a repair was tried and did not hold
        return self.VERIFYING if self._recovering else self.SUSPECT

    def reset_incident(self):
        """A new map (or a new device) is a new baseline -- nothing carries over."""
        self._seen_at.clear()
        self._last_attempt.clear()
        self._attempts.clear()
        self._terminal.clear()
        self._repair_times.clear()
        self._recovering.clear()
        self._route_suspect = False
        self._sends_seen = self._send_total()
        self.state = self.HEALTHY

    def snapshot_line(self):
        """One line for the staff read-out (never raises)."""
        try:
            return (f"[AUDIO RECOVERY] state={self.state} repairs={self.repairs} "
                    f"recovered={self.recovered} gave_up={self.gave_up} "
                    f"enabled={self.enabled}")[:512]
        except Exception:
            return "[AUDIO RECOVERY] unavailable."


class GameAudioView:
    """The recovery's only access to gameplay: what is wrong, and how to fix it.

    Every repair is the game's existing mechanism (``ensure_slot``,
    ``slot.reload``, ``SoundGroup.apply_effect``, ``Entity.sync_reverb``) -- the
    recovery adds policy, never a second routing path.  Owner thread only.
    """

    def __init__(self, game):
        self.game = game

    def _gameplay(self):
        return getattr(self.game, "gameplay", None)

    def _map(self):
        return getattr(self._gameplay(), "map", None)

    # -- checks -------------------------------------------------------------

    def rooms(self):
        return list(getattr(self._map(), "reverb_list", ()) or ())

    def room_label(self, room):
        return _safe_label(getattr(room, "id", "?"), 24)

    def room_slot(self, room):
        return getattr(room, "reverb", None)

    def slot_live(self, slot):
        """Does the slot handle still answer? ``None`` means no device to ask."""
        if slot is None:
            return None
        try:
            _ = slot.gain      # a real alGetAuxiliaryEffectSlotf, not a shadow
        except Exception:
            return False
        return True

    # -- repairs ------------------------------------------------------------

    def repair(self, kind, room):
        if kind == RECOVERY_ROUTE:
            return self.reassert() > 0
        if kind == RECOVERY_DRY:
            return self._ensure(room) is not None
        if kind == RECOVERY_DEAD:
            slot = self.room_slot(room)
            if slot is None:
                return self._ensure(room) is not None
            try:
                slot.reload()      # cyal's own re-attach of the stored effect
            except Exception:
                return False
            return self.slot_live(slot) is not False
        return False

    def _ensure(self, room):
        ensure = getattr(room, "ensure_slot", None)
        if not callable(ensure):
            return None
        try:
            slot = ensure(force=True)
        except TypeError:
            try:
                slot = ensure()
            except Exception:
                return None
        except Exception:
            return None
        if slot is not None:
            # The room is back; the ears standing in it should hear it now, not
            # on their next step.
            self._reassert_listener()
        return slot

    def reassert(self):
        """Re-apply the route every owner should have, once (L1).

        Nothing is inspected: a source's aux send cannot be read back, so this
        is the repair itself rather than a fix confirmed against a mismatch.
        The next counted send failure says whether it held.
        """
        count = self._reassert_listener()
        entities = getattr(self._map(), "entities", None) or {}
        try:
            peers = list(entities.values())
        except Exception:
            return count
        for entity in peers:
            if not getattr(entity, "player", False):
                continue
            sync = getattr(entity, "sync_reverb", None)
            if not callable(sync):
                continue
            try:
                if sync():
                    count += 1
            except Exception:
                continue
        return count

    def _reassert_listener(self):
        camera = getattr(self._gameplay(), "camera", None)
        if camera is None:
            return 0
        try:
            map_ = camera._listener_map()
            reverb = None
            if map_ is not None:
                reverb = map_.get_reverb_at(camera.x, camera.y, camera.z)
            slot = getattr(reverb, "reverb", None) if reverb is not None else None
            if reverb is not None and slot is None:
                ensure = getattr(reverb, "ensure_slot", None)
                if callable(ensure):
                    slot = ensure()
            group = getattr(getattr(camera, "focus_object", None), "soundgroup", None)
            if group is None:
                return 0
            group.apply_effect(slot, 0)
            camera.reverb = reverb if slot is not None else None
            return 1
        except Exception as error:
            try:
                probe.note_swallowed("recovery.reassert_listener",
                                     type(error).__name__)
            except Exception:
                pass
            return 0


probe = AudioHealth()
recovery = AudioRecovery(health=probe)
