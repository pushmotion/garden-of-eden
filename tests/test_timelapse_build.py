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


class DarkFrameTestCase(unittest.TestCase):
    """Lights-off frames are black and only dilute a growth timelapse.

    Judged by mean luminance, not file size: a dark frame is full of sensor
    noise and compresses *worse* than a lit one (measured medians were 189 KB at
    midnight against 173 KB at midday), so size separates nothing.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        for p in (
            patch.object(config, "TIMELAPSE_DIR", self.tmp),
            patch.object(config, "TIMELAPSE_MIN_LUMA", 40),
        ):
            p.start()
            self.addCleanup(p.stop)
        self.folder = os.path.join(self.tmp, "upper")
        os.makedirs(self.folder, exist_ok=True)

    def _src(self, name="frame.jpg"):
        path = os.path.join(self.tmp, name)
        with open(path, "wb") as fh:
            fh.write(b"\xff\xd8" + b"\0" * 60_000)
        return path

    def test_dark_frame_is_not_archived(self):
        with patch.object(camera, "frame_mean_luma", lambda p: 3):
            camera.archive_frame(self._src(), "upper")
        self.assertEqual(glob.glob(os.path.join(self.folder, "*.jpg")), [])

    def test_lit_frame_is_archived(self):
        with patch.object(camera, "frame_mean_luma", lambda p: 98):
            camera.archive_frame(self._src(), "upper")
        self.assertEqual(len(glob.glob(os.path.join(self.folder, "*.jpg"))), 1)

    def test_unmeasurable_brightness_keeps_the_frame(self):
        """Fails open: dropping a good frame is worse than keeping a dark one."""
        with patch.object(camera, "frame_mean_luma", lambda p: None):
            camera.archive_frame(self._src(), "upper")
        self.assertEqual(len(glob.glob(os.path.join(self.folder, "*.jpg"))), 1)

    def test_threshold_of_zero_disables_the_check(self):
        with patch.object(config, "TIMELAPSE_MIN_LUMA", 0):
            with patch.object(camera, "frame_mean_luma", lambda p: 0):
                camera.archive_frame(self._src(), "upper")
        self.assertEqual(len(glob.glob(os.path.join(self.folder, "*.jpg"))), 1)

    def test_prune_dark_frames_quarantines_existing_archives(self):
        lumas = {}
        for name, luma in (("a.jpg", 98), ("b.jpg", 2), ("c.jpg", 101), ("d.jpg", 0)):
            path = os.path.join(self.folder, name)
            with open(path, "wb") as fh:
                fh.write(b"\xff\xd8" + b"\0" * 60_000)
            lumas[path] = luma

        with patch.object(camera, "frame_mean_luma", lambda p: lumas[p]):
            self.assertEqual(camera.prune_dark_frames("upper"), 2)

        kept = sorted(os.path.basename(f) for f in glob.glob(os.path.join(self.folder, "*.jpg")))
        self.assertEqual(kept, ["a.jpg", "c.jpg"])
        rejected = sorted(
            os.path.basename(f) for f in glob.glob(os.path.join(self.folder, "rejected", "*.jpg"))
        )
        self.assertEqual(rejected, ["b.jpg", "d.jpg"])


class BulkLumaScanTestCase(unittest.TestCase):
    """The bulk scan must never mis-map an index onto the wrong file.

    Spawning one ffmpeg per frame cost ~1.3s on a Pi Zero W -- 12 minutes for a
    579-frame archive. One pass at 1/8 DCT-scaled decode does it in ~0.26s per
    frame. But a misaligned index would quarantine a *good* frame, so a scan
    that cannot be verified must be refused rather than trusted.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        for p in (
            patch.object(config, "TIMELAPSE_DIR", self.tmp),
            patch.object(config, "TIMELAPSE_MIN_LUMA", 40),
        ):
            p.start()
            self.addCleanup(p.stop)
        self.folder = os.path.join(self.tmp, "upper")
        os.makedirs(self.folder, exist_ok=True)
        for name in ("a.jpg", "b.jpg", "c.jpg"):
            with open(os.path.join(self.folder, name), "wb") as fh:
                fh.write(b"\xff\xd8" + b"\0" * 60_000)

    def _ffmpeg_output(self, values):
        lines = []
        for i, v in enumerate(values):
            lines.append(f"frame:{i}    pts:{i}       pts_time:{i}")
            lines.append(f"lavfi.signalstats.YAVG={v}")
        return "\n".join(lines).encode()

    def _patch_run(self, stdout):
        class R:
            pass

        R.stdout = stdout
        R.stderr = b""
        return patch("subprocess.run", lambda *a, **k: R)

    def test_scan_returns_one_value_per_frame(self):
        with self._patch_run(self._ffmpeg_output([92.5, 0.0, 121.4])):
            self.assertEqual(camera.scan_mean_luma(self.folder), [92.5, 0.0, 121.4])

    def test_uses_lowres_and_signalstats(self):
        seen = {}

        class R:
            stdout = self._ffmpeg_output([1, 2, 3])
            stderr = b""

        def run(cmd, **kwargs):
            seen["cmd"] = cmd
            return R

        with patch("subprocess.run", run):
            camera.scan_mean_luma(self.folder)
        self.assertIn("-lowres", seen["cmd"])
        self.assertIn("signalstats,metadata=print:file=-", seen["cmd"])

    def test_short_scan_is_refused_not_guessed(self):
        """ffmpeg stops at an unreadable frame; a short result means the
        index->file mapping is wrong and acting on it moves the wrong files."""
        with self._patch_run(self._ffmpeg_output([92.5])):  # 1 reading, 3 frames
            self.assertIsNone(camera.scan_mean_luma(self.folder))

    def test_gappy_frame_indices_are_refused(self):
        out = b"frame:0\nlavfi.signalstats.YAVG=90\nframe:2\nlavfi.signalstats.YAVG=0\n"
        with self._patch_run(out):
            self.assertIsNone(camera.scan_mean_luma(self.folder))

    def test_prune_uses_the_bulk_scan(self):
        with self._patch_run(self._ffmpeg_output([92.5, 0.0, 121.4])):
            self.assertEqual(camera.prune_dark_frames("upper"), 1)
        kept = sorted(os.path.basename(f) for f in glob.glob(os.path.join(self.folder, "*.jpg")))
        self.assertEqual(kept, ["a.jpg", "c.jpg"])

    def test_prune_falls_back_when_the_scan_cannot_be_trusted(self):
        """Correctness beats speed: an unverifiable scan drops to per-frame."""
        lumas = {
            os.path.join(self.folder, "a.jpg"): 95,
            os.path.join(self.folder, "b.jpg"): 1,
            os.path.join(self.folder, "c.jpg"): 99,
        }
        with (
            patch.object(camera, "scan_mean_luma", lambda f: None),
            patch.object(camera, "frame_mean_luma", lambda p: lumas[p]),
        ):
            self.assertEqual(camera.prune_dark_frames("upper"), 1)
        kept = sorted(os.path.basename(f) for f in glob.glob(os.path.join(self.folder, "*.jpg")))
        self.assertEqual(kept, ["a.jpg", "c.jpg"])

    def test_threshold_of_zero_prunes_nothing(self):
        with patch.object(config, "TIMELAPSE_MIN_LUMA", 0):
            self.assertEqual(camera.prune_dark_frames("upper"), 0)
