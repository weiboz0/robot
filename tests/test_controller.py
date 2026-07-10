"""Ported safety/behavior tests for rovercontrold (the Python controller) —
the invariants pinned by the Go test suite, carried over 1:1 where they are
load-bearing: serial encoding + clamps, e-stop latch, nudge server-side
auto-stop + generation tokens, drive watchdog, mapping semantics, hub/splitter,
and the pure joystick math."""
import json
import threading
import time
import unittest

import rovercontrold as rc


class RecLink:
    """Fake serial link recording every line written."""
    def __init__(self):
        self.lines = []
        self._mu = threading.Lock()

    def write(self, data: bytes):
        with self._mu:
            self.lines.append(data.decode().rstrip("\n"))

    def close(self):
        pass

    def last(self):
        with self._mu:
            return self.lines[-1] if self.lines else ""

    def all(self):
        with self._mu:
            return list(self.lines)


def test_rover():
    r = rc.Rover()
    link = RecLink()
    r.set_status(link, "")
    return r, link


class SerialEncodingTest(unittest.TestCase):
    def test_drive_encoding_and_clamp(self):
        r, link = test_rover()
        r.drive(0.2, -0.3)
        self.assertEqual(link.last(), '{"L":0.2,"R":-0.3,"T":1}')
        r.drive(9, -9)
        self.assertEqual(link.last(), '{"L":0.5,"R":-0.5,"T":1}')

    def test_lights_clamp(self):
        r, link = test_rover()
        r.lights(999, -5)
        self.assertEqual(link.last(), '{"IO4":0,"IO5":255,"T":132}')

    def test_aim_clamp(self):
        r, _ = test_rover()
        p, t = r.aim_camera(999, 999)
        self.assertEqual((p, t), (180, 90))

    def test_estop_sequence(self):
        r, link = test_rover()
        r.estop()
        self.assertEqual(link.all()[-2:], ['{"L":0,"R":0,"T":1}', '{"T":0}'])

    def test_init_link_sequence(self):
        link = RecLink()
        rc.init_link(link)
        self.assertEqual(link.all(), ['{"T":143,"cmd":0}', '{"T":131,"cmd":0}',
                                      '{"T":4,"cmd":2}'])

    def test_send_without_link_raises(self):
        r = rc.Rover()
        with self.assertRaises(OSError):
            r.send({"T": 1})


class MovementTest(unittest.TestCase):
    def test_estop_latch_refuses_motion_until_released(self):
        r, link = test_rover()
        m = rc.Movement(r)
        m.do_estop()
        self.assertTrue(m.is_estopped())
        m.set_drive(0.3, 0.3)                       # refused while latched
        self.assertEqual(link.last(), '{"T":0}')    # still the estop tail
        self.assertTrue(m.is_estopped())
        m.set_drive(0, 0)                           # zero releases the latch
        self.assertFalse(m.is_estopped())
        m.set_drive(0.3, 0.3)
        self.assertEqual(link.last(), '{"L":0.3,"R":0.3,"T":1}')

    def test_stop_clears_latch(self):
        r, _ = test_rover()
        m = rc.Movement(r)
        m.do_estop()
        m.stop()
        self.assertFalse(m.is_estopped())

    def test_nudge_auto_stops_server_side(self):
        r, link = test_rover()
        m = rc.Movement(r)
        m.nudge(1, 1, 0.05)
        self.assertEqual(link.last(), '{"L":0.25,"R":0.25,"T":1}')  # cap-scaled
        time.sleep(0.15)
        self.assertEqual(link.last(), '{"L":0,"R":0,"T":1}')        # timer stopped it

    def test_stale_nudge_does_not_stop_newer_command(self):
        r, link = test_rover()
        m = rc.Movement(r)
        m.nudge(1, 1, 0.05)
        m.set_drive(0.4, 0.4)                       # newer command supersedes
        time.sleep(0.15)
        self.assertEqual(link.last(), '{"L":0.4,"R":0.4,"T":1}')   # NOT stopped

    def test_cap_clamped_and_scales_nudge(self):
        r, link = test_rover()
        m = rc.Movement(r)
        m.set_cap(99)
        self.assertEqual(m.get_cap(), rc.SPEED_LIMIT)
        m.set_cap(0.1)
        m.nudge(1, -1, 0.05)
        self.assertEqual(link.last(), '{"L":0.1,"R":-0.1,"T":1}')

    def test_watchdog_stops_stale_continuous_drive(self):
        r, link = test_rover()
        m = rc.Movement(r)
        m.set_drive(0.2, 0.2)
        self.assertFalse(m.watchdog_tick(time.monotonic()))         # fresh
        self.assertTrue(m.watchdog_tick(time.monotonic() + 1.0))    # stale → stop
        self.assertEqual(link.last(), '{"L":0,"R":0,"T":1}')

    def test_watchdog_not_tripped_by_steady_refresh(self):
        r, _ = test_rover()
        m = rc.Movement(r)
        m.set_drive(0.2, 0.2)
        now = time.monotonic()
        for i in range(5):
            m.set_drive(0.2, 0.2)                                   # refresh
            self.assertFalse(m.watchdog_tick(now + i * 0.1))

    def test_nudge_not_watchdog_managed(self):
        # a long nudge relies on its own timer, not the 0.5s watchdog
        r, link = test_rover()
        m = rc.Movement(r)
        m.nudge(1, 1, 10)
        self.assertFalse(m.watchdog_tick(time.monotonic() + 5.0))
        self.assertNotEqual(link.last(), '{"L":0,"R":0,"T":1}')


class HubSplitterTest(unittest.TestCase):
    def test_split_frames_soi_to_soi(self):
        import io
        data = b"\xff\xd8AAA\xff\xd8BBB\xff\xd8"
        frames = []
        rc.split_frames(io.BytesIO(data), frames.append, chunk=4)
        self.assertEqual(frames, [b"\xff\xd8AAA", b"\xff\xd8BBB"])

    def test_hub_latest_wins(self):
        h = rc.Hub()
        get, cancel = h.subscribe()
        h.publish(b"one")
        h.publish(b"two")                          # slow client: one is dropped
        self.assertEqual(get(0.1), b"two")
        self.assertIsNone(get(0.05))
        cancel()
        self.assertEqual(h.latest_frame(), b"two")

    def test_subscribe_preloads_latest(self):
        h = rc.Hub()
        h.publish(b"x")
        get, cancel = h.subscribe()
        self.assertEqual(get(0.1), b"x")
        cancel()


class FakeState:
    def __init__(self, axes=None, buttons=None):
        self._axes = axes or {}
        self._buttons = buttons or {}

    def axis(self, i):
        return self._axes.get(i, 0.0)

    def button(self, i):
        return self._buttons.get(i, False)


class MappingTest(unittest.TestCase):
    def test_default_mapping_pinned(self):
        m = rc.default_mapping()
        self.assertEqual(m["stop"], {"kind": "button", "index": 0})
        self.assertEqual(m["estop"], {"kind": "button", "index": 6})
        self.assertEqual(m["relax"], {"kind": "button", "index": 9})
        self.assertEqual(m["throttle"], {"index": 1, "invert": True})
        self.assertEqual(m["precision"]["kind"], "none")

    def test_legacy_int_and_null_semantics(self):
        import tempfile, os
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "gamepad.json")
            with open(p, "w") as f:
                json.dump({"stop": 7, "relax": None,
                           "lock": {"kind": "axis", "axis": {"index": 5, "invert": True}}}, f)
            m, src = rc.load_mapping(p)
            self.assertEqual(src, "config")
            self.assertEqual(m["stop"], {"kind": "button", "index": 7})
            self.assertEqual(m["relax"], rc.default_mapping()["relax"])  # null keeps default
            self.assertEqual(m["lock"]["kind"], "axis")

    def test_missing_file_default(self):
        m, src = rc.load_mapping("/nonexistent/gamepad.json")
        self.assertEqual(src, "default")
        self.assertEqual(m, rc.default_mapping())

    def test_invalid_raises(self):
        import tempfile, os
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "gamepad.json")
            open(p, "w").write("not json")
            with self.assertRaises(json.JSONDecodeError):
                rc.load_mapping(p)
            with open(p, "w") as f:
                json.dump({"stop": {"kind": "bogus"}}, f)
            with self.assertRaises(ValueError):
                rc.load_mapping(p)

    def test_control_held_axis_trigger(self):
        c = {"kind": "axis", "axis": {"index": 2, "invert": False}}
        self.assertTrue(rc.control_held(c, FakeState(axes={2: 0.9})))
        self.assertFalse(rc.control_held(c, FakeState(axes={2: 0.2})))
        self.assertFalse(rc.control_held({"kind": "none"}, FakeState(buttons={0: True})))


class JoystickMathTest(unittest.TestCase):
    def test_edges_fire_once(self):
        m = rc.default_mapping()
        prev = rc.GpPrev()
        a1 = rc.compute_joystick(m, FakeState(buttons={0: True}), prev)
        self.assertTrue(a1["stop"])
        a2 = rc.compute_joystick(m, FakeState(buttons={0: True}), prev)
        self.assertFalse(a2["stop"])                # held ≠ re-fire

    def test_estop_via_panic(self):
        m = rc.default_mapping()
        m["panic_stop"] = {"kind": "button", "index": 8}
        prev = rc.GpPrev()
        a = rc.compute_joystick(m, FakeState(buttons={8: True}), prev)
        self.assertTrue(a["estop"])

    def test_hat_speed_edge(self):
        m = rc.default_mapping()
        prev = rc.GpPrev()
        a = rc.compute_joystick(m, FakeState(axes={7: -1.0}), prev)  # up (inverted)
        self.assertEqual(a["hat_delta"], 1)
        a = rc.compute_joystick(m, FakeState(axes={7: -1.0}), prev)  # held
        self.assertEqual(a["hat_delta"], 0)

    def test_top_speed_precedence(self):
        self.assertEqual(rc.top_speed(2, False, False, False), 0.25)
        self.assertEqual(rc.top_speed(2, True, False, False), rc.TURBO)
        self.assertEqual(rc.top_speed(2, True, True, False), rc.SPEED_LIMIT)
        self.assertEqual(rc.top_speed(2, True, True, True), rc.PRECISION_CAP)  # slow wins

    def test_drive_mix_and_gate(self):
        l, r = rc.drive_mix(1.0, 0.0, 0.25)
        self.assertEqual((l, r), (0.25, 0.25))
        emit, active = rc.drive_gate(0, 0, 0, 0, False)
        self.assertFalse(emit)                       # idle pad is silent
        emit, active = rc.drive_gate(0, 0, 0, 0, True)
        self.assertTrue(emit)                        # one final stop on idle edge
        self.assertFalse(active)

    def test_deadzone(self):
        self.assertEqual(rc.dz(0.1), 0.0)
        self.assertEqual(rc.dz(0.2), 0.2)
        self.assertEqual(rc.dz(-0.2), -0.2)

    def test_parse_js_event(self):
        import struct
        b = struct.pack("<IhBB", 123, -32767, rc.JS_EVENT_AXIS | rc.JS_EVENT_INIT, 3)
        value, etype, is_init, number = rc.parse_js_event(b)
        self.assertEqual((value, etype, is_init, number),
                         (-32767, rc.JS_EVENT_AXIS, True, 3))


class MiscTest(unittest.TestCase):
    def test_safe_photo_name(self):
        self.assertTrue(rc.safe_photo_name("rover_x.jpg"))
        for bad in ("../x.jpg", "a/b.jpg", ".hidden.jpg", "x.png", "x.jpg.meta.json"):
            self.assertFalse(rc.safe_photo_name(bad), bad)

    def test_resolve_camera_mode(self):
        self.assertEqual(rc.resolve_camera_mode("v4l2", "/dev/none"), "v4l2")
        self.assertEqual(rc.resolve_camera_mode("off", "/dev/none"), "off")
        self.assertEqual(rc.resolve_camera_mode("auto", "/nonexistent"), "rpicam")
        self.assertEqual(rc.resolve_camera_mode("bogus", "/dev/none"), "rpicam")

    def test_build_camera_cmd_pinned(self):
        args = rc.build_camera_cmd("v4l2", "/dev/video0", 1920, 1080, 15)
        self.assertEqual(" ".join(args),
                         "v4l2-ctl -d /dev/video0 "
                         "--set-fmt-video=width=1920,height=1080,pixelformat=MJPG "
                         "--set-parm=15 --stream-mmap --stream-count=0 --stream-to=-")
        args = rc.build_camera_cmd("v4l2", "/dev/video0", 0, 0, 0)
        self.assertNotIn("--set-parm=0", " ".join(args))

    def test_placeholder_is_jpeg(self):
        self.assertEqual(rc.PLACEHOLDER_FRAME[:2], b"\xff\xd8")
        self.assertEqual(rc.PLACEHOLDER_FRAME[-2:], b"\xff\xd9")


if __name__ == "__main__":
    unittest.main()
