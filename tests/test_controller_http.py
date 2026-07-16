"""HTTP-layer tests for rovercontrold — the real server on a random port with a
fake serial link, covering every endpoint family the Go tests pinned."""
import http.client
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer

import rovercontrold as rc
from tests.test_controller import RecLink


class HTTPBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.rover = rc.Rover()
        cls.link = RecLink()
        cls.rover.set_status(cls.link, "")
        cls.hub = rc.Hub()
        cls.cam = rc.Camera("off", "", 0, 0, 0)
        cls.cam.set_status(True, "")
        cls.move = rc.Movement(cls.rover)
        cls.aim = rc.CameraAim(cls.rover)
        cls.app = rc.App(cls.rover, cls.move, cls.aim, cls.hub, cls.cam,
                         cls.tmp.name)
        cls.app.identify_builder = None    # tests never spawn the LLM step
        cls.app.mapping, cls.app.map_source = rc.default_mapping(), "default"
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), rc.make_handler(cls.app))
        cls.server.daemon_threads = True
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()

    def req(self, method, path, body=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.request(method, path, body=body)
        r = c.getresponse()
        data = r.read()
        c.close()
        return r.status, data

    def jreq(self, method, path, body=None):
        status, data = self.req(method, path, body)
        try:
            return status, json.loads(data)
        except json.JSONDecodeError:
            return status, {}


class MovementHTTPTest(HTTPBase):
    def test_nudge_scales_by_cap_and_autostops(self):
        self.move.set_cap(0.25)
        s, j = self.jreq("POST", "/move_forward?ms=60")
        self.assertEqual((s, j["ok"]), (200, True))
        self.assertEqual(self.link.last(), '{"L":0.25,"R":0.25,"T":1}')
        time.sleep(0.2)
        self.assertEqual(self.link.last(), '{"L":0,"R":0,"T":1}')

    def test_drive_scaled_and_watchdogged(self):
        self.move.set_cap(0.25)
        s, _ = self.jreq("POST", "/drive?l=1&r=-1")
        self.assertEqual(s, 200)
        self.assertEqual(self.link.last(), '{"L":0.25,"R":-0.25,"T":1}')
        self.jreq("POST", "/stop")

    def test_speed_get_set_and_nan_rejected(self):
        s, j = self.jreq("POST", "/speed?cap=0.3")
        self.assertEqual((s, j["cap"]), (200, 0.3))
        s, j = self.jreq("GET", "/speed")
        self.assertEqual((s, j["cap"]), (200, 0.3))
        s, _ = self.jreq("POST", "/speed?cap=NaN")
        self.assertEqual(s, 400)
        s, j = self.jreq("GET", "/speed")
        self.assertEqual(j["cap"], 0.3)                 # not poisoned
        self.jreq("POST", "/speed?cap=0.25")

    def test_estop_then_stop(self):
        self.jreq("POST", "/estop")
        self.assertTrue(self.move.is_estopped())
        self.jreq("POST", "/stop")
        self.assertFalse(self.move.is_estopped())

    def test_bad_param_rejected(self):
        s, _ = self.jreq("POST", "/move_forward?ms=abc")
        self.assertEqual(s, 400)


class CameraHTTPTest(HTTPBase):
    def test_aim_center_nudge(self):
        s, j = self.jreq("POST", "/camera_aim?pan=30&tilt=-10")
        self.assertEqual((s, j["pan"], j["tilt"]), (200, 30, -10))
        s, j = self.jreq("POST", "/camera_up?deg=10")
        self.assertEqual(j["tilt"], 0)                  # -10 + 10
        s, j = self.jreq("POST", "/camera_center")
        self.assertEqual((j["pan"], j["tilt"]), (0, 0))

    def test_aim_clamps(self):
        s, j = self.jreq("POST", "/camera_aim?pan=999&tilt=999")
        self.assertEqual((j["pan"], j["tilt"]), (180, 90))
        self.jreq("POST", "/camera_center")


class LightsHTTPTest(HTTPBase):
    def test_toggle_and_set_preserve(self):
        s, j = self.jreq("POST", "/light_head?on=1")
        self.assertEqual((s, j["on"]), (200, True))
        s, j = self.jreq("POST", "/light_base?on=1")
        self.assertTrue(self.app.head_on and self.app.base_on)
        s, j = self.jreq("POST", "/light_head?on=0")
        self.assertFalse(self.app.head_on)
        self.assertTrue(self.app.base_on)               # preserved
        s, j = self.jreq("POST", "/light_base")          # bare = toggle
        self.assertEqual(j["on"], False)


class SerialDownTest(unittest.TestCase):
    def test_control_endpoints_503_when_serial_down(self):
        tmp = tempfile.TemporaryDirectory()
        rover = rc.Rover()                               # no link
        app = rc.App(rover, rc.Movement(rover), rc.CameraAim(rover),
                     rc.Hub(), rc.Camera("off", "", 0, 0, 0), tmp.name)
        server = ThreadingHTTPServer(("127.0.0.1", 0), rc.make_handler(app))
        server.daemon_threads = True
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            for path in ("/move_forward", "/stop", "/estop", "/drive?l=0&r=0",
                         "/camera_center", "/light_head", "/gimbal_relax",
                         "/scan"):
                c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                c.request("POST", path)
                self.assertEqual(c.getresponse().status, 503, path)
                c.close()
        finally:
            server.shutdown()
            server.server_close()
            tmp.cleanup()


class ScanHTTPTest(HTTPBase):
    def test_scan_starts_conflicts_and_completes(self):
        import threading as _t
        gate = _t.Event()
        old_builder, old_settle = self.app.pano_builder, self.app.scan_settle_s
        self.app.scan_settle_s = 0
        self.app.pano_builder = lambda frames: gate.wait(5) or True
        self.hub.publish(b"\xff\xd8JPEG")
        try:
            s, j = self.jreq("POST", "/scan")
            self.assertEqual((s, j["ok"]), (200, True))
            s, _ = self.jreq("POST", "/scan")            # single-flight
            self.assertEqual(s, 409)
            gate.set()
            deadline = time.time() + 5
            while time.time() < deadline:
                s, j = self.jreq("GET", "/pano_status")
                if j.get("state") in ("done", "failed"):
                    break
                time.sleep(0.02)
            self.assertEqual(j["state"], "done")
        finally:
            self.app.pano_builder, self.app.scan_settle_s = old_builder, old_settle

    def test_scan_cancel_endpoint(self):
        s, j = self.jreq("POST", "/scan_cancel")       # idle → 409
        self.assertEqual(s, 409)
        import threading as _t
        gate = _t.Event()
        old_builder, old_settle = self.app.pano_builder, self.app.scan_settle_s
        self.app.scan_settle_s = 0
        self.app.pano_builder = (
            lambda frames: not self.app._scan_cancel.wait(5) and gate.set())
        self.hub.publish(b"\xff\xd8JPEG")
        try:
            s, _ = self.jreq("POST", "/scan")
            self.assertEqual(s, 200)
            deadline = time.time() + 5
            while self.app.pano_state != "stitching" and time.time() < deadline:
                time.sleep(0.02)
            s, j = self.jreq("POST", "/scan_cancel")   # running → 200
            self.assertEqual((s, j["ok"]), (200, True))
            deadline = time.time() + 5
            while time.time() < deadline:
                s, j = self.jreq("GET", "/pano_status")
                if j.get("state") in ("done", "failed"):
                    break
                time.sleep(0.02)
            self.assertEqual(j["state"], "failed")     # discarded, not saved
        finally:
            self.app.pano_builder, self.app.scan_settle_s = old_builder, old_settle

    def test_scan_409_while_driving(self):
        self.move.set_drive(0.1, 0.1)
        try:
            s, j = self.jreq("POST", "/scan")
            self.assertEqual(s, 409)
            self.assertIn("moving", j.get("error", ""))
        finally:
            self.move.stop()


class PoseHTTPTest(HTTPBase):
    def test_pose_shape_and_reset(self):
        for i in range(11):                     # 10 steps × 10 cm = 1 m
            self.app.pose.update(i * 10, i * 10)
        s, j = self.jreq("GET", "/pose")
        self.assertEqual(s, 200)
        for k in ("x", "y", "heading", "pan", "tilt", "battery_v", "fresh"):
            self.assertIn(k, j)
        self.assertAlmostEqual(j["x"], 1.0)
        self.assertTrue(j["fresh"])
        s, _ = self.jreq("POST", "/pose_reset")
        self.assertEqual(s, 200)
        s, j = self.jreq("GET", "/pose")
        self.assertEqual((j["x"], j["y"], j["heading"]), (0, 0, 0))

    def test_pose_200_not_503_when_serial_down(self):
        rover = rc.Rover()                              # no link
        app = rc.App(rover, rc.Movement(rover), rc.CameraAim(rover),
                     rc.Hub(), rc.Camera("off", "", 0, 0, 0), self.tmp.name)
        server = ThreadingHTTPServer(("127.0.0.1", 0), rc.make_handler(app))
        server.daemon_threads = True
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            c.request("GET", "/pose")
            r = c.getresponse()
            j = json.loads(r.read())
            c.close()
            self.assertEqual(r.status, 200)             # NOT 503
            self.assertFalse(j["fresh"])
        finally:
            server.shutdown()
            server.server_close()


class ScansHTTPTest(HTTPBase):
    def _put_scan(self, name, data=b"\xff\xd8SCAN"):
        os.makedirs(self.app.scans_dir, exist_ok=True)
        with open(os.path.join(self.app.scans_dir, name), "wb") as f:
            f.write(data)

    def test_list_serve_delete_flow(self):
        self._put_scan("scan_20260712_120000.jpg")
        self._put_scan("scan_20260712_130000_1.jpg")    # collision suffix form
        s, j = self.jreq("GET", "/scans")
        self.assertEqual(s, 200)
        self.assertLess(j["scans"].index("scan_20260712_130000_1.jpg"),
                        j["scans"].index("scan_20260712_120000.jpg"))  # newest first
        s, data = self.req("GET", "/scans/scan_20260712_120000.jpg")
        self.assertEqual((s, data), (200, b"\xff\xd8SCAN"))
        s, _ = self.jreq("POST", "/delete_scan/scan_20260712_120000.jpg")
        self.assertEqual(s, 200)
        s, j = self.jreq("GET", "/scans")
        self.assertNotIn("scan_20260712_120000.jpg", j["scans"])

    def test_scan_content_type(self):
        self._put_scan("scan_20260712_140000.jpg")
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request("GET", "/scans/scan_20260712_140000.jpg")
        r = c.getresponse()
        r.read()
        c.close()
        self.assertEqual(r.getheader("Content-Type"), "image/jpeg")

    def test_traversal_and_name_rejects(self):
        for bad in ("../panorama.jpg", "..%2Fx.jpg", "scan_2026.jpg",
                    "scan_20260712_120000.png", "panorama.jpg",
                    "scan_20260712_120000_.jpg"):
            s, _ = self.req("GET", "/scans/" + bad)
            self.assertIn(s, (400, 404), bad)
            s, _ = self.req("POST", "/delete_scan/" + bad)
            self.assertEqual(s, 400, bad)

    def test_meta_endpoints(self):
        os.makedirs(self.app.scans_dir, exist_ok=True)
        with open(os.path.join(self.app.photo_dir, "panorama.meta.json"), "w") as f:
            f.write('{"objects":[{"name":"printer","lon":5,"lat":0,"w":10,"h":8}]}')
        with open(os.path.join(self.app.scans_dir,
                               "scan_20260714_120000.jpg.meta.json"), "w") as f:
            f.write('{"objects":[]}')
        s, j = self.jreq("GET", "/pano_meta")
        self.assertEqual((s, j["objects"][0]["name"]), (200, "printer"))
        s, _ = self.jreq("GET", "/scan_meta/scan_20260714_120000.jpg")
        self.assertEqual(s, 200)
        s, _ = self.req("GET", "/scan_meta/../evil")
        self.assertEqual(s, 400)
        s, _ = self.req("GET", "/scan_meta/scan_20990101_000000.jpg")
        self.assertEqual(s, 404)
        os.remove(os.path.join(self.app.photo_dir, "panorama.meta.json"))
        s, _ = self.req("GET", "/pano_meta")
        self.assertEqual(s, 404)

    def test_post_panorama_clears_stale_meta(self):
        with open(os.path.join(self.app.photo_dir, "panorama.meta.json"), "w") as f:
            f.write('{"objects":[]}')
        s, _ = self.jreq("POST", "/panorama", b"\xff\xd8\xff\xe0new")
        self.assertEqual(s, 200)
        self.assertFalse(os.path.exists(
            os.path.join(self.app.photo_dir, "panorama.meta.json")))

    def test_delete_scan_removes_meta_sidecar(self):
        os.makedirs(self.app.scans_dir, exist_ok=True)
        for suffix in ("", ".meta.json"):
            with open(os.path.join(self.app.scans_dir,
                                   "scan_20260714_130000.jpg" + suffix), "w") as f:
                f.write("x")
        s, _ = self.jreq("POST", "/delete_scan/scan_20260714_130000.jpg")
        self.assertEqual(s, 200)
        self.assertFalse(os.path.exists(os.path.join(
            self.app.scans_dir, "scan_20260714_130000.jpg.meta.json")))

    def test_archive_name_matches_regex_and_photos_unpolluted(self):
        pano = os.path.join(self.tmp.name, "panorama.jpg")
        with open(pano, "wb") as f:
            f.write(b"\xff\xd8P")
        name = self.app.archive_scan(pano)
        self.assertRegex(name, rc.SCAN_NAME_RE)
        name2 = self.app.archive_scan(pano)             # same second → suffix
        self.assertNotEqual(name, name2)
        self.assertRegex(name2, rc.SCAN_NAME_RE)
        s, j = self.jreq("GET", "/photos")              # subdir never leaks
        self.assertNotIn(name, j["photos"])


class FakeChatUpstream:
    """Tiny loopback chat service double with a configurable poll delay."""
    def __init__(self, poll_delay=0.0):
        from http.server import BaseHTTPRequestHandler
        delay = poll_delay

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _j(self, code, obj):
                b = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

            def do_GET(self):
                if self.path.startswith("/chat_status"):
                    self._j(200, {"ok": True, "model": "fake", "rover": None,
                                  "dobot": False, "busy": False})
                elif self.path.startswith("/chat_poll"):
                    if "turn=404" in self.path:
                        self._j(404, {"error": "unknown or expired turn"})
                        return
                    time.sleep(delay)
                    self._j(200, {"done": True, "reply": "fake reply"})
                else:
                    self._j(404, {"error": "nf"})

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                self.rfile.read(n)
                if self.path == "/chat":
                    self._j(200, {"turn": 7})
                else:
                    self._j(404, {"error": "nf"})
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class ChatBridgeTest(HTTPBase):
    def _with_upstream(self, up):
        self._old_port = rc.CHAT_PORT
        rc.CHAT_PORT = up.port

    def tearDown(self):
        if hasattr(self, "_old_port"):
            rc.CHAT_PORT = self._old_port

    def test_proxy_roundtrip_and_status(self):
        up = FakeChatUpstream()
        self._with_upstream(up)
        try:
            s, j = self.jreq("POST", "/chat", json.dumps({"text": "hi"}).encode())
            self.assertEqual((s, j["turn"]), (200, 7))
            s, j = self.jreq("GET", "/chat_poll?turn=7")
            self.assertEqual((s, j["reply"]), (200, "fake reply"))
            s, j = self.jreq("GET", "/chat_poll?turn=404")   # upstream 404
            self.assertEqual(s, 404)                          # passes through
            self.assertIn("expired", j["error"])
            s, j = self.jreq("GET", "/chat_status")
            self.assertEqual((s, j["ok"]), (200, True))
        finally:
            up.close()

    def test_service_down_mapping(self):
        rc.CHAT_PORT = 1                          # nothing listens on port 1
        self._old_port = 8090
        s, j = self.jreq("GET", "/chat_status")
        self.assertEqual((s, j["up"]), (200, False))   # down is a 200 answer
        s, _ = self.jreq("POST", "/chat", b'{"text":"x"}')
        self.assertEqual(s, 503)
        s, _ = self.jreq("GET", "/chat_poll?turn=1")
        self.assertEqual(s, 503)

    def test_chat_start_409_when_up_and_cmd_pin(self):
        up = FakeChatUpstream()
        self._with_upstream(up)
        try:
            s, j = self.jreq("POST", "/chat_start")
            self.assertEqual(s, 409)
            self.assertIn("already running", j["error"])
        finally:
            up.close()
        argv, cwd = self.app.chat_cmd()
        self.assertEqual(argv[0], sys.executable)
        self.assertIn("agent_chat.py", argv[1])
        self.assertEqual(argv[2:], ["--serve", str(rc.CHAT_PORT)])

    def test_chat_start_double_start_refused_while_child_boots(self):
        # codex catch: the child takes seconds before it listens (robot
        # detection first) — a second start in that window must NOT spawn
        # and orphan-overwrite the live child
        rc.CHAT_PORT = 1
        self._old_port = 8090
        old_cmd = self.app.chat_cmd
        try:
            self.app.chat_cmd = lambda: (
                [sys.executable, "-c", "import time; time.sleep(10)"], None)
            s, _ = self.jreq("POST", "/chat_start")
            self.assertEqual(s, 200)
            first = self.app._chat_proc
            s, j = self.jreq("POST", "/chat_start")
            self.assertEqual(s, 409)
            self.assertIn("starting", j["error"])
            self.assertIs(self.app._chat_proc, first)   # not overwritten
        finally:
            self.app.chat_cmd = old_cmd
            with self.app._chat_mu:
                if self.app._chat_proc is not None:
                    self.app._chat_proc.kill()
                    self.app._chat_proc.wait()
                    self.app._chat_proc = None

    def test_chat_start_spawn_and_early_exit_reporting(self):
        rc.CHAT_PORT = 1                          # probe finds nothing
        self._old_port = 8090
        old_cmd = self.app.chat_cmd
        try:
            self.app.chat_cmd = lambda: (
                [sys.executable, "-c", "import time; time.sleep(5)"], None)
            s, j = self.jreq("POST", "/chat_start")
            self.assertEqual((s, j["ok"]), (200, True))
            with self.app._chat_mu:               # clear phase 1's live child
                self.app._chat_proc.kill()
                self.app._chat_proc.wait()
                self.app._chat_proc = None
            self.app.chat_cmd = lambda: (
                [sys.executable, "-c", "import sys; sys.exit(1)"], None)
            s, j = self.jreq("POST", "/chat_start")
            self.assertEqual(s, 409)
            self.assertIn("exited at startup", j["error"])
        finally:
            self.app.chat_cmd = old_cmd
            with self.app._chat_mu:
                if self.app._chat_proc is not None:
                    self.app._chat_proc.kill()
                    self.app._chat_proc.wait()

    def test_drive_stays_fast_under_chat_and_stream_load(self):
        # plan 030 responsiveness guard: slow chat polls + live MJPEG streams
        # must not delay a drive command
        up = FakeChatUpstream(poll_delay=3.0)
        self._with_upstream(up)
        streams = []
        try:
            self.hub.publish(b"\xff\xd8JPEG")
            for _ in range(2):                    # two live stream clients
                c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
                c.request("GET", "/video_feed")
                streams.append(c)
            polls = [threading.Thread(
                target=lambda: self.req("GET", "/chat_poll?turn=1"), daemon=True)
                for _ in range(3)]
            for t in polls:
                t.start()
            time.sleep(0.1)                       # polls are in flight
            t0 = time.monotonic()
            s, _ = self.jreq("POST", "/drive?l=0&r=0")
            dt = time.monotonic() - t0
            self.assertEqual(s, 200)
            self.assertLess(dt, 1.0, f"drive took {dt:.2f}s under load")
        finally:
            for c in streams:
                c.close()
            up.close()


class PhotoHTTPTest(HTTPBase):
    def test_snapshot_photos_delete_flow(self):
        self.hub.publish(b"\xff\xd8FRAME\xff\xd9")
        s, j = self.jreq("POST", "/snapshot")
        self.assertEqual((s, j["ok"]), (200, True))
        name = j["name"]
        s, j = self.jreq("GET", "/photos")
        self.assertIn(name, j["photos"])
        s, data = self.req("GET", "/photos/" + name)
        self.assertEqual((s, data), (200, b"\xff\xd8FRAME\xff\xd9"))
        s, _ = self.jreq("POST", "/delete_photo/" + name)
        self.assertEqual(s, 200)
        s, j = self.jreq("GET", "/photos")
        self.assertNotIn(name, j["photos"])

    def test_photo_meta_roundtrip_and_validation(self):
        body = json.dumps({"target": "a green pen", "label": "green pen",
                           "color": "green", "bbox": [0.1, 0.2, 0.5, 0.6],
                           "confidence": 0.9})
        s, _ = self.jreq("POST", "/photo_meta/rover_x.jpg", body)
        self.assertEqual(s, 200)
        s, j = self.jreq("GET", "/photo_meta/rover_x.jpg")
        self.assertEqual((s, j["label"], j["bbox"][2]), (200, "green pen", 0.5))
        s, j = self.jreq("GET", "/photos")
        self.assertIn("rover_x.jpg", j["outlined"])
        for bad in ('{"bbox":[0.5,0.2,0.1,0.6]}', '{"bbox":[0.1,0.2,0.5,1.5]}',
                    '{"bbox":[0.1,0.2,0.5]}', "junk",
                    '{"bbox":[0.1,0.2,0.5,0.6],"confidence":NaN}',
                    '{"bbox":[0.1,0.2,0.5,0.6],"confidence":"x"}'):
            s, _ = self.jreq("POST", "/photo_meta/rover_x.jpg", bad)
            self.assertEqual(s, 400, bad)
        s, _ = self.jreq("POST", "/photo_meta/../evil.jpg",
                         '{"bbox":[0.1,0.2,0.5,0.6]}')
        self.assertEqual(s, 400)
        # sidecar deleted with the photo
        open(os.path.join(self.tmp.name, "rover_x.jpg"), "wb").write(b"\xff\xd8")
        self.jreq("POST", "/delete_photo/rover_x.jpg")
        self.assertFalse(os.path.exists(
            os.path.join(self.tmp.name, "rover_x.jpg.meta.json")))

    def test_bad_photo_name_rejected(self):
        s, _ = self.req("GET", "/photos/..%2Fevil.jpg")
        self.assertEqual(s, 400)


class BlobEndpointsTest(HTTPBase):
    def test_panorama_roundtrip(self):
        s, _ = self.jreq("GET", "/panorama")
        # may exist from other tests; upload then check
        s, j = self.jreq("POST", "/panorama", b"\xff\xd8PANO")
        self.assertEqual((s, j["ok"]), (200, True))
        s, data = self.req("GET", "/panorama")
        self.assertEqual((s, data), (200, b"\xff\xd8PANO"))
        s, _ = self.jreq("POST", "/panorama", b"not a jpeg")
        self.assertEqual(s, 400)

    def test_pano_status(self):
        s, _ = self.jreq("POST", "/pano_status?state=stitching")
        self.assertEqual(s, 200)
        s, j = self.jreq("GET", "/pano_status")
        self.assertEqual(j["state"], "stitching")
        self.assertGreaterEqual(j["age_s"], 0)
        s, _ = self.jreq("POST", "/pano_status?state=hacked")
        self.assertEqual(s, 400)

    def test_pano_variant_and_det_image(self):
        for prefix in ("/pano_variant/", "/det_image/"):
            s, _ = self.jreq("GET", prefix + "nosuch")
            self.assertEqual(s, 404)
            s, _ = self.jreq("POST", prefix + "seamcut", b"\xff\xd8IMG")
            self.assertEqual(s, 200)
            s, data = self.req("GET", prefix + "seamcut")
            self.assertEqual(data, b"\xff\xd8IMG")
            s, _ = self.jreq("POST", prefix + "Bad-Name!", b"\xff\xd8IMG")
            self.assertEqual(s, 400)

    def test_tour_upload_and_feed(self):
        two = b"\xff\xd8frameone\xff\xd8frametwo"
        s, _ = self.jreq("POST", "/tour", two)
        self.assertEqual(s, 200)
        s, data = self.req("GET", "/tour_feed?loops=1")
        self.assertEqual(s, 200)
        self.assertEqual(data.count(b"--tourframe"), 2)
        s, _ = self.jreq("POST", "/tour", b"junk")
        self.assertEqual(s, 400)


class PageAndHealthTest(HTTPBase):
    def test_healthz_shape(self):
        s, j = self.jreq("GET", "/healthz")
        self.assertEqual(s, 200)
        self.assertTrue(j["ok"])
        self.assertIn("up", j["serial"])
        self.assertIn("up", j["camera"])
        self.assertIn("mapping", j["gamepad"])

    def test_page_serves_all_ui_markers(self):
        s, data = self.req("GET", "/")
        self.assertEqual(s, 200)
        body = data.decode()
        for marker in ('id="cmdin"', "runCmd(", 'onsubmit="runCmd();return false"',
                       'id="capNum"', "Clear all", "clearAll(", "initCap(",
                       "toggleHelp(", 'id="cmdhelp"', "pick(", "addStep(",
                       "runProgram(", 'id="program"', "roverprog:",
                       "outline(", "coverPct(", "photo_meta", "boxLabel(",
                       "lightbox(", "lbwrap", "fetchMeta(",
                       "pano3d(", "3D view", "/panorama", "pano_status",
                       "downBg", "panostat", "tour_feed", "Room tour",
                       "detcmp(", "det_image", "Detectors",
                       "pano_variant", "setSrc", "no result for this method",
                       "up:'camera_up'", "photo:'snapshot'", "cam:'camera_aim'",
                       "c==='spinl'", "c==='light'", "chatbot names also work",
                       # plan 026: pose badge + scan history tabs + viewer prefs
                       'id="posebadge"', 'id="posetext"', "poseReset(",
                       "/pose_reset", "no telemetry", "setInterval(poseTick,500)",
                       'id="tabphotos"', 'id="tabscans"', 'id="scangrid"',
                       "showTab(", "loadScans(", "clearAllScans(",
                       "Clear all 3D views", "delScan(", "/delete_scan/",
                       "pano3d(\\'/scans/", "DEFAULTS TO THE CLEAREST",
                       "markActive(", "'#08f'", "if(!avail[mv[0]])return;",
                       # plan 028: stop button inside the polled status HTML
                       'id="scanstop"', "scanCancel(", "/scan_cancel",
                       "j.state==='scanning'||j.state==='stitching'",
                       # plan 029: object boxes + scans auto-refresh
                       "drawBoxes(", "boxesCmd(", "tb.id='boxtoggle'",
                       "/pano_meta", "/scan_meta/", "scansTick(",
                       "roverboxes:on", "roverboxes:filter", ".hbox",
                       "boxes on|off|all|NAMES",
                       "Ry(yaw)^T", "Rx(pitch)^T",
                       # plan 030: dashboard + embedded chat
                       'class="dash"', 'id="tabchat"', 'id="tabprog"',
                       'id="chatpanel"', 'id="chatlog"', 'id="chatin"',
                       'id="chatstartbtn"', 'id="progpanel"',
                       "chatSend(", "chatStart(", "chatStatusTick(",
                       "/chat_poll?turn=", "/chat_start",
                       'onsubmit="chatSend();return false"',
                       "showTab('chat')", 'id="posebadge"'):
            self.assertIn(marker, body, marker)

    def test_boxes_cmd_intercepted_before_parse(self):
        s, data = self.req("GET", "/")
        body = data.decode()
        # the client-only 'boxes' command must be handled before parseCmd
        self.assertLess(body.index("if(boxesCmd(raw))return"),
                        body.index("const p=parseCmd(raw);"))

    def test_viewer_archived_scans_have_no_variant_buttons(self):
        s, data = self.req("GET", "/")
        body = data.decode()
        # variant buttons are built only inside the if(!src) branch
        i = body.index("if(!src){\n  VARIANTS.forEach") if "if(!src){\n  VARIANTS.forEach" in body else body.index("if(!src){")
        self.assertGreater(body.index("VARIANTS.forEach"), i)

    def test_video_feed_streams_frames(self):
        self.hub.publish(b"\xff\xd8LIVE\xff\xd9")
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request("GET", "/video_feed")
        r = c.getresponse()
        self.assertEqual(r.status, 200)
        self.assertIn("multipart/x-mixed-replace", r.getheader("Content-Type"))
        chunk = r.read(200)
        self.assertIn(b"--rovercamframe", chunk)
        self.assertIn(b"\xff\xd8", chunk)
        c.close()

    def test_unknown_path_404(self):
        s, _ = self.jreq("GET", "/nope")
        self.assertEqual(s, 404)
        s, _ = self.jreq("POST", "/nope")
        self.assertEqual(s, 404)


if __name__ == "__main__":
    unittest.main()
