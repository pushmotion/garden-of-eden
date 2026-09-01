"""Dry-run guard for the cron/CLI watering path.

    python -m app.lib.water_guard      # exit 0 = pump allowed, 1 = refused

``bin/water.sh`` runs this before energizing the pump. Upstream's safeguard
(iot-root#83) asked for protection "regardless of how it's called", but it only
ever covered the MQTT command handlers -- so every *unattended* run, which is
most of them, went unguarded.

Why it consults a file rather than the sensor: the ultrasonic sensor cannot be
read by two processes at once. A second reader cross-talks with the MQTT
service's own polling and both come back wrong -- which is how a bogus 14.16 cm
reading nearly became a calibration constant on this tower. So the service
records its median-filtered verdict and this reads that.

The reading has to be recent to mean anything. A stale one is treated as "no
opinion" and the pump is allowed, so a stopped service degrades to the old
behaviour instead of withholding water indefinitely. Every failure here fails
open for the same reason: refusing to water is also a way to kill the plants.
"""

import logging
import sys
from datetime import datetime

import config
from app.lib import state as state_lib
from app.lib.water import is_reading_fresh

logger = logging.getLogger(__name__)

ALLOW, REFUSE = 0, 1


def pump_allowed(state=None, now=None):
    """Return ``(allowed, reason)`` for the persisted water verdict."""
    state = state_lib.load_state() if state is None else state
    now = now or datetime.now()

    checked_at = state.get("water_checked_at")
    if not is_reading_fresh(checked_at, now, config.WATER_READING_MAX_AGE_SECONDS):
        return True, (
            f"no recent water reading (last: {checked_at or 'never'}); "
            "allowing the run so a stopped service cannot withhold water"
        )

    if not state.get("pump_blocked"):
        airgap = state.get("water_airgap_cm")
        return True, f"water above the pump cutoff (airgap {airgap} cm)"

    airgap = state.get("water_airgap_cm")
    return False, (
        f"water below the pump cutoff (airgap {airgap} cm at {checked_at}); "
        "refusing to run the pump dry"
    )


def main(argv=None):
    # A backstop, not decoration. Every branch inside pump_allowed() fails open
    # deliberately, so an *unhandled* exception escaping to here would be the
    # only path that fails closed -- and a crash withholding water indefinitely
    # is worse than the dry-run risk this guard exists to manage. Whatever went
    # wrong, say so and let the run proceed.
    try:
        allowed, reason = pump_allowed()
    except Exception as exc:  # noqa: BLE001 - deliberately broad; see above
        print(f"water guard failed ({exc!r}); allowing the run", file=sys.stderr)
        return ALLOW
    stream = sys.stdout if allowed else sys.stderr
    print(reason, file=stream)
    return ALLOW if allowed else REFUSE


if __name__ == "__main__":
    sys.exit(main())
