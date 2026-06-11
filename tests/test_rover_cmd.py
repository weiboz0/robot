"""No-hardware tests for the rover $-command parser + camera-angle tracking."""
import unittest

import agent_chat


class FakeRover:
    """Mimics RoverCtl's common interface and clamping, recording calls."""

    def __init__(self):
        self.backend = "fake"
        self.pan = 0.0
        self.tilt = 0.0
        self.calls = []

    def set_camera(self, p, t):
        self.pan = max(-180.0, min(180.0, float(p)))
        self.tilt = max(-45.0, min(90.0, float(t)))
        self.calls.append(("cam", self.pan, self.tilt))

    def drive(self, l, r, s):
        self.calls.append(("drive", float(l), float(r), float(s)))

    def stop(self):
        self.calls.append(("stop",))

    def center(self):
        self.set_camera(0, 0)


class RoverCmdTest(unittest.TestCase):
    def test_nudge_default_and_explicit_degrees(self):
        r = FakeRover()
        agent_chat.rover_command(r, "up")
        self.assertEqual(r.tilt, 15.0)
        agent_chat.rover_command(r, "up 45")
        self.assertEqual(r.tilt, 60.0)        # relative, accumulates
        agent_chat.rover_command(r, "left 30")
        self.assertEqual(r.pan, -30.0)

    def test_tilt_clamped(self):
        r = FakeRover()
        agent_chat.rover_command(r, "up 100")
        self.assertEqual(r.tilt, 90.0)

    def test_cam_absolute(self):
        r = FakeRover()
        agent_chat.rover_command(r, "cam 10 20")
        self.assertEqual((r.pan, r.tilt), (10.0, 20.0))

    def test_drive_and_stop(self):
        r = FakeRover()
        agent_chat.rover_command(r, "drive 0.2 0.2 1")
        self.assertIn(("drive", 0.2, 0.2, 1.0), r.calls)
        agent_chat.rover_command(r, "stop")
        self.assertIn(("stop",), r.calls)

    def test_spin_uses_opposite_wheels(self):
        r = FakeRover()
        agent_chat.rover_command(r, "spinr 0.5")
        self.assertIn(("drive", 0.2, -0.2, 0.5), r.calls)

    def test_unknown_command(self):
        r = FakeRover()
        self.assertIn("unknown", agent_chat.rover_command(r, "bogus").lower())


if __name__ == "__main__":
    unittest.main()
