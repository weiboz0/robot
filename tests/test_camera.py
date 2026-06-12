"""No-hardware tests for rover_camera.take_photo (capture tool is mocked)."""
import unittest
from unittest import mock

import rover_camera


class CameraTest(unittest.TestCase):
    def test_no_tool_returns_empty(self):
        with mock.patch.object(rover_camera, "_tool", return_value=None):
            self.assertEqual(rover_camera.take_photo(), "")

    def test_nonblocking_uses_popen_and_returns_jpg_path(self):
        with mock.patch.object(rover_camera, "_tool", return_value="rpicam-still"), \
             mock.patch("rover_camera.os.makedirs"), \
             mock.patch("rover_camera.subprocess.Popen") as popen, \
             mock.patch("rover_camera.subprocess.run") as run:
            path = rover_camera.take_photo()              # default: non-blocking
        self.assertTrue(path.endswith(".jpg"))
        self.assertIn("photos", path)
        popen.assert_called_once()
        run.assert_not_called()

    def test_wait_uses_run(self):
        with mock.patch.object(rover_camera, "_tool", return_value="rpicam-still"), \
             mock.patch("rover_camera.os.makedirs"), \
             mock.patch("rover_camera.subprocess.Popen") as popen, \
             mock.patch("rover_camera.subprocess.run") as run:
            rover_camera.take_photo(wait=True)
        run.assert_called_once()
        popen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
