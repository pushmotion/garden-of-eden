"""Read-only runtime inventory; no actuator commands or new sensor readers."""

import os
import subprocess
from datetime import datetime
from functools import lru_cache
from pathlib import Path

import config
from app.lib import state
from app.lib.water import is_reading_fresh


@lru_cache(maxsize=1)
def revision():
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=Path(__file__).resolve().parents[2],
            stderr=subprocess.DEVNULL,
            timeout=3,
            text=True,
        ).strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def inventory():
    saved = state.load_state()
    return {
        "pump_maintenance": config.PUMP_MAINTENANCE,
        "revision": revision(),
        "water_checked_at": saved.get("water_checked_at"),
        "water_reading_fresh": is_reading_fresh(
            saved.get("water_checked_at"), datetime.now(), config.WATER_READING_MAX_AGE_SECONDS
        ),
        "camera_devices_present": {
            "upper": os.path.exists(config.UPPER_CAMERA_DEVICE),
            "lower": os.path.exists(config.LOWER_CAMERA_DEVICE),
        },
    }
