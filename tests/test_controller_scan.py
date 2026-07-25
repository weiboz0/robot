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
    app.identify_builder = None         # tests must never spawn the LLM step
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

    def test_cancel_scan_aborts_and_discards(self):
        app, link = make_scan_app()
        app.pano_builder = lambda frames: self.fail("builder must not run")
        app.hub.on_grab = lambda n: n == 2 and app.cancel_scan()
        self.assertTrue(app.start_scan()[0])
        self.assertEqual(wait_scan_end(app), "failed")
        # no gimbal command (incl. recenter) after the cancel took effect
        lines = link.all()
        aims = [i for i, l in enumerate(lines) if '"T":133' in l]
        self.assertLess(len(aims), 15)             # aborted before completion

    def test_cancel_scan_idle_returns_false_and_leaves_event_clear(self):
        app, _ = make_scan_app()
        self.assertFalse(app.cancel_scan())
        self.assertFalse(app._scan_cancel.is_set())   # contract: no side effect

    def test_cancel_during_stitching_kills_builder(self):
        app, _ = make_scan_app()
        gate = threading.Event()

        def blocking_builder(frames):
            # simulate the subprocess loop: watch the cancel event
            cancelled = app._scan_cancel.wait(5)
            gate.set()
            return not cancelled
        app.pano_builder = blocking_builder
        self.assertTrue(app.start_scan()[0])
        deadline = time.monotonic() + 5
        while app.pano_state != "stitching" and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(app.cancel_scan())
        self.assertTrue(gate.wait(5))
        self.assertEqual(wait_scan_end(app), "failed")

    def test_cancel_after_publish_point_refused(self):
        # publish won the race: a later cancel must 409, never lie with a 200
        app, _ = make_scan_app()
        gate = threading.Event()

        def builder(frames):
            self.assertTrue(app._mark_published())   # point of no return
            self.assertFalse(app.cancel_scan())      # _scan_active still True
            gate.set()
            return True
        app.pano_builder = builder
        self.assertTrue(app.start_scan()[0])
        self.assertTrue(gate.wait(5))
        self.assertEqual(wait_scan_end(app), "done")  # published, not failed

    def test_estop_during_identify_window_keeps_done(self):
        # the identify phase can run minutes after publish — a drive/e-stop
        # event then must not flip the (correct) done state to failed
        app, _ = make_scan_app()

        def builder(frames):
            self.assertTrue(app._mark_published())       # pano published
            app.move.do_estop()                          # event lands later
            return True
        app.pano_builder = builder
        self.assertTrue(app.start_scan()[0])
        self.assertEqual(wait_scan_end(app), "done")     # not "failed"

    def test_cancel_before_publish_point_discards(self):
        # cancel won the race: _mark_published must refuse the publish
        app, _ = make_scan_app()

        def builder(frames):
            self.assertTrue(app.cancel_scan())
            self.assertFalse(app._mark_published())   # too late to publish
            return False
        app.pano_builder = builder
        self.assertTrue(app.start_scan()[0])
        self.assertEqual(wait_scan_end(app), "failed")

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


class IdentifyArchivedTest(unittest.TestCase):
    def _app_with_scan(self, name="scan_20260716_120000.jpg"):
        app, _ = make_scan_app()
        os.makedirs(app.scans_dir, exist_ok=True)
        with open(os.path.join(app.scans_dir, name), "wb") as f:
            f.write(b"\xff\xd8PANO")
        return app, name

    def _wait_flag_clear(self, app, timeout=8):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with app._pano_mu:
                if not app._ident_busy:
                    return
            time.sleep(0.02)
        raise AssertionError("identify flag never released")

    def test_worker_writes_sidecar_and_live_when_newest(self):
        app, name = self._app_with_scan()
        app.identify_pano_cmd = lambda p, o, f: (
            [sys.executable, "-c",
             f"import json;json.dump({{'objects':[{{'name':'books','lon':1,"
             f"'lat':2,'w':3,'h':4}}],'made':'t1'}}, open({o!r},'w'))"], None)
        ok, why = app.start_scan_identify(name)
        self.assertTrue(ok, why)
        self._wait_flag_clear(app)
        sidecar = os.path.join(app.scans_dir, name + ".meta.json")
        live = os.path.join(app.photo_dir, "panorama.meta.json")
        self.assertTrue(os.path.exists(sidecar))
        self.assertEqual(os.stat(sidecar).st_ino, os.stat(live).st_ino)

    def test_commit_race_older_name_writes_sidecar_only(self):
        # a NEWER scan archives while the LLM runs → live meta untouched
        app, old = self._app_with_scan("scan_20260716_110000.jpg")
        with open(os.path.join(app.scans_dir, "scan_20260716_115000.jpg"), "wb") as f:
            f.write(b"\xff\xd8NEWER")
        live = os.path.join(app.photo_dir, "panorama.meta.json")
        os.makedirs(app.photo_dir, exist_ok=True)
        with open(live, "w") as f:
            f.write('{"objects":[],"made":"newer-scan"}')
        app.identify_pano_cmd = lambda p, o, f: (
            [sys.executable, "-c",
             f"import json;json.dump({{'objects':[{{'name':'old','lon':0,"
             f"'lat':0,'w':1,'h':1}}],'made':'old'}}, open({o!r},'w'))"], None)
        ok, _ = app.start_scan_identify(old)
        self.assertTrue(ok)
        self._wait_flag_clear(app)
        self.assertTrue(os.path.exists(
            os.path.join(app.scans_dir, old + ".meta.json")))
        with open(live) as f:                       # live still the newer scan's
            self.assertIn("newer-scan", f.read())

    def test_single_flight_between_paths(self):
        app, name = self._app_with_scan()
        gate = threading.Event()

        def slow_cmd(p, o, f):
            return [sys.executable, "-c", "import time; time.sleep(5)"], None
        app.identify_pano_cmd = slow_cmd
        self.assertTrue(app.start_scan_identify(name)[0])
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:          # wait until flag held
            with app._pano_mu:
                if app._ident_busy:
                    break
            time.sleep(0.01)
        ok, why = app.start_scan_identify(name)     # archived vs archived
        self.assertFalse(ok)
        self.assertIn("running", why)
        # scan-time path SKIPS (never blocks) while the flag is held
        t0 = time.monotonic()
        app.identify_builder = lambda d, m: (
            [sys.executable, "-c", "pass"], None)
        app._identify_frames([(0, -5, b"x")], None)
        self.assertLess(time.monotonic() - t0, 1.0)  # skipped, not queued
        self.assertFalse(os.path.exists(
            os.path.join(app.photo_dir, "panorama.meta.json")))
        with app._pano_mu:                           # kill the slow worker
            if app._identify_proc is not None:
                import os as _os
                import signal as _sig
                try:
                    _os.killpg(app._identify_proc.pid, _sig.SIGKILL)
                except OSError:
                    pass
        self._wait_flag_clear(app)
        del gate

    def test_stale_scan_cancel_does_not_kill_archived_identify(self):
        # codex catch: drive/e-stop set _scan_cancel and nothing clears it
        # until the next scan — an archived identify must ignore it
        app, name = self._app_with_scan()
        app._scan_cancel.set()                       # stale event from a drive
        app.identify_pano_cmd = lambda p, o, f: (
            [sys.executable, "-c",
             "import time; time.sleep(0.5); "
             f"import json;json.dump({{'objects':[{{'name':'x','lon':0,"
             f"'lat':0,'w':1,'h':1}}],'made':'t'}}, open({o!r},'w'))"], None)
        self.assertTrue(app.start_scan_identify(name)[0])
        self._wait_flag_clear(app)
        self.assertTrue(os.path.exists(
            os.path.join(app.scans_dir, name + ".meta.json")))   # NOT killed

    def test_flag_released_when_mkdtemp_fails(self):
        import tempfile as _tf
        app, _ = make_scan_app()
        orig = _tf.mkdtemp

        def boom(*a, **k):
            raise OSError("disk full")
        _tf.mkdtemp = boom
        try:
            app.identify_builder = lambda d, m: ([sys.executable, "-c", "pass"], None)
            app._identify_frames([(0, -5, b"x")], None)  # must not raise or wedge
        finally:
            _tf.mkdtemp = orig
        with app._pano_mu:
            self.assertFalse(app._ident_busy)        # flag released

    def test_flag_released_on_worker_failure(self):
        app, name = self._app_with_scan()
        app.identify_pano_cmd = lambda p, o, f: (
            [sys.executable, "-c", "import sys; sys.exit(1)"], None)
        self.assertTrue(app.start_scan_identify(name)[0])
        self._wait_flag_clear(app)                   # released despite no meta
        self.assertTrue(app.start_scan_identify(name)[0])   # reusable
        self._wait_flag_clear(app)

    def test_missing_scan_404_reason(self):
        app, _ = make_scan_app()
        ok, why = app.start_scan_identify("scan_20990101_000000.jpg")
        self.assertFalse(ok)
        self.assertIn("no such scan", why)

    def test_focus_reaches_cmd(self):
        app, name = self._app_with_scan()
        seen = {}

        def cmd(p, o, f):
            seen["focus"] = f
            return [sys.executable, "-c", "pass"], None
        app.identify_pano_cmd = cmd
        self.assertTrue(app.start_scan_identify(name, "stack of books")[0])
        self._wait_flag_clear(app)
        self.assertEqual(seen["focus"], "stack of books")

    def test_identify_pano_cmd_pin(self):
        app, _ = make_scan_app()
        argv, _ = app.identify_pano_cmd("/s.jpg", "/m.json", "books")
        self.assertEqual(argv[:3], ["nice", "-n", "10"])
        self.assertIn("scene.py", argv[4])
        self.assertEqual(argv[5:], ["identify-pano", "/s.jpg", "/m.json", "books"])


class ScanPoseStampTest(unittest.TestCase):
    """Plan 032: archived scans carry the pose captured at scan START; the
    identify publish merges (never clobbers or strands) it; re-identify
    carries it over; the archive name binds by parameter, not singleton."""

    def _wait_flag_clear(self, app, timeout=8):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with app._pano_mu:
                if not app._ident_busy:
                    return
            time.sleep(0.02)
        raise AssertionError("identify flag never released")

    def _set_pose(self, app, x, y, heading):
        with app.pose._mu:
            app.pose.x, app.pose.y, app.pose.heading = x, y, heading

    def _put_scan(self, app, name):
        os.makedirs(app.scans_dir, exist_ok=True)
        with open(os.path.join(app.scans_dir, name), "wb") as f:
            f.write(b"\xff\xd8PANO")

    def test_sidecar_pose_is_scan_start_pose(self):
        app, _ = make_scan_app()
        self._set_pose(app, 1.25, -0.5, 90.0)

        def builder(frames):
            # pose "drifts" after scan start — the stamp must NOT follow it
            self._set_pose(app, 9.9, 9.9, 0.0)
            src = os.path.join(app.photo_dir, "cap.jpg")
            with open(src, "wb") as f:
                f.write(b"\xff\xd8P")
            archived = app.archive_scan(src)
            with app._pano_mu:
                app._last_archived = archived
            return True
        app.pano_builder = builder
        self.assertTrue(app.start_scan()[0])
        self.assertEqual(wait_scan_end(app), "done")
        metas = [f for f in os.listdir(app.scans_dir)
                 if f.endswith(".meta.json")]
        self.assertEqual(len(metas), 1)
        with open(os.path.join(app.scans_dir, metas[0])) as f:
            meta = json.load(f)
        self.assertEqual(meta["objects"], [])
        self.assertIn("made", meta)
        self.assertEqual(meta["pose"], {"x": 1.25, "y": -0.5, "heading": 90.0})

    def test_identify_replaces_minimal_sidecar_keeps_pose(self):
        # the plan-review blocker: a bare os.link over the pre-written minimal
        # sidecar would FileExistsError and freeze the made-stamp forever
        app, _ = make_scan_app()
        name = "scan_20260720_120000.jpg"
        self._put_scan(app, name)
        with app._pano_mu:
            app._scan_pose = {"x": 2.0, "y": 3.0, "heading": -45.0}
        app._write_min_sidecar(name)
        sidecar = os.path.join(app.scans_dir, name + ".meta.json")
        with open(sidecar) as f:
            before = json.load(f)["made"]
        app.identify_builder = lambda d, m: (
            [sys.executable, "-c",
             f"import json;json.dump({{'objects':[{{'name':'sofa','lon':1,"
             f"'lat':2,'w':3,'h':4}}],'made':'NEW'}}, open({m!r},'w'))"], None)
        app._identify_frames([(0, -5, b"\xff\xd8F")], name)
        with open(sidecar) as f:
            meta = json.load(f)
        self.assertEqual(meta["made"], "NEW")
        self.assertNotEqual(meta["made"], before)     # advanced, not stranded
        self.assertEqual(meta["objects"][0]["name"], "sofa")
        self.assertEqual(meta["pose"], {"x": 2.0, "y": 3.0, "heading": -45.0})
        live = os.path.join(app.photo_dir, "panorama.meta.json")
        self.assertEqual(os.stat(sidecar).st_ino, os.stat(live).st_ino)

    def test_meta_attaches_to_param_not_last_archived(self):
        # back-to-back race: scan B owns _last_archived by the time scan A's
        # identify publishes — A's meta must go to A (parameter binding)
        app, _ = make_scan_app()
        a, b = "scan_20260720_100000.jpg", "scan_20260720_110000.jpg"
        self._put_scan(app, a)
        self._put_scan(app, b)
        with app._pano_mu:
            app._last_archived = b
        app.identify_builder = lambda d, m: (
            [sys.executable, "-c",
             f"import json;json.dump({{'objects':[],'made':'A'}},"
             f"open({m!r},'w'))"], None)
        app._identify_frames([(0, -5, b"\xff\xd8F")], a)
        self.assertTrue(os.path.exists(
            os.path.join(app.scans_dir, a + ".meta.json")))
        self.assertFalse(os.path.exists(
            os.path.join(app.scans_dir, b + ".meta.json")))
        # and A (not newest) must NOT have described the live panorama
        self.assertFalse(os.path.exists(
            os.path.join(app.photo_dir, "panorama.meta.json")))

    def test_reidentify_carries_pose_over(self):
        app, _ = make_scan_app()
        name = "scan_20260720_130000.jpg"
        self._put_scan(app, name)
        sidecar = os.path.join(app.scans_dir, name + ".meta.json")
        with open(sidecar, "w") as f:
            json.dump({"made": "t0", "objects": [],
                       "pose": {"x": 7, "y": 8, "heading": 9}}, f)
        app.identify_pano_cmd = lambda p, o, f: (
            [sys.executable, "-c",
             f"import json;json.dump({{'objects':[{{'name':'shelf','lon':0,"
             f"'lat':0,'w':1,'h':1}}],'made':'t1'}}, open({o!r},'w'))"], None)
        self.assertTrue(app.start_scan_identify(name)[0])
        self._wait_flag_clear(app)
        with open(sidecar) as f:
            meta = json.load(f)
        self.assertEqual(meta["pose"], {"x": 7, "y": 8, "heading": 9})
        self.assertEqual(meta["made"], "t1")          # fresh identify won

    def test_late_identify_sidecar_only_while_scan_active(self):
        # codex code-review catch: scan A's straggling identify must never
        # describe scan B's newer panorama — live commit is guarded
        app, _ = make_scan_app()
        name = "scan_20260720_150000.jpg"
        self._put_scan(app, name)
        with app._pano_mu:
            app._scan_active = True                   # scan B mid-flight
        app.identify_builder = lambda d, m: (
            [sys.executable, "-c",
             f"import json;json.dump({{'objects':[],'made':'late'}},"
             f"open({m!r},'w'))"], None)
        app._identify_frames([(0, -5, b"\xff\xd8F")], name)
        with app._pano_mu:
            app._scan_active = False
        self.assertFalse(os.path.exists(
            os.path.join(app.photo_dir, "panorama.meta.json")))
        self.assertTrue(os.path.exists(
            os.path.join(app.scans_dir, name + ".meta.json")))

    def test_archive_failed_identify_no_live_when_newer_archived(self):
        # archived=None (our archive failed) + someone archived since →
        # live meta must stay untouched
        app, _ = make_scan_app()
        with app._pano_mu:
            app._last_archived = "scan_20260720_160000.jpg"
        app.identify_builder = lambda d, m: (
            [sys.executable, "-c",
             f"import json;json.dump({{'objects':[],'made':'x'}},"
             f"open({m!r},'w'))"], None)
        app._identify_frames([(0, -5, b"\xff\xd8F")], None)
        self.assertFalse(os.path.exists(
            os.path.join(app.photo_dir, "panorama.meta.json")))

    def test_min_sidecar_write_failure_never_raises(self):
        # "costs the pin, never the archive": a squatted temp path → OSError
        # swallowed, no exception escapes toward archive_scan
        app, _ = make_scan_app()
        name = "scan_20260720_170000.jpg"
        os.makedirs(os.path.join(app.scans_dir, name + ".meta.json.tmp"))
        with app._pano_mu:
            app._scan_pose = {"x": 0, "y": 0, "heading": 0}
        app._write_min_sidecar(name)                  # must not raise
        self.assertFalse(os.path.exists(
            os.path.join(app.scans_dir, name + ".meta.json")))

    def test_reidentify_legacy_and_corrupt_sidecars_stay_poseless(self):
        for content in ('{"made": "t0", "objects": []}', "NOT JSON {"):
            app, _ = make_scan_app()
            name = "scan_20260720_140000.jpg"
            self._put_scan(app, name)
            sidecar = os.path.join(app.scans_dir, name + ".meta.json")
            with open(sidecar, "w") as f:
                f.write(content)
            app.identify_pano_cmd = lambda p, o, f: (
                [sys.executable, "-c",
                 f"import json;json.dump({{'objects':[],'made':'t1'}},"
                 f"open({o!r},'w'))"], None)
            self.assertTrue(app.start_scan_identify(name)[0])
            self._wait_flag_clear(app)
            with open(sidecar) as f:
                meta = json.load(f)                   # identify still landed
            self.assertEqual(meta["made"], "t1")
            self.assertNotIn("pose", meta)


class ListObjectsTest(unittest.TestCase):
    """Plan 033: /objects aggregation — every sighting kept (no dedup),
    newest scan first, stable ids, poseless/corrupt/non-numeric skipped."""

    def _sidecar(self, app, name, meta):
        os.makedirs(app.scans_dir, exist_ok=True)
        with open(os.path.join(app.scans_dir, name), "wb") as f:
            f.write(b"\xff\xd8P")
        if meta is not None:
            with open(os.path.join(app.scans_dir, name + ".meta.json"),
                      "w") as f:
                if isinstance(meta, str):
                    f.write(meta)
                else:
                    json.dump(meta, f)

    def test_aggregation_keeps_all_sightings(self):
        app, _ = make_scan_app()
        pose_c = {"x": 1.0, "y": 2.0, "heading": 90.0}
        self._sidecar(app, "scan_20260721_030000.jpg", {   # newest
            "made": "tC", "pose": pose_c, "objects": [
                {"name": "printer", "lon": 30, "lat": 0},
                {"name": "printer", "lon": -50, "lat": 0, "color": "white"},
                {"name": "suitcase", "lon": 0, "lat": -10},
                {"name": "ghost", "lon": "NaNish", "lat": 0}]})   # skipped
        self._sidecar(app, "scan_20260721_020000.jpg", {   # older sighting
            "made": "tB", "pose": {"x": 0, "y": 0, "heading": 0},
            "objects": [{"name": "printer", "lon": 10, "lat": 5}]})
        self._sidecar(app, "scan_20260721_010000.jpg", {   # legacy: no pose
            "made": "tA", "objects": [{"name": "sofa", "lon": 1, "lat": 1}]})
        self._sidecar(app, "scan_20260721_000000.jpg", "NOT JSON {")  # corrupt
        out = app.list_objects()
        self.assertEqual([o["name"] for o in out],
                         ["printer", "printer", "suitcase", "printer"])
        self.assertEqual(out[0]["id"], "scan_20260721_030000.jpg#0")
        self.assertEqual(out[1]["color"], "white")
        # bearing = heading − lon in the scan's pose frame
        self.assertAlmostEqual(out[0]["bearing"], 60.0)    # 90 − 30
        self.assertAlmostEqual(out[3]["bearing"], -10.0)   # 0 − 10
        self.assertEqual(out[0]["pose"], pose_c)
        self.assertEqual(out[3]["scan"], "scan_20260721_020000.jpg")

    def test_empty_and_missing_dir(self):
        app, _ = make_scan_app()
        self.assertEqual(app.list_objects(), [])


class AutoFlashFlagTest(unittest.TestCase):
    def test_default_on_even_without_photo_dir(self):
        app, _ = make_scan_app()
        import shutil
        shutil.rmtree(app.photo_dir, ignore_errors=True)   # fresh deploy
        self.assertTrue(app.auto_flash_on())

    def test_off_persists_across_app_instances(self):
        app, _ = make_scan_app()
        import shutil
        shutil.rmtree(app.photo_dir, ignore_errors=True)
        app.set_auto_flash(False)                    # creates the dir + marker
        self.assertFalse(app.auto_flash_on())
        rover = rc.Rover()
        app2 = rc.App(rover, rc.Movement(rover), rc.CameraAim(rover),
                      rc.Hub(), rc.Camera("off", "", 0, 0, 0), app.photo_dir)
        self.assertFalse(app2.auto_flash_on())       # marker survived
        app2.set_auto_flash(True)
        self.assertTrue(app.auto_flash_on())


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
                                "build-pano", "/f", "/o.jpg", "/f"])
        iargv, _ = app.identify_cmd("/f", "/m.json")
        self.assertEqual(iargv, ["nice", "-n", "10", sys.executable, scene_py,
                                 "identify", "/f", "/m.json"])
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

    def test_variant_names_match_scene_builders(self):
        import scene
        self.assertEqual(rc.PANO_VARIANT_NAMES,
                         tuple(n for n, _ in scene.VARIANT_BUILDERS))

    def test_variants_published_and_stale_deleted(self):
        app = self._app()
        # a previous scan's stitcher variant lingers in photo_dir
        os.makedirs(app.photo_dir, exist_ok=True)
        stale = os.path.join(app.photo_dir, "pano_var_stitcher.jpg")
        with open(stale, "wb") as f:
            f.write(b"OLD")

        def cmd(d, o):
            # this run: seamcut + projector succeed, stitcher fails
            code = (f"open({o!r},'wb').write(b'PANO');"
                    f"import os;d={d!r};"
                    "open(os.path.join(d,'pano_var_seamcut.jpg'),'wb').write(b'SEAM');"
                    "open(os.path.join(d,'pano_var_projector.jpg'),'wb').write(b'PROJ')")
            return [sys.executable, "-c", code], None
        app.pano_build_cmd = cmd
        self.assertTrue(app._build_pano_subprocess([(0, -5, b"x")]))
        with open(os.path.join(app.photo_dir, "pano_var_seamcut.jpg"), "rb") as f:
            self.assertEqual(f.read(), b"SEAM")
        with open(os.path.join(app.photo_dir, "pano_var_projector.jpg"), "rb") as f:
            self.assertEqual(f.read(), b"PROJ")
        self.assertFalse(os.path.exists(stale))   # stale button removed

    def test_variant_publish_failure_does_not_fail_scan(self):
        app = self._app()
        os.makedirs(app.photo_dir, exist_ok=True)
        # a DIRECTORY where the variant file should go → os.replace fails
        os.makedirs(os.path.join(app.photo_dir, "pano_var_seamcut.jpg"))

        def cmd(d, o):
            code = (f"open({o!r},'wb').write(b'PANO');"
                    f"import os;d={d!r};"
                    "open(os.path.join(d,'pano_var_seamcut.jpg'),'wb').write(b'SEAM')")
            return [sys.executable, "-c", code], None
        app.pano_build_cmd = cmd
        self.assertTrue(app._build_pano_subprocess([(0, -5, b"x")]))
        with open(os.path.join(app.photo_dir, "panorama.jpg"), "rb") as f:
            self.assertEqual(f.read(), b"PANO")    # canonical unaffected

    def test_identify_meta_published_live_and_sidecar(self):
        app = self._app()
        os.makedirs(app.scans_dir, exist_ok=True)
        # the scan must exist and be newest — the live commit is guarded now
        with open(os.path.join(app.scans_dir,
                               "scan_20260715_010101.jpg"), "wb") as f:
            f.write(b"\xff\xd8P")
        app.identify_builder = lambda d, m: (
            [sys.executable, "-c",
             f"import json;json.dump({{'objects':[{{'name':'bin','lon':1,"
             f"'lat':2,'w':3,'h':4}}]}}, open({m!r},'w'))"], None)
        app._identify_frames([(0, -5, b"x")], "scan_20260715_010101.jpg")
        live = os.path.join(app.photo_dir, "panorama.meta.json")
        with open(live) as f:
            self.assertEqual(json.load(f)["objects"][0]["name"], "bin")
        sidecar = os.path.join(app.scans_dir,
                               "scan_20260715_010101.jpg.meta.json")
        self.assertEqual(os.stat(live).st_ino, os.stat(sidecar).st_ino)

    def test_identify_failure_or_timeout_writes_nothing_no_crash(self):
        app = self._app()
        app.identify_builder = lambda d, m: (
            [sys.executable, "-c", "import sys; sys.exit(1)"], None)
        app._identify_frames([(0, -5, b"x")], None)
        live = os.path.join(app.photo_dir, "panorama.meta.json")
        self.assertFalse(os.path.exists(live))
        old = rc.IDENTIFY_TIMEOUT_S
        rc.IDENTIFY_TIMEOUT_S = 0.4
        try:
            app.identify_builder = lambda d, m: (
                [sys.executable, "-c", "import time; time.sleep(60)"], None)
            t0 = time.monotonic()
            app._identify_frames([(0, -5, b"x")], None)
            self.assertLess(time.monotonic() - t0, 10)
        finally:
            rc.IDENTIFY_TIMEOUT_S = old

    def test_scan_slot_free_during_identify_and_new_scan_kills_it(self):
        # glm N1: back-to-back scans must not be refused while the previous
        # scan's identify is still running — and the new scan kills it
        app, _ = make_scan_app()
        gate = threading.Event()

        def builder(frames):
            app._mark_published()
            return True
        app.pano_builder = builder
        app.identify_builder = lambda d, m: (
            [sys.executable, "-c", "import time; time.sleep(60)"], None)
        self.assertTrue(app.start_scan()[0])
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:                # wait for identify
            with app._pano_mu:
                if app._identify_proc is not None:
                    break
            time.sleep(0.02)
        with app._pano_mu:
            self.assertIsNotNone(app._identify_proc)
        ok, why = app.start_scan()                        # slot must be FREE
        self.assertTrue(ok, why)
        deadline = time.monotonic() + 5                   # old identify dies
        while time.monotonic() < deadline:
            with app._pano_mu:
                if app._identify_proc is None:
                    break
            time.sleep(0.05)
        wait_scan_end(app)
        del gate

    def test_scan_without_meta_deletes_stale_live_meta(self):
        app = self._app()
        os.makedirs(app.photo_dir, exist_ok=True)
        live = os.path.join(app.photo_dir, "panorama.meta.json")
        with open(live, "w") as f:
            f.write('{"objects":[]}')
        app.pano_build_cmd = lambda d, o: (
            [sys.executable, "-c", f"open({o!r},'wb').write(b'PANO')"], None)
        self.assertTrue(app._build_pano_subprocess([(0, -5, b"x")]))
        self.assertFalse(os.path.exists(live))     # old boxes never linger

    def test_stale_meta_gone_the_moment_the_new_pano_publishes(self):
        # identification takes minutes — the old meta must not describe the
        # new pano even DURING that window (full start_scan flow)
        app, _ = make_scan_app()
        os.makedirs(app.photo_dir, exist_ok=True)
        live = os.path.join(app.photo_dir, "panorama.meta.json")
        with open(live, "w") as f:
            f.write('{"objects":[{"name":"ghost"}]}')
        seen = {}

        def identify_probe(d, m):
            seen["meta_during_identify"] = os.path.exists(live)
            return [sys.executable, "-c", "pass"], None
        app.pano_build_cmd = lambda d, o: (
            [sys.executable, "-c", f"open({o!r},'wb').write(b'PANO')"], None)
        app.identify_builder = identify_probe
        self.assertTrue(app.start_scan()[0])
        self.assertEqual(wait_scan_end(app), "done")
        deadline = time.monotonic() + 5
        while "meta_during_identify" not in seen and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertFalse(seen["meta_during_identify"])

    def test_scan_stop_mapping_and_edge(self):
        m = rc.default_mapping()
        self.assertEqual(m["scan_stop"], {"kind": "button", "index": 8})
        from tests.test_controller import FakeState
        st = FakeState(buttons={8: True})
        prev = rc.GpPrev()
        self.assertTrue(rc.compute_joystick(m, st, prev)["scan_stop"])
        self.assertFalse(rc.compute_joystick(m, st, prev)["scan_stop"])

    def test_success_archives_a_history_copy(self):
        app = self._app()
        app.pano_build_cmd = lambda d, o: (
            [sys.executable, "-c", f"open({o!r},'wb').write(b'PANO')"], None)
        self.assertTrue(app._build_pano_subprocess([(0, -5, b"x")]))
        scans = app.list_scans()
        self.assertEqual(len(scans), 1)
        with open(os.path.join(app.scans_dir, scans[0]), "rb") as f:
            self.assertEqual(f.read(), b"PANO")

    def test_archive_failure_does_not_fail_scan(self):
        app = self._app()
        app.pano_build_cmd = lambda d, o: (
            [sys.executable, "-c", f"open({o!r},'wb').write(b'PANO')"], None)
        # scans_dir path blocked by a plain file → os.makedirs raises
        with open(app.scans_dir, "wb") as f:
            f.write(b"in the way")
        self.assertTrue(app._build_pano_subprocess([(0, -5, b"x")]))
        with open(os.path.join(app.photo_dir, "panorama.jpg"), "rb") as f:
            self.assertEqual(f.read(), b"PANO")    # latest stays authoritative

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
