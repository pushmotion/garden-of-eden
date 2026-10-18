import unittest
from unittest.mock import patch

from app import create_app


class CameraRouteTestCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app("default")
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()

    @patch("app.sensors.camera.routes.camera.capture_upper")
    def test_capture_failure_returns_503(self, mock_capture):
        mock_capture.side_effect = FileNotFoundError("fswebcam missing")
        resp = self.client.get("/camera/upper")
        self.assertEqual(resp.status_code, 503)

    @patch("app.sensors.camera.routes.send_file")
    @patch("app.sensors.camera.routes.camera.capture_lower")
    def test_capture_success_serves_file(self, mock_capture, mock_send_file):
        mock_send_file.return_value = "IMG"
        resp = self.client.get("/camera/lower")
        mock_capture.assert_called_once()
        self.assertEqual(resp.status_code, 200)

    @patch("app.sensors.camera.routes.camera.has_video", return_value=False)
    def test_timelapse_stub_is_not_served_as_a_clip(self, mock_has_video):
        """os.path.exists said "ready" for a 48-byte header for three weeks."""
        resp = self.client.get("/camera/timelapse/upper")
        self.assertEqual(resp.status_code, 404)
        mock_has_video.assert_called_once_with("upper")

    @patch("app.sensors.camera.routes.send_file", return_value="MP4")
    @patch("app.sensors.camera.routes.camera.has_video", return_value=True)
    def test_timelapse_is_served_when_real(self, mock_has_video, mock_send_file):
        resp = self.client.get("/camera/timelapse/lower")
        self.assertEqual(resp.status_code, 200)
        mock_send_file.assert_called_once()


if __name__ == "__main__":
    unittest.main()
