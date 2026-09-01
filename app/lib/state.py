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

    Every key in the file is preserved, not only those in DEFAULT_STATE. The old
    behaviour dropped anything else on load, so ``save_state(**changes)`` -- which
    accepts any key -- would write a value that the next ``load_state`` silently
    threw away. Nothing in tree relied on that yet, which is exactly why it was
    worth fixing before something did.
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
