"""The sole PortMidi worker used by the process-owned MIDI hub."""

import queue
import threading
import time

from .events import MidiDevice, MidiEvent


class MidiInputWorker:
    """Read PortMidi off-thread and publish bounded, generation-tagged events."""

    RESCAN_SECONDS = 10.0
    MAX_QUEUED_EVENTS = 2048
    MAX_READ_BATCHES = 4

    def __init__(self):
        self._events = queue.Queue(maxsize=self.MAX_QUEUED_EVENTS)
        self._active = threading.Event()
        self._stopping = threading.Event()
        self._rescan = threading.Event()
        self._state_lock = threading.Lock()
        self._generation = 0
        self._thread = None

    def activate(self, generation):
        self.clear_events()
        with self._state_lock:
            self._generation = int(generation)
        self._active.set()
        self._rescan.set()
        if self._thread is None or not self._thread.is_alive():
            self._stopping.clear()
            self._thread = threading.Thread(
                target=self._run,
                name="MidiInputWorker",
                daemon=True,
            )
            self._thread.start()

    def deactivate(self):
        self._active.clear()
        self.clear_events()

    def request_rescan(self):
        if self._active.is_set():
            self._rescan.set()

    def shutdown(self):
        self._active.clear()
        self._stopping.set()
        self.clear_events()
        # A native driver scan may block. Never stall the game thread on exit.
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

    def _current_generation(self):
        with self._state_lock:
            return self._generation

    def _emit(self, event):
        try:
            self._events.put_nowait(event)
        except queue.Full:
            # A faulty device must not grow memory or stall realtime audio.
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

    def _open_inputs(self, midi, generation):
        inputs = []
        devices = []
        for device_id in range(midi.get_count()):
            info = midi.get_device_info(device_id)
            if not info or not info[2]:
                continue
            try:
                midi_input = midi.Input(device_id, buffer_size=256)
            except Exception:
                continue
            inputs.append((device_id, midi_input))
            devices.append(MidiDevice(device_id, self._decode_name(info[1])))
        self._emit(MidiEvent(
            generation=generation,
            kind="devices",
            devices=tuple(devices),
        ))
        return inputs

    def _restart_and_scan(self, midi, inputs, generation):
        self._close_inputs(inputs)
        try:
            if midi.get_init():
                midi.quit()
            midi.init()
            return self._open_inputs(midi, generation)
        except Exception:
            self._emit(MidiEvent(generation=generation, kind="devices"))
            return []

    def _read_inputs(self, inputs, generation):
        failed_ids = set()
        for device_id, midi_input in list(inputs):
            try:
                for _ in range(self.MAX_READ_BATCHES):
                    if not midi_input.poll():
                        break
                    for packet, _timestamp in midi_input.read(64):
                        self._parse_message(generation, device_id, packet)
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
                self._emit(MidiEvent(
                    generation=generation,
                    kind="device_lost",
                    device_id=device_id,
                ))
            # Rebuild the complete device snapshot even when another input
            # remains open, allowing unplugged devices to reconnect cleanly.
            self._rescan.set()

    def _parse_message(self, generation, device_id, packet):
        if not packet or len(packet) < 3:
            return
        status = int(packet[0])
        message_type = status & 0xF0
        channel = status & 0x0F
        data1 = int(packet[1])
        data2 = int(packet[2])

        kind = None
        event_data1 = data1
        event_data2 = data2
        if message_type == 0x90 and data2 > 0:
            kind = "note_on"
        elif message_type == 0x80 or (message_type == 0x90 and data2 == 0):
            kind = "note_off"
            event_data2 = None
        elif message_type == 0xB0:
            kind = "control_change"
        elif message_type == 0xE0:
            kind = "pitch_bend"
            event_data1 = (data1 | (data2 << 7)) - 8192
            event_data2 = None
        if kind is None:
            return
        self._emit(MidiEvent(
            generation=generation,
            kind=kind,
            device_id=device_id,
            channel=channel,
            data1=event_data1,
            data2=event_data2,
        ))

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

                generation = self._current_generation()
                now = time.monotonic()
                if self._rescan.is_set():
                    self._rescan.clear()
                    inputs = self._restart_and_scan(
                        midi, inputs, generation
                    )
                    next_scan = time.monotonic() + self.RESCAN_SECONDS
                elif not inputs and now >= next_scan:
                    inputs = self._restart_and_scan(
                        midi, inputs, generation
                    )
                    next_scan = time.monotonic() + self.RESCAN_SECONDS
                if inputs:
                    self._read_inputs(inputs, generation)
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
