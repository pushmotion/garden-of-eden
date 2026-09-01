"""Capture stills from the Gardyn USB cameras via fswebcam.

Shared by the REST camera endpoints and the MQTT image publisher so capture
behavior lives in one place.
"""

import datetime
import glob
import logging
import os
import shutil
import subprocess
import threading

import config

logger = logging.getLogger(__name__)


# One capture at a time, process-wide. Two fswebcam runs against the same USB
# node fail, and there are three callers that can overlap: the MQTT service's
# hourly publish thread, the thread `refresh/all` spawns, and the REST camera
# endpoints. Serialising here rather than in each caller means a new caller
# cannot reintroduce the collision by forgetting.
#
# This does not cover the Flask process racing the MQTT service -- separate
# processes need a file lock for that -- but it does cover every overlap within
# one process, which includes the easiest to trigger (Refresh All landing on
# top of the hourly capture).
_capture_lock = threading.Lock()


def capture(device, output_path, resolution=None):
    """Capture a frame from ``device`` to ``output_path``.

    Returns the output path on success, or raises CalledProcessError/OSError.
    Blocks while another capture is in flight.
    """
    resolution = resolution or config.CAMERA_RESOLUTION
    cmd = [
        "fswebcam",
        "-d",
        device,
        "--no-banner",
        "-r",
        resolution,
        "-S",
        "2",  # skip initial frames so exposure settles
        "-F",
        "2",  # then average two, which is what the MQTT path has always done
        output_path,
    ]
    with _capture_lock:
        logger.info("Capturing image from %s -> %s", device, output_path)
        subprocess.run(cmd, capture_output=True, check=True)
    return output_path


def capture_upper():
    return capture(config.UPPER_CAMERA_DEVICE, config.UPPER_IMAGE_PATH)


def capture_lower():
    return capture(config.LOWER_CAMERA_DEVICE, config.LOWER_IMAGE_PATH)


# --- Timelapse: archive frames over time, assemble into mp4 with ffmpeg --------

CAMERAS = ("upper", "lower")


def _frames_dir(cam):
    path = os.path.join(config.TIMELAPSE_DIR, cam)
    os.makedirs(path, exist_ok=True)
    return path


def timelapse_path(cam):
    """Path to the assembled mp4 for ``cam`` (may not exist yet)."""
    return os.path.join(config.TIMELAPSE_DIR, f"{cam}.mp4")


def archive_frame(src_path, cam):
    """Save a timestamped copy of ``src_path`` into the timelapse archive and
    prune to TIMELAPSE_MAX_FRAMES. Best-effort: never raises."""
    try:
        folder = _frames_dir(cam)
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        shutil.copy(src_path, os.path.join(folder, f"{stamp}.jpg"))
        frames = sorted(glob.glob(os.path.join(folder, "*.jpg")))
        for stale in frames[: max(0, len(frames) - config.TIMELAPSE_MAX_FRAMES)]:
            os.remove(stale)
    except Exception as exc:  # noqa: BLE001 - archiving must never break capture
        logger.error("Timelapse archive failed for %s: %s", cam, exc)


def _frame_stamp(path):
    """Parse the capture time back out of an archived frame's filename."""
    try:
        name = os.path.basename(path).split(".")[0]
        return datetime.datetime.strptime(name, "%Y%m%d-%H%M%S").isoformat()
    except ValueError:
        return None


def framerate_for(count):
    """Frame rate to assemble ``count`` frames at.

    A week of hourly frames at the full rate is only a few seconds long, so slow
    short archives down toward TIMELAPSE_TARGET_SECONDS rather than letting them
    flash past. Never below 1fps, never above TIMELAPSE_FPS.
    """
    target = count / float(max(1, config.TIMELAPSE_TARGET_SECONDS))
    return max(1, min(config.TIMELAPSE_FPS, int(round(target))))


def clip_seconds(count):
    """How long the assembled clip would run, at the rate framerate_for picks."""
    if not count:
        return 0.0
    return round(count / float(framerate_for(count)), 1)


def frame_stats(cam):
    """Archived frame count plus first/last capture times, so the UI can say how
    much history exists before a build is worth doing."""
    frames = sorted(glob.glob(os.path.join(_frames_dir(cam), "*.jpg")))
    return {
        "frames": len(frames),
        "first": _frame_stamp(frames[0]) if frames else None,
        "last": _frame_stamp(frames[-1]) if frames else None,
        "seconds": clip_seconds(len(frames)),
    }


def generate_timelapse(cam):
    """Assemble the archived frames for ``cam`` into an mp4. Raises
    FileNotFoundError if no frames have been archived yet."""
    folder = _frames_dir(cam)
    frames = glob.glob(os.path.join(folder, "*.jpg"))
    if not frames:
        raise FileNotFoundError("no frames archived yet")
    out = timelapse_path(cam)
    cmd = [
        "ffmpeg",
        "-y",
        "-framerate",
        str(framerate_for(len(frames))),
        "-pattern_type",
        "glob",
        "-i",
        os.path.join(folder, "*.jpg"),
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        out,
    ]
    logger.info("Assembling timelapse for %s -> %s", cam, out)
    subprocess.run(cmd, capture_output=True, check=True)
    return out
