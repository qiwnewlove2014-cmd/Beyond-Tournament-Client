"""The lifecycle simulation must keep passing, and keep being able to fail.

``tools/audio_lifecycle_sim.py`` drives the real map/audio lifecycle through a
fake driver: map loads, room leases, entity sends, exhaustion, spectator ears.
It is the device-free stand-in for the tester session the audio work cannot get,
so a regression in it is a regression in the evidence -- this pins the run.
"""

import os
import subprocess
import sys
import unittest

CLIENT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOL = os.path.join(CLIENT_ROOT, "tools", "audio_lifecycle_sim.py")
EAR_TOOL = os.path.join(CLIENT_ROOT, "tools", "audio_ear_probe.py")
OPENAL_DLL = os.path.join(CLIENT_ROOT, "openal.dll")


class AudioLifecycleSimTest(unittest.TestCase):
    def run_sim(self, *args):
        if not os.path.exists(TOOL):
            self.fail(f"missing simulator: {TOOL}")
        result = subprocess.run(
            [sys.executable, TOOL, *args],
            cwd=CLIENT_ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=300,
        )
        return result

    def test_full_run_holds_every_invariant(self):
        result = self.run_sim("--loads", "2")
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 0, output[-4000:])
        self.assertIn("All invariants held.", output)
        # The map the run drives has to be structurally sound too.
        self.assertIn("PASS  map structure", output)
        self.assertIn("8 distinct reverb setting(s)", output)
        # The checker itself is checked: an injected decay swap and an orphaned
        # send must both be caught, or "PASS" below means nothing.
        self.assertIn("detected decay swap: True; detected orphaned send: True",
                      output)
        for scenario in ("control", "rotate", "long", "dead source",
                         "pool churn", "spectator map change", "walk",
                         "exhaustion", "recovery", "device reopen"):
            self.assertIn(f"PASS  {scenario}", output)
        # The recovery is the automatic half: twelve dry rooms, nobody walking,
        # every one back inside the verification window.
        self.assertIn("every room was wet again after", output)
        self.assertIn("repairs=12", output)

    def test_a_dead_handle_is_reported_and_routing_survives(self):
        result = self.run_sim("--loads", "2")
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 0, output[-4000:])
        self.assertIn("send failures 0 -> 1", output)
        self.assertIn("send_failure:group:DeadHandle=1", output)

    def test_a_hostile_device_reopen_is_reported_not_hidden(self):
        result = self.run_sim("--loads", "2", "--hostile-reopen")
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 0, output[-4000:])
        self.assertIn("expected breach:", output)
        self.assertIn("hostile", output)


@unittest.skipUnless(os.path.exists(OPENAL_DLL), "the client's OpenAL.dll is absent")
class AudioEarProbeTest(unittest.TestCase):
    """The simulated ear must keep measuring every room as its own."""

    def test_every_room_follows_the_maps_own_decay(self):
        result = subprocess.run(
            [sys.executable, EAR_TOOL],
            cwd=CLIENT_ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=300,
        )
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 0, output[-4000:])
        self.assertIn("12 rooms, 8 distinct setting(s), 0 outside the map's own curve",
                      output)
        # The metric itself is checked: a gross wrong-room swap has to be
        # caught, and the resolution it cannot see has to be admitted.
        self.assertIn("self-check:", output)
        self.assertIn("flagged (good)", output)
        self.assertIn("resolution:", output)


if __name__ == "__main__":
    unittest.main()
