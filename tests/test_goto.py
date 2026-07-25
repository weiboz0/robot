"""Plan 036: go-to / come-back navigation — the safety-critical suite.
Every wheel nudge must sit behind a fresh clearance verdict; a camera-pan
sighting must never trigger a forward pulse until the body is aligned; sign
detection fails closed; backtrack follows the index-sliced segment. All on
fakes — no hardware, no LLM, no motion."""
import os
import unittest

import autodrive as ad
import agent_chat as ac


class NavClient:
    """rovercontrol_client-shaped fake that logs every call in order."""

    def __init__(self):
        self.log = []
        self._speed = 0.5

    def healthz(self, timeout=None):
        return {"serial": {"up": True}, "camera": {"up": True}}

    def stop(self):
        self.log.append(("stop",))

    def estop(self):
        self.log.append(("estop",))

    def get_speed(self):
        return self._speed

    def set_speed(self, v):
        self._speed = v

    def set_timeout(self, s):
        pass

    def set_camera(self, pan, tilt):
        self.log.append(("aim", pan, tilt))

    def nudge(self, direction, ms):
        self.log.append(("nudge", direction, ms))


def make_driver(client, **kw):
    kw.setdefault("settle_s", 0)
    kw.setdefault("forward_cooldown_s", 0)
    kw.setdefault("sleep", lambda s: None)
    kw.setdefault("timer", FakeTimer)
    return ad.SafeDriver(client, **kw)


class FakeTimer:
    def __init__(self, *a, **k):
        self.daemon = True

    def start(self):
        pass

    def cancel(self):
        pass


class Clearance:
    """Records when it's consulted; scriptable verdicts (holds last)."""

    def __init__(self, verdicts=(True,)):
        self.verdicts = list(verdicts)
        self.calls = 0

    def __call__(self):
        self.calls += 1
        v = self.verdicts.pop(0) if len(self.verdicts) > 1 else self.verdicts[0]
        return v


def nudges(client):
    return [e for e in client.log if e[0] == "nudge"]


def clear_before_every_nudge(client, clear_marks):
    """True iff at least one clearance call lands between consecutive nudges
    (clearance calls are injected into the client log via marker entries)."""
    last = -1
    for i, e in enumerate(client.log):
        if e[0] == "nudge":
            window = client.log[last + 1:i]
            if not any(w[0] == "clear" for w in window):
                return False
            last = i
    return True


class MarkedClearance(Clearance):
    """Clearance that also stamps the client log, for ordering proofs."""

    def __init__(self, client, verdicts=(True,)):
        super().__init__(verdicts)
        self.client = client

    def __call__(self):
        self.client.log.append(("clear",))
        return super().__call__()


class TurnGatedTest(unittest.TestCase):
    def test_survey_then_side_check_then_nudge(self):
        c = NavClient()
        d = make_driver(c)
        cl = MarkedClearance(c)
        with d:
            self.assertTrue(d.turn_gated("left", 200, cl))
        aims = [e for e in c.log if e[0] == "aim"]
        # survey: -40, 0, +40 then the turn-direction re-aim (-40 for left)
        self.assertEqual([a[1] for a in aims[:4]], [-40.0, 0.0, 40.0, -40.0])
        self.assertEqual(cl.calls, 4)          # 3 survey + 1 per-pulse
        self.assertTrue(clear_before_every_nudge(c, None))

    def test_survey_reused_within_ttl_single_check_per_pulse(self):
        c = NavClient()
        d = make_driver(c)
        cl = MarkedClearance(c)
        with d:
            self.assertTrue(d.turn_gated("left", 200, cl))
            n = cl.calls
            self.assertTrue(d.turn_gated("left", 200, cl))
        self.assertEqual(cl.calls, n + 1)      # only the per-pulse re-check

    def test_forward_voids_survey(self):
        c = NavClient()
        d = make_driver(c)
        cl = MarkedClearance(c)
        with d:
            self.assertTrue(d.turn_gated("left", 200, cl))
            self.assertTrue(d.forward(cl))
            n = cl.calls
            self.assertTrue(d.turn_gated("right", 200, cl))
        self.assertEqual(cl.calls, n + 4)      # full re-survey + per-pulse

    def test_ttl_expiry_forces_resurvey(self):
        t = {"now": 0.0}
        c = NavClient()
        d = make_driver(c, clock=lambda: t["now"])
        cl = MarkedClearance(c)
        with d:
            self.assertTrue(d.turn_gated("left", 200, cl))
            t["now"] += ad.TURN_SURVEY_TTL_S + 1
            n = cl.calls
            self.assertTrue(d.turn_gated("left", 200, cl))
        self.assertEqual(cl.calls, n + 4)

    def test_survey_never_leaks_across_driver_reentry(self):
        # codex catch: a prior run's survey must not let a reused SafeDriver
        # skip the 3-view survey inside its TTL window
        c = NavClient()
        d = make_driver(c)
        cl = MarkedClearance(c)
        with d:
            self.assertTrue(d.turn_gated("left", 200, cl))
        n = cl.calls
        with d:                                    # SAME instance, new run
            self.assertTrue(d.turn_gated("left", 200, cl))
        self.assertEqual(cl.calls, n + 4)          # full re-survey demanded

    def test_one_dirty_view_zero_turn_nudges(self):
        c = NavClient()
        d = make_driver(c)
        # survey views: -40 clear, 0 clear, +40 DIRTY
        cl = MarkedClearance(c, verdicts=[True, True, False, True])
        with d:
            self.assertFalse(d.turn_gated("left", 200, cl))
        self.assertEqual(nudges(c), [])

    def test_dirty_side_check_blocks_and_voids(self):
        c = NavClient()
        d = make_driver(c)
        cl = MarkedClearance(c, verdicts=[True, True, True, False, True])
        with d:
            self.assertFalse(d.turn_gated("left", 200, cl))   # side check dirty
            self.assertEqual(nudges(c), [])
            # survey voided → next attempt re-surveys
            n = cl.calls
            self.assertTrue(d.turn_gated("left", 200, cl))
        self.assertEqual(cl.calls, n + 4)


class TurnToHeadingTest(unittest.TestCase):
    class PoseSim:
        """Heading responds to nudges with a configurable sign/gain."""

        def __init__(self, client, heading=0.0, left_gain=+20.0, fresh=True):
            self.client = client
            self.heading = heading
            self.left_gain = left_gain
            self.fresh = fresh
            self._seen = 0

        def __call__(self):
            for e in self.client.log[self._seen:]:
                if e[0] == "nudge" and e[1] in ("left", "right"):
                    g = self.left_gain if e[1] == "left" else -self.left_gain
                    self.heading = ((self.heading + g + 180) % 360) - 180
            self._seen = len(self.client.log)
            return {"x": 0.0, "y": 0.0, "heading": self.heading,
                    "fresh": self.fresh}

    def _run(self, target, **pose_kw):
        c = NavClient()
        d = make_driver(c)
        pose = self.PoseSim(c, **pose_kw)
        with d:
            ok = ad.turn_to_heading(d, pose, target, clearance=Clearance())
        return ok, pose, c

    def test_correct_sign_converges(self):
        ok, pose, _ = self._run(90.0, left_gain=+20.0)
        self.assertTrue(ok)
        self.assertLessEqual(abs(ad._norm180(90.0 - pose.heading)), 12.0)

    def test_inverted_sign_auto_detected_and_converges(self):
        # HEADING_SIGN wrong: "left" DECREASES heading — must still converge
        ok, pose, _ = self._run(90.0, left_gain=-20.0)
        self.assertTrue(ok)
        self.assertLessEqual(abs(ad._norm180(90.0 - pose.heading)), 12.0)

    def test_no_delta_fails_closed_after_one_retry(self):
        c = NavClient()
        d = make_driver(c)
        pose = self.PoseSim(c, left_gain=0.0)      # turns don't register
        with d:
            with self.assertRaises(ad.SafetyLimit):
                ad.turn_to_heading(d, pose, 90.0, clearance=Clearance())
        self.assertEqual(len(nudges(c)), 2)        # cal pulse + ONE retry only

    def test_stale_pose_mid_loop_fails_closed(self):
        c = NavClient()
        d = make_driver(c)
        pose = self.PoseSim(c, left_gain=+20.0)
        reads = {"n": 0}

        def flaky_pose():
            reads["n"] += 1
            out = pose()
            if reads["n"] >= 2:
                out["fresh"] = False           # goes stale after the first read
            return out
        with d:
            with self.assertRaises(ad.SafetyLimit):
                ad.turn_to_heading(d, flaky_pose, 179.0, clearance=Clearance())

    def test_max_pulses_is_a_hard_nudge_cap_even_during_calibration(self):
        # codex catch: the 1.5× calibration retry must consume the SAME
        # budget — max_pulses=1 means exactly ONE nudge, then fail closed
        c = NavClient()
        d = make_driver(c)
        pose = self.PoseSim(c, left_gain=0.0)
        with d:
            with self.assertRaises(ad.SafetyLimit):
                ad.turn_to_heading(d, pose, 90.0, clearance=Clearance(),
                                   max_pulses=1)
        self.assertEqual(len(nudges(c)), 1)

    def test_sign_disagreement_fails_closed(self):
        # mapping flips mid-run (slip/noise): detected sign says left=+,
        # then a later pulse measures the opposite → SafetyLimit
        c = NavClient()
        d = make_driver(c)
        state = {"turns": 0}

        class FlipPose(self.PoseSim):
            def __call__(self2):
                for e in c.log[self2._seen:]:
                    if e[0] == "nudge" and e[1] in ("left", "right"):
                        state["turns"] += 1
                        gain = 20.0 if state["turns"] <= 1 else -20.0
                        g = gain if e[1] == "left" else -gain
                        self2.heading = ((self2.heading + g + 180) % 360) - 180
                self2._seen = len(c.log)
                return {"x": 0, "y": 0, "heading": self2.heading,
                        "fresh": True}
        pose = FlipPose(c, heading=0.0)
        with d:
            with self.assertRaises(ad.SafetyLimit):
                ad.turn_to_heading(d, pose, 170.0, clearance=Clearance())

    def test_blocked_turn_raises(self):
        c = NavClient()
        d = make_driver(c)
        pose = self.PoseSim(c)
        with d:
            with self.assertRaises(ad.SafetyLimit):
                ad.turn_to_heading(d, pose, 90.0,
                                   clearance=Clearance([False]))
        self.assertEqual(nudges(c), [])


class ApproachTest(unittest.TestCase):
    def _looker_script(self, driver, script):
        """script: {(pan_condition)} → returns obs per current camera aim."""
        def looker(nm, img):
            pan = (driver._aim or (0, 0))[0]
            return script(pan)
        return looker

    def test_pan_sighting_never_forwards_until_body_aligned(self):
        # THE plan-036 review bug: target sighted at pan=+50 — the rover must
        # turn its BODY (gated) and re-sight; zero forward nudges before that.
        c = NavClient()
        d = make_driver(c)
        cl = MarkedClearance(c)
        turns_done = {"n": 0}

        def script(pan):
            turns = len([e for e in c.log
                         if e[0] == "nudge" and e[1] in ("left", "right")])
            fwds = len([e for e in c.log
                        if e[0] == "nudge" and e[1] == "forward"])
            if turns == 0:
                # only visible far right until the body has turned
                if pan == 50:
                    return {"seen": True, "bbox": [0.4, 0.4, 0.6, 0.6],
                            "confidence": 0.9}
                return {"seen": False, "bbox": None, "confidence": 0.0}
            # after the body turn: straight ahead, centered but NOT close
            # until two forward pulses have happened (glm review: the test
            # must also prove forwards DO fire, post-alignment)
            if abs(pan) <= 10:
                return {"seen": True, "bbox": [0.3, 0.3, 0.7, 0.8],
                        "close": fwds >= 2, "confidence": 0.9}
            return {"seen": False, "bbox": None, "confidence": 0.0}
        ok, obs, why = ad.approach_object(
            d, None, "suitcase", capture=lambda: (None, b"img"),
            look=self._looker_script(d, script), clearance=cl)
        self.assertTrue(ok, why)
        log = c.log
        first_fwd = next((i for i, e in enumerate(log)
                          if e[0] == "nudge" and e[1] == "forward"), None)
        first_turn = next((i for i, e in enumerate(log)
                           if e[0] == "nudge" and e[1] in ("left", "right")),
                          None)
        self.assertIsNotNone(first_turn)
        self.assertIsNotNone(first_fwd)        # forwards genuinely happen…
        self.assertGreater(first_fwd, first_turn)   # …but only after aligning
        self.assertGreaterEqual(
            len([e for e in nudges(c) if e[1] == "forward"]), 2)
        self.assertTrue(clear_before_every_nudge(c, None))

    def test_centered_close_arrives_without_wheels(self):
        c = NavClient()
        d = make_driver(c)

        def script(pan):
            if pan == 0:
                return {"seen": True, "bbox": [0.3, 0.3, 0.7, 0.8],
                        "close": True, "confidence": 0.9}
            return {"seen": False, "bbox": None, "confidence": 0.0}
        ok, obs, why = ad.approach_object(
            d, None, "bin", capture=lambda: (None, b"img"),
            look=self._looker_script(d, script), clearance=Clearance())
        self.assertTrue(ok)
        self.assertEqual(nudges(c), [])            # sighted close: no motion

    def test_blocked_floor_stops_with_no_forward(self):
        c = NavClient()
        d = make_driver(c)

        def script(pan):
            if pan == 0:
                return {"seen": True, "bbox": [0.45, 0.4, 0.55, 0.5],
                        "close": False, "confidence": 0.9}
            return {"seen": False, "bbox": None, "confidence": 0.0}
        ok, obs, why = ad.approach_object(
            d, None, "bin", capture=lambda: (None, b"img"),
            look=self._looker_script(d, script),
            clearance=Clearance([False]))
        self.assertFalse(ok)
        self.assertIn("blocked", why)
        self.assertEqual([e for e in nudges(c) if e[1] == "forward"], [])

    def test_lost_sight_three_times_stops_honestly(self):
        c = NavClient()
        d = make_driver(c)
        state = {"seen_once": False}

        def script(pan):
            if pan == 0 and not state["seen_once"]:
                state["seen_once"] = True      # sighted once in the sweep…
                return {"seen": True, "bbox": [0.45, 0.4, 0.55, 0.5],
                        "close": False, "confidence": 0.9}
            return {"seen": False, "bbox": None,      # …then gone for good
                    "confidence": 0.0}
        ok, obs, why = ad.approach_object(
            d, None, "bin", capture=lambda: (None, b"img"),
            look=self._looker_script(d, script), clearance=Clearance())
        self.assertFalse(ok)
        self.assertIn("lost sight", why)

    def test_not_visible_no_motion_at_all(self):
        c = NavClient()
        d = make_driver(c)
        ok, obs, why = ad.approach_object(
            d, None, "unicorn", capture=lambda: (None, b"img"),
            look=lambda nm, im: {"seen": False, "bbox": None,
                                 "confidence": 0.0},
            clearance=Clearance())
        self.assertFalse(ok)
        self.assertIn("not visible", why)
        self.assertEqual(nudges(c), [])


class WaypointPlanTest(unittest.TestCase):
    def test_index_slice_beats_crossover(self):
        # pre-home wandering passes THROUGH home; the plan must use only the
        # post-home segment (index), never nearest-point matching
        pre = [[0.0, 0.0], [1.0, 0.0], [0.0, 0.05], [-1.0, 0.0]]
        post = [[-1.0, 0.0], [-1.0, 1.0], [-1.0, 2.0]]
        trail = pre + post
        home = {"pose": {"x": -1.0, "y": 0.0, "heading": 0.0},
                "trail_len": len(pre) + 1}
        wps, note = ad.plan_return_waypoints(trail, home, wp_spacing=0.4)
        self.assertIsNone(note)
        # reversed post-home segment: from (−1,2) back to home (−1,0)
        self.assertEqual(wps[0], (-1.0, 2.0))
        self.assertEqual(wps[-1], (-1.0, 0.0))
        for x, y in wps:
            self.assertAlmostEqual(x, -1.0)        # never the pre-home leg

    def test_eviction_fallback_notes_and_ends_home(self):
        trail = [[5.0, 5.0], [5.0, 6.0]]           # home's points evicted
        home = {"pose": {"x": 0.0, "y": 0.0, "heading": 0.0}, "trail_len": 1}
        wps, note = ad.plan_return_waypoints(trail, home)
        self.assertIsNotNone(note)
        self.assertEqual(wps[-1], (0.0, 0.0))

    def test_empty_trail_home_only(self):
        home = {"pose": {"x": 1.0, "y": 2.0, "heading": 0.0}, "trail_len": 0}
        wps, _ = ad.plan_return_waypoints([], home)
        self.assertEqual(wps, [(1.0, 2.0)])

    def test_subsampling_spacing(self):
        seg = [[0.0, i * 0.05] for i in range(41)]  # 2m of 5cm points
        home = {"pose": {"x": 0.0, "y": 0.0, "heading": 0.0}, "trail_len": 1}
        wps, _ = ad.plan_return_waypoints(seg, home, wp_spacing=0.4)
        for (x1, y1), (x2, y2) in zip(wps, wps[1:-1]):
            self.assertGreaterEqual(abs(y2 - y1) + abs(x2 - x1), 0.39)


class BacktrackTest(unittest.TestCase):
    class World:
        """Pose sim: forward moves along heading; turns rotate (left +20°)."""

        def __init__(self, client, x, y, heading, fresh=lambda: True):
            self.client = client
            self.x, self.y, self.heading = x, y, heading
            self.fresh = fresh
            self._seen = 0

        def __call__(self):
            import math
            for e in self.client.log[self._seen:]:
                if e[0] != "nudge":
                    continue
                if e[1] == "left":
                    self.heading = ((self.heading + 20 + 180) % 360) - 180
                elif e[1] == "right":
                    self.heading = ((self.heading - 20 + 180) % 360) - 180
                elif e[1] == "forward":
                    self.x += 0.15 * math.cos(math.radians(self.heading))
                    self.y += 0.15 * math.sin(math.radians(self.heading))
            self._seen = len(self.client.log)
            return {"x": self.x, "y": self.y, "heading": self.heading,
                    "fresh": self.fresh()}

    def test_returns_home_every_nudge_cleared(self):
        c = NavClient()
        d = make_driver(c, max_steps=200, max_seconds=1e9)
        world = self.World(c, x=1.5, y=0.0, heading=0.0)
        trail = [[0.0, 0.0], [0.5, 0.0], [1.0, 0.0], [1.5, 0.0]]
        home = {"pose": {"x": 0.0, "y": 0.0, "heading": 0.0}, "trail_len": 1}
        cl = MarkedClearance(c)
        ok, rem, why = ad.backtrack(d, world, trail, home, clearance=cl)
        self.assertTrue(ok, why)
        self.assertLessEqual(rem, ad.ARRIVE_M + 0.05)
        self.assertTrue(clear_before_every_nudge(c, None))
        self.assertGreater(len(nudges(c)), 0)

    def test_blocked_midway_partial_report(self):
        c = NavClient()
        d = make_driver(c, max_steps=200, max_seconds=1e9)
        world = self.World(c, x=1.5, y=0.0, heading=0.0)
        trail = [[0.0, 0.0], [0.75, 0.0], [1.5, 0.0]]
        home = {"pose": {"x": 0.0, "y": 0.0, "heading": 0.0}, "trail_len": 1}
        # let the turnaround work, then block the floor
        cl = Clearance([True] * 12 + [False])
        ok, rem, why = ad.backtrack(d, world, trail, home, clearance=cl)
        self.assertFalse(ok)
        self.assertIn("blocked", why)
        self.assertGreater(rem, 0.0)

    def test_nonconverging_turn_never_followed_by_forward(self):
        # codex catch: turn_to_heading exhausting max_pulses without
        # converging must NOT be followed by a forward pulse
        c = NavClient()
        d = make_driver(c, max_steps=200, max_seconds=1e9)
        # 2°/pulse: 180° away can never converge within the pulse budget
        world = self.World(c, x=1.0, y=0.0, heading=180.0)

        class SlowWorld(self.World):
            def __call__(self2):
                import math
                for e in c.log[self2._seen:]:
                    if e[0] != "nudge":
                        continue
                    if e[1] == "left":
                        self2.heading = ((self2.heading + 2 + 180) % 360) - 180
                    elif e[1] == "right":
                        self2.heading = ((self2.heading - 2 + 180) % 360) - 180
                    elif e[1] == "forward":
                        self2.x += 0.15 * math.cos(math.radians(self2.heading))
                        self2.y += 0.15 * math.sin(math.radians(self2.heading))
                self2._seen = len(c.log)
                return {"x": self2.x, "y": self2.y,
                        "heading": self2.heading, "fresh": True}
        # facing AWAY from home (bearing 180°, heading 0°): with 2°/pulse
        # the alignment can never converge inside the pulse budget
        world = SlowWorld(c, x=1.0, y=0.0, heading=0.0)
        trail = [[0.0, 0.0], [1.0, 0.0]]
        home = {"pose": {"x": 0.0, "y": 0.0, "heading": 0.0}, "trail_len": 1}
        ok, rem, why = ad.backtrack(d, world, trail, home,
                                    clearance=Clearance())
        self.assertFalse(ok)
        self.assertIn("align", why)
        self.assertEqual([e for e in nudges(c) if e[1] == "forward"], [])

    def test_stale_pose_mid_drive_stops(self):
        c = NavClient()
        d = make_driver(c, max_steps=200, max_seconds=1e9)
        reads = {"n": 0}

        def freshness():
            reads["n"] += 1
            return reads["n"] < 4              # goes stale mid-run
        world = self.World(c, x=1.0, y=0.0, heading=180.0, fresh=freshness)
        trail = [[0.0, 0.0], [1.0, 0.0]]
        home = {"pose": {"x": 0.0, "y": 0.0, "heading": 0.0}, "trail_len": 1}
        ok, rem, why = ad.backtrack(d, world, trail, home,
                                    clearance=Clearance())
        self.assertFalse(ok)
        self.assertIn("stale", why)


class RaisingRover:
    """Any attribute access beyond .backend explodes — pins 'gate refuses
    BEFORE any rover call'."""
    backend = "rovercontrol"

    def __getattr__(self, name):
        raise AssertionError(f"rover touched before the gate: {name}")


class ToolGateTest(unittest.TestCase):
    def setUp(self):
        self._env = {k: os.environ.pop(k, None)
                     for k in ("ROVER_GO_ENABLE", "ROVER_FIND_ENABLE")}

    def tearDown(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_disabled_refuses_before_any_rover_call(self):
        for tool in ("rover_go_to", "rover_come_back"):
            out = ac.run_tool(RaisingRover(), None, tool, {"target": "x"})
            self.assertIn("ROVER_GO_ENABLE", out)

    def test_find_enable_alone_does_not_unlock_driving(self):
        os.environ["ROVER_FIND_ENABLE"] = "1"      # rotation-only consent
        for tool in ("rover_go_to", "rover_come_back"):
            out = ac.run_tool(RaisingRover(), None, tool, {"target": "x"})
            self.assertIn("ROVER_GO_ENABLE", out)

    def test_come_back_without_home_refuses(self):
        os.environ["ROVER_GO_ENABLE"] = "1"
        old = dict(ac._NAV_HOME)
        try:
            ac._NAV_HOME["pose"] = None
            ac._NAV_HOME["trail_len"] = None
            out = ac.run_tool(RaisingRover(), None, "rover_come_back", {})
            self.assertIn("haven't driven anywhere", out)
        finally:
            ac._NAV_HOME.update(old)


class RoutingPinTest(unittest.TestCase):
    def test_system_prompt_carries_the_travel_verb_rule(self):
        self.assertIn("MOTION ROUTING (strict)", ac.SYSTEM)
        self.assertIn("rover_go_to", ac.SYSTEM)
        self.assertIn("NEVER with a driving tool", ac.SYSTEM)

    def test_find_object_description_no_longer_claims_driving(self):
        class Stub:
            backend = "rovercontrol"
        tools = ac.build_tools(Stub(), None)
        by_name = {t["function"]["name"]: t["function"] for t in tools}
        self.assertNotIn("drives toward", by_name["rover_find_object"]["description"])
        self.assertIn("IN PLACE", by_name["rover_find_object"]["description"])
        self.assertIn("GO TO / DRIVE TO", by_name["rover_go_to"]["description"])
        self.assertIn("ROVER_GO_ENABLE", by_name["rover_go_to"]["description"])
        self.assertIn("come back", by_name["rover_come_back"]["description"])


if __name__ == "__main__":
    unittest.main()
