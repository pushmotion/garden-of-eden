"""Reject bad ambient readings before they reach Home Assistant.

The AM2320 intermittently returns the *humidity* value in the temperature slot.
Observed on a live tower: a steady 24.0 C ambient, with occasional readings of
52.00 (humidity 51.80 at the same moment) and 56.50 (humidity 56.50 exactly).

That failure is nastier than the I2C errors the drivers already retry on,
because nothing raises. A desynced read returns a well-formed float, the retry
loop never fires, and the value is published. Home Assistant retains it, so one
bad read shows a 125 F room until the next cycle half an hour later.

Two defences here, because neither is sufficient alone:

* A **median of several reads**, which is how the distance sensor already
  rejects ultrasonic spikes. An occasional wrong value gets outvoted.
* A **plausibility band**, because a desynced read is only detectable as an
  outlier when humidity and temperature happen to differ. If humidity drifted to
  25%, a desynced temperature of 25 C would look perfectly reasonable and no
  amount of sampling would catch it -- but it would still be wrong.

Nothing here can catch the case where the wrong value is also a believable one.
That is a sensor limitation; the goal is to stop the obviously-wrong values,
which is what people actually notice.
"""

import logging

logger = logging.getLogger(__name__)

# Ambient air around a hydroponic tower. Deliberately wider than any healthy
# growing range -- this is a "the sensor is lying" filter, not a grow alarm.
TEMPERATURE_RANGE_C = (-10.0, 50.0)
# Lower bound is above zero on purpose: the DHT20 returns a bogus 0% on a
# collision, and the humidity driver was already discarding exactly that.
HUMIDITY_RANGE_PCT = (0.1, 100.0)


def _median(values):
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def plausible(value, bounds):
    """True when ``value`` is a real number inside ``bounds`` (inclusive)."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    if number != number:  # NaN
        return False
    low, high = bounds
    return low <= number <= high


def stable_reading(read, bounds, samples=3, label="sensor"):
    """Median of several reads, ignoring implausible ones.

    ``read`` is called up to ``samples`` times. Values outside ``bounds`` are
    discarded with a warning -- they are the symptom worth surfacing, since a
    sensor doing this regularly wants replacing rather than filtering.

    Raises the last exception if every read failed, matching what the callers
    did before. Returns the median of whatever survived.
    """
    good, last_exc = [], None
    for _ in range(max(1, samples)):
        try:
            value = read()
        except Exception as exc:  # noqa: BLE001 - caller decides what to do
            last_exc = exc
            continue
        if plausible(value, bounds):
            good.append(float(value))
        else:
            logger.warning("Discarding implausible %s reading: %r", label, value)

    if good:
        return _median(good)
    if last_exc is not None:
        raise last_exc
    raise ValueError(f"no plausible {label} reading in {samples} attempts")
