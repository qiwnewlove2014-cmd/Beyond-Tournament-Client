"""Immutable normalized MIDI events shared by the worker and main-thread hub."""

from dataclasses import dataclass, field
import time


@dataclass(frozen=True, slots=True)
class MidiDevice:
    device_id: int
    name: str


@dataclass(frozen=True, slots=True)
class MidiEvent:
    generation: int
    kind: str
    device_id: int | None = None
    channel: int | None = None
    data1: int | None = None
    data2: int | None = None
    devices: tuple[MidiDevice, ...] = ()
    timestamp: float = field(default_factory=time.monotonic)

    def to_legacy_tuple(self):
        """Preserve the tuple contract used by the former piano-only service."""
        if self.kind == "devices":
            return ("devices", tuple(device.name for device in self.devices))
        if self.kind == "note_on":
            return (
                "note_on", self.device_id, self.channel,
                self.data1, self.data2,
            )
        if self.kind == "note_off":
            return ("note_off", self.device_id, self.channel, self.data1)
        if self.kind == "control_change" and self.data1 == 64:
            return ("sustain", bool((self.data2 or 0) >= 64))
        if self.kind == "control_change" and self.data1 == 67:
            return ("soft", bool((self.data2 or 0) >= 64))
        if self.kind == "pitch_bend":
            return ("pitch_bend", self.device_id, self.channel, self.data1)
        if self.kind == "device_lost":
            return ("device_lost", self.device_id)
        return (self.kind,)
