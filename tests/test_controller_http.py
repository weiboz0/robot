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
                         "/camera_center", "/light_head", "/gimbal_relax"):
                c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                c.request("POST", path)
                self.assertEqual(c.getresponse().status, 503, path)
                c.close()
        finally:
            server.shutdown()
            server.server_close()
            tmp.cleanup()


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
                       "c==='spinl'", "c==='light'", "chatbot names also work"):
            self.assertIn(marker, body, marker)

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
