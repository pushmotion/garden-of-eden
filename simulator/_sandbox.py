"""Shared sandbox for the simulator entry points.

Both `serve` and `mqtt_sim` need the same two things before importing `app` or
`config`: state redirected somewhere disposable, and the crontab writer stubbed
so a simulated schedule never reaches the host's real cron.

They used to do this separately and had already drifted -- `mqtt_sim` omitted
`LOG_LEVEL`, and *neither* redirected `PODS_FILE`, so saving pods in the
simulator wrote the developer's real `~/.garden_pods.json`. Keeping it in one
place is what stops the next omission.
"""

import os

STATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".sim")


def seed_env(**extra):
    """Point every persisted path at the sandbox. Call before importing config.

    ``setdefault`` throughout, so anything already exported by the caller wins.
    """
    os.makedirs(STATE_DIR, exist_ok=True)

    defaults = {
        "SENSOR_TYPE": "DHT20",
        "GARDYN_MODEL": "gardyn 3.0 (simulated)",
        "WATER_LOW_CM": "11",
        "LOG_LEVEL": "INFO",
        # Every file the app persists. A path missing from this list is a file
        # written into the developer's home directory.
        "STATE_FILE": os.path.join(STATE_DIR, "state.json"),
        "GROW_STATE_FILE": os.path.join(STATE_DIR, "grow.json"),
        "SCHEDULE_FILE": os.path.join(STATE_DIR, "schedule.json"),
        "PODS_FILE": os.path.join(STATE_DIR, "pods.json"),
        "TIMELAPSE_DIR": os.path.join(STATE_DIR, "timelapse"),
    }
    defaults.update(extra)

    for key, value in defaults.items():
        os.environ.setdefault(key, value)


def sandbox_crontab(schedule_module):
    """Hold the compiled cron lines in memory instead of writing the host's.

    Not a no-op returning ``[]``: that made one-time pump runs unobservable
    (armed, then absent from the listing) and left the cron-vs-saved comparison
    permanently reading "0 jobs", so neither could be exercised off-Pi.
    """
    lines = []

    schedule_module._read_crontab = lambda: list(lines)

    def _write(new_lines):
        lines[:] = list(new_lines)

    schedule_module._write_crontab = _write
    return lines
