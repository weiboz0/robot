"""Safety-envelope tests for autodrive (plan 017). All fakes — NO real motion,
NO live vision. Asserts the SafeDriver guarantees and the find_object logic."""
import unittest

import autodrive


class FakeTimer:
    """Stand-in for threading.Timer (records; fire() invokes the callback)."""
    def __init__(self, delay, fn):
        self.delay, self.fn = delay, fn
        self.started = self.cancelled = self.daemon = False

    def start(self):
        self.started = True

    def cancel(self):
        self.cancelled = True

    def fire(self):
        self.fn()


class FakeClient:
    """rovercontrol_client-shaped fake; records every call."""
    def __init__(self, serial=True, camera=True, cap=0.25, stop_fail=False):
        self.calls = []
        self._cap, self._serial, self._camera, self._stop_fail = cap, serial, camera, stop_fail

    def set_timeout(self, s): self.calls.append(("set_timeout", s))
    def healthz(self, timeout=None):
        return {"serial": {"up": self._serial}, "camera": {"up": self._camera}}
    def stop(self):
        self.calls.append(("stop",))
        if self._stop_fail:
            raise OSError("stop failed")
    def estop(self): self.calls.append(("estop",))
    def get_speed(self): return self._cap
    def set_speed(self, c): self._cap = c; self.calls.append(("set_speed", c))
    def nudge(self, d, ms): self.calls.append(("nudge", d, ms))
    def set_camera(self, p, t): self.calls.append(("cam", p, t))

    def names(self): return [c[0] for c in self.calls]
    def caps(self): return [c[1] for c in self.calls if c[0] == "set_speed"]


def mk(client, **kw):
    kw.setdefault("sleep", lambda s: None)
    kw.setdefault("timer", FakeTimer)
    return autodrive.SafeDriver(client, **kw)


class SafeDriverTest(unittest.TestCase):
    def test_no_continuous_drive_ever(self):
        c = FakeClient()
        with mk(c) as d:
            d.forward(lambda: True)
            d.turn_left()
        self.assertNotIn("drive", c.names())     # never continuous; only bounded nudges
        self.assertNotIn("move", c.names())
        self.assertIn("nudge", c.names())

    def test_crawl_cap_set_on_enter_restored_on_exit(self):
        c = FakeClient(cap=0.30)
        with mk(c):
            pass
        self.assertEqual(c.caps(), [0.12, 0.30])  # crawl, then restore
        self.assertIn("stop", c.names())          # stop clears the e-stop latch

    def test_cap_restored_even_on_exception(self):
        c = FakeClient(cap=0.30)
        with self.assertRaises(ValueError):
            with mk(c):
                raise ValueError("boom")
        self.assertEqual(c.caps()[-1], 0.30)      # restored despite the exception

    def test_forward_blocked_when_not_clear(self):
        c = FakeClient()
        with mk(c) as d:
            moved = d.forward(lambda: False)
        self.assertFalse(moved)
        self.assertNotIn("nudge", c.names())      # no motion when floor not clear
        self.assertIn("cam", c.names())           # but it DID aim the camera to look

    def test_forward_looks_down_then_nudges_when_clear(self):
        c = FakeClient()
        with mk(c) as d:
            moved = d.forward(lambda: True)
        self.assertTrue(moved)
        # camera aimed forward+down BEFORE the nudge (look-where-you-drive)
        cam = next(x for x in c.calls if x[0] == "cam")
        self.assertEqual(cam, ("cam", 0.0, autodrive.FLOOR_TILT))
        self.assertIn(("nudge", "forward", 250), c.calls)

    def test_forward_ms_clamped_small(self):
        c = FakeClient()
        d = mk(c, forward_ms=9999)
        self.assertEqual(d.forward_ms, 400)       # hard client-side clamp

    def test_step_cap_raises(self):
        c = FakeClient()
        with mk(c, max_steps=2) as d:
            d.turn_left()
            d.turn_right()
            with self.assertRaises(autodrive.SafetyLimit):
                d.turn_left()

    def test_time_cap_raises(self):
        c = FakeClient()
        t = [0.0]
        d = mk(c, max_seconds=10.0, clock=lambda: t[0])
        with d:
            d.turn_left()
            t[0] = 11.0
            with self.assertRaises(autodrive.SafetyLimit):
                d.turn_left()

    def test_preflight_refuses_dead_serial(self):
        with self.assertRaises(autodrive.SafetyLimit):
            with mk(FakeClient(serial=False)):
                pass

    def test_preflight_refuses_dead_camera(self):
        with self.assertRaises(autodrive.SafetyLimit):
            with mk(FakeClient(camera=False)):
                pass

    def test_exit_escalates_to_estop_when_stop_fails(self):
        c = FakeClient(cap=0.30, stop_fail=True)
        with mk(c):
            pass
        self.assertIn("estop", c.names())
        # a higher cap must NOT be restored if we couldn't confirm a stop
        self.assertNotIn(0.30, c.caps()[1:])

    def test_watchdog_armed_and_cancelled(self):
        c = FakeClient()
        timers = []

        def timer(delay, fn):
            t = FakeTimer(delay, fn)
            timers.append(t)
            return t
        with mk(c, timer=timer):
            pass
        self.assertTrue(timers and timers[0].started and timers[0].cancelled)

    def test_cleanup_on_keyboard_interrupt(self):
        c = FakeClient(cap=0.30)
        with self.assertRaises(KeyboardInterrupt):
            with mk(c):
                raise KeyboardInterrupt()
        self.assertEqual(c.caps()[-1], 0.30)      # cap restored even on Ctrl-C
        self.assertIn("stop", c.names())

    def test_watchdog_fires_estop(self):
        c = FakeClient()
        timers = []

        def timer(delay, fn):
            t = FakeTimer(delay, fn)
            timers.append(t)
            return t
        d = mk(c, timer=timer)
        with d:
            timers[0].fire()                      # simulate overrun while wedged
        self.assertIn("estop", c.names())


# ───────────────────────── find_object loop logic ──────────────────────────

class FakeDriver:
    def __init__(self, max_actions=6, floor_ok=True):
        self.actions = []
        self.max_actions = max_actions
        self._motions = 0

    def __enter__(self): return self
    def __exit__(self, *a): self.actions.append(("halt",)); return False

    def _tick(self):
        self._motions += 1
        if self._motions > self.max_actions:
            raise autodrive.SafetyLimit("budget")

    def look(self, p, t): self.actions.append(("look", p, t))
    def center_camera(self): self.actions.append(("center",))
    def forward(self, clearance):
        self._tick(); ok = clearance(); self.actions.append(("forward", ok)); return ok
    def turn_left(self): self._tick(); self.actions.append(("left",))
    def turn_right(self): self._tick(); self.actions.append(("right",))
    def halt(self): self.actions.append(("halt",))


class FakeVision:
    def __init__(self, find_seq, floor=True):
        self.find_seq = list(find_seq)
        self.floor = floor

    def describe(self, img, prompt, json_out=False, max_tokens=None):
        if "bearing" in prompt:                 # FIND_PROMPT asks for a bearing; FLOOR_PROMPT doesn't
            return self.find_seq.pop(0) if self.find_seq else {
                "seen": False, "bearing": "center", "close": False, "confidence": 0.0}
        return {"clear": self.floor, "confidence": 0.9, "hazard": ""}


def cap():
    return ("rover_x.jpg", b"IMG")


class FindObjectTest(unittest.TestCase):
    def test_found_immediately_returns_photo(self):
        v = FakeVision([{"seen": True, "close": True, "bearing": "center", "confidence": 0.9}])
        d = FakeDriver()
        out = autodrive.find_object(d, v, "screwdriver", capture=cap)
        self.assertEqual(out, "rover_x.jpg")
        self.assertNotIn("forward", [a[0] for a in d.actions])   # no driving needed

    def test_bearing_left_turns_left(self):
        v = FakeVision([{"seen": True, "close": False, "bearing": "left", "confidence": 0.8},
                        {"seen": True, "close": True, "bearing": "center", "confidence": 0.9}])
        d = FakeDriver()
        autodrive.find_object(d, v, "x", capture=cap)
        self.assertIn(("left",), d.actions)

    def test_center_far_advances_when_floor_clear(self):
        v = FakeVision([{"seen": True, "close": False, "bearing": "center", "confidence": 0.8},
                        {"seen": True, "close": True, "bearing": "center", "confidence": 0.9}],
                       floor=True)
        d = FakeDriver()
        autodrive.find_object(d, v, "x", capture=cap)
        self.assertIn(("forward", True), d.actions)

    def test_center_far_blocked_turns_when_floor_not_clear(self):
        v = FakeVision([{"seen": True, "close": False, "bearing": "center", "confidence": 0.8}] * 20,
                       floor=False)
        d = FakeDriver(max_actions=4)
        out = autodrive.find_object(d, v, "x", capture=cap)
        self.assertIsNone(out)                               # never advances → gives up
        self.assertIn(("forward", False), d.actions)         # forward was refused (floor unsafe)
        self.assertIn(("left",), d.actions)                  # fell back to turning

    def test_not_seen_scans_then_budget_gives_up(self):
        v = FakeVision([], floor=True)                       # always "not seen"
        d = FakeDriver(max_actions=3)
        out = autodrive.find_object(d, v, "x", capture=cap)
        self.assertIsNone(out)
        self.assertEqual(d.actions.count(("left",)), 3)      # scanned to the budget, then stopped
        self.assertIn(("halt",), d.actions)                  # context exit halted

    def test_vision_error_is_fail_closed(self):
        class Boom:
            def describe(self, *a, **k): raise RuntimeError("api down")
        self.assertFalse(autodrive.floor_is_clear(Boom(), b"IMG"))
        obs = autodrive.look_for(Boom(), b"IMG", "x")
        self.assertFalse(obs["seen"])                        # unseen, not a crash

    def test_low_confidence_floor_is_unsafe(self):
        class LowConf:
            def describe(self, *a, **k): return {"clear": True, "confidence": 0.2}
        self.assertFalse(autodrive.floor_is_clear(LowConf(), b"IMG"))  # fail closed on low conf

    def test_vision_errors_bail_out_not_spin(self):
        class Boom:
            def describe(self, *a, **k): raise RuntimeError("api down")
        d = FakeDriver(max_actions=100)
        out = autodrive.find_object(d, Boom(), "x", capture=cap)
        self.assertIsNone(out)
        self.assertLessEqual(d._motions, autodrive.MAX_VISION_ERRORS)  # bailed, didn't spin to budget

    def test_low_confidence_not_declared_found(self):
        # seen+close+center but LOW confidence must NOT be declared found
        v = FakeVision([{"seen": True, "close": True, "bearing": "center", "confidence": 0.2}] * 20,
                       floor=True)
        d = FakeDriver(max_actions=3)
        self.assertIsNone(autodrive.find_object(d, v, "x", capture=cap))


if __name__ == "__main__":
    unittest.main()
