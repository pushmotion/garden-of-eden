"""The timelapse build must not report success when it produced no video.

A live tower served a 48-byte mp4 for three weeks while every build "succeeded".
ffmpeg reads stdin for interactive keys; under a service stdin is at EOF, which
it treats as the "q" quit key. It writes the MP4 container header, stops before
the first frame, and **exits 0** -- so `check=True` passes and
`os.path.exists()` says the video is there.

Both halves are tested here: the flag that prevents it, and the verification
that catches it anyway if the flag is ever dropped.
"""

import glob
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
        # Must exceed MIN_FRAME_BYTES or the build quarantines it as a failed
        # capture -- which is the behaviour UnusableFrameTestCase covers.
        with open(os.path.join(folder, "20260830-120000.jpg"), "wb") as fh:
            fh.write(b"\xff\xd8" + b"\0" * 60_000)

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
            # generate_timelapse also shells out to ffprobe to verify the
            # result; only the encode call is under test here.
            if cmd[0] == "ffmpeg":
                seen["cmd"] = cmd
                seen["stdin"] = kwargs.get("stdin")
                with open(cmd[-1], "wb") as fh:
                    fh.write(b"\0" * 50_000)

            class R:
                stderr = b""
                stdout = b"1"

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


class UnusableFrameTestCase(unittest.TestCase):
    """One bad frame must not silently truncate the clip.

    ffmpeg's image2 demuxer stops at the first file it cannot read and exits 0.
    A single zero-byte capture -- archived while a tower's cameras were
    unplugged -- cut a 225-frame timelapse down to 2, and the result was 66 KB,
    comfortably past a size-only check.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        p = patch.object(config, "TIMELAPSE_DIR", self.tmp)
        p.start()
        self.addCleanup(p.stop)
        self.folder = os.path.join(self.tmp, "upper")
        os.makedirs(self.folder, exist_ok=True)

    def _frame(self, name, size):
        with open(os.path.join(self.folder, name), "wb") as fh:
            fh.write(b"\xff\xd8" + b"\0" * max(0, size - 2))

    def test_archive_frame_rejects_a_failed_capture(self):
        src = os.path.join(self.tmp, "empty.jpg")
        open(src, "wb").close()
        camera.archive_frame(src, "upper")
        self.assertEqual(glob.glob(os.path.join(self.folder, "*.jpg")), [])

    def test_archive_frame_accepts_a_real_capture(self):
        src = os.path.join(self.tmp, "good.jpg")
        with open(src, "wb") as fh:
            fh.write(b"\xff\xd8" + b"\0" * 60_000)
        camera.archive_frame(src, "upper")
        self.assertEqual(len(glob.glob(os.path.join(self.folder, "*.jpg"))), 1)

    def test_build_quarantines_a_bad_frame_and_still_encodes(self):
        self._frame("20260912-201506.jpg", 60_000)
        self._frame("20260912-202028.jpg", 60_000)
        self._frame("20260912-202547.jpg", 0)  # the poison frame
        self._frame("20260913-121908.jpg", 60_000)

        def run(cmd, **kwargs):
            with open(cmd[-1], "wb") as fh:
                fh.write(b"\0" * 50_000)

            class R:
                stderr = b""

            return R()

        with (
            patch("subprocess.run", run),
            patch.object(camera, "_encoded_frame_count", lambda p: 3),
        ):
            camera.generate_timelapse("upper")

        # The bad frame is moved aside, not deleted: it is evidence of when the
        # camera was failing.
        self.assertEqual(len(glob.glob(os.path.join(self.folder, "*.jpg"))), 3)
        rejected = glob.glob(os.path.join(self.folder, "rejected", "*.jpg"))
        self.assertEqual(len(rejected), 1)
        self.assertIn("20260912-202547", rejected[0])

    def test_short_encode_raises_even_when_the_file_is_large_enough(self):
        """The 66 KB / 2-frame case: big enough to pass a byte check, still wrong."""
        for i in range(10):
            self._frame(f"2026091{i}-120000.jpg", 60_000)

        def run(cmd, **kwargs):
            with open(cmd[-1], "wb") as fh:
                fh.write(b"\0" * 66_267)

            class R:
                stderr = b""

            return R()

        with (
            patch("subprocess.run", run),
            patch.object(camera, "_encoded_frame_count", lambda p: 2),
        ):
            with self.assertRaises(RuntimeError) as ctx:
                camera.generate_timelapse("upper")
        self.assertIn("only 2 of 10 frames", str(ctx.exception))

    def test_unverifiable_encode_is_allowed(self):
        """ffprobe missing must not fail an otherwise good build."""
        self._frame("20260912-201506.jpg", 60_000)

        def run(cmd, **kwargs):
            with open(cmd[-1], "wb") as fh:
                fh.write(b"\0" * 50_000)

            class R:
                stderr = b""

            return R()

        with (
            patch("subprocess.run", run),
            patch.object(camera, "_encoded_frame_count", lambda p: None),
        ):
            self.assertTrue(camera.generate_timelapse("upper"))
