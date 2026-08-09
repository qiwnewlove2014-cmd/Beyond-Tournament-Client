"""Instrument-specific MIDI profiles dispatched by the main-thread hub."""


def velocity_to_volume(velocity, minimum=60, maximum=300, exponent=1.35):
    velocity = max(1, min(127, int(velocity)))
    normalized = velocity / 127.0
    return int(minimum + ((maximum - minimum) * (normalized ** exponent)) + 0.5)


class MidiProfile:
    profile_id = "base"
    device_label = "MIDI input"
    device_label_plural = "MIDI inputs"

    def on_activate(self, owner):
        pass

    def on_deactivate(self, owner, reason):
        pass

    def on_event(self, owner, event):
        pass

    def after_poll(self, owner):
        pass


class DeclarativeMidiProfile(MidiProfile):
    """Configure ordinary note instruments without writing another poll loop.

    Handler names are resolved on the active owner only during main-thread
    dispatch, so profiles never retain Gameplay or audio objects.
    """

    def __init__(
        self,
        profile_id,
        note_mapper,
        note_on_handler,
        note_off_handler=None,
        controller_handlers=None,
        pitch_bend_handler=None,
        device_label="MIDI input",
        device_label_plural="MIDI inputs",
    ):
        self.profile_id = profile_id
        self.note_mapper = note_mapper
        self.note_on_handler = note_on_handler
        self.note_off_handler = note_off_handler
        self.controller_handlers = dict(controller_handlers or {})
        self.pitch_bend_handler = pitch_bend_handler
        self.device_label = device_label
        self.device_label_plural = device_label_plural

    @staticmethod
    def _call(owner, handler_name, *args, **kwargs):
        return getattr(owner, handler_name)(*args, **kwargs)

    def on_activate(self, owner):
        handler_names = [self.note_on_handler]
        if self.note_off_handler:
            handler_names.append(self.note_off_handler)
        handler_names.extend(self.controller_handlers.values())
        if self.pitch_bend_handler:
            handler_names.append(self.pitch_bend_handler)
        missing = [
            name for name in handler_names
            if not callable(getattr(owner, name, None))
        ]
        if missing:
            raise AttributeError(
                f"MIDI profile {self.profile_id} is missing handlers: "
                + ", ".join(sorted(set(missing)))
            )

    def on_event(self, owner, event):
        if event.kind in ("note_on", "note_off"):
            action = self.note_mapper(event.data1)
            if action is None:
                return
            if event.kind == "note_on":
                self._call(
                    owner, self.note_on_handler,
                    action, velocity=event.data2,
                )
            elif self.note_off_handler:
                self._call(owner, self.note_off_handler, action)
        elif event.kind == "control_change":
            handler_name = self.controller_handlers.get(event.data1)
            if handler_name:
                self._call(owner, handler_name, event.data2, event)
        elif event.kind == "pitch_bend" and self.pitch_bend_handler:
            self._call(owner, self.pitch_bend_handler, event.data1, event)


class PianoMidiProfile(MidiProfile):
    profile_id = "piano"
    device_label = "MIDI keyboard"
    device_label_plural = "MIDI inputs"
    MIN_NOTE = 24
    MAX_NOTE = 95

    @classmethod
    def note_name(cls, midi_note):
        if isinstance(midi_note, bool) or not isinstance(midi_note, int):
            return None
        if not cls.MIN_NOTE <= midi_note <= cls.MAX_NOTE:
            return None
        names = (
            "C", "Db", "D", "Eb", "E", "F",
            "Gb", "G", "Ab", "A", "Bb", "B",
        )
        return f"{names[midi_note % 12]}{(midi_note // 12) - 1}"

    @staticmethod
    def volume(velocity):
        return velocity_to_volume(velocity)

    def on_activate(self, owner):
        owner._piano_midi_active_notes.clear()
        owner._piano_midi_sustained_notes.clear()
        owner._piano_midi_sustain_sources.clear()
        owner._piano_midi_sustain = False

    def on_deactivate(self, owner, reason):
        owner._stop_all_piano_midi_notes()
        owner._piano_midi_pitch_bend_value = 0
        owner._piano_midi_pitch_bend_source = None
        owner._piano_pitch_bend_pending = None

    def _release_device(self, owner, device_id):
        removed_names = set()
        for state in (
            owner._piano_midi_active_notes,
            owner._piano_midi_sustained_notes,
        ):
            for key, note_name in list(state.items()):
                if key[0] == device_id:
                    removed_names.add(note_name)
                    state.pop(key, None)
        remaining_names = set(owner._piano_midi_active_notes.values())
        remaining_names.update(owner._piano_midi_sustained_notes.values())
        for note_name in removed_names - remaining_names:
            owner._stop_local_piano_note(note_name)

        was_sustaining = bool(owner._piano_midi_sustain_sources)
        owner._piano_midi_sustain_sources = {
            source for source in owner._piano_midi_sustain_sources
            if source[0] != device_id
        }
        owner._piano_midi_sustain = bool(owner._piano_midi_sustain_sources)
        if (
            was_sustaining
            and not owner._piano_midi_sustain
            and not owner._keyboard_sustain_is_down()
        ):
            owner._release_piano_midi_sustain()

        source = owner._piano_midi_pitch_bend_source
        if source is not None and source[0] == device_id:
            owner._set_piano_midi_pitch_bend(0, force_network=True)

    def on_event(self, owner, event):
        if event.kind == "note_on":
            note_name = self.note_name(event.data1)
            if note_name is None:
                return
            key = (event.device_id, event.channel, event.data1)
            owner._piano_midi_sustained_notes.pop(key, None)
            owner._piano_midi_active_notes[key] = note_name
            owner._play_local_piano_note(note_name, velocity=event.data2)
        elif event.kind == "note_off":
            key = (event.device_id, event.channel, event.data1)
            note_name = owner._piano_midi_active_notes.pop(key, None)
            if note_name is None:
                return
            if owner._piano_midi_sustain or owner._keyboard_sustain_is_down():
                owner._piano_midi_sustained_notes[key] = note_name
            elif note_name not in owner._piano_midi_active_notes.values():
                owner._stop_local_piano_note(note_name)
        elif event.kind == "control_change" and event.data1 == 64:
            enabled = bool((event.data2 or 0) >= 64)
            source = (event.device_id, event.channel)
            was_sustaining = bool(owner._piano_midi_sustain_sources)
            if enabled:
                owner._piano_midi_sustain_sources.add(source)
            else:
                owner._piano_midi_sustain_sources.discard(source)
            owner._piano_midi_sustain = bool(
                owner._piano_midi_sustain_sources
            )
            if (
                was_sustaining
                and not enabled
                and not owner._piano_midi_sustain
                and not owner._keyboard_sustain_is_down()
            ):
                owner._release_piano_midi_sustain()
        elif event.kind == "control_change" and event.data1 == 67:
            owner._set_piano_soft_pedal(bool((event.data2 or 0) >= 64))
        elif event.kind == "pitch_bend":
            owner._set_piano_midi_pitch_bend(
                int(event.data1), source=(event.device_id, event.channel)
            )
        elif event.kind == "device_lost":
            self._release_device(owner, event.device_id)

    def after_poll(self, owner):
        owner._flush_piano_pitch_bend_network()


class DrumMidiProfile(MidiProfile):
    profile_id = "drumset"
    device_label = "MIDI drum input"
    device_label_plural = "MIDI drum inputs"
    CHROMATIC_FIRST_NOTE = 60
    CHROMATIC_LAST_NOTE = 76
    GENERAL_MIDI_NOTE_TO_PAD = {
        35: 0, 36: 0,
        37: 2,
        38: 1, 39: 1, 40: 1,
        41: 9, 43: 9,
        42: 3,
        44: 5,
        45: 8,
        46: 4,
        47: 7,
        48: 6, 50: 6,
        49: 10,
        51: 14, 59: 14,
        52: 12,
        53: 15,
        55: 13,
        56: 16,
        57: 11,
    }

    @classmethod
    def note_to_pad(cls, midi_note):
        if isinstance(midi_note, bool) or not isinstance(midi_note, int):
            return None
        pad = cls.GENERAL_MIDI_NOTE_TO_PAD.get(midi_note)
        if pad is not None:
            return pad
        if cls.CHROMATIC_FIRST_NOTE <= midi_note <= cls.CHROMATIC_LAST_NOTE:
            return midi_note - cls.CHROMATIC_FIRST_NOTE
        return None

    @staticmethod
    def volume(velocity):
        return velocity_to_volume(velocity)

    def on_event(self, owner, event):
        if event.kind != "note_on":
            return
        pad = self.note_to_pad(event.data1)
        if pad is not None:
            owner._play_local_drum_hit(pad, velocity=event.data2)


PIANO_MIDI_PROFILE = PianoMidiProfile()
DRUM_MIDI_PROFILE = DrumMidiProfile()
DEFAULT_MIDI_PROFILES = (PIANO_MIDI_PROFILE, DRUM_MIDI_PROFILE)
