"""Run the web UI + REST API locally with simulated hardware.

    python -m simulator.serve            # http://localhost:5000/

No Raspberry Pi, pigpiod, or sensors required. Camera endpoints return a
placeholder image so the UI renders end to end.
"""

import base64

from simulator import _sandbox

# Seed sim config BEFORE importing config/app (config reads env at import).
_sandbox.seed_env()

from simulator import fake_hardware  # noqa: E402

fake_hardware.install()

from app import create_app  # noqa: E402
from app.lib.logging_config import configure_logging  # noqa: E402
from app.sensors.camera import camera as _camera  # noqa: E402
from app.sensors.schedule import schedule as _schedule  # noqa: E402

_sandbox.sandbox_crontab(_schedule)

# 1x1 JPEG used as a stand-in for real camera frames.
_PLACEHOLDER_JPEG = base64.b64decode(
    "/9j/4AAQSkZJRgABAQEAYABgAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRof"
    "Hh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/wAALCAABAAEBAREA/8QAFAAB"
    "AAAAAAAAAAAAAAAAAAAACP/EABQQAQAAAAAAAAAAAAAAAAAAAAD/2gAIAQEAAD8AfwD/2Q=="
)


def _fake_capture(device, output_path, resolution=None):
    with open(output_path, "wb") as fh:
        fh.write(_PLACEHOLDER_JPEG)
    return output_path


# Route camera capture to the placeholder (no fswebcam needed).
_camera.capture = _fake_capture

configure_logging()
app = create_app()

if __name__ == "__main__":
    print("Garden of Eden simulator (web + REST) on http://localhost:5000/")
    # Reloader off: single clean process, no double-init of simulated state.
    app.run(debug=True, host="0.0.0.0", port=5000, use_reloader=False)
