"""Thread-owned MIDI input service for the playable piano."""

import queue
import threading
import time


class PianoMidiService:
    """Own PortMidi off the game thread and publish compact input events."""

    _RESCAN_SECONDS = 10.0
    _MAX_QUEUED_EVENTS = 2048
    _MAX_READ_BATCHES = 4

    def __init__(self):
        self._events = queue.Queue(maxsize=self._MAX_QUEUED_EVENTS)
        self._active = threading.Event()
        self._stopping = threading.Event()
        self._rescan = threading.Event()
        self._thread = None

    def activate(self):
        self.clear_events()
        self._active.set()
        self._rescan.set()
        if self._thread is None or not self._thread.is_alive():
            self._stopping.clear()
            self._thread = threading.Thread(
                target=self._run,
                name="PianoMidiInput",
                daemon=True,
            )
            self._thread.start()

    def deactivate(self):
        self._active.clear()
        self.clear_events()

    def shutdown(self):
        self._active.clear()
        self._stopping.set()
        self.clear_events()
        # PortMidi initialization can block inside its native driver scan.
        # Never stall gameplay teardown waiting for the daemon worker.
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=0.05)

    def clear_events(self):
        while True:
            try:
                self._events.get_nowait()
            except queue.Empty:
                return

    def drain_events(self, limit=256):
        events = []
        for _ in range(limit):
            try:
                events.append(self._events.get_nowait())
            except queue.Empty:
                break
        return events

    def _emit(self, event):
        try:
            self._events.put_nowait(event)
        except queue.Full:
            # A faulty device must not grow memory without bound or stall audio.
            pass

    @staticmethod
    def _decode_name(raw_name):
        if isinstance(raw_name, bytes):
            return raw_name.decode("utf-8", errors="replace")
        return str(raw_name)

    @staticmethod
    def _close_inputs(inputs):
        for _, midi_input in inputs:
            try:
                midi_input.close()
            except Exception:
                pass
        inputs.clear()

    def _open_inputs(self, midi):
        inputs = []
        names = []
        for device_id in range(midi.get_count()):
            info = midi.get_device_info(device_id)
            if not info or not info[2]:
                continue
            try:
                midi_input = midi.Input(device_id, buffer_size=256)
            except Exception:
                continue
            inputs.append((device_id, midi_input))
            names.append(self._decode_name(info[1]))
        self._emit(("devices", tuple(names)))
        return inputs

    def _restart_and_scan(self, midi, inputs):
        self._close_inputs(inputs)
        try:
            if midi.get_init():
                midi.quit()
            midi.init()
            return self._open_inputs(midi)
        except Exception:
            self._emit(("devices", ()))
            return []

    def _read_inputs(self, inputs):
        failed_ids = set()
        for device_id, midi_input in list(inputs):
            try:
                for _ in range(self._MAX_READ_BATCHES):
                    if not midi_input.poll():
                        break
                    for packet, _timestamp in midi_input.read(64):
                        self._parse_message(device_id, packet)
            except Exception:
                failed_ids.add(device_id)
        if failed_ids:
            for device_id, midi_input in list(inputs):
                if device_id not in failed_ids:
                    continue
                try:
                    midi_input.close()
                except Exception:
                    pass
                inputs.remove((device_id, midi_input))
                self._emit(("device_lost", device_id))

    def _parse_message(self, device_id, packet):
        if not packet or len(packet) < 3:
            return
        status = int(packet[0])
        message_type = status & 0xF0
        channel = status & 0x0F
        data1 = int(packet[1])
        data2 = int(packet[2])

        if message_type == 0x90 and data2 > 0:
            self._emit(("note_on", device_id, channel, data1, data2))
        elif message_type == 0x80 or (message_type == 0x90 and data2 == 0):
            self._emit(("note_off", device_id, channel, data1))
        elif message_type == 0xB0 and data1 == 64:
            self._emit(("sustain", data2 >= 64))
        elif message_type == 0xB0 and data1 == 67:
            self._emit(("soft", data2 >= 64))
        elif message_type == 0xE0:
            # MIDI pitch bend is a 14-bit little-endian value: LSB then MSB.
            # Publish it centered at zero in the standard -8192..8191 range.
            bend = (data1 | (data2 << 7)) - 8192
            self._emit(("pitch_bend", device_id, channel, bend))

    def _run(self):
        inputs = []
        midi = None
        next_scan = 0.0
        try:
            import pygame.midi as midi_module

            midi = midi_module
            while not self._stopping.is_set():
                if not self._active.is_set():
                    self._close_inputs(inputs)
                    self._stopping.wait(0.05)
                    continue

                now = time.monotonic()
                if self._rescan.is_set():
                    self._rescan.clear()
                    next_scan = 0.0
                if not inputs and now >= next_scan:
                    inputs = self._restart_and_scan(midi, inputs)
                    next_scan = time.monotonic() + self._RESCAN_SECONDS
                if inputs:
                    self._read_inputs(inputs)
                    if not inputs:
                        next_scan = time.monotonic()
                self._stopping.wait(0.002)
        finally:
            self._close_inputs(inputs)
            if midi is not None:
                try:
                    if midi.get_init():
                        midi.quit()
                except Exception:
                    pass
