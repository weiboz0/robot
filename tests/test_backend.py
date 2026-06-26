"""No-hardware tests for the shared rover_backend: connect() selection, host/
timeout propagation, serial vs HTTP dispatch, and the set_torque/servo_torque
parity. Uses a fake rover_direct + mocked rover_client (no serial, no network)."""
import sys
import types
import unittest
from unittest import mock

import rover_backend


class FakeSerialRover:
    def __init__(self, port=None, **kw):
        self.port = port or "/dev/fake"
        self.calls = []

    def set_camera(self, p, t): self.calls.append(("set_camera", p, t))
    def drive_for(self, l, r, s): self.calls.append(("drive_for", l, r, s))
    def drive(self, l, r): self.calls.append(("drive", l, r))
    def stop(self): self.calls.append(("stop",))
    def estop(self): self.calls.append(("estop",))
    def lights(self, f, b): self.calls.append(("lights", f, b))
    def servo_torque(self, lock): self.calls.append(("torque", lock))
    def oled(self, n, t): self.calls.append(("oled", n, t))
    def oled_default(self): self.calls.append(("oled_default",))
    def close(self): self.calls.append(("close",))


def fake_rover_direct(port="/dev/fake", rover=None):
    m = types.ModuleType("rover_direct")
    m.detect_port = lambda: port
    m.stop_http_service = lambda: False
    m._rover = rover or FakeSerialRover(port)
    m.Rover = lambda port=None, **kw: m._rover
    return m


class ConnectSelectionTest(unittest.TestCase):
    def setUp(self):
        self._saved = sys.modules.get("rover_direct")

    def tearDown(self):
        if self._saved is not None:
            sys.modules["rover_direct"] = self._saved
        else:
            sys.modules.pop("rover_direct", None)

    def test_picks_serial_when_port_exists(self):
        sys.modules["rover_direct"] = fake_rover_direct("/dev/ttyTEST")
        with mock.patch("os.path.exists", return_value=True):
            r = rover_backend.connect()
        self.assertEqual(r.backend, "serial")
        self.assertEqual(r.where, "/dev/ttyTEST")

    def test_falls_back_to_http_with_host_and_timeout(self):
        sys.modules["rover_direct"] = fake_rover_direct("/dev/none")
        rc = rover_backend.rover_client
        hosts, tos = [], []
        with mock.patch("os.path.exists", return_value=False), \
             mock.patch.object(rover_backend, "_reachable", return_value=True), \
             mock.patch.object(rc, "set_host", side_effect=hosts.append), \
             mock.patch.object(rc, "set_timeout", side_effect=tos.append):
            r = rover_backend.connect(host="9.9.9.9", timeout=0.5)
        self.assertEqual(r.backend, "http")
        self.assertIn("9.9.9.9", r.where)          # codex #3: host reflected
        self.assertIn("9.9.9.9", hosts)            # codex #3: set_host called
        self.assertEqual(tos, [0.5])               # codex #4: timeout set

    def test_none_when_no_serial_and_unreachable(self):
        sys.modules["rover_direct"] = fake_rover_direct("/dev/none")
        # mock _rovercontrol_ready too, or this probes the real rover's :8080
        with mock.patch("os.path.exists", return_value=False), \
             mock.patch.object(rover_backend, "_rovercontrol_ready", return_value=False), \
             mock.patch.object(rover_backend, "_reachable", return_value=False):
            self.assertIsNone(rover_backend.connect())


class DispatchTest(unittest.TestCase):
    def test_serial_dispatch_and_both_torque_names(self):
        fake = fake_rover_direct()
        sys.modules["rover_direct"] = fake
        try:
            r = rover_backend.RoverCtl("serial", port="/dev/x")
            r.move(0.9, -0.9)            # clamps to ±0.5
            r.servo_torque(True)         # alias → set_torque (codex #2)
            r.set_torque(False)
        finally:
            sys.modules.pop("rover_direct", None)
        self.assertIn(("drive", 0.5, -0.5), fake._rover.calls)
        torque = [c for c in fake._rover.calls if c[0] == "torque"]
        self.assertEqual(torque, [("torque", True), ("torque", False)])

    def test_http_dispatch_targets_overridden_host(self):
        rc = rover_backend.rover_client
        calls = []
        with mock.patch.object(rc, "set_host", side_effect=lambda h: None), \
             mock.patch.object(rc, "move", side_effect=lambda l, r: calls.append(("move", l, r))), \
             mock.patch.object(rc, "servo_torque", side_effect=lambda lock: calls.append(("torque", lock))):
            r = rover_backend.RoverCtl("http", http_host="1.2.3.4")
            self.assertIn("1.2.3.4", r.where)
            r.move(0.2, 0.2)
            r.servo_torque(True)
            r.set_torque(False)          # alias also reaches HTTP backend
        self.assertEqual(calls, [("move", 0.2, 0.2), ("torque", True), ("torque", False)])


class RovercontrolBackendTest(unittest.TestCase):
    def setUp(self):
        self._saved = sys.modules.get("rover_direct")
        sys.modules["rover_direct"] = fake_rover_direct("/dev/none")  # no serial

    def tearDown(self):
        if self._saved is not None:
            sys.modules["rover_direct"] = self._saved
        else:
            sys.modules.pop("rover_direct", None)

    def test_picks_rovercontrol_when_healthz_serial_up(self):
        rcc = rover_backend.rovercontrol_client
        with mock.patch("os.path.exists", return_value=False), \
             mock.patch.object(rcc, "set_host", lambda h: None), \
             mock.patch.object(rcc, "healthz", return_value={"serial": {"up": True}}):
            r = rover_backend.connect(host="1.2.3.4")
        self.assertEqual(r.backend, "rovercontrol")
        self.assertIn("1.2.3.4:8080", r.where)

    def test_serial_down_falls_through_to_app_py(self):
        rcc = rover_backend.rovercontrol_client
        with mock.patch("os.path.exists", return_value=False), \
             mock.patch.object(rcc, "set_host", lambda h: None), \
             mock.patch.object(rcc, "healthz", return_value={"serial": {"up": False}}), \
             mock.patch.object(rover_backend, "_reachable", return_value=True), \
             mock.patch.object(rover_backend.rover_client, "set_host", lambda h: None):
            r = rover_backend.connect(host="1.2.3.4")
        self.assertEqual(r.backend, "http")        # codex: reachable-but-serial-down → :5000

    def test_dispatch_and_oled_unsupported(self):
        rcc = rover_backend.rovercontrol_client
        calls = []
        with mock.patch.object(rcc, "set_host", lambda h: None), \
             mock.patch.object(rcc, "move", side_effect=lambda l, r: calls.append(("move", l, r))), \
             mock.patch.object(rcc, "servo_torque", side_effect=lambda lock: calls.append(("torque", lock))):
            r = rover_backend.RoverCtl("rovercontrol", http_host="1.1.1.1")
            self.assertIn("1.1.1.1:8080", r.where)
            r.move(0.2, 0.2)
            r.set_torque(True)                     # alias reaches rovercontrol
            with self.assertRaises(NotImplementedError):
                r.oled(0, "hi")                    # no OLED on the Go controller
        self.assertEqual(calls, [("move", 0.2, 0.2), ("torque", True)])


class SpeedCapTest(unittest.TestCase):
    """Plan 014: cap = max wheel magnitude; serial/http SCALE by 2*cap to match
    rovercontrol's multiplier; rovercontrol is NOT scaled locally (no double-cap)."""

    def test_serial_scales_by_two_cap(self):
        fake = fake_rover_direct()
        sys.modules["rover_direct"] = fake
        try:
            r = rover_backend.RoverCtl("serial", port="/dev/x")
            self.assertEqual(r.get_speed(), 0.5)         # default = full
            r.move(0.2, 0.2)                             # cap 0.5 → factor 1.0 → unchanged
            r.set_speed(0.25)                            # factor 0.5
            self.assertEqual(r.get_speed(), 0.25)
            r.move(0.2, 0.2)                             # → 0.1 effective on EVERY backend
            r.drive(0.4, -0.4, 1.0)                      # → 0.2 / -0.2
        finally:
            sys.modules.pop("rover_direct", None)
        self.assertIn(("drive", 0.2, 0.2), fake._rover.calls)        # unscaled at cap 0.5
        self.assertIn(("drive", 0.1, 0.1), fake._rover.calls)        # scaled at cap 0.25
        self.assertIn(("drive_for", 0.2, -0.2, 1.0), fake._rover.calls)

    def test_rovercontrol_not_double_capped(self):
        rcc = rover_backend.rovercontrol_client
        calls = []
        with mock.patch.object(rcc, "set_host", lambda h: None), \
             mock.patch.object(rcc, "move", side_effect=lambda l, r: calls.append((l, r))), \
             mock.patch.object(rcc, "set_speed", side_effect=lambda c: calls.append(("set", c))), \
             mock.patch.object(rcc, "get_speed", return_value=0.25):
            r = rover_backend.RoverCtl("rovercontrol", http_host="1.1.1.1")
            r.set_speed(0.25)                            # goes to the server, not local
            r.move(0.2, 0.2)                             # raw value to client (server scales)
            self.assertEqual(r.get_speed(), 0.25)
        self.assertEqual(calls, [("set", 0.25), (0.2, 0.2)])

    def test_set_speed_clamped(self):
        fake = fake_rover_direct()
        sys.modules["rover_direct"] = fake
        try:
            r = rover_backend.RoverCtl("serial", port="/dev/x")
            self.assertEqual(r.set_speed(99), 0.5)       # clamps to 0.5
            self.assertEqual(r.set_speed(-1), 0.0)
        finally:
            sys.modules.pop("rover_direct", None)


class StatusAndExtrasTest(unittest.TestCase):
    def test_serial_status_shape_and_list_photos(self):
        fake = fake_rover_direct()
        sys.modules["rover_direct"] = fake
        try:
            r = rover_backend.RoverCtl("serial", port="/dev/x")
            st = r.status()
            with mock.patch.object(rover_backend.os, "listdir",
                                   return_value=["rover_b.jpg", "rover_a.jpg", "x.txt"]):
                photos = r.list_photos()
        finally:
            sys.modules.pop("rover_direct", None)
        self.assertEqual(set(st), {"backend", "where", "serial", "camera",
                                   "gamepad", "speed_cap"})
        self.assertEqual(st["backend"], "serial")
        self.assertEqual(st["speed_cap"], 0.5)
        self.assertIsNone(st["camera"]["up"])            # unknown on serial
        self.assertEqual(photos, ["rover_b.jpg", "rover_a.jpg"])   # newest first, .jpg only

    def test_rovercontrol_status_passthrough_and_center(self):
        rcc = rover_backend.rovercontrol_client
        calls = []
        health = {"serial": {"up": True}, "camera": {"up": True},
                  "gamepad": {"up": False, "mapping": "default"}}
        with mock.patch.object(rcc, "set_host", lambda h: None), \
             mock.patch.object(rcc, "healthz", return_value=health), \
             mock.patch.object(rcc, "get_speed", return_value=0.25), \
             mock.patch.object(rcc, "center", side_effect=lambda: calls.append("center")), \
             mock.patch.object(rcc, "set_camera", side_effect=lambda p, t: calls.append(("aim", p, t))):
            r = rover_backend.RoverCtl("rovercontrol", http_host="1.1.1.1")
            st = r.status()
            r.center()                                   # must hit /camera_center, NOT /camera_aim
        self.assertEqual(st["camera"], {"up": True})
        self.assertEqual(st["speed_cap"], 0.25)
        self.assertEqual(st["backend"], "rovercontrol")
        self.assertEqual(calls, ["center"])              # codex blocker: real endpoint


class ImportSmokeTest(unittest.TestCase):
    def test_core_modules_import(self):
        import importlib
        for m in ("rover_backend", "llm_config", "rover_client",
                  "rovercontrol_client", "rover_camera", "agent_chat"):
            importlib.import_module(m)

    def test_llm_config_exposes_lister_names(self):
        import llm_config
        for name in ("PROVIDERS", "load_dotenv", "pick_provider", "resolve_base_url"):
            self.assertTrue(hasattr(llm_config, name), name)


if __name__ == "__main__":
    unittest.main()
