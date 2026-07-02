"""Gate tests for the $find/$screwdriver entrypoint (plan 017, review item).
The MOST safety-critical thing to pin: an unset ROVER_FIND_ENABLE or an
unconfigured vision model must make ZERO contact with the rover (never set the
shared speed cap, never nudge)."""
import os
import unittest
from unittest import mock

import agent_chat


class FakeRover:
    backend = "rovercontrol"
    where = "http://1.2.3.4:8080"


class GateTest(unittest.TestCase):
    def setUp(self):
        import rovercontrol_client as rc
        self.calls = []
        self.patchers = []
        for name in ("set_timeout", "healthz", "set_speed", "get_speed", "stop",
                     "estop", "nudge", "set_camera", "snapshot", "get_photo"):
            p = mock.patch.object(rc, name,
                                  side_effect=lambda *a, _n=name, **k: self.calls.append(_n))
            p.start()
            self.patchers.append(p)

    def tearDown(self):
        for p in self.patchers:
            p.stop()

    def test_disabled_flag_makes_no_rover_contact(self):
        with mock.patch.dict(os.environ, {}, clear=True):        # ROVER_FIND_ENABLE unset
            out = agent_chat.autonomous_find(FakeRover(), "screwdriver")
        self.assertIn("DISABLED", out)
        self.assertEqual(self.calls, [])                         # never touched the rover

    def test_vision_unconfigured_makes_no_rover_contact(self):
        with mock.patch.dict(os.environ, {"ROVER_FIND_ENABLE": "1"}, clear=True):
            out = agent_chat.autonomous_find(FakeRover(), "screwdriver")
        self.assertIn("vision not available", out)
        self.assertEqual(self.calls, [])                         # vision-first: no cap change, no motion

    def test_wrong_backend_makes_no_rover_contact(self):
        class SerialRover:
            backend = "serial"
            where = "/dev/ttyAMA0"
        with mock.patch.dict(os.environ, {"ROVER_FIND_ENABLE": "1"}, clear=True):
            out = agent_chat.autonomous_find(SerialRover(), "screwdriver")
        self.assertIn("rovercontrol backend", out)
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
