"""No-network tests for rovercontrol_client: each call POSTs to the right :8080
URL with the right query params (urlopen mocked to capture the request)."""
import unittest

import rovercontrol_client as rc


class FakeResp:
    def read(self): return b""
    def __enter__(self): return self
    def __exit__(self, *a): return False


class RovercontrolClientTest(unittest.TestCase):
    def setUp(self):
        self._host = rc.ROVER_HOST
        self.calls = []
        self._orig = rc.urllib.request.urlopen

        def fake(req, timeout=None):
            if hasattr(req, "full_url"):          # a POST Request
                self.calls.append((req.get_method(), req.full_url))
            else:                                  # a GET url string
                self.calls.append(("GET", req))
            return FakeResp()

        rc.urllib.request.urlopen = fake

    def tearDown(self):
        rc.urllib.request.urlopen = self._orig
        rc.set_host(self._host)

    def urls(self):
        return [u for _, u in self.calls]

    def last(self):
        return self.calls[-1][1]

    def test_move_normalizes_to_drive(self):
        # chatbot -0.5..0.5 maps ×2 to rovercontrol's normalized -1..1
        rc.move(0.25, -0.5)
        m, url = self.calls[-1]
        self.assertEqual(m, "POST")
        self.assertIn("/drive?", url)
        self.assertIn("l=0.5", url)        # 0.25 → 0.5
        self.assertIn("r=-1.0", url)       # -0.5 → -1.0 (full)

    def test_move_clamps_normalized(self):
        rc.move(0.9, -0.9)                  # ×2 → ±1.8, clamped to ±1.0
        url = self.last()
        self.assertIn("l=1.0", url)
        self.assertIn("r=-1.0", url)

    def test_camera_aim(self):
        rc.set_camera(30, -10)
        self.assertIn("/camera_aim?", self.last())
        self.assertIn("pan=30", self.last())
        self.assertIn("tilt=-10", self.last())

    def test_lights_always_send_on_never_toggle(self):
        rc.lights(128, 0)                          # 128 PWM degrades to on=1
        urls = self.urls()
        self.assertTrue(any("/light_head?on=1" in u for u in urls))
        self.assertTrue(any("/light_base?on=0" in u for u in urls))
        # never the bare toggle form (no on=)
        self.assertFalse(any(u.endswith("/light_head") or u.endswith("/light_base") for u in urls))

    def test_servo_torque_routes(self):
        rc.servo_torque(True)
        self.assertTrue(self.last().endswith("/gimbal_lock"))
        rc.servo_torque(False)
        self.assertTrue(self.last().endswith("/gimbal_relax"))

    def test_stop_and_estop(self):
        rc.stop()
        self.assertTrue(self.last().endswith("/stop"))
        rc.estop()
        self.assertTrue(self.last().endswith("/estop"))

    def test_drive_refreshes_then_stops(self):
        rc.drive(0.2, 0.2, 0.0)                    # seconds=0 → one /drive then /stop
        urls = self.urls()
        self.assertTrue(any("/drive?" in u for u in urls))
        self.assertTrue(urls[-1].endswith("/stop"))

    def test_set_host_changes_target(self):
        rc.set_host("5.6.7.8")
        rc.stop()
        self.assertIn("5.6.7.8:8080", self.last())


if __name__ == "__main__":
    unittest.main()
