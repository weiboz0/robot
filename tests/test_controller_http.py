"""HTTP-layer tests for rovercontrold — the real server on a random port with a
fake serial link, covering every endpoint family the Go tests pinned."""
import http.client
import json
import os
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
                       "j.state==='scanning'||j.state==='stitching'"):
            self.assertIn(marker, body, marker)

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
