"""Process-owned MIDI lease manager and main-thread instrument router."""

from dataclasses import dataclass, field
import threading
import weakref

from .profiles import DEFAULT_MIDI_PROFILES
from .worker import MidiInputWorker


@dataclass(frozen=True, slots=True)
class MidiLease:
    generation: int
    profile_id: str
    owner_id: int


@dataclass(slots=True)
class _ActiveLease:
    lease: MidiLease
    owner_ref: weakref.ReferenceType
    profile: object
    devices: dict[int, str] = field(default_factory=dict)


class MidiHub:
    """Own one native worker and route only current-generation events."""

    def __init__(self, announce=None, profiles=None, worker=None):
        self._main_thread_id = threading.get_ident()
        self._announce = announce or (lambda _message: None)
        self._profiles = {}
        for profile in profiles or DEFAULT_MIDI_PROFILES:
            self.register_profile(profile)
        self._worker = worker or MidiInputWorker()
        self._generation = 0
        self._active = None
        self._shutdown = False

    @property
    def active_profile_id(self):
        return self._active.lease.profile_id if self._active else None

    def _assert_main_thread(self):
        if threading.get_ident() != self._main_thread_id:
            raise RuntimeError("MidiHub must be controlled from the main thread")

    def register_profile(self, profile):
        profile_id = getattr(profile, "profile_id", None)
        if not isinstance(profile_id, str) or not profile_id:
            raise ValueError("MIDI profiles require a non-empty profile_id")
        if profile_id in self._profiles:
            raise ValueError(f"Duplicate MIDI profile: {profile_id}")
        self._profiles[profile_id] = profile

    def acquire(self, owner, profile_id):
        self._assert_main_thread()
        if self._shutdown:
            raise RuntimeError("MidiHub is shut down")
        profile = self._profiles.get(profile_id)
        if profile is None:
            raise KeyError(f"Unknown MIDI profile: {profile_id}")
        self._release_active("switched")
        self._generation += 1
        lease = MidiLease(self._generation, profile_id, id(owner))
        self._active = _ActiveLease(lease, weakref.ref(owner), profile)
        try:
            profile.on_activate(owner)
        except Exception:
            self._active = None
            self._generation += 1
            self._worker.deactivate()
            raise
        self._worker.activate(lease.generation)
        return lease

    def release(self, lease, reason="released"):
        self._assert_main_thread()
        if self._active is None or self._active.lease != lease:
            return False
        self._release_active(reason)
        return True

    def release_owner(self, owner, reason="released"):
        self._assert_main_thread()
        active = self._active
        if active is None or active.owner_ref() is not owner:
            return False
        self._release_active(reason)
        return True

    def _release_active(self, reason):
        active = self._active
        if active is None:
            return
        self._active = None
        self._generation += 1
        self._worker.deactivate()
        owner = active.owner_ref()
        if owner is not None:
            active.profile.on_deactivate(owner, reason)

    def _handle_device_event(self, active, event):
        if event.kind == "devices":
            previous = dict(active.devices)
            active.devices = {
                device.device_id: device.name for device in event.devices
            }
            if active.devices and active.devices != previous:
                names = tuple(active.devices.values())
                if len(names) == 1:
                    self._announce(
                        f"{active.profile.device_label} connected: {names[0]}"
                    )
                else:
                    self._announce(
                        f"{len(names)} {active.profile.device_label_plural} connected"
                    )
            elif previous and not active.devices:
                self._announce(
                    f"{active.profile.device_label} disconnected. Scanning for devices."
                )
        elif event.kind == "device_lost":
            if event.device_id in active.devices:
                active.devices.pop(event.device_id, None)
                self._announce(
                    f"{active.profile.device_label} disconnected. Scanning for devices."
                )

    def poll(self, limit=256):
        self._assert_main_thread()
        events = self._worker.drain_events(limit)
        for event in events:
            active = self._active
            if active is None or event.generation != active.lease.generation:
                continue
            owner = active.owner_ref()
            if owner is None:
                self._release_active("owner_collected")
                break
            self._handle_device_event(active, event)
            active.profile.on_event(owner, event)

        active = self._active
        if active is not None:
            owner = active.owner_ref()
            if owner is None:
                self._release_active("owner_collected")
            else:
                active.profile.after_poll(owner)

    def rescan(self):
        self._assert_main_thread()
        self._worker.request_rescan()

    def shutdown(self):
        self._assert_main_thread()
        if self._shutdown:
            return
        self._release_active("shutdown")
        self._worker.shutdown()
        self._shutdown = True
