"""The pump interlock, and its split from the low-water alert.

One threshold used to do both jobs, which forced a bad trade: early enough to be
a useful "top me up" warning meant refusing to water a tank that still had
plenty. PUMP_CUTOFF_CM separates them.

Both are airgaps -- distance from the sensor down to the water -- so a *larger*
number means *less* water.
"""

import unittest
from datetime import datetime, timedelta
from unittest import mock

from app.lib.water import is_plausible_airgap, is_reading_fresh, is_water_low, pump_cutoff


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

    def test_an_inverted_pair_keeps_the_earlier_trip(self):
        """When the pair is inverted the smaller airgap -- more water -- wins.

        This used to fall back to the alert. On 2026-09-21 an alert persisted
        from Home Assistant at 25 cm, past the 23.05 cm tank floor, became the
        effective cutoff that way: no reading could exceed it and the dry-run
        interlock was silently gone. "Watering Blocked" now tells the user when
        the pump stops, so the earlier trip is the safe choice.
        """
        self.assertEqual(pump_cutoff(9.0, 11.0), 9.0)

    def test_a_cutoff_past_the_tank_floor_is_pulled_back_to_the_min_depth(self):
        """The incident: alert 25.0 persisted, cutoff 12.89, floor 23.05."""
        self.assertEqual(pump_cutoff(None, 25.0, 23.05, 5.0), 18.05)
        self.assertEqual(pump_cutoff(25.0, 9.1, 23.05, 5.0), 18.05)
        self.assertEqual(pump_cutoff(30.0, None, 23.05, 5.0), 18.05)

    def test_a_cutoff_inside_the_tank_is_left_alone(self):
        self.assertEqual(pump_cutoff(17.6, 13.0, 23.05, 5.0), 17.6)
        self.assertEqual(pump_cutoff(15.5, 11.0, 20.0, 5.0), 15.5)

    def test_implausible_airgaps_are_not_readings(self):
        """gpiozero saturates at 1 m: a missing echo is exactly 100.00 cm."""
        self.assertFalse(is_plausible_airgap(100.0, 23.05))
        self.assertFalse(is_plausible_airgap(100.0, None))
        self.assertFalse(is_plausible_airgap(40.0, 23.05), "no tank floor is 17 cm below the floor")
        self.assertFalse(is_plausible_airgap(None, 23.05))
        self.assertFalse(is_plausible_airgap(-1.0, 23.05))
        self.assertTrue(
            is_plausible_airgap(23.9, 23.05), "a hair past the floor is noise, not a fault"
        )
        self.assertTrue(is_plausible_airgap(4.5, 23.05))
        self.assertTrue(is_plausible_airgap(0.0, 20.0))

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


class GuardFailsOpenTestCase(unittest.TestCase):
    """The guard must never fail closed.

    Every branch inside pump_allowed() allows the run when it cannot decide.
    An exception escaping to main() would be the one path that refuses instead,
    and a crash that withholds water indefinitely is worse than the dry-run risk
    the guard manages. Found on real hardware: an offset-aware timestamp in the
    state file raised TypeError, and a *stale* block -- which must fail open --
    exited 1 and refused to water.
    """

    @classmethod
    def setUpClass(cls):
        from app.lib import water_guard

        cls.guard = water_guard

    def test_an_offset_aware_timestamp_is_compared_correctly(self):
        """Converted to local naive rather than raising -- or being discarded."""
        now = datetime(2026, 9, 1, 12, 0, 0)
        # Built in the machine's own zone so the assertion holds anywhere.
        aware = (now - timedelta(seconds=30)).astimezone().isoformat()
        self.assertTrue(is_reading_fresh(aware, now, 540))

        old = (now - timedelta(hours=6)).astimezone().isoformat()
        self.assertFalse(is_reading_fresh(old, now, 540))

    def test_an_aware_stale_block_still_allows_the_run(self):
        now = datetime(2026, 9, 1, 12, 0, 0)
        old = (now - timedelta(hours=6)).astimezone().isoformat()
        allowed, _ = self.guard.pump_allowed(
            {"pump_blocked": True, "water_checked_at": old, "water_airgap_cm": 19.2},
            now=now,
        )
        self.assertTrue(allowed, "a stale block must never withhold water")

    def test_main_allows_when_pump_allowed_raises(self):
        with mock.patch.object(self.guard, "pump_allowed", side_effect=RuntimeError("boom")):
            self.assertEqual(self.guard.ALLOW, self.guard.main())
