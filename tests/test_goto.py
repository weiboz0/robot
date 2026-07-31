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


class BlockedForwardVoidsSurveyTest(unittest.TestCase):
    def test_hazard_verdict_invalidates_cached_survey(self):
        # plan-037 Opus B1: a blocked forward must void the turn survey so
        # a following (detour) turn re-surveys instead of reusing views the
        # hazard verdict just contradicted
        c = NavClient()
        d = make_driver(c)
        cl = MarkedClearance(c, verdicts=[True] * 4 + [False] + [True])
        with d:
            self.assertTrue(d.turn_gated("left", 200, cl))   # survey + side
            self.assertFalse(d.forward(cl))                  # blocked (5th)
            n = cl.calls
            self.assertTrue(d.turn_gated("left", 200, cl))
        self.assertEqual(cl.calls, n + 4)   # FULL fresh survey, not reuse


class SearchAroundTest(unittest.TestCase):
    def _looker(self, driver, script):
        return lambda nm, img: script((driver._aim or (0, 0))[0])

    def test_target_behind_found_after_gated_rotations(self):
        c = NavClient()
        d = make_driver(c)
        cl = MarkedClearance(c)

        def script(pan):
            turns = len([e for e in c.log
                         if e[0] == "nudge" and e[1] == "left"])
            if turns >= 3 and pan == 0:
                return {"seen": True, "bbox": [0.4, 0.4, 0.6, 0.6],
                        "confidence": 0.9}
            return {"seen": False, "bbox": None, "confidence": 0.0}
        with d:
            best, why = ad.search_around(d, self._looker(d, script),
                                         lambda: (None, b"img"), cl)
        self.assertIsNotNone(best, why)
        self.assertEqual(why, "sighted")
        self.assertEqual(len(nudges(c)), 3)          # exactly 3 rotations
        self.assertTrue(clear_before_every_nudge(c, None))

    def test_dirty_turn_view_stops_search_zero_nudges(self):
        c = NavClient()
        d = make_driver(c)
        cl = MarkedClearance(c, verdicts=[False])    # nothing is clear
        never = lambda nm, img: {"seen": False, "bbox": None,
                                 "confidence": 0.0}
        with d:
            best, why = ad.search_around(d, never,
                                         lambda: (None, b"img"), cl)
        self.assertIsNone(best)
        self.assertIn("look further around", why)
        self.assertEqual(nudges(c), [])

    def test_phase_cap_bites_and_leaves_wall_budget(self):
        t = {"now": 0.0}
        c = NavClient()
        d = make_driver(c, clock=lambda: t["now"], max_seconds=480.0)

        def slow_looker(nm, img):
            t["now"] += 90.0                          # slow vision looks
            return {"seen": False, "bbox": None, "confidence": 0.0}
        with d:
            best, why = ad.search_around(d, slow_looker,
                                         lambda: (None, b"img"),
                                         MarkedClearance(c))
        self.assertIsNone(best)
        self.assertIn("time", why)
        self.assertLess(t["now"], 480.0)              # approach budget left

    def test_phase_cap_enforced_mid_sweep_rejects_post_cap_sighting(self):
        # codex code-review catch: the deadline must be checked before
        # EVERY look — a sighting the sweep would only reach after the cap
        # is never accepted
        t = {"now": 0.0}
        c = NavClient()
        d = make_driver(c, clock=lambda: t["now"], max_seconds=1e9)

        def looker(nm, img):
            # 90 s/look: look 3 STARTS in-budget (t=180) but its sighting
            # lands post-cap (t=270) — must be discarded, not accepted
            t["now"] += 90.0
            pan = (d._aim or (0, 0))[0]
            if pan == 50:
                return {"seen": True, "bbox": [0.4, 0.4, 0.6, 0.6],
                        "confidence": 0.9}
            return {"seen": False, "bbox": None, "confidence": 0.0}
        with d:
            best, why = ad.search_around(d, looker,
                                         lambda: (None, b"img"),
                                         MarkedClearance(c))
        self.assertIsNone(best)                 # …but the cap is authoritative
        self.assertIn("time", why)

    def test_never_sighted_exhausts_rotations_honest_why(self):
        c = NavClient()
        d = make_driver(c)
        never = lambda nm, img: {"seen": False, "bbox": None,
                                 "confidence": 0.0}
        with d:
            best, why = ad.search_around(d, never,
                                         lambda: (None, b"img"),
                                         MarkedClearance(c))
        self.assertIsNone(best)
        self.assertIn("without seeing it", why)
        self.assertEqual(len(nudges(c)), 5)           # rotation cap


class AimClearance:
    """Clearance whose verdict depends on the CURRENT camera aim: the
    forward path (pan 0 at floor tilt) consumes a scripted queue; all
    other views (probes, survey, side checks) are clear."""

    def __init__(self, driver, forward_verdicts):
        self.driver = driver
        self.q = list(forward_verdicts)
        self.calls = 0

    def __call__(self):
        self.calls += 1
        self.driver.c.log.append(("clear",))
        pan, tilt = self.driver._aim or (0.0, 0.0)
        if pan == 0.0 and tilt == self.driver.floor_tilt:
            return self.q.pop(0) if self.q else True
        return True


class DetourTest(unittest.TestCase):
    def _approach(self, d, script, cl, detours=ad.DETOUR_MAX):
        return ad.approach_object(
            d, None, "bin", capture=lambda: (None, b"img"),
            look=lambda nm, img: script((d._aim or (0, 0))[0]),
            clearance=cl, detours=detours)

    def _target_script(self, c):
        """Centered-not-close until one forward lands post-detour, then
        close. Off-view at pan 0 right after the detour turn; visible at
        the reacquire sweep's biased first look."""
        def script(pan):
            fwds = len([e for e in c.log
                        if e[0] == "nudge" and e[1] == "forward"])
            if fwds >= 2:
                if abs(pan) <= 55:
                    return {"seen": True, "bbox": [0.3, 0.3, 0.7, 0.8],
                            "close": True, "confidence": 0.9}
                return {"seen": False, "bbox": None, "confidence": 0.0}
            if pan == 0:
                return {"seen": True, "bbox": [0.45, 0.4, 0.55, 0.5],
                        "close": False, "confidence": 0.9}
            return {"seen": False, "bbox": None, "confidence": 0.0}
        return script

    def test_static_obstacle_dead_ahead_stops_honestly(self):
        # glm review catch: the detour's fresh survey re-checks the SAME
        # view that just blocked — a static obstacle re-fails it and the
        # honest outcome is "path blocked while turning", zero turn nudges
        c = NavClient()
        d = make_driver(c)
        # forward view stays blocked forever (static hazard)
        cl = AimClearance(d, forward_verdicts=[False])

        class StaticBlock(AimClearance):
            def __call__(self):
                self.calls += 1
                self.driver.c.log.append(("clear",))
                pan, tilt = self.driver._aim or (0.0, 0.0)
                if pan == 0.0 and tilt == self.driver.floor_tilt:
                    return False          # dead ahead: blocked, stays blocked
                return True
        cl = StaticBlock(d, [])

        def script(pan):
            if pan == 0:
                return {"seen": True, "bbox": [0.45, 0.4, 0.55, 0.5],
                        "close": False, "confidence": 0.9}
            return {"seen": False, "bbox": None, "confidence": 0.0}
        ok, obs, why = self._approach(d, script, cl)
        self.assertFalse(ok)
        self.assertIn("blocked while turning", why)
        self.assertEqual([e for e in nudges(c)
                          if e[1] in ("left", "right")], [])

    def test_transient_obstacle_detour_goes_around_and_arrives(self):
        # the case detours genuinely help: the hazard MOVED between the
        # block and the survey re-check (verdict legitimately flips) —
        # e.g. the cat wandered off
        c = NavClient()
        d = make_driver(c, max_steps=100)
        cl = AimClearance(d, forward_verdicts=[False, True, True, True])
        ok, obs, why = self._approach(d, self._target_script(c), cl)
        self.assertTrue(ok, why)
        dirs = [e[1] for e in nudges(c)]
        self.assertIn("forward", dirs)
        self.assertTrue(any(x in ("left", "right") for x in dirs))
        # ordering: after the blocked forward's False verdict, the next
        # turn nudge must be preceded by a FULL fresh 3-view survey —
        # find the block (first "clear" that returned False is the first
        # forward check) then count clears before the next turn nudge
        self.assertTrue(clear_before_every_nudge(c, None))
        # the detour turn's survey re-ran: aims contain the -40/0/+40
        # triple AFTER the first forward nudge attempt window
        aims = [e[1] for e in c.log if e[0] == "aim"]
        self.assertGreaterEqual(aims.count(-40.0) + aims.count(40.0), 4)

    def test_boxed_in_no_nudges_after_block(self):
        c = NavClient()
        d = make_driver(c)

        class AllDirty(AimClearance):
            def __call__(self):
                self.calls += 1
                self.driver.c.log.append(("clear",))
                pan, tilt = self.driver._aim or (0.0, 0.0)
                if tilt == self.driver.floor_tilt:
                    return False               # every floor view dirty
                return True
        cl = AllDirty(d, [])

        def script(pan):
            if pan == 0:
                return {"seen": True, "bbox": [0.45, 0.4, 0.55, 0.5],
                        "close": False, "confidence": 0.9}
            return {"seen": False, "bbox": None, "confidence": 0.0}
        ok, obs, why = self._approach(d, script, cl)
        self.assertFalse(ok)
        self.assertIn("boxed in", why)
        self.assertEqual(nudges(c), [])

    def test_detours_zero_keeps_v1_wording(self):
        c = NavClient()
        d = make_driver(c)
        cl = AimClearance(d, forward_verdicts=[False])

        def script(pan):
            if pan == 0:
                return {"seen": True, "bbox": [0.45, 0.4, 0.55, 0.5],
                        "close": False, "confidence": 0.9}
            return {"seen": False, "bbox": None, "confidence": 0.0}
        ok, obs, why = self._approach(d, script, cl, detours=0)
        self.assertFalse(ok)
        self.assertEqual(why, "path blocked ahead")

    def test_detour_budget_exhausts_honestly(self):
        c = NavClient()
        d = make_driver(c, max_steps=200)
        # pan-0 floor views per detour cycle: block(F), survey-center(T),
        # detour-forward(T); after 3 cycles the 4th block exhausts the
        # budget (the fake can't tell a forward check from the survey's
        # center view — same aim — so the queue scripts all of them)
        cl = AimClearance(d,
                          forward_verdicts=[False, True, True] * 3 + [False])

        def script(pan):
            if pan == 0:      # sighted straight ahead only — no align turns
                return {"seen": True, "bbox": [0.45, 0.4, 0.55, 0.5],
                        "close": False, "confidence": 0.9}
            return {"seen": False, "bbox": None, "confidence": 0.0}
        ok, obs, why = self._approach(d, script, cl)
        self.assertFalse(ok)
        self.assertIn("out of detour attempts", why)

    def test_probe_advisory_gate_authoritative(self):
        c = NavClient()
        d = make_driver(c)

        class ProbeLiar(AimClearance):
            """Probes clear, but the turn's own survey/side checks dirty."""
            def __call__(self):
                self.calls += 1
                self.driver.c.log.append(("clear",))
                pan, tilt = self.driver._aim or (0.0, 0.0)
                if pan == 0.0 and tilt == self.driver.floor_tilt:
                    return self.q.pop(0) if self.q else True
                # dirty from the moment the turn survey starts (after the
                # two probe looks)
                probes = len([e for e in self.driver.c.log
                              if e[0] == "clear"])
                return probes <= 3        # 1 fwd check + 2 probes pass
        cl = ProbeLiar(d, forward_verdicts=[False])

        def script(pan):
            if pan == 0:
                return {"seen": True, "bbox": [0.45, 0.4, 0.55, 0.5],
                        "close": False, "confidence": 0.9}
            return {"seen": False, "bbox": None, "confidence": 0.0}
        ok, obs, why = self._approach(d, script, cl)
        self.assertFalse(ok)
        self.assertIn("blocked while turning", why)
        self.assertEqual(nudges(c), [])   # probe said yes, gate said NO

    def test_tie_break_toward_target(self):
        for cx, want in ((0.42, "left"), (0.58, "right")):
            c = NavClient()
            d = make_driver(c, max_steps=100)
            cl = AimClearance(d, forward_verdicts=[False, True])

            def script(pan, cx=cx):
                fwds = len([e for e in c.log
                            if e[0] == "nudge" and e[1] == "forward"])
                if fwds >= 1 and abs(pan) <= 55:
                    return {"seen": True, "bbox": [0.3, 0.3, 0.7, 0.8],
                            "close": True, "confidence": 0.9}
                if pan == 0:
                    return {"seen": True,
                            "bbox": [cx - 0.05, 0.4, cx + 0.05, 0.5],
                            "close": False, "confidence": 0.9}
                return {"seen": False, "bbox": None, "confidence": 0.0}
            ok, obs, why = self._approach(d, script, cl)
            first_turn = next(e for e in nudges(c)
                              if e[1] in ("left", "right"))
            self.assertEqual(first_turn[1], want, (cx, why))

    def test_reacquire_miss_stops_honestly(self):
        c = NavClient()
        d = make_driver(c, max_steps=100)
        cl = AimClearance(d, forward_verdicts=[False, True])
        state = {"gone": False}

        def script(pan):
            fwds = len([e for e in c.log
                        if e[0] == "nudge" and e[1] == "forward"])
            if fwds >= 1:
                return {"seen": False, "bbox": None,   # vanished for good
                        "confidence": 0.0}
            if pan == 0:
                return {"seen": True, "bbox": [0.45, 0.4, 0.55, 0.5],
                        "close": False, "confidence": 0.9}
            return {"seen": False, "bbox": None, "confidence": 0.0}
        ok, obs, why = self._approach(d, script, cl)
        self.assertFalse(ok)
        self.assertIn("lost sight", why)


class MotionTypedGateTest(unittest.TestCase):
    """Plan 039: forward pulses judge the drive strip; turns keep
    full-frame strictness; animals veto anywhere under both."""

    def test_floor_prompt_pins(self):
        p = ad.FLOOR_PROMPT
        self.assertIn("ANYWHERE", p)                  # animal rule
        self.assertIn("ANY animal", p)
        self.assertIn("CENTER DRIVE STRIP", p)
        self.assertIn("OUTSIDE the strip", p)         # ignore side clutter
        self.assertIn("If unsure", p)
        self.assertIn("overhang", p)                  # Opus's hazard class
        self.assertIn("EDGE", p)
        # the old whole-frame veto line must be gone
        self.assertNotIn("NO object, person, foot, wall", p)

    def test_turn_prompt_pins(self):
        p = ad.TURN_PROMPT
        self.assertIn("rotate IN PLACE", p)
        self.assertIn("ANYWHERE", p)                  # animal rule
        self.assertIn("30 cm", p)                     # baseline strictness
        self.assertIn("ANY doubt", p)

    class RecordingVision:
        def __init__(self, verdict=None):
            self.prompts = []
            self.verdict = verdict or {"clear": True, "confidence": 0.9,
                                       "hazard": ""}

        def describe(self, img, prompt, **kw):
            self.prompts.append(prompt)
            return dict(self.verdict)

    def test_forward_uses_floor_prompt_turn_uses_turn_prompt(self):
        c = NavClient()
        d = make_driver(c)
        vm = self.RecordingVision()
        cl = ad.make_llm_clearance(vm, lambda: (None, b"img"), driver=d)
        with d:
            self.assertTrue(d.forward(cl))
            self.assertEqual(vm.prompts, [ad.FLOOR_PROMPT])
            vm.prompts.clear()
            self.assertTrue(d.turn_gated("left", 200, cl))
        # survey (3) + side check (1): ALL turn-strict
        self.assertEqual(vm.prompts, [ad.TURN_PROMPT] * 4)

    def test_context_transitions(self):
        c = NavClient()
        d = make_driver(c)
        cl = Clearance()
        with d:
            d.turn_gated("left", 200, cl)
            self.assertEqual(d.motion_context, "turn")
            d.forward(cl)
            self.assertEqual(d.motion_context, "forward")

    def test_verdict_logging_and_fail_paths(self):
        lines = []
        vm = self.RecordingVision({"clear": False, "confidence": 0.95,
                                   "hazard": "pen on floor"})
        ok = ad.floor_is_clear(vm, b"img", log=lines.append)
        self.assertFalse(ok)
        self.assertIn("clear=False", lines[0])
        self.assertIn("pen on floor", lines[0])

        class Boom:
            def describe(self, *a, **k):
                raise RuntimeError("gateway down")
        lines.clear()
        self.assertFalse(ad.floor_is_clear(Boom(), b"img",
                                           log=lines.append))
        self.assertIn("fail closed", lines[0])

    def test_low_confidence_still_vetoes(self):
        vm = self.RecordingVision({"clear": True, "confidence": 0.3,
                                   "hazard": ""})
        self.assertFalse(ad.floor_is_clear(vm, b"img"))

    def test_error_only_retries_transient_timeout_recovers(self):
        # plan 039 addendum: a gateway timeout must not consume the block
        # path — fresh frame + retry; a real verdict is never retried
        calls = {"n": 0}

        class FlakyVision:
            def describe(self, img, prompt, **kw):
                calls["n"] += 1
                if calls["n"] == 1:
                    raise TimeoutError("Request timed out.")
                return {"clear": True, "confidence": 0.9, "hazard": ""}
        captures = {"n": 0}

        def capture():
            captures["n"] += 1
            return None, b"img"
        lines = []
        cl = ad.make_llm_clearance(FlakyVision(), capture,
                                   log=lines.append)
        self.assertTrue(cl())                       # recovered on retry
        self.assertEqual(calls["n"], 2)
        self.assertEqual(captures["n"], 2)          # FRESH frame each try
        self.assertTrue(any("retrying after error" in l for l in lines))

    def test_bad_shape_output_is_retryable_then_recovers(self):
        calls = {"n": 0}

        class BadThenGood:
            def describe(self, img, prompt, **kw):
                calls["n"] += 1
                if calls["n"] == 1:
                    return "not a dict"             # malformed → retryable
                return {"clear": True, "confidence": 0.9, "hazard": ""}
        cl = ad.make_llm_clearance(BadThenGood(), lambda: (None, b"img"))
        self.assertTrue(cl())
        self.assertEqual(calls["n"], 2)

    def test_persistent_error_fails_closed_after_retries(self):
        calls = {"n": 0}

        class DeadVision:
            def describe(self, img, prompt, **kw):
                calls["n"] += 1
                raise TimeoutError("down")
        cl = ad.make_llm_clearance(DeadVision(), lambda: (None, b"img"))
        self.assertFalse(cl())
        self.assertEqual(calls["n"], 1 + ad.CLEARANCE_ERROR_RETRIES)

    def test_real_not_clear_verdict_never_retried(self):
        calls = {"n": 0}

        class NoVision:
            def describe(self, img, prompt, **kw):
                calls["n"] += 1
                return {"clear": False, "confidence": 0.95,
                        "hazard": "cat"}
        cl = ad.make_llm_clearance(NoVision(), lambda: (None, b"img"))
        self.assertFalse(cl())
        self.assertEqual(calls["n"], 1)             # instant stop, no retry

    def test_slow_gate_never_nudges_past_the_time_cap(self):
        # Opus review: a slow clearance (vision retries) returning True
        # AFTER the cap must raise, never nudge (the watchdog may already
        # have e-stopped)
        t = {"now": 0.0}
        c = NavClient()
        d = make_driver(c, clock=lambda: t["now"], max_seconds=100.0)

        def slow_clear():
            t["now"] += 200.0                # gate outlives the cap
            return True
        with d:
            with self.assertRaises(ad.SafetyLimit):
                d.forward(slow_clear)
        self.assertEqual(nudges(c), [])
        c2 = NavClient()
        d2 = make_driver(c2, clock=lambda: t["now"], max_seconds=100.0)
        t["now"] = 0.0
        with d2:
            with self.assertRaises(ad.SafetyLimit):
                d2.turn_gated("left", 200, slow_clear)
        self.assertEqual(nudges(c2), [])

    def test_prompt_override_param(self):
        vm = self.RecordingVision()
        ad.floor_is_clear(vm, b"img", prompt=ad.TURN_PROMPT)
        self.assertEqual(vm.prompts, [ad.TURN_PROMPT])

    def test_detour_probes_are_turn_strict(self):
        # the probes judge a corridor the rover would TURN into
        c = NavClient()
        d = make_driver(c)
        vm = self.RecordingVision()
        seen_ctx = []

        class CtxClearance:
            def __call__(self2):
                seen_ctx.append(d.motion_context)
                c.log.append(("clear",))
                return False                # block everything: probes run,
                                            # then boxed-in stop
        cl = CtxClearance()

        def script(pan):
            if pan == 0:
                return {"seen": True, "bbox": [0.45, 0.4, 0.55, 0.5],
                        "close": False, "confidence": 0.9}
            return {"seen": False, "bbox": None, "confidence": 0.0}
        ok, obs, why = ad.approach_object(
            d, None, "bin", capture=lambda: (None, b"img"),
            look=lambda nm, img: script((d._aim or (0, 0))[0]),
            clearance=cl, detours=3)
        self.assertFalse(ok)
        # first check is the forward gate; the two probes after the block
        # are turn-context — exact list, so a vanished probe can't pass
        self.assertEqual(seen_ctx, ["forward", "turn", "turn"])


class PreferCenterSightingTest(unittest.TestCase):
    """Plan 039 addendum 2: the approach search prefers the most CENTERED
    confident sighting — a peripheral early-accept committed the rover to
    a body turn toward real furniture when the target was also dead
    ahead."""

    def _sweep(self, script, prefer_center):
        c = NavClient()
        d = make_driver(c)
        with d:
            err = {"n": 0}
            return ad._sweep_for(
                d, lambda nm, img: script((d._aim or (0, 0))[0]),
                lambda: (None, b"img"), err=err,
                prefer_center=prefer_center)

    def test_centered_lower_conf_beats_peripheral_higher_conf(self):
        def script(pan):
            if pan == -50:
                return {"seen": True, "bbox": [0.4, 0.4, 0.6, 0.6],
                        "confidence": 0.85}       # early-accept threshold!
            if pan == 0:
                return {"seen": True, "bbox": [0.4, 0.4, 0.6, 0.6],
                        "confidence": 0.7}
            return {"seen": False, "bbox": None, "confidence": 0.0}
        state, best = self._sweep(script, prefer_center=True)
        self.assertEqual(state, "found")
        self.assertEqual(best[2], 0)              # CENTERED sighting wins
        # legacy mode: raw confidence wins, early-accept at -50
        state, best = self._sweep(script, prefer_center=False)
        self.assertEqual(best[2], -50)

    def test_early_accept_only_when_centered(self):
        looks = {"n": 0}

        def script(pan):
            looks["n"] += 1
            if pan == -50:
                return {"seen": True, "bbox": [0.4, 0.4, 0.6, 0.6],
                        "confidence": 0.95}       # very strong, peripheral
            return {"seen": False, "bbox": None, "confidence": 0.0}
        state, best = self._sweep(script, prefer_center=True)
        # did NOT early-accept at -50: the whole sweep ran (3 search pans
        # would be the coarse grid; the default grid here is 10 looks)
        self.assertGreater(looks["n"], 1)
        self.assertEqual(best[2], -50)            # still found, just later

    def test_early_accept_returns_most_centered_not_trigger(self):
        # glm review: with SEARCH pans (-50, 0, 50) both centered-ish
        # sightings possible — the return must be the most-centered seen,
        # even when a later stronger one triggers the early accept
        c = NavClient()
        d = make_driver(c)

        def script(pan):
            if pan == 0:
                return {"seen": True, "bbox": [0.4, 0.4, 0.6, 0.6],
                        "confidence": 0.6}        # weak but dead-center
            if pan == 5:                          # hypothetical near-center
                return {"seen": True, "bbox": [0.4, 0.4, 0.6, 0.6],
                        "confidence": 0.95}
            return {"seen": False, "bbox": None, "confidence": 0.0}
        with d:
            err = {"n": 0}
            state, best = ad._sweep_for(
                d, lambda nm, img: script((d._aim or (0, 0))[0]),
                lambda: (None, b"img"), err=err,
                sweep_pans=(0, 5), sweep_tilts=(-18,),
                prefer_center=True)
        self.assertEqual(state, "found")
        self.assertEqual(best[2], 0)              # most centered, not trigger

    def test_centered_strong_sighting_early_accepts(self):
        looks = {"n": 0}

        def script(pan):
            looks["n"] += 1
            if pan == 0:
                return {"seen": True, "bbox": [0.4, 0.4, 0.6, 0.6],
                        "confidence": 0.95}
            return {"seen": False, "bbox": None, "confidence": 0.0}
        state, best = self._sweep(script, prefer_center=True)
        self.assertEqual(best[2], 0)
        self.assertLess(looks["n"], 10)           # stopped at the sighting


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
        # plan 037: honest coverage + honest detour wording (glm catch: no
        # "steers around obstacles" overclaim — static blocks stop it)
        desc = by_name["rover_go_to"]["description"]
        self.assertIn("approximately a full circle", desc)
        self.assertIn("as time allows", desc)
        self.assertIn("try to go around", desc)
        self.assertIn("fixed obstacle dead ahead stops it", desc)
        self.assertNotIn("guaranteed", desc)
        self.assertNotIn("steering around obstacles", desc)

    def test_go_budget_split_pinned(self):
        # plan 037: search capped at half the wall budget
        self.assertEqual(ac.GO_MAX_STEPS, 80)
        self.assertEqual(ac.GO_MAX_SECONDS, 480.0)
        import autodrive as ad2
        self.assertEqual(ad2.SEARCH_PHASE_S, 240.0)
        self.assertLessEqual(ad2.SEARCH_PHASE_S, ac.GO_MAX_SECONDS / 2)


if __name__ == "__main__":
    unittest.main()
