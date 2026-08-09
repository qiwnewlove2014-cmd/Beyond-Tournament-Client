"""Compatibility facade for the former piano-owned MIDI worker.

New gameplay code uses the process-owned ``game.midi_hub``. This class keeps
the old tuple API available for adjacent code during the migration window.
"""

from .midi.worker import MidiInputWorker


class PianoMidiService:
    """Legacy tuple adapter over the generic generation-aware MIDI worker."""

    def __init__(self):
        self._worker = MidiInputWorker()
        self._generation = 0

    def activate(self):
        self._generation += 1
        self._worker.activate(self._generation)

    def deactivate(self):
        self._worker.deactivate()

    def shutdown(self):
        self._worker.shutdown()

    def clear_events(self):
        self._worker.clear_events()

    def drain_events(self, limit=256):
        legacy_events = []
        supported = {
            "devices", "note_on", "note_off", "sustain", "soft",
            "pitch_bend", "device_lost",
        }
        for event in self._worker.drain_events(limit):
            if event.generation != self._generation:
                continue
            legacy = event.to_legacy_tuple()
            if legacy[0] in supported:
                legacy_events.append(legacy)
        return legacy_events

    def _parse_message(self, device_id, packet):
        """Retain the pure parser seam used by legacy tests and diagnostics."""
        self._worker._parse_message(
            self._generation, device_id, packet
        )
