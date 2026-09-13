"""Restore scheduled lighting after a temporary cleaning override."""

import logging

from app.lib import state
from app.lib.locking import pump_locked


@pump_locked
def restore(light=None):
    from app.sensors.schedule import schedule

    try:
        expected = schedule.expected_light_state(schedule.load_schedule())
        if expected is None:
            return
        if light is None:
            from app.sensors.light.light import Light

            light = Light()
        on, level = expected
        light.set_brightness(level if on else 0)
        state.save_state(light_on=on, **({"brightness": level} if on else {}))
    except Exception:
        logging.getLogger(__name__).exception("Could not restore scheduled lighting after cleaning")
