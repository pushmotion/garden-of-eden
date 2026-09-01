"""The pump interlock, and its split from the low-water alert.

One threshold used to do both jobs, which forced a bad trade: early enough to be
a useful "top me up" warning meant refusing to water a tank that still had
plenty. PUMP_CUTOFF_CM separates them.

Both are airgaps -- distance from the sensor down to the water -- so a *larger*
number means *less* water.
"""

import unittest
from datetime import datetime, timedelta

from app.lib.water import is_reading_fresh, is_water_low, pump_cutoff


class PumpCutoffResolutionTestCase(unittest.TestCase):
    def test_unset_cutoff_falls_back_to_the_alert(self):
        """Exactly the behaviour before the split, so upgrading changes nothing."""
        self.assertEqual(pump_cutoff(None, 11.0), 11.0)
        self.assertEqual(pump_cutoff(0, 11.0), 11.0)

    def test_cutoff_is_used_when_set(self):
        self.assertEqual(pump_cutoff(15.5, 11.0), 15.5)

    def test_both_unset_means_no_interlock(self):
        self.assertIsNone(pump_cutoff(None, None))
        self.assertIsNone(pump_cutoff(0, 0))

    def test_cutoff_without_an_alert_still_works(self):
        """Someone may want the interlock and no alert at all."""
        self.assertEqual(pump_cutoff(15.5, None), 15.5)

    def test_an_inverted_pair_falls_back_to_the_alert(self):
        """A cutoff must sit at a larger airgap than the alert.

        Otherwise the pump stops before the user is ever told the tank is low --
        watering silently ceases with no warning shown anywhere. Falling back to
        the alert keeps the earlier, safer of the two.
        """
        self.assertEqual(pump_cutoff(9.0, 11.0), 11.0)

    def test_the_split_leaves_a_band_that_warns_without_blocking(self):
        """The whole point: warn at 55%, keep watering until 30%.

        Airgaps for this tower's geometry (full 4.81, empty 23.05).
        """
        alert, cutoff = 13.0, 17.6
        effective = pump_cutoff(cutoff, alert)

        half_full = 15.0  # between the two thresholds
        self.assertTrue(is_water_low(half_full, alert), "should be warning by now")
        self.assertFalse(
            is_water_low(half_full, effective),
            "must still water -- the alert is a nudge, not a stop",
        )

        nearly_dry = 19.0
        self.assertTrue(is_water_low(nearly_dry, alert))
        self.assertTrue(is_water_low(nearly_dry, effective), "below the cutoff, refuse")


class ReadingFreshnessTestCase(unittest.TestCase):
    """The cron path acts on a stored reading, so its age is the safety margin."""

    def setUp(self):
        self.now = datetime(2026, 8, 31, 12, 0, 0)

    def test_a_recent_reading_is_fresh(self):
        recent = (self.now - timedelta(seconds=60)).isoformat()
        self.assertTrue(is_reading_fresh(recent, self.now, 540))

    def test_an_old_reading_is_not(self):
        old = (self.now - timedelta(seconds=900)).isoformat()
        self.assertFalse(is_reading_fresh(old, self.now, 540))

    def test_missing_or_corrupt_timestamps_are_not_fresh(self):
        """Fails open: an unreadable state file must not withhold water."""
        for value in (None, "", "not-a-date", 12345):
            with self.subTest(value=value):
                self.assertFalse(is_reading_fresh(value, self.now, 540))

    def test_a_future_timestamp_is_not_fresh(self):
        """Clock skew must not make a bogus reading look authoritative."""
        future = (self.now + timedelta(seconds=300)).isoformat()
        self.assertFalse(is_reading_fresh(future, self.now, 540))


class WaterGuardDecisionTestCase(unittest.TestCase):
    """The verdict bin/water.sh acts on, exercised without touching the sensor."""

    @classmethod
    def setUpClass(cls):
        from app.lib import water_guard

        cls.guard = water_guard

    def setUp(self):
        self.now = datetime(2026, 8, 31, 12, 0, 0)
        self.fresh = (self.now - timedelta(seconds=30)).isoformat()

    def test_blocked_and_fresh_refuses(self):
        allowed, reason = self.guard.pump_allowed(
            {"pump_blocked": True, "water_checked_at": self.fresh, "water_airgap_cm": 19.2},
            now=self.now,
        )
        self.assertFalse(allowed)
        self.assertIn("19.2", reason)

    def test_clear_and_fresh_allows(self):
        allowed, _ = self.guard.pump_allowed(
            {"pump_blocked": False, "water_checked_at": self.fresh, "water_airgap_cm": 8.0},
            now=self.now,
        )
        self.assertTrue(allowed)

    def test_a_stale_block_allows(self):
        """A stopped service must not withhold water indefinitely.

        Refusing to water is also a way to kill the plants, so once the reading
        stops describing the tank the guard steps aside.
        """
        stale = (self.now - timedelta(hours=6)).isoformat()
        allowed, reason = self.guard.pump_allowed(
            {"pump_blocked": True, "water_checked_at": stale, "water_airgap_cm": 19.2},
            now=self.now,
        )
        self.assertTrue(allowed)
        self.assertIn("no recent water reading", reason)

    def test_an_empty_state_allows(self):
        """Before the service has ever run, there is no verdict to act on."""
        allowed, reason = self.guard.pump_allowed({}, now=self.now)
        self.assertTrue(allowed)
        self.assertIn("never", reason)


if __name__ == "__main__":
    unittest.main()
