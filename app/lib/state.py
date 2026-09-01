"""Persist actuator state so it can be restored after a power loss or restart
(issue #3). The MQTT service saves on every actuator change and restores on
startup, so the garden returns to its last known light/pump settings.
"""

import json
import logging

import config
from app.lib.persist import write_json_atomic

logger = logging.getLogger(__name__)

DEFAULT_STATE = {
    "light_on": False,
    "brightness": config.DEFAULT_BRIGHTNESS,
    "pump_on": False,
    "speed": config.DEFAULT_PUMP_SPEED,
}


def load_state():
    """Return the persisted state, with DEFAULT_STATE filling any gaps.

    Every key in the file is preserved, not just the ones in DEFAULT_STATE. The
    old behaviour dropped anything else on load, which quietly broke every
    caller that persisted something outside the four actuator fields:
    ``save_state`` wrote the value, and the next ``load_state`` threw it away.

    That is why the "One-Time Pump Run" time reset to 12:00 on every restart
    despite being saved, and why the water verdict this module now carries for
    ``bin/water.sh`` would never have reached it.
    """
    try:
        with open(config.STATE_FILE) as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            raise ValueError("state file is not a JSON object")
        merged = dict(DEFAULT_STATE)
        merged.update(data)
        return merged
    except (FileNotFoundError, ValueError):
        return dict(DEFAULT_STATE)


def save_state(**changes):
    """Merge ``changes`` into the persisted state and write it back."""
    state = load_state()
    state.update(changes)
    try:
        write_json_atomic(config.STATE_FILE, state)
    except OSError as exc:
        logger.error("Failed to persist actuator state: %s", exc)
    return state
