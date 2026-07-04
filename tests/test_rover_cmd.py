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

    def photo(self):
        self.calls.append(("photo",))
        return "photos/rover_test.jpg"

    def center(self):
        self.calls.append(("center",))
        self.pan = self.tilt = 0.0

    def set_speed(self, cap):
        cap = max(0.0, min(0.5, float(cap)))
        self.calls.append(("set_speed", cap))
        self._cap = cap
        return cap

    def get_speed(self):
        return getattr(self, "_cap", 0.5)

    def status(self):
        return {"backend": self.backend, "where": "fake", "serial": {"up": True},
                "camera": {"up": None}, "gamepad": {"up": None}, "speed_cap": self.get_speed()}

    def list_photos(self):
        return ["rover_b.jpg", "rover_a.jpg"]

    def nudge(self, direction, ms=400):
        self.calls.append(("nudge", direction, ms))

    def light_channel(self, which, on=None):
        self.calls.append(("light_channel", which, on))
        return True if on is None else bool(on)


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

    def test_photo(self):
        r = FakeRover()
        out = agent_chat.rover_command(r, "photo")
        self.assertIn(("photo",), r.calls)
        self.assertIn(".jpg", out)

    def test_center_command(self):
        r = FakeRover()
        agent_chat.rover_command(r, "center")
        self.assertIn(("center",), r.calls)

    def test_speed_set_and_query(self):
        r = FakeRover()
        out = agent_chat.rover_command(r, "speed 0.3")
        self.assertIn(("set_speed", 0.3), r.calls)
        self.assertIn("0.3", out)
        q = agent_chat.rover_command(r, "speed")          # no arg → query
        self.assertIn("0.3", q)

    def test_status_command(self):
        r = FakeRover()
        out = agent_chat.rover_command(r, "status")
        self.assertIn("backend", out)
        self.assertIn("speed_cap", out)

    def test_photos_command(self):
        r = FakeRover()
        out = agent_chat.rover_command(r, "photos")
        self.assertIn("rover_b.jpg", out)

    def test_unknown_command(self):
        r = FakeRover()
        self.assertIn("unknown", agent_chat.rover_command(r, "bogus").lower())


class ParityAliasTest(unittest.TestCase):
    """Plan 019: website command names work in the chatbot."""

    def test_camera_up_equals_up(self):
        a, b = FakeRover(), FakeRover()
        agent_chat.rover_command(a, "up 10")
        agent_chat.rover_command(b, "camera_up 10")
        self.assertEqual(a.calls, b.calls)

    def test_camera_aim_center_snapshot(self):
        r = FakeRover()
        agent_chat.rover_command(r, "camera_aim 30 -10")
        self.assertIn(("cam", 30.0, -10.0), r.calls)
        agent_chat.rover_command(r, "camera_center")
        self.assertIn(("center",), r.calls)
        out = agent_chat.rover_command(r, "snapshot")
        self.assertIn(("photo",), r.calls)
        self.assertIn(".jpg", out)

    def test_gimbal_aliases(self):
        r = FakeRover()
        agent_chat.rover_command(r, "gimbal_relax")
        agent_chat.rover_command(r, "gimbal_lock")
        self.assertIn(("torque", False), r.calls)
        self.assertIn(("torque", True), r.calls)

    def test_light_channel_set_and_toggle(self):
        r = FakeRover()
        agent_chat.rover_command(r, "light_head on")
        self.assertIn(("light_channel", "head", True), r.calls)
        agent_chat.rover_command(r, "light_base off")
        self.assertIn(("light_channel", "base", False), r.calls)
        agent_chat.rover_command(r, "light_head")          # no arg = toggle
        self.assertIn(("light_channel", "head", None), r.calls)
        self.assertIn("on|off", agent_chat.rover_command(r, "light_head maybe"))

    def test_move_nudges(self):
        r = FakeRover()
        agent_chat.rover_command(r, "move_forward 300")
        self.assertIn(("nudge", "forward", 300.0), r.calls)
        agent_chat.rover_command(r, "move_left")           # default ms
        self.assertIn(("nudge", "left", 400), r.calls)

    def test_drive_keeps_chatbot_units(self):
        # drive is deliberately NOT remapped: chatbot semantics (L R [seconds])
        r = FakeRover()
        agent_chat.rover_command(r, "drive 0.2 0.2 1")
        self.assertIn(("drive", 0.2, 0.2, 1.0), r.calls)


class RoverCmdErrorTest(unittest.TestCase):
    """P2: a backend/network exception must not crash the REPL — return a string."""

    def test_backend_exception_returns_error(self):
        class Boom(FakeRover):
            def stop(self):
                raise OSError("link down")
        out = agent_chat.rover_command(Boom(), "stop")
        self.assertIsInstance(out, str)
        self.assertIn("error", out.lower())


class TrimHistoryTest(unittest.TestCase):
    """P3: cap history, always keep the system message, snap the window to a user
    boundary so a tool reply is never orphaned from its assistant.tool_calls."""

    def test_short_history_untouched(self):
        msgs = [{"role": "system", "content": "S"}, {"role": "user", "content": "a"}]
        agent_chat.trim_history(msgs, limit=24)
        self.assertEqual(len(msgs), 2)

    def test_keeps_system_and_snaps_to_user(self):
        msgs = [{"role": "system", "content": "S"}]
        for i in range(40):
            msgs.append({"role": "user", "content": f"u{i}"})
            msgs.append({"role": "assistant", "content": f"a{i}"})
        agent_chat.trim_history(msgs, limit=10)
        self.assertEqual(msgs[0]["role"], "system")
        self.assertEqual(msgs[1]["role"], "user")           # snapped to a user boundary
        self.assertLessEqual(len(msgs), 1 + 10)

    def test_never_orphans_tool_message(self):
        msgs = [{"role": "system", "content": "S"}]
        for i in range(20):
            msgs.append({"role": "user", "content": f"u{i}"})
            msgs.append({"role": "assistant", "content": None,
                         "tool_calls": [{"id": f"t{i}"}]})
            msgs.append({"role": "tool", "tool_call_id": f"t{i}", "content": "ok"})
        agent_chat.trim_history(msgs, limit=8)
        self.assertEqual(msgs[0]["role"], "system")
        self.assertEqual(msgs[1]["role"], "user")           # never starts on tool/assistant


class ToolSurfaceTest(unittest.TestCase):
    """Plan 014: the new controller-parity capabilities are real LLM tools and
    run_tool dispatches them (codex nit: $-cmd tests alone miss tool promotion)."""

    def test_build_tools_includes_new_tools(self):
        tools = agent_chat.build_tools(FakeRover(), None)
        names = {t["function"]["name"] for t in tools}
        for n in ("rover_set_speed", "rover_get_status", "rover_center_camera",
                  "rover_gimbal_torque", "rover_list_photos", "rover_find_object"):
            self.assertIn(n, names)

    def test_run_tool_dispatches_new_tools(self):
        r = FakeRover()
        self.assertIn("centered", agent_chat.run_tool(r, None, "rover_center_camera", {}))
        agent_chat.run_tool(r, None, "rover_gimbal_torque", {"lock": False})
        self.assertIn(("torque", False), r.calls)
        out = agent_chat.run_tool(r, None, "rover_set_speed", {"cap": 0.2})
        self.assertIn(("set_speed", 0.2), r.calls)
        self.assertIn("0.2", out)
        self.assertIn("backend", agent_chat.run_tool(r, None, "rover_get_status", {}))
        self.assertIn("rover_b.jpg", agent_chat.run_tool(r, None, "rover_list_photos", {}))


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
