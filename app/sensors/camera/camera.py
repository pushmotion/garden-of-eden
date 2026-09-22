"""Capture stills from the Gardyn USB cameras via fswebcam.

Shared by the REST camera endpoints and the MQTT image publisher so capture
behavior lives in one place.
"""

import datetime
import glob
import logging
import os
import re
import shutil
import subprocess
import threading

import config
from app.lib.locking import file_lock

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
    with _capture_lock, file_lock(config.STATE_FILE + ".camera.lock"):
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


# An MP4 that failed to encode is still a *structurally valid* MP4: ffmpeg writes
# the `ftyp`/`free`/`mdat` header before it has a single frame, which lands at
# exactly 48 bytes. So neither the file existing nor ffmpeg's exit status proves
# a build produced video -- see generate_timelapse(). A real clip is kilobytes
# even for one frame, so anything this small is an empty container.
MIN_VIDEO_BYTES = 1024

# ffmpeg's image2 demuxer stops at the first frame it cannot read, so a single
# unusable file truncates the whole clip from that point -- and still exits 0.
# One zero-byte frame, archived while a tower's cameras were unplugged, cut a
# 225-frame timelapse down to 2. A real 640x480 capture is tens of kilobytes.
MIN_FRAME_BYTES = 1024


def frame_mean_luma(path):
    """Mean luminance of a frame, 0-255, or None if it cannot be determined.

    Scaling an image to a single pixel *is* its mean, so this is one cheap
    ffmpeg call rather than an image library -- and ffmpeg is already a hard
    dependency of the timelapse path.
    """
    try:
        proc = subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-i",
                path,
                "-vf",
                "scale=1:1",
                "-f",
                "rawvideo",
                "-pix_fmt",
                "gray",
                "-",
            ],
            capture_output=True,
            stdin=subprocess.DEVNULL,
            timeout=60,
        )
        return proc.stdout[0] if proc.stdout else None
    except Exception:  # noqa: BLE001 - brightness is advisory, never fatal
        return None


def is_too_dark(path):
    """True when a frame is lights-off black and not worth keeping.

    Fails *open*: a frame whose brightness cannot be measured is kept. Dropping
    a good frame is worse than keeping a dark one, and this runs unattended.
    """
    if not config.TIMELAPSE_MIN_LUMA:
        return False
    luma = frame_mean_luma(path)
    return luma is not None and luma < config.TIMELAPSE_MIN_LUMA


_YAVG_RE = re.compile(r"^lavfi\.signalstats\.YAVG=([\d.]+)", re.MULTILINE)
_FRAME_RE = re.compile(r"^frame:(\d+)", re.MULTILINE)


def scan_mean_luma(folder):
    """Mean luma for every frame in ``folder``, in one ffmpeg pass.

    Returns a list aligned with ``sorted(glob("*.jpg"))``, or None when the scan
    cannot be trusted.

    Spawning one ffmpeg per frame costs ~1.3s on a Pi Zero W -- 12 minutes for a
    579-frame archive, almost all of it process startup. One pass at 1/8 DCT-
    scaled decode does the same work in ~0.26s per frame.

    ``-lowres 3`` decodes JPEG at an eighth of full size straight out of the DCT,
    which is far cheaper than a full decode and leaves the mean unchanged for
    this purpose: measured against a full decode the values agreed to within
    0.1% (92.58 vs 92.51, 121.55 vs 121.47).

    signalstats rather than the cheaper ``scale=1:1`` trick used for single
    frames, because it prints a ``frame:N`` marker per reading. A bare byte
    stream gives no way to notice a skipped frame, and a misaligned index here
    would quarantine the *wrong* file -- so alignment has to be checkable.
    """
    frames = sorted(glob.glob(os.path.join(folder, "*.jpg")))
    if not frames:
        return []
    try:
        proc = subprocess.run(
            [
                "ffmpeg",
                "-nostdin",
                "-v",
                "error",
                "-lowres",
                "3",
                "-f",
                "image2",
                "-pattern_type",
                "glob",
                "-i",
                os.path.join(folder, "*.jpg"),
                "-vf",
                "signalstats,metadata=print:file=-",
                "-f",
                "null",
                "-",
            ],
            capture_output=True,
            stdin=subprocess.DEVNULL,
            timeout=1800,
        )
        out = proc.stdout.decode("utf-8", "replace")
        values = [float(v) for v in _YAVG_RE.findall(out)]
        indices = [int(i) for i in _FRAME_RE.findall(out)]
    except Exception:  # noqa: BLE001 - the caller falls back to per-frame
        logger.warning("Bulk luma scan failed for %s; falling back", folder)
        return None

    # Refuse to guess. ffmpeg's image2 demuxer stops at the first frame it
    # cannot read, so a short or gappy result means the index->file mapping is
    # wrong, and acting on it would move frames that are perfectly good.
    if len(values) != len(frames) or indices != list(range(len(frames))):
        logger.warning(
            "Bulk luma scan for %s returned %d readings for %d frames; falling back",
            folder,
            len(values),
            len(frames),
        )
        return None
    return values


def prune_dark_frames(cam):
    """Quarantine already-archived dark frames. Returns how many were moved.

    For archives captured before the brightness check existed; ordinary captures
    are filtered at archive time so the weekly build stays fast.
    """
    folder = _frames_dir(cam)
    reject_dir = os.path.join(folder, "rejected")
    frames = sorted(glob.glob(os.path.join(folder, "*.jpg")))
    threshold = config.TIMELAPSE_MIN_LUMA
    if not threshold or not frames:
        return 0

    lumas = scan_mean_luma(folder)
    if lumas is None:
        # One process per frame: correct but ~5x slower. Only reached when the
        # bulk scan could not be verified.
        lumas = [frame_mean_luma(f) for f in frames]

    moved = 0
    for frame, luma in zip(frames, lumas):
        if luma is None or luma >= threshold:
            continue
        os.makedirs(reject_dir, exist_ok=True)
        shutil.move(frame, os.path.join(reject_dir, os.path.basename(frame)))
        moved += 1
    if moved:
        logger.info("Quarantined %d dark %s frames", moved, cam)
    return moved


def has_video(cam):
    """True when an assembled clip exists *and* is more than an empty container.

    Deliberately not ``os.path.exists``: that reported "ready" for months on a
    tower whose every build had silently produced a 48-byte stub.
    """
    try:
        return os.path.getsize(timelapse_path(cam)) >= MIN_VIDEO_BYTES
    except OSError:
        return False


def archive_frame(src_path, cam):
    """Save a timestamped copy of ``src_path`` into the timelapse archive and
    prune to TIMELAPSE_MAX_FRAMES. Best-effort: never raises."""
    try:
        # A failed capture leaves a zero-byte file behind. Archiving it poisons
        # every future build, because the encoder stops there -- so reject it at
        # the door rather than discovering it weeks later in a truncated clip.
        if os.path.getsize(src_path) < MIN_FRAME_BYTES:
            logger.warning(
                "Not archiving %s frame: source is %d bytes, capture likely failed",
                cam,
                os.path.getsize(src_path),
            )
            return
        # Captures run hourly around the clock; the lights-off ones are black
        # and only dilute the timelapse. Cheaper to reject here than to decode
        # the whole archive at build time.
        if is_too_dark(src_path):
            logger.info("Not archiving %s frame: too dark (lights off)", cam)
            return
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

    # Quarantine anything the encoder would choke on. Moved aside rather than
    # deleted -- they are worthless as video but they are still evidence of when
    # a camera was failing, and this runs unattended.
    unusable = [f for f in frames if os.path.getsize(f) < MIN_FRAME_BYTES]
    if unusable:
        reject_dir = os.path.join(folder, "rejected")
        os.makedirs(reject_dir, exist_ok=True)
        for bad in unusable:
            logger.warning(
                "Quarantining unusable %s frame %s (%d bytes)",
                cam,
                os.path.basename(bad),
                os.path.getsize(bad),
            )
            shutil.move(bad, os.path.join(reject_dir, os.path.basename(bad)))
        frames = [f for f in frames if f not in set(unusable)]
        if not frames:
            raise FileNotFoundError("no usable frames archived yet")
    out = timelapse_path(cam)
    cmd = [
        "ffmpeg",
        # Without this ffmpeg reads stdin looking for interactive keys. Under a
        # service (or any non-tty parent) stdin is at EOF, which it takes as the
        # "q" quit key: it writes the container header, stops before the first
        # frame, and **exits 0**. That produced a 48-byte mp4 on a live tower for
        # three weeks while every build reported success.
        "-nostdin",
        "-y",
        "-framerate",
        str(framerate_for(len(frames))),
        "-pattern_type",
        "glob",
        "-i",
        os.path.join(folder, "*.jpg"),
        "-c:v",
        "libx264",
        "-preset",
        config.TIMELAPSE_PRESET,
        "-pix_fmt",
        "yuv420p",
        out,
    ]
    logger.info("Assembling timelapse for %s (%d frames) -> %s", cam, len(frames), out)
    proc = subprocess.run(cmd, capture_output=True, check=True, stdin=subprocess.DEVNULL)

    # check=True is not enough on its own: the failure mode above exits 0. Verify
    # the artifact instead of trusting the status, so a build that produced no
    # video fails loudly here rather than being served as a broken clip.
    size = os.path.getsize(out) if os.path.exists(out) else 0
    if size < MIN_VIDEO_BYTES:
        stderr = (proc.stderr or b"").decode("utf-8", "replace").strip()
        raise RuntimeError(
            f"ffmpeg produced no video for {cam} ({size} bytes from {len(frames)} "
            f"frames); last output: {stderr[-500:] or '(none)'}"
        )
    # Size alone is not enough either: a 2-frame clip out of 225 was 66 KB and
    # sailed past the byte check. Compare what the file actually contains with
    # what went in. Best-effort -- a missing or unhappy ffprobe must not fail an
    # otherwise good build.
    encoded = _encoded_frame_count(out)
    if encoded is not None and encoded < len(frames) * 0.9:
        raise RuntimeError(
            f"ffmpeg encoded only {encoded} of {len(frames)} frames for {cam}; "
            "the archive likely contains a frame it cannot read"
        )

    logger.info(
        "Timelapse for %s assembled: %d bytes, %s frames",
        cam,
        size,
        encoded if encoded is not None else len(frames),
    )
    return out


def _encoded_frame_count(path):
    """Frames actually present in ``path``, or None if ffprobe cannot say."""
    try:
        proc = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-count_frames",
                "-show_entries",
                "stream=nb_read_frames",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                path,
            ],
            capture_output=True,
            stdin=subprocess.DEVNULL,
            timeout=300,
        )
        return int(proc.stdout.decode().strip())
    except Exception:  # noqa: BLE001 - verification is advisory, never fatal
        return None
