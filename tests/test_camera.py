"""No-hardware tests for rover_camera (stream + rpicam paths are mocked)."""
import unittest
from unittest import mock

import rover_camera


class CameraTest(unittest.TestCase):
    def test_stream_success_returns_path(self):
        with mock.patch.object(rover_camera, "_grab_from_stream", return_value=True), \
             mock.patch("rover_camera.os.makedirs"):
            path = rover_camera.take_photo(wait=True)
        self.assertTrue(path.endswith(".jpg"))
        self.assertIn("photos", path)

    def test_falls_back_to_rpicam_when_stream_down(self):
        with mock.patch.object(rover_camera, "_grab_from_stream", return_value=False), \
             mock.patch.object(rover_camera, "_tool", return_value="rpicam-still"), \
             mock.patch("rover_camera.os.makedirs"), \
             mock.patch("rover_camera.subprocess.run") as run, \
             mock.patch("rover_camera.os.path.exists", return_value=True), \
             mock.patch("rover_camera.os.path.getsize", return_value=4096):
            path = rover_camera.take_photo(wait=True)
        run.assert_called_once()
        self.assertTrue(path.endswith(".jpg"))

    def test_both_paths_fail_returns_empty(self):
        with mock.patch.object(rover_camera, "_grab_from_stream", return_value=False), \
             mock.patch.object(rover_camera, "_tool", return_value=None), \
             mock.patch("rover_camera.os.makedirs"):
            self.assertEqual(rover_camera.take_photo(wait=True), "")

    def test_nonblocking_returns_jpg_path_and_does_not_block(self):
        # capture runs in a daemon thread (mocked); caller gets the path at once
        with mock.patch.object(rover_camera, "_capture") as cap:
            path = rover_camera.take_photo(wait=False)
        self.assertTrue(path.endswith(".jpg"))
        self.assertIn("photos", path)


if __name__ == "__main__":
    unittest.main()
