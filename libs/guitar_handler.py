"""
Guitar handler — extracted from Gameplay to keep gameplay.py focused on
core loop orchestration.  Owns guitar state, note-to-sample mapping,
local monitor, and signal scanning.

Usage from Gameplay::

    self.guitar = GuitarHandler(self)
    # in update():
    if self.guitar.active:
        self.guitar.feed_monitor()
    # toggle:
    self.guitar.toggle()
"""

import contextlib

from .speech import speak
from . import options


class GuitarHandler:
    """Manages line-in guitar input, local monitor, and note playback."""

    SHARP_TO_FLAT = {
        "C#": "Db", "D#": "Eb", "F#": "Gb", "G#": "Ab", "A#": "Bb",
    }
    NOTE_ROOTS = set("ABCDEFG")

    def __init__(self, gameplay):
        self._gp = gameplay          # back-reference to Gameplay
        self.active = False
        self.monitor = None          # GuitarLocalMonitor
        self.instrument_input = None  # InstrumentInput instance

    @property
    def _game(self):
        return self._gp.game

    def _is_megaphone_owner(self):
        return self._gp._is_megaphone_owner()

    def _attach_music_timeline(self, packet):
        return self._gp._attach_music_timeline(packet)

    def _send_jam_note(self, event, packet):
        return self._gp._send_jam_note(event, packet)

    # ------------------------------------------------------------------
    # note mapping
    # ------------------------------------------------------------------

    @classmethod
    def _sample_for(cls, note_name):
        """Map a detected note (e.g. "G#3") to a playable sample path.

        The bundled samples use flat spelling (Ab3, not G#3) and cover
        B0..A7, so out-of-range notes simply resolve to a missing file that
        the audio manager skips silently.
        """
        if not isinstance(note_name, str) or len(note_name) < 2:
            return None
        root, octave = note_name[:-1], note_name[-1]
        if root not in cls.NOTE_ROOTS and root not in cls.SHARP_TO_FLAT:
            return None
        if not octave.isdigit():
            return None
        flat = cls.SHARP_TO_FLAT.get(root, root)
        return f"piano/Piano.mf.{flat}{octave}.ogg"

    @staticmethod
    def _volume(velocity):
        """Map MIDI velocity 1-127 to the game's volume scale (60..300)."""
        if velocity is None:
            return 300
        v = max(1, min(127, int(velocity)))
        return max(60, min(300, round(60 + 240 * (v / 127) ** 1.35)))

    # ------------------------------------------------------------------
    # note playback
    # ------------------------------------------------------------------

    def play_local_note(self, note_name, velocity=None):
        """Play the detected line-in note locally at the player's position.

        This is the on-machine (monitor) event for the line-in guitar: it
        lets the performer hear the notes the pitch detector is tracking
        without broadcasting anything to other players.
        """
        sample = self._sample_for(note_name)
        if not sample:
            return
        snd = self._game.audio_mngr.play_unbound(
            sample,
            self._gp.player.x,
            self._gp.player.y,
            self._gp.player.z,
            False,
            volume=self._volume(velocity),
            cat="miscelaneous",
            reference_distance=3.0,
            rolloff=1.0,
            max_distance=25.0,
        )
        if snd and getattr(self._gp, "map", None):
            reverb = self._gp.map.get_reverb_at(
                self._gp.player.x, self._gp.player.y, self._gp.player.z
            )
            if reverb and reverb.reverb:
                s_list = snd if isinstance(snd, (list, tuple)) else [snd]
                for s in s_list:
                    if s and hasattr(s, "source") and s.source:
                        with contextlib.suppress(Exception):
                            self._game.audio_mngr.efx.send(s.source, 0, reverb.reverb)

    # ------------------------------------------------------------------
    # monitor feed
    # ------------------------------------------------------------------

    def feed_monitor(self):
        """Play the raw strum/chord frames back at the player's position so
        the performer hears their own playing (3D monitor)."""
        raw_frames = self.instrument_input.drain_raw_frames()
        if raw_frames and self.monitor:
            self.monitor.set_position(
                self._gp.player.x, self._gp.player.y, self._gp.player.z
            )
            for frame in raw_frames:
                self.monitor.feed(frame)

    def process_notes(self):
        """Legacy placeholder-note path (piano samples), now unused.

        Guitar sound is raw-only since v1.7.0: the local monitor and the 3D
        voice stream carry the real pedal/guitar audio. This method is kept so
        note-based playback can be re-enabled later (e.g. when real guitar
        samples ship); it plays a piano placeholder locally and broadcasts a
        play_guitar_note event like the piano path.
        """
        for note, velocity in self.instrument_input.drain_notes():
            self.play_local_note(note, velocity)
            packet = {"note": note}
            if velocity is not None:
                packet["velocity"] = max(1, min(127, int(velocity)))
            self._attach_music_timeline(packet)
            self._send_jam_note("play_guitar_note", packet)

    # ------------------------------------------------------------------
    # mode toggle
    # ------------------------------------------------------------------

    def toggle(self):
        """Toggle the line-in guitar: capture pitch from the instrument input
        device and broadcast detected notes like the piano."""
        if self.active:
            self.active = False
            if self.instrument_input:
                self.instrument_input.stop_recording()
            if self.monitor:
                self.monitor.close()
                self.monitor = None
            speak("Guitar mode off")
            return
        if self.instrument_input is None:
            from . import instrument_input
            self.instrument_input = instrument_input.InstrumentInput(self._game)

        # If the instrument device is still the default and a USB guitar-like
        # interface is plugged in, select it automatically so the player can
        # just plug in and play.
        from . import instrument_input as _instr
        if options.get("audio_instrument_input_device", "system default") == "system default":
            guitar_devices = _instr.detect_guitar_inputs()
            if guitar_devices:
                device = guitar_devices[0]
                options.set("audio_instrument_input_device", device)
                self.instrument_input.reopen(device)
                speak(f"USB guitar or effects pedal detected: {device[14:]}")
            else:
                # No device name revealed a guitar/pedal (e.g. a generic
                # "USB Audio Device" pedal). Fall back to a real signal scan:
                # briefly open every capture device and keep the one carrying
                # signal while the player strums. Runs on a background thread
                # so the game never freezes during the scan.
                if self.instrument_input.audio_input is not None:
                    self.instrument_input.audio_input.stop()
                    self.instrument_input.audio_input = None
                speak("No guitar or pedal name detected. Scanning for signal, play a note.")
                import threading as _threading
                _threading.Thread(
                    target=self._run_signal_scan, daemon=True
                ).start()
                return

        self._start_recording()

    def _run_signal_scan(self):
        """Probe every capture device off the main thread, then finish the
        toggle on the main thread with the result."""
        from . import instrument_input as _instr
        try:
            found = _instr.scan_for_signal_devices()
        except Exception:
            found = []
        self._game.put(lambda: self._on_signal_scan_done(found))

    def _on_signal_scan_done(self, found):
        """Complete the guitar-mode toggle after a signal scan."""
        from . import instrument_input as _instr
        if found:
            device = _instr.pick_best_signal_device(found)
            options.set("audio_instrument_input_device", device)
            self.instrument_input.reopen(device)
            speak(f"Signal detected on {device[14:]}, from {len(found)} device.")
            self._start_recording()
        else:
            speak("No signal found. Choose the input device manually in Options.")

    def _start_recording(self):
        """Turn on guitar mode with the currently selected instrument device."""
        from . import instrument_input as _instr
        if self.instrument_input.audio_input is None:
            speak("Instrument input device unavailable.")
            return
        self.instrument_input.start_recording()
        self.active = True
        self.monitor = _instr.GuitarLocalMonitor(self._game.audio_mngr)
        speak("Guitar mode on")

    # ------------------------------------------------------------------
    # cleanup
    # ------------------------------------------------------------------

    def cleanup(self):
        """Close the guitar monitor on exit."""
        if self.monitor:
            self.monitor.close()
            self.monitor = None

    # ------------------------------------------------------------------
    # compatibility shims — kept so Gameplay can still delegate directly
    # ------------------------------------------------------------------

    def _guitar_sample_for(cls, note_name):
        return cls._sample_for(note_name)

    def _guitar_volume(velocity):
        return GuitarHandler._volume(velocity)

    def _play_local_guitar_note(self, note_name, velocity=None):
        self.play_local_note(note_name, velocity)

    def _feed_guitar_monitor(self):
        self.feed_monitor()

    def _process_guitar_notes(self):
        self.process_notes()

    def toggle_guitar_mode(self, mod=None):
        self.toggle()

    def _run_signal_scan(self):
        from . import instrument_input as _instr
        try:
            found = _instr.scan_for_signal_devices()
        except Exception:
            found = []
        self._game.put(lambda: self._on_signal_scan_done(found))

    def _on_signal_scan_done(self, found):
        from . import instrument_input as _instr
        if found:
            device = _instr.pick_best_signal_device(found)
            options.set("audio_instrument_input_device", device)
            self.instrument_input.reopen(device)
            speak(f"Signal detected on {device[14:]}, from {len(found)} device.")
            self._start_recording()
        else:
            speak("No signal found. Choose the input device manually in Options.")

    def _start_guitar_recording(self):
        self._start_recording()
