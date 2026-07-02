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

    def test_set_speed_posts_cap(self):
        rc.set_speed(0.25)
        m, url = self.calls[-1]
        self.assertEqual(m, "POST")
        self.assertIn("/speed?", url)
        self.assertIn("cap=0.25", url)

    def test_set_speed_clamps(self):
        rc.set_speed(9)
        self.assertIn("cap=0.5", self.last())

    def test_nudge_posts_move_endpoint(self):
        rc.nudge("forward", 300)
        m, url = self.calls[-1]
        self.assertEqual(m, "POST")
        self.assertIn("/move_forward?", url)
        self.assertIn("ms=300", url)

    def test_nudge_clamps_ms_and_validates_dir(self):
        rc.nudge("left", 99999)
        self.assertIn("ms=5000", self.last())
        with self.assertRaises(ValueError):
            rc.nudge("sideways", 100)

    def test_camera_nudge(self):
        rc.camera_nudge("up", 10)
        self.assertIn("/camera_up?", self.last())
        self.assertIn("deg=10", self.last())


class FakeJSONResp:
    def __init__(self, body): self._b = body.encode()
    def read(self): return self._b
    def __enter__(self): return self
    def __exit__(self, *a): return False


class RovercontrolGetTest(unittest.TestCase):
    """GET endpoints that parse JSON bodies (get_speed, list_photos)."""

    def setUp(self):
        self._orig = rc.urllib.request.urlopen

    def tearDown(self):
        rc.urllib.request.urlopen = self._orig

    def test_get_speed_parses_cap(self):
        rc.urllib.request.urlopen = lambda url, timeout=None: FakeJSONResp('{"ok":true,"cap":0.3}')
        self.assertEqual(rc.get_speed(), 0.3)

    def test_list_photos_parses(self):
        rc.urllib.request.urlopen = lambda url, timeout=None: FakeJSONResp(
            '{"photos":["rover_b.jpg","rover_a.jpg"]}')
        self.assertEqual(rc.list_photos(), ["rover_b.jpg", "rover_a.jpg"])

    def test_snapshot_returns_name(self):
        rc.urllib.request.urlopen = lambda req, timeout=None: FakeJSONResp('{"ok":true,"name":"rover_z.jpg"}')
        self.assertEqual(rc.snapshot(), "rover_z.jpg")

    def test_get_photo_returns_bytes(self):
        rc.urllib.request.urlopen = lambda url, timeout=None: FakeJSONResp("BINARYJPEG")
        self.assertEqual(rc.get_photo("rover_z.jpg"), b"BINARYJPEG")


if __name__ == "__main__":
    unittest.main()
