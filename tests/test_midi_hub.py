import unittest
import threading

from libs.midi.events import MidiDevice, MidiEvent
from libs.midi.hub import MidiHub
from libs.midi.profiles import (
    DRUM_MIDI_PROFILE,
    PIANO_MIDI_PROFILE,
    DeclarativeMidiProfile,
    MidiProfile,
)
from libs.midi.worker import MidiInputWorker
from libs.piano_midi import PianoMidiService


class FakeWorker:
    def __init__(self):
        self.events = []
        self.activations = []
        self.deactivations = 0
        self.shutdowns = 0

    def activate(self, generation):
        self.activations.append(generation)

    def deactivate(self):
        self.deactivations += 1

    def drain_events(self, limit=256):
        events, self.events = self.events[:limit], self.events[limit:]
        return events

    def request_rescan(self):
        pass

    def shutdown(self):
        self.shutdowns += 1


class RecordingProfile(MidiProfile):
    device_label = "Test MIDI"
    device_label_plural = "Test MIDI inputs"

    def __init__(self, profile_id):
        self.profile_id = profile_id

    def on_activate(self, owner):
        owner.calls.append(("activate", self.profile_id))

    def on_deactivate(self, owner, reason):
        owner.calls.append(("deactivate", self.profile_id, reason))

    def on_event(self, owner, event):
        owner.calls.append(("event", self.profile_id, event.kind, event.data1))

    def after_poll(self, owner):
        owner.calls.append(("after", self.profile_id))


class Owner:
    def __init__(self):
        self.calls = []


class MidiHubTests(unittest.TestCase):
    def test_worker_parser_tags_normalized_events_with_generation(self):
        worker = MidiInputWorker()
        worker._parse_message(7, 3, [0x90, 60, 96])
        worker._parse_message(7, 3, [0x90, 60, 0])
        worker._parse_message(7, 3, [0xB0, 64, 127])
        worker._parse_message(7, 3, [0xE0, 0, 64])
        events = worker.drain_events()
        self.assertEqual([event.generation for event in events], [7] * 4)
        self.assertEqual(
            [event.kind for event in events],
            ["note_on", "note_off", "control_change", "pitch_bend"],
        )
        self.assertEqual(events[-1].data1, 0)

    def test_switch_drops_stale_generation_and_cleans_old_profile(self):
        worker = FakeWorker()
        first_profile = RecordingProfile("first")
        second_profile = RecordingProfile("second")
        announcements = []
        hub = MidiHub(
            announce=announcements.append,
            profiles=(first_profile, second_profile),
            worker=worker,
        )
        first = Owner()
        second = Owner()

        first_lease = hub.acquire(first, "first")
        worker.events.append(MidiEvent(
            generation=first_lease.generation,
            kind="note_on",
            data1=60,
            data2=100,
        ))
        hub.poll()
        second_lease = hub.acquire(second, "second")
        worker.events.extend((
            MidiEvent(
                generation=first_lease.generation,
                kind="note_on",
                data1=61,
                data2=100,
            ),
            MidiEvent(
                generation=second_lease.generation,
                kind="devices",
                devices=(MidiDevice(4, "Keyboard"),),
            ),
            MidiEvent(
                generation=second_lease.generation,
                kind="note_on",
                data1=62,
                data2=100,
            ),
        ))
        hub.poll()

        self.assertIn(("deactivate", "first", "switched"), first.calls)
        self.assertNotIn(("event", "second", "note_on", 61), second.calls)
        self.assertIn(("event", "second", "note_on", 62), second.calls)
        self.assertEqual(announcements, ["Test MIDI connected: Keyboard"])

    def test_release_token_cannot_release_a_newer_owner(self):
        worker = FakeWorker()
        profile = RecordingProfile("instrument")
        hub = MidiHub(profiles=(profile,), worker=worker)
        first = Owner()
        second = Owner()
        old_lease = hub.acquire(first, "instrument")
        new_lease = hub.acquire(second, "instrument")
        self.assertFalse(hub.release(old_lease))
        self.assertEqual(hub.active_profile_id, "instrument")
        self.assertTrue(hub.release(new_lease))

    def test_hub_rejects_dispatch_from_a_worker_thread(self):
        hub = MidiHub(
            profiles=(RecordingProfile("instrument"),),
            worker=FakeWorker(),
        )
        errors = []

        def poll_off_thread():
            try:
                hub.poll()
            except Exception as error:
                errors.append(error)

        thread = threading.Thread(target=poll_off_thread)
        thread.start()
        thread.join()
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], RuntimeError)

    def test_declarative_profile_needs_only_mapping_and_handler_names(self):
        profile = DeclarativeMidiProfile(
            profile_id="xylophone",
            note_mapper=lambda note: note - 60 if 60 <= note <= 72 else None,
            note_on_handler="play",
            note_off_handler="stop",
        )

        class InstrumentOwner:
            def __init__(self):
                self.calls = []

            def play(self, action, velocity=None):
                self.calls.append(("play", action, velocity))

            def stop(self, action):
                self.calls.append(("stop", action))

        owner = InstrumentOwner()
        profile.on_event(owner, MidiEvent(
            generation=1, kind="note_on", data1=64, data2=90
        ))
        profile.on_event(owner, MidiEvent(
            generation=1, kind="note_off", data1=64
        ))
        self.assertEqual(owner.calls, [("play", 4, 90), ("stop", 4)])

    def test_invalid_declarative_profile_rolls_back_lease(self):
        worker = FakeWorker()
        profile = DeclarativeMidiProfile(
            profile_id="broken",
            note_mapper=lambda note: note,
            note_on_handler="missing_handler",
        )
        hub = MidiHub(profiles=(profile,), worker=worker)
        with self.assertRaises(AttributeError):
            hub.acquire(Owner(), "broken")
        self.assertIsNone(hub.active_profile_id)

    def test_drum_profile_preserves_gm_and_chromatic_contracts(self):
        self.assertEqual(
            [DRUM_MIDI_PROFILE.note_to_pad(note) for note in range(60, 77)],
            list(range(17)),
        )
        self.assertEqual(DRUM_MIDI_PROFILE.note_to_pad(36), 0)
        self.assertEqual(DRUM_MIDI_PROFILE.note_to_pad(42), 3)
        self.assertEqual(DRUM_MIDI_PROFILE.note_to_pad(57), 11)
        self.assertIsNone(DRUM_MIDI_PROFILE.note_to_pad(True))

    def test_default_drum_profile_dispatches_on_main_thread(self):
        class DrumOwner:
            def __init__(self):
                self.hits = []

            def _play_local_drum_hit(self, pad, velocity=None):
                self.hits.append((pad, velocity))

        worker = FakeWorker()
        hub = MidiHub(profiles=(DRUM_MIDI_PROFILE,), worker=worker)
        owner = DrumOwner()
        lease = hub.acquire(owner, "drumset")
        worker.events.append(MidiEvent(
            generation=lease.generation,
            kind="note_on",
            device_id=1,
            channel=9,
            data1=42,
            data2=73,
        ))
        hub.poll()
        self.assertEqual(owner.hits, [(3, 73)])

    def test_piano_profile_preserves_note_and_velocity_ranges(self):
        self.assertEqual(PIANO_MIDI_PROFILE.note_name(24), "C1")
        self.assertEqual(PIANO_MIDI_PROFILE.note_name(95), "B6")
        self.assertIsNone(PIANO_MIDI_PROFILE.note_name(23))
        volumes = [PIANO_MIDI_PROFILE.volume(value) for value in range(1, 128)]
        self.assertEqual((volumes[0], volumes[-1]), (60, 300))
        self.assertEqual(volumes, sorted(volumes))

    def test_piano_device_loss_releases_its_notes_and_sustain_source(self):
        class PianoOwner:
            def __init__(self):
                self._piano_midi_active_notes = {}
                self._piano_midi_sustained_notes = {}
                self._piano_midi_sustain_sources = set()
                self._piano_midi_sustain = False
                self._piano_midi_pitch_bend_source = None
                self.stopped = []
                self.released_sustain = 0

            def _play_local_piano_note(self, note_name, velocity=None):
                pass

            def _stop_local_piano_note(self, note_name):
                self.stopped.append(note_name)

            def _keyboard_sustain_is_down(self):
                return False

            def _release_piano_midi_sustain(self):
                self.released_sustain += 1

            def _set_piano_midi_pitch_bend(self, value, force_network=False):
                pass

        owner = PianoOwner()
        PIANO_MIDI_PROFILE.on_activate(owner)
        PIANO_MIDI_PROFILE.on_event(owner, MidiEvent(
            generation=1, kind="note_on", device_id=9,
            channel=0, data1=60, data2=100,
        ))
        PIANO_MIDI_PROFILE.on_event(owner, MidiEvent(
            generation=1, kind="control_change", device_id=9,
            channel=0, data1=64, data2=127,
        ))
        PIANO_MIDI_PROFILE.on_event(owner, MidiEvent(
            generation=1, kind="note_off", device_id=9,
            channel=0, data1=60,
        ))
        PIANO_MIDI_PROFILE.on_event(owner, MidiEvent(
            generation=1, kind="device_lost", device_id=9,
        ))
        self.assertEqual(owner.stopped, ["C4"])
        self.assertFalse(owner._piano_midi_sustain)
        self.assertFalse(owner._piano_midi_sustain_sources)

    def test_legacy_piano_service_still_returns_tuple_events(self):
        service = PianoMidiService()
        service._parse_message(2, [0x91, 60, 80])
        self.assertEqual(
            service.drain_events(),
            [("note_on", 2, 1, 60, 80)],
        )

    def test_legacy_piano_service_ignores_unknown_controllers(self):
        service = PianoMidiService()
        service._parse_message(2, [0xB0, 1, 100])
        service._parse_message(2, [0xB0, 64, 127])
        self.assertEqual(service.drain_events(), [("sustain", True)])


if __name__ == "__main__":
    unittest.main()
