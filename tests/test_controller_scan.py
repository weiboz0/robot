"""Gamepad 3D-scan button (plan 025) — safety invariants on fakes, no hardware:
single-flight, wheel-motion interlock, e-stop/drive cancellation with NO gimbal
command after the cancel (not even the recenter), stitcher subprocess kill, and
the scan never touching the wheels."""
import json
import os
import sys
import tempfile
import time
import threading
import unittest

import rovercontrold as rc
from tests.test_controller import RecLink


class FakeHub:
    """latest_frame() with an optional per-call side effect (fires estop/drive
    mid-scan from inside the scan thread, like a racing operator input)."""
    def __init__(self, frame=b"\xff\xd8JPEG"):
        self.frame = frame
        self.calls = 0
        self.on_grab = None

    def latest_frame(self):
        self.calls += 1
        if self.on_grab:
            self.on_grab(self.calls)
        return self.frame


def make_scan_app(frame=b"\xff\xd8JPEG"):
    rover = rc.Rover()
    link = RecLink()
    rover.set_status(link, "")
    hub = FakeHub(frame)
    move = rc.Movement(rover)
    aim = rc.CameraAim(rover)
    tmp = tempfile.TemporaryDirectory()
    app = rc.App(rover, move, aim, hub, rc.Camera("off", "", 0, 0, 0), tmp.name)
    app._scan_tmp = tmp                 # keep the dir alive with the app
    app.scan_settle_s = 0               # tests: no gimbal settle wait
    return app, link


def wait_scan_end(app, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with app._pano_mu:
            if not app._scan_active and app.pano_state in ("done", "failed"):
                return app.pano_state
        time.sleep(0.01)
    raise AssertionError("scan did not finish")


def gimbal_lines(link):
    return [(i, l) for i, l in enumerate(link.all()) if '"T":133' in l]


class ScanHappyPathTest(unittest.TestCase):
    def test_sweeps_builds_and_recenters(self):
        app, link = make_scan_app()
        got = {}

        def builder(frames):
            got["frames"] = frames
            return True
        app.pano_builder = builder
        ok, why = app.start_scan()
        self.assertEqual((ok, why), (True, ""))
        self.assertEqual(wait_scan_end(app), "done")
        self.assertEqual(len(got["frames"]), 13)       # 2 rings ×6 + ceiling
        aims = gimbal_lines(link)
        self.assertEqual(len(aims), 14)                # 13 poses + recenter
        last = json.loads(aims[-1][1])
        self.assertEqual((last["X"], last["Y"]), (0, 0))   # recentered
        # the scan NEVER commands the wheels
        self.assertFalse(any('"T":1,' in l or l.endswith('"T":1}')
                             for l in link.all() if '"L"' in l))

    def test_second_press_refused_while_running_even_stitching(self):
        app, _ = make_scan_app()
        gate = threading.Event()

        def blocking_builder(frames):
            gate.wait(5)
            return True
        app.pano_builder = blocking_builder
        self.assertTrue(app.start_scan()[0])
        # wait until stitching (sweep is instant with settle 0)
        deadline = time.monotonic() + 5
        while app.pano_state != "stitching" and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(app.pano_state, "stitching")
        ok, why = app.start_scan()
        self.assertEqual((ok, why), (False, "scan already running"))
        # an external POST /pano_status write cannot defeat single-flight
        with app._pano_mu:
            app.pano_state = ""
        self.assertFalse(app.start_scan()[0])
        gate.set()
        wait_scan_end(app)


class ScanInterlockTest(unittest.TestCase):
    def test_refused_while_wheels_moving(self):
        app, _ = make_scan_app()
        app.move.set_drive(0.1, 0.1)
        ok, why = app.start_scan()
        self.assertEqual((ok, why), (False, "wheels are moving"))
        app.move.stop()
        self.assertTrue(app.start_scan()[0])
        wait_scan_end(app)

    def test_refused_during_inflight_nudge(self):
        app, _ = make_scan_app()
        app.move.nudge(1, 1, 0.05)
        self.assertTrue(app.move.is_moving())
        self.assertEqual(app.start_scan(), (False, "wheels are moving"))
        time.sleep(0.2)                                # nudge timer fires
        self.assertFalse(app.move.is_moving())

    def test_estop_mid_scan_stops_gimbal_no_recenter(self):
        app, link = make_scan_app()
        app.pano_builder = lambda frames: self.fail("builder must not run")
        app.hub.on_grab = lambda n: n == 2 and app.move.do_estop()
        self.assertTrue(app.start_scan()[0])
        self.assertEqual(wait_scan_end(app), "failed")
        lines = link.all()
        estop_at = max(i for i, l in enumerate(lines) if l == '{"T":0}')
        after = [l for l in lines[estop_at + 1:] if '"T":133' in l]
        self.assertEqual(after, [])        # ZERO gimbal commands after e-stop

    def test_drive_mid_scan_cancels(self):
        app, link = make_scan_app()
        app.pano_builder = lambda frames: self.fail("builder must not run")
        app.hub.on_grab = lambda n: n == 2 and app.move.set_drive(0.2, 0.2)
        self.assertTrue(app.start_scan()[0])
        self.assertEqual(wait_scan_end(app), "failed")
        lines = link.all()
        drive_at = max(i for i, l in enumerate(lines) if '"L":0.2' in l)
        self.assertEqual([l for l in lines[drive_at + 1:] if '"T":133' in l], [])

    def test_refused_while_estop_latched(self):
        app, _ = make_scan_app()
        app.move.do_estop()
        self.assertEqual(app.start_scan(), (False, "e-stopped"))
        app.move.stop()                    # releases the latch
        self.assertTrue(app.start_scan()[0])
        wait_scan_end(app)

    def test_estop_landing_around_clear_still_refused(self):
        # e-stop between the pre-check and clear() would have its cancel-event
        # set erased — the post-clear re-check must still refuse the scan
        app, _ = make_scan_app()
        calls = []

        def estop_lands_mid_start():
            calls.append(1)
            return len(calls) >= 2      # pre-check False, re-check True
        app.move.is_estopped = estop_lands_mid_start
        self.assertEqual(app.start_scan(), (False, "e-stopped"))
        with app._pano_mu:
            self.assertFalse(app._scan_active)   # slot released
        self.assertEqual(app.pano_state, "failed")

    def test_cancel_after_builder_success_discards_result(self):
        app, _ = make_scan_app()

        def builder(frames):
            app.move.do_estop()            # cancel lands during/after the build
            return True
        app.pano_builder = builder
        self.assertTrue(app.start_scan()[0])
        self.assertEqual(wait_scan_end(app), "failed")

    def test_cancel_after_subprocess_exit_not_published(self):
        app, _ = make_scan_app()
        out_holder = {}

        def cmd(d, o):
            out_holder["out"] = o
            return [sys.executable, "-c", f"open({o!r},'wb').write(b'PANO')"], None
        app.pano_build_cmd = cmd
        app._scan_cancel.set()             # cancel already set → discard
        self.assertFalse(app._build_pano_subprocess([(0, -5, b"x")]))
        self.assertFalse(
            os.path.exists(os.path.join(app.photo_dir, "panorama.jpg")))

    def test_no_frame_fails_cleanly(self):
        app, _ = make_scan_app(frame=None)
        app.pano_builder = lambda frames: self.fail("builder must not run")
        self.assertTrue(app.start_scan()[0])
        self.assertEqual(wait_scan_end(app), "failed")

    def test_estop_hook_only_on_estop_and_drive_hook_only_on_accepted(self):
        app, _ = make_scan_app()
        fired = []
        app.move.on_nonzero_drive = lambda: fired.append("drive")
        app.move.on_estop = lambda: fired.append("estop")
        app.move.do_estop()
        self.assertEqual(fired, ["estop"])
        app.move.set_drive(0.1, 0.1)                   # refused: latched
        self.assertEqual(fired, ["estop"])
        app.move.set_drive(0, 0)                       # releases latch, zero
        self.assertEqual(fired, ["estop"])
        app.move.set_drive(0.1, 0.1)                   # accepted nonzero
        self.assertEqual(fired, ["estop", "drive"])


class BuilderSubprocessTest(unittest.TestCase):
    def _app(self):
        app, _ = make_scan_app()
        return app

    def test_cmd_pin(self):
        app = self._app()
        argv, env = app.pano_build_cmd("/f", "/o.jpg")
        scene_py = os.path.join(os.path.dirname(os.path.abspath(rc.__file__)),
                                "scene.py")
        self.assertEqual(argv, ["nice", "-n", "10", sys.executable, scene_py,
                                "build-pano", "/f", "/o.jpg"])
        for k in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS",
                  "MKL_NUM_THREADS", "VECLIB_MAXIMUM_THREADS"):
            self.assertEqual(env[k], "1")

    def test_timeout_kills_group(self):
        app = self._app()
        app.scan_build_timeout = 0.4
        app.pano_build_cmd = lambda d, o: (
            [sys.executable, "-c", "import time; time.sleep(60)"], None)
        t0 = time.monotonic()
        self.assertFalse(app._build_pano_subprocess([(0, -5, b"x")]))
        self.assertLess(time.monotonic() - t0, 5)

    def test_cancel_kills_group(self):
        app = self._app()
        app.pano_build_cmd = lambda d, o: (
            [sys.executable, "-c", "import time; time.sleep(60)"], None)
        app._scan_cancel.set()
        t0 = time.monotonic()
        self.assertFalse(app._build_pano_subprocess([(0, -5, b"x")]))
        self.assertLess(time.monotonic() - t0, 5)

    def test_nonzero_exit_fails(self):
        app = self._app()
        app.pano_build_cmd = lambda d, o: (
            [sys.executable, "-c", "import sys; sys.exit(3)"], None)
        self.assertFalse(app._build_pano_subprocess([(0, -5, b"x")]))

    def test_success_publishes_atomically(self):
        app = self._app()
        real_cmd = app.pano_build_cmd

        def cmd(d, o):
            return [sys.executable, "-c",
                    f"open({o!r},'wb').write(b'PANO')"], None
        app.pano_build_cmd = cmd
        self.assertTrue(app._build_pano_subprocess([(0, -5, b"x")]))
        with open(os.path.join(app.photo_dir, "panorama.jpg"), "rb") as f:
            self.assertEqual(f.read(), b"PANO")
        del real_cmd


class ScanMappingTest(unittest.TestCase):
    def test_default_binding_and_key(self):
        m = rc.default_mapping()
        self.assertEqual(m["scan"], {"kind": "button", "index": 7})
        self.assertIn("scan", rc.CONTROL_KEYS)

    def test_edge_trigger(self):
        from tests.test_controller import FakeState
        m = rc.default_mapping()
        st = FakeState(buttons={7: True})
        prev = rc.GpPrev()
        self.assertTrue(rc.compute_joystick(m, st, prev)["scan"])
        self.assertFalse(rc.compute_joystick(m, st, prev)["scan"])  # held
        st._buttons[7] = False
        rc.compute_joystick(m, st, prev)
        st._buttons[7] = True
        self.assertTrue(rc.compute_joystick(m, st, prev)["scan"])   # re-press


if __name__ == "__main__":
    unittest.main()
