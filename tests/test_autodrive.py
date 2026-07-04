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
    floor_tilt = -20.0
    max_seconds = 9999.0

    def __init__(self, max_actions=6, floor_ok=True):
        self.actions = []
        self.max_actions = max_actions
        self._motions = 0

    def elapsed(self):
        return 0.0

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
    def turn_left(self, ms=None): self._tick(); self.actions.append(("left",))
    def turn_right(self, ms=None): self._tick(); self.actions.append(("right",))
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
    # ---- camera-first sweep loop (v3): find WITHOUT wheel motion ----
    SEEN = {"seen": True, "bearing": "center", "close": True, "confidence": 0.9,
            "bbox": [0.45, 0.45, 0.55, 0.55], "color": "green"}
    UNSEEN = {"seen": False, "bearing": "center", "close": False, "confidence": 0.0}

    def run_find(self, look_seq, d=None, snap=None, **kw):
        seq = list(look_seq)
        look = lambda nm, im: (seq.pop(0) if seq else dict(self.UNSEEN))
        d = d or FakeDriver(max_actions=20)
        return d, autodrive.find_object(
            d, None, "a green pen", capture=lambda: (None, b"IMG"),
            look=look, snap=snap or (lambda: "rover_final.jpg"), **kw)

    def test_found_from_standstill_no_wheel_motion(self):
        # THE requirement: the target visible from the current spot is found
        # with camera moves only — zero wheel commands.
        d, out = self.run_find([self.SEEN],
                               sweep_pans=(0,), sweep_tilts=(-20,), max_rotations=2)
        self.assertEqual(out, "rover_final.jpg")
        acts = [a[0] for a in d.actions]
        self.assertNotIn("left", acts)
        self.assertNotIn("right", acts)
        self.assertNotIn("forward", acts)
        self.assertIn("look", acts)                     # camera moved
        self.assertIn("halt", acts)

    def test_sweep_covers_all_views_then_rotates(self):
        # unseen across a full 2x2 sweep -> one in-place rotation -> found
        seq = [self.UNSEEN] * 5 + [self.SEEN]           # 4 sweep views + 1 confirm-ish
        d, out = self.run_find(seq, sweep_pans=(-25, 25), sweep_tilts=(-15, -28),
                               max_rotations=2)
        self.assertEqual(out, "rover_final.jpg")
        self.assertIn(("left",), d.actions)             # rotated exactly between sweeps

    def test_gives_up_after_full_circle(self):
        d, out = self.run_find([self.UNSEEN] * 100,
                               sweep_pans=(0,), sweep_tilts=(-20,), max_rotations=3)
        self.assertIsNone(out)
        self.assertEqual([a for a in d.actions if a[0] == "left"], [("left",)] * 3)
        self.assertIn(("halt",), d.actions)             # context exit stopped wheels

    def test_no_forward_driving_ever(self):
        # the default find NEVER advances — finding does not require approaching
        d, out = self.run_find([self.UNSEEN] * 4 + [self.SEEN],
                               sweep_pans=(-25, 25), sweep_tilts=(-15, -28),
                               max_rotations=2)
        self.assertNotIn("forward", [a[0] for a in d.actions])

    def test_low_confidence_sighting_not_accepted(self):
        weak = dict(self.SEEN, confidence=0.3)
        d, out = self.run_find([weak] * 30,
                               sweep_pans=(0,), sweep_tilts=(-20,), max_rotations=1)
        self.assertIsNone(out)

    def test_vision_errors_bail_out(self):
        err = {"seen": False, "confidence": 0.0, "error": True}
        d, out = self.run_find([err] * 10,
                               sweep_pans=(0, 10), sweep_tilts=(-20,), max_rotations=5)
        self.assertIsNone(out)
        looks = [a for a in d.actions if a[0] == "look"]
        self.assertLessEqual(len(looks), autodrive.MAX_VISION_ERRORS)   # bailed fast, no spin

    def test_found_uses_snap_and_on_found(self):
        got = {}
        seq = [dict(self.SEEN)]
        look = lambda nm, im: (seq.pop(0) if seq else dict(self.SEEN))
        d = FakeDriver(max_actions=20)
        out = autodrive.find_object(d, None, "a green pen",
                                    capture=lambda: (None, b"IMG"), look=look,
                                    snap=lambda: "rover_final.jpg",
                                    on_found=lambda n, o: got.update(n=n, c=o.get("color")),
                                    sweep_pans=(0,), sweep_tilts=(-20,), max_rotations=1)
        self.assertEqual(out, "rover_final.jpg")
        self.assertEqual(got["n"], "rover_final.jpg")
        self.assertEqual(got["c"], "green")

    # ---- pure-function tests (unchanged behavior) ----
    def test_vision_error_is_fail_closed(self):
        class Boom:
            def describe(self, *a, **k): raise RuntimeError("api down")
        self.assertFalse(autodrive.floor_is_clear(Boom(), b"IMG"))
        obs = autodrive.look_for(Boom(), b"IMG", "x")
        self.assertFalse(obs["seen"])

    def test_low_confidence_floor_is_unsafe(self):
        class LowConf:
            def describe(self, *a, **k): return {"clear": True, "confidence": 0.2}
        self.assertFalse(autodrive.floor_is_clear(LowConf(), b"IMG"))

    def test_sane_bbox_validation(self):
        ok = autodrive._sane_bbox([0.1, 0.2, 0.5, 0.6])
        self.assertEqual(ok, [0.1, 0.2, 0.5, 0.6])
        for bad in (None, "x", [0.1, 0.2, 0.5], [0.5, 0.2, 0.1, 0.6],
                    [0.1, 0.2, 0.5, 1.5], [-0.1, 0.2, 0.5, 0.6],
                    [0.1, "a", 0.5, 0.6], [float("nan"), 0.2, 0.5, 0.6]):
            self.assertIsNone(autodrive._sane_bbox(bad), bad)

    def test_bbox_overrides_bearing_and_close(self):
        class V:
            def describe(self, *a, **k):
                return {"seen": True, "bbox": [0.62, 0.8, 0.95, 0.86],
                        "bearing": "left", "close": False, "confidence": 0.9}
        obs = autodrive.look_for(V(), b"IMG", "a pen")
        self.assertEqual(obs["bearing"], "right")
        self.assertTrue(obs["close"])                   # max-dim via width

    def test_garbage_bbox_falls_back_to_model_flags(self):
        class V:
            def describe(self, *a, **k):
                return {"seen": True, "bbox": [9, 9, 9, 9],
                        "bearing": "left", "close": True, "confidence": 0.9}
        obs = autodrive.look_for(V(), b"IMG", "a pen")
        self.assertIsNone(obs["bbox"])
        self.assertEqual(obs["bearing"], "left")

    def test_small_object_close_via_bottom_proximity(self):
        class V:
            def __init__(self, y2): self.y2 = y2
            def describe(self, *a, **k):
                return {"seen": True, "bbox": [0.48, self.y2 - 0.06, 0.52, self.y2],
                        "bearing": "center", "close": False, "confidence": 0.9}
        self.assertFalse(autodrive.look_for(V(0.64), b"IMG", "a pen")["close"])
        self.assertTrue(autodrive.look_for(V(0.75), b"IMG", "a pen")["close"])

    def test_refine_center_pans_camera_toward_target(self):
        d = FakeDriver()
        obs = {"seen": True, "bbox": [0.7, 0.4, 0.8, 0.5]}
        seq = [{"seen": True, "bbox": [0.55, 0.4, 0.65, 0.5]},
               {"seen": True, "bbox": [0.46, 0.4, 0.54, 0.5]}]
        looker = lambda nm, im: seq.pop(0)
        out = autodrive._refine_center(d, looker, lambda: (None, b"IMG"), obs, lambda m: None)
        cx = (out["bbox"][0] + out["bbox"][2]) / 2
        self.assertAlmostEqual(cx, 0.5, delta=0.05)
        self.assertGreaterEqual(len([a for a in d.actions if a[0] == "look"]), 2)

    def test_refine_center_keeps_last_good_on_loss(self):
        d = FakeDriver()
        obs = {"seen": True, "bbox": [0.7, 0.4, 0.8, 0.5]}
        out = autodrive._refine_center(d, lambda nm, im: {"seen": False},
                                       lambda: (None, b"IMG"), obs, lambda m: None)
        self.assertEqual(out["bbox"], [0.7, 0.4, 0.8, 0.5])


if __name__ == "__main__":
    unittest.main()
