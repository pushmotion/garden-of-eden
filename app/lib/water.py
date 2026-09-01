"""Water-level helpers shared by the MQTT service and REST API.

The distance sensor reports the gap (cm) from the sensor down to the water
surface, so a *larger* distance means a *lower* tank.
"""


def is_water_low(distance_cm, threshold_cm):
    """Return True if the tank is low.

    ``threshold_cm`` of ``None``/0 means alerting is disabled -> never low.
    A ``distance_cm`` of ``None`` (failed reading) is treated as "not low" so a
    sensor glitch doesn't raise a false alarm.
    """
    if not threshold_cm:
        return False
    if distance_cm is None:
        return False
    return distance_cm > threshold_cm


def pump_cutoff(cutoff_cm, alert_cm):
    """The airgap at which the pump is refused, as opposed to merely alerted on.

    ``cutoff_cm`` unset falls back to ``alert_cm``, which is the behaviour from
    before the two were split -- one threshold doing both jobs.

    A cutoff must be a *larger* airgap than the alert, because a larger airgap
    means less water: the tank has to warn before it stops the pump. A cutoff
    that trips first would refuse to water a tank the user was never told was
    low, so an inverted pair is rejected in favour of the alert value, which
    fails toward the earlier, safer of the two.
    """
    if not cutoff_cm:
        return alert_cm or None
    if alert_cm and cutoff_cm < alert_cm:
        return alert_cm
    return cutoff_cm


def is_reading_fresh(checked_at_iso, now, max_age_seconds):
    """True when a persisted water reading is recent enough to act on.

    The CLI cannot read the sensor itself -- a second process triggering the
    ultrasonic sensor cross-talks with the service's own polling -- so it acts on
    the verdict the service last wrote. That is only safe while the reading is
    recent; an old one says nothing about the tank now.

    Unparseable or missing timestamps are *not* fresh, so a corrupt state file
    fails open (the pump still runs) rather than bricking watering.

    ``now`` is naive local time. The service writes naive timestamps too, but an
    offset-aware one (a hand-edited file, or some future writer) must not raise
    -- subtracting mixed datetimes is a TypeError, and an exception here fails
    *closed*, which is the one outcome this module exists to avoid. Aware values
    are converted to local naive first.
    """
    if not checked_at_iso:
        return False
    try:
        from datetime import datetime

        checked = datetime.fromisoformat(str(checked_at_iso))
        if checked.tzinfo is not None:
            checked = checked.astimezone().replace(tzinfo=None)
        age = (now - checked).total_seconds()
    except (TypeError, ValueError, OverflowError, OSError):
        return False
    return 0 <= age <= max_age_seconds


def gallons_remaining(distance_cm, full_cm, empty_cm, capacity_gal):
    """Estimate gallons left from the sensor distance via a linear full->empty map.

    ``full_cm`` is the distance when the tank is full (small), ``empty_cm`` when
    empty (large). Returns None on a bad reading or invalid calibration; clamps
    to the 0..capacity range otherwise.
    """
    if distance_cm is None or not capacity_gal:
        return None
    span = empty_cm - full_cm
    if span <= 0:
        return None
    fraction = (empty_cm - distance_cm) / span
    fraction = max(0.0, min(1.0, fraction))
    return round(fraction * capacity_gal, 1)


def tank_readings(distance_cm, full_cm, empty_cm, capacity_gal):
    """Every derived water figure, from one airgap and one calibration.

    Returns ``{"level_cm", "depth_cm", "percent", "gallons"}`` -- the numbers
    Home Assistant and the web UI both display. This exists so the two cannot
    disagree. Before it, the MQTT service derived depth and percent inline while
    the web page re-derived percent in JavaScript against the *default* 5/20 cm
    calibration, so any tower calibrated to anything else showed two different
    fill levels for the same tank.

    Derived values are ``None`` when the reading failed, or when the calibration
    is unusable because ``empty_cm`` is not the larger airgap of the pair.
    ``level_cm`` passes through regardless: the raw airgap is what the hardware
    measured, and it stays true whether or not the tank has been calibrated.

    ``depth_cm`` is measured up from the tank floor and is only clamped at zero,
    not at the top -- a tank filled past the calibrated full mark should read as
    the overfill it is rather than being quietly capped.
    """
    readings = {"level_cm": distance_cm, "depth_cm": None, "percent": None, "gallons": None}
    if distance_cm is None:
        return readings

    span = empty_cm - full_cm
    if span <= 0:
        return readings

    readings["depth_cm"] = max(0.0, empty_cm - distance_cm)
    fraction = max(0.0, min(1.0, (empty_cm - distance_cm) / span))
    readings["percent"] = fraction * 100.0
    readings["gallons"] = gallons_remaining(distance_cm, full_cm, empty_cm, capacity_gal)
    return readings
