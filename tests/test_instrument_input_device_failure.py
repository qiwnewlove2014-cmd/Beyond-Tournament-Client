"""A dead guitar/pedal capture handle must never crash or go quiet silently.

The same failure the microphone path was fixed for -- an unplugged or disabled
USB interface, whose every later call raises an OpenAL device error -- reached
the guitar input through four unguarded calls: starting, stopping, the capture
worker's two reads (which killed the worker thread outright, so the guitar went
quiet with nothing on screen) and letting the handle go on a close or a device
switch. A capture worker cannot speak for itself, so the failure is handed to
the next main-thread frame, which turns guitar mode off and says why.
"""
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

import cyal.exceptions

from libs import guitar_handler, instrument_input


def dead_device():
    """A device handle OpenAL no longer accepts, on every call it can get."""
    class DeadCapture:
        name = "dead"

        def start(self):
            raise cyal.exceptions.InvalidDeviceError("Invalid OpenAL device")

        def stop(self):
            raise cyal.exceptions.InvalidDeviceError("Invalid OpenAL device")

        @property
        def available_samples(self):
            raise cyal.exceptions.InvalidDeviceError("Invalid OpenAL device")

        def capture_samples(self, buf):
            raise cyal.exceptions.InvalidDeviceError("Invalid OpenAL device")

    return DeadCapture()


def live_device(samples=0):
    class LiveCapture:
        name = "live"
        started = False
        stopped = 0

        def start(self):
            self.started = True

        def stop(self):
            self.stopped += 1

        @property
        def available_samples(self):
            return samples

        def capture_samples(self, buf):
            return None

    return LiveCapture()


def make_input(device, capture_ext=None):
    """An InstrumentInput with no device opened and no worker started."""
    rec = instrument_input.InstrumentInput.__new__(instrument_input.InstrumentInput)
    rec.audio_input = device
    rec.capture_ext = capture_ext or SimpleNamespace(
        default_device=b"system default",
        open_device=mock.Mock(
            side_effect=cyal.exceptions.DeviceNotFoundError("no such device")))
    rec.stereo = False
    rec.recording = False
    rec.device_error = None
    rec.frames = __import__("collections").deque(maxlen=8)
    rec.notes = __import__("collections").deque(maxlen=8)
    rec._guitar_voice = None
    rec.running = True
    rec.game = None
    return rec


class StartStopTests(unittest.TestCase):
    def test_a_handle_that_will_not_start_is_retired_not_raised(self):
        rec = make_input(dead_device())
        with mock.patch.object(instrument_input.logger, "log"), \
                mock.patch.object(instrument_input.speech, "speak"):
            self.assertFalse(rec.start_recording())   # must not raise
        self.assertIsNone(rec.audio_input)
        self.assertFalse(rec.recording)
        self.assertIsNotNone(rec.device_error)

    def test_a_live_handle_starts_and_reports_success(self):
        device = live_device()
        rec = make_input(device)
        self.assertTrue(rec.start_recording())
        self.assertTrue(rec.recording)
        self.assertTrue(device.started)
        self.assertIs(rec.audio_input, device)

    def test_a_handle_that_will_not_stop_does_not_raise_on_the_way_out(self):
        rec = make_input(dead_device())
        rec.recording = True
        with mock.patch.object(instrument_input.logger, "log"):
            rec.stop_recording()                      # must not raise
        self.assertFalse(rec.recording)
        self.assertIsNone(rec.audio_input)

    def test_letting_a_dead_handle_go_is_never_an_error(self):
        rec = make_input(dead_device())
        rec.release_device()                          # must not raise
        self.assertIsNone(rec.audio_input)

    def test_closing_a_dead_handle_is_never_an_error(self):
        rec = make_input(dead_device())
        rec.close()                                   # must not raise
        self.assertIsNone(rec.audio_input)
        self.assertFalse(rec.running)

    def test_the_failure_is_reported_once(self):
        rec = make_input(dead_device())
        with mock.patch.object(instrument_input.logger, "log"):
            rec.start_recording()
        self.assertIsNotNone(rec.take_device_error())
        self.assertIsNone(rec.take_device_error())


class CaptureWorkerTests(unittest.TestCase):
    def test_the_worker_survives_a_device_that_dies_mid_capture(self):
        rec = make_input(dead_device())
        rec.recording = True
        worker = threading.Thread(target=rec.run, daemon=True)
        with mock.patch.object(instrument_input.logger, "log"):
            worker.start()
            deadline = time.monotonic() + 2
            while rec.device_error is None and time.monotonic() < deadline:
                time.sleep(0.005)
            alive = worker.is_alive()
            rec.running = False
            worker.join(2)
        self.assertTrue(alive, "the capture worker must not die on a dead device")
        self.assertFalse(rec.recording)
        self.assertIsNone(rec.audio_input)
        self.assertFalse(worker.is_alive())

    def test_the_worker_keeps_capturing_on_a_live_device(self):
        device = live_device(samples=0)
        rec = make_input(device)
        rec.recording = True
        worker = threading.Thread(target=rec.run, daemon=True)
        worker.start()
        time.sleep(0.05)
        rec.running = False
        worker.join(2)
        self.assertTrue(rec.recording)
        self.assertIs(rec.audio_input, device)
        self.assertIsNone(rec.device_error)


class GuitarModeTests(unittest.TestCase):
    """What the player is told, and that toggling again is enough."""

    def handler(self, device, device_name="system default"):
        handler = guitar_handler.GuitarHandler.__new__(guitar_handler.GuitarHandler)
        handler._gp = SimpleNamespace(player=SimpleNamespace(x=0.0, y=0.0, z=0.0),
                                      game=SimpleNamespace(
                                          audio_mngr=object(),
                                          put=lambda fn: fn()))
        handler.active = False
        handler.monitor = None
        handler.instrument_input = make_input(device)
        self.device_name = device_name
        return handler

    def start(self, handler):
        with mock.patch.object(guitar_handler, "speak") as speak, \
                mock.patch.object(guitar_handler.options, "get",
                                  return_value=self.device_name), \
                mock.patch.object(instrument_input.speech, "speak"), \
                mock.patch.object(instrument_input.logger, "log"):
            handler._start_recording()
        return [str(call) for call in speak.call_args_list]

    def test_a_started_session_says_it_is_on(self):
        handler = self.handler(live_device())
        self.assertTrue(any("Guitar mode on" in line for line in self.start(handler)))
        self.assertTrue(handler.active)
        self.assertIsNotNone(handler.monitor)

    def test_a_dead_handle_is_reopened_rather_than_reported_missing(self):
        """The device is a name in the options; the handle is not the device."""
        handler = self.handler(None)
        opened = live_device()
        with mock.patch.object(handler.instrument_input, "_open",
                               side_effect=lambda device: setattr(
                                   handler.instrument_input, "audio_input", opened)), \
                mock.patch.object(instrument_input.speech, "speak"):
            said = self.start(handler)
        self.assertTrue(handler.active, said)
        self.assertIs(handler.instrument_input.audio_input, opened)

    def test_a_device_that_cannot_be_opened_still_says_unavailable(self):
        handler = self.handler(None)
        said = self.start(handler)
        self.assertFalse(handler.active)
        self.assertIsNone(handler.instrument_input.audio_input)
        self.assertTrue(any("unavailable" in line for line in said), said)

    def test_a_device_that_opens_but_will_not_start_says_so(self):
        handler = self.handler(dead_device())
        said = self.start(handler)
        self.assertFalse(handler.active)
        self.assertTrue(any("stopped working" in line for line in said), said)

    def test_the_next_frame_reports_a_dead_capture_and_switches_off(self):
        handler = self.handler(live_device())
        handler.active = True
        monitor = SimpleNamespace(close=mock.Mock())
        handler.monitor = monitor
        handler.instrument_input.device_error = "Instrument input device stopped working (capture)"
        with mock.patch.object(guitar_handler, "speak") as speak:
            handler.feed_monitor()
        self.assertFalse(handler.active)
        self.assertIsNone(handler.monitor)
        monitor.close.assert_called_once()
        self.assertIn("stopped working", str(speak.call_args_list))

    def test_switching_the_device_never_touches_a_dead_handle(self):
        handler = self.handler(dead_device())
        handler.instrument_input.device_error = None
        with mock.patch.object(instrument_input.logger, "log"):
            handler.instrument_input.release_device()  # must not raise
        self.assertIsNone(handler.instrument_input.audio_input)


if __name__ == "__main__":
    unittest.main()
