"""Bad ambient readings must not reach Home Assistant.

Built from what a live tower actually produced. The AM2320 returned the
*humidity* value in the temperature slot on 2 of 12 readings: a steady 24.0 C
ambient, punctuated by 52.00 (humidity 51.80 that moment) and 56.50 (humidity
56.50 exactly). Home Assistant retains what it is given, so a single bad read
displayed a 125 F room until the next cycle half an hour later.

The drivers already retried on exceptions, which never fired: a desynced read
returns a well-formed float.
"""

import unittest

from app.lib.ambient import (
    HUMIDITY_RANGE_PCT,
    TEMPERATURE_RANGE_C,
    plausible,
    stable_reading,
)


class PlausibilityTestCase(unittest.TestCase):
    def test_real_room_temperatures_pass(self):
        for value in (-5.0, 0.0, 18.5, 24.3, 30.0, 49.9):
            with self.subTest(value=value):
                self.assertTrue(plausible(value, TEMPERATURE_RANGE_C))

    def test_the_readings_that_actually_broke_it_are_rejected(self):
        for value in (52.00, 56.50):
            with self.subTest(value=value):
                self.assertFalse(plausible(value, TEMPERATURE_RANGE_C))

    def test_garbage_is_rejected_without_raising(self):
        for value in (None, "", "warm", float("nan"), float("inf")):
            with self.subTest(value=value):
                self.assertFalse(plausible(value, TEMPERATURE_RANGE_C))

    def test_the_dht20_bogus_zero_humidity_is_rejected(self):
        self.assertFalse(plausible(0.0, HUMIDITY_RANGE_PCT))
        self.assertTrue(plausible(51.8, HUMIDITY_RANGE_PCT))


class StableReadingTestCase(unittest.TestCase):
    def _reader(self, values):
        it = iter(values)

        def read():
            value = next(it)
            if isinstance(value, Exception):
                raise value
            return value

        return read

    def test_one_bad_read_is_outvoted(self):
        """The real-world case: two good reads either side of a desynced one."""
        got = stable_reading(self._reader([24.0, 52.0, 24.2]), TEMPERATURE_RANGE_C, samples=3)
        self.assertAlmostEqual(24.1, got, places=2)

    def test_a_clean_run_returns_the_median(self):
        got = stable_reading(self._reader([24.0, 24.4, 24.2]), TEMPERATURE_RANGE_C, samples=3)
        self.assertAlmostEqual(24.2, got, places=2)

    def test_a_single_survivor_is_still_returned(self):
        """Better one plausible reading than none."""
        got = stable_reading(self._reader([52.0, 24.0, 56.5]), TEMPERATURE_RANGE_C, samples=3)
        self.assertAlmostEqual(24.0, got, places=2)

    def test_exceptions_are_tolerated_while_any_read_succeeds(self):
        got = stable_reading(
            self._reader([OSError("[Errno 5] I/O error"), 24.0, 24.0]),
            TEMPERATURE_RANGE_C,
            samples=3,
        )
        self.assertAlmostEqual(24.0, got, places=2)

    def test_all_reads_failing_raises_the_last_error(self):
        """Callers rely on this: the guard decorator turns it into a 503."""
        err = OSError("[Errno 5] I/O error")
        with self.assertRaises(OSError):
            stable_reading(self._reader([err, err, err]), TEMPERATURE_RANGE_C, samples=3)

    def test_all_reads_implausible_raises_rather_than_publishing_one(self):
        """A wrong number is worse than no number -- HA retains what it is given."""
        with self.assertRaises(ValueError):
            stable_reading(self._reader([52.0, 56.5, 53.1]), TEMPERATURE_RANGE_C, samples=3)


if __name__ == "__main__":
    unittest.main()
