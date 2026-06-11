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

    def move(self, l, r):
        self.calls.append(("move", float(l), float(r)))

    def stop(self):
        self.calls.append(("stop",))

    def estop(self):
        self.calls.append(("estop",))

    def lights(self, front=0, base=0):
        f = int(max(0, min(255, float(front))))
        b = int(max(0, min(255, float(base))))
        self.calls.append(("lights", f, b))
        return f, b

    def set_torque(self, lock):
        self.calls.append(("torque", lock))

    def oled(self, line, text):
        self.calls.append(("oled", int(line), text))

    def oled_default(self):
        self.calls.append(("oled_default",))

    def demo(self):
        self.calls.append(("demo",))

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

    def test_lights_parse_and_clamp(self):
        r = FakeRover()
        out = agent_chat.rover_command(r, "light 300 -5")   # out of range
        self.assertIn(("lights", 255, 0), r.calls)
        self.assertIn("front=255", out)

    def test_move_continuous(self):
        r = FakeRover()
        agent_chat.rover_command(r, "move 0.2 -0.2")
        self.assertIn(("move", 0.2, -0.2), r.calls)

    def test_estop(self):
        r = FakeRover()
        agent_chat.rover_command(r, "estop")
        self.assertIn(("estop",), r.calls)

    def test_relax_and_lock(self):
        r = FakeRover()
        agent_chat.rover_command(r, "relax")
        agent_chat.rover_command(r, "lock")
        self.assertIn(("torque", False), r.calls)
        self.assertIn(("torque", True), r.calls)

    def test_oled_write_and_clear(self):
        r = FakeRover()
        agent_chat.rover_command(r, "oled 1 hello world")   # text keeps spaces
        self.assertIn(("oled", 1, "hello world"), r.calls)
        agent_chat.rover_command(r, "oledclear")
        self.assertIn(("oled_default",), r.calls)

    def test_demo(self):
        r = FakeRover()
        agent_chat.rover_command(r, "demo")
        self.assertIn(("demo",), r.calls)

    def test_unknown_command(self):
        r = FakeRover()
        self.assertIn("unknown", agent_chat.rover_command(r, "bogus").lower())


class RoverCtlClampTest(unittest.TestCase):
    """RoverCtl.drive must clamp speed/duration even on out-of-range input."""

    def test_http_backend_clamps_drive(self):
        import rover_client
        calls = []
        orig = rover_client.drive
        rover_client.drive = lambda l, r, s: calls.append((l, r, s))
        try:
            rc = agent_chat.RoverCtl("http")     # http backend: no serial / no I/O on init
            rc.drive(3, -3, 60)                  # wildly out of range
        finally:
            rover_client.drive = orig
        self.assertEqual(calls, [(0.5, -0.5, 5.0)])


if __name__ == "__main__":
    unittest.main()
