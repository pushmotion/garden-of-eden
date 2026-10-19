"""Water-level helpers shared by the MQTT service and REST API.

The distance sensor reports the gap (cm) from the sensor down to the water
surface, so a *larger* distance means a *lower* tank.
"""

from datetime import datetime, timezone


def utc_now():
    """The moment to stamp a reading with, and to judge one against."""
    return datetime.now(timezone.utc)


def as_utc(value):
    """``value`` as an offset-aware UTC datetime.

    A naive value is taken as local time: that is what every build before the
    switch to UTC stamps wrote, and what a hand-edited file most likely holds.
    """
    if value.tzinfo is None:
        value = value.astimezone()
    return value.astimezone(timezone.utc)


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


def pump_cutoff(cutoff_cm, alert_cm, empty_cm=None, min_depth_cm=None):
    """The airgap at which the pump is refused, as opposed to merely alerted on.

    ``cutoff_cm`` unset falls back to ``alert_cm``, which is the behaviour from
    before the two were split -- one threshold doing both jobs.

    A cutoff should be a *larger* airgap than the alert, because a larger airgap
    means less water: the tank warns before it stops the pump. When the pair is
    inverted the *smaller* airgap wins -- the earlier trip, the one that keeps
    more water in the tank. This used to fall back to the alert instead, and on
    2026-09-21 an alert persisted from Home Assistant at 25 cm (past the 23.05 cm
    tank floor) made that the effective cutoff: no reading could ever exceed it,
    and the dry-run interlock was silently gone.

    ``empty_cm`` is the tank floor. A resolved cutoff at or past it means "run
    until dry", so it is replaced by ``empty_cm - min_depth_cm``, the same hard
    floor ``cleaning_cutoff`` keeps. Values inside the tank are left alone.
    """
    if not cutoff_cm:
        resolved = alert_cm or None
    elif alert_cm and cutoff_cm < alert_cm:
        resolved = cutoff_cm
    else:
        resolved = cutoff_cm
    if resolved is None:
        return None
    if empty_cm and resolved >= empty_cm:
        floor = empty_cm - (min_depth_cm or 0)
        if floor > 0:
            return floor
    return resolved


def is_plausible_airgap(distance_cm, empty_cm, max_range_cm=100.0, margin_cm=10.0):
    """True when a reading could be a real water surface in this tank.

    ``gpiozero.DistanceSensor`` saturates at ``max_distance`` (1 m), so a missing
    echo -- a dead echo line, a bad header joint, or the reservoir pulled out --
    comes back as exactly 100.00 cm, a well-formed float. Nothing is more than
    ``margin_cm`` below the tank floor either. Neither is a depth; treating one
    as a dry tank refused watering on the very fault the guard fails open on.
    """
    if distance_cm is None:
        return False
    if distance_cm >= max_range_cm:
        return False
    if empty_cm and distance_cm > empty_cm + margin_cm:
        return False
    return distance_cm >= 0


def is_reading_fresh(checked_at_iso, now, max_age_seconds):
    """True when a persisted water reading is recent enough to act on.

    The CLI cannot read the sensor itself -- a second process triggering the
    ultrasonic sensor cross-talks with the service's own polling -- so it acts on
    the verdict the service last wrote. That is only safe while the reading is
    recent; an old one says nothing about the tank now.

    Unparseable or missing timestamps are *not* fresh, so a corrupt state file
    fails open (the pump still runs) rather than bricking watering.

    The comparison is made in UTC. The service stamps readings with
    offset-aware UTC; a naive stamp (an older build, or a hand edit) and a
    naive ``now`` are read as local time. Local time is not safe to compare
    on its own: the hour after a DST fall-back repeats, so a reading taken at
    01:50 EDT judged at 01:05 EST looked forty-five minutes in the future and
    was rejected as stale for the whole hour -- watering failed open and
    cleaning failed closed -- while a spring-forward made a reading an hour
    younger than it was. UTC has no repeated or skipped hour.

    Mixed aware and naive values must never raise: subtracting them is a
    TypeError, and an exception here fails *closed*, the one outcome this
    module exists to avoid. A negative age is still not fresh. A clock stepped
    back by NTP after a reading was stamped must not make that reading look
    authoritative.
    """
    if not checked_at_iso:
        return False
    try:
        checked = as_utc(datetime.fromisoformat(str(checked_at_iso)))
        age = (as_utc(now) - checked).total_seconds()
    except (AttributeError, TypeError, ValueError, OverflowError, OSError):
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


def gallons_from_state(state, full_cm, empty_cm, capacity_gal):
    """Reservoir gallons from the *persisted* airgap, for callers off the sensor.

    The ultrasonic sensor cannot be read by two processes at once -- a second
    reader cross-talks with the MQTT service's polling and both come back wrong
    -- so anything outside that service works from the median-filtered value the
    service last recorded, the same way ``bin/water.sh`` does.

    Returns None when there is no reading to work from. Callers should read that
    as "no opinion", not as "empty": guessing an empty tank would under-dose a
    full one just as badly as the reverse.
    """
    if not isinstance(state, dict):
        return None
    return gallons_remaining(state.get("water_airgap_cm"), full_cm, empty_cm, capacity_gal)


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
