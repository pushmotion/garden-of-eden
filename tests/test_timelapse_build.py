"""The timelapse build must not report success when it produced no video.

A live tower served a 48-byte mp4 for three weeks while every build "succeeded".
ffmpeg reads stdin for interactive keys; under a service stdin is at EOF, which
it treats as the "q" quit key. It writes the MP4 container header, stops before
the first frame, and **exits 0** -- so `check=True` passes and
`os.path.exists()` says the video is there.

Both halves are tested here: the flag that prevents it, and the verification
that catches it anyway if the flag is ever dropped.
"""

import os
import tempfile
import unittest
from unittest.mock import patch

import config
from app.sensors.camera import camera


class TimelapseBuildTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        p = patch.object(config, "TIMELAPSE_DIR", self.tmp)
        p.start()
        self.addCleanup(p.stop)
        # One frame is enough; ffmpeg itself is mocked.
        folder = os.path.join(self.tmp, "upper")
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, "20260830-120000.jpg"), "wb") as fh:
            fh.write(b"\xff\xd8\xff\xd9")

    def _fake_run(self, size):
        """Stand in for ffmpeg, writing an output file of ``size`` bytes."""

        class Result:
            stderr = b"Error muxing a packet\nTask finished with error code: -1414092869"
            returncode = 0

        def run(cmd, **kwargs):
            out = cmd[-1]
            with open(out, "wb") as fh:
                fh.write(b"\0" * size)
            return Result()

        return run

    # -- the flag -------------------------------------------------------------

    def test_ffmpeg_is_invoked_with_nostdin(self):
        """The one-flag fix. Without it ffmpeg quits before the first frame."""
        seen = {}

        def run(cmd, **kwargs):
            seen["cmd"] = cmd
            seen["stdin"] = kwargs.get("stdin")
            with open(cmd[-1], "wb") as fh:
                fh.write(b"\0" * 50_000)

            class R:
                stderr = b""

            return R()

        with patch("subprocess.run", run):
            camera.generate_timelapse("upper")

        self.assertIn("-nostdin", seen["cmd"])
        # Belt and braces: even if the flag were dropped, the child should not
        # inherit a stdin that could feed it a keypress.
        self.assertIsNotNone(seen["stdin"])

    # -- the verification -----------------------------------------------------

    def test_empty_container_raises_even_though_ffmpeg_exited_zero(self):
        """The regression itself: exit 0, valid MP4 header, no video."""
        with patch("subprocess.run", self._fake_run(48)):
            with self.assertRaises(RuntimeError) as ctx:
                camera.generate_timelapse("upper")
        msg = str(ctx.exception)
        self.assertIn("48 bytes", msg)
        # The ffmpeg output is the diagnostic; losing it is what made this take
        # three weeks to find.
        self.assertIn("-1414092869", msg)

    def test_a_real_clip_is_accepted(self):
        with patch("subprocess.run", self._fake_run(342_320)):
            self.assertEqual(camera.generate_timelapse("upper"), camera.timelapse_path("upper"))

    def test_no_frames_still_raises_filenotfound(self):
        with patch.object(config, "TIMELAPSE_DIR", tempfile.mkdtemp()):
            with self.assertRaises(FileNotFoundError):
                camera.generate_timelapse("upper")

    # -- has_video ------------------------------------------------------------

    def test_has_video_rejects_the_stub(self):
        """`os.path.exists` reported this as ready for three weeks."""
        with open(camera.timelapse_path("upper"), "wb") as fh:
            fh.write(b"\0" * 48)
        self.assertTrue(os.path.exists(camera.timelapse_path("upper")))
        self.assertFalse(camera.has_video("upper"))

    def test_has_video_accepts_a_real_clip(self):
        with open(camera.timelapse_path("upper"), "wb") as fh:
            fh.write(b"\0" * 342_320)
        self.assertTrue(camera.has_video("upper"))

    def test_has_video_is_false_when_absent(self):
        self.assertFalse(camera.has_video("lower"))


if __name__ == "__main__":
    unittest.main()
