"""Cleaning mode: the long-run budget, its cutoff, and its session model.

The property under test throughout is that a cleaning run is bounded in *two*
independent ways -- by a deadline that survives the process holding it, and by a
water cutoff re-evaluated for the whole run -- because a two-hour pump run has
time to go wrong in ways a five-minute one does not.
"""

import os
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

import config
from app.lib import cleaning


class CleaningCutoffTestCase(unittest.TestCase):
    """``cleaning_cutoff`` is the one number standing between a two-hour run and
    a dry pump, so it is tested for what it refuses as much as what it allows."""

    def test_unset_falls_back_to_the_watering_cutoff(self):
        # A tower that never configures cleaning must be exactly as protected as
        # it is today -- not less.
        self.assertEqual(cleaning.cleaning_cutoff(None, 12.9, 23.05, 5.0), 12.9)

    def test_configured_value_wins_over_the_watering_cutoff(self):
        # The whole point: cleaning tolerates a lower tank than watering does.
        self.assertEqual(cleaning.cleaning_cutoff(18.0, 12.9, 23.05, 5.0), 18.0)

    def test_nothing_configured_anywhere_means_no_interlock(self):
        self.assertIsNone(cleaning.cleaning_cutoff(None, None, 23.05, 5.0))

    def test_cutoff_is_clamped_to_leave_the_minimum_depth(self):
        # 22.0 cm of airgap in a 23.05 cm tank is a centimetre of water: "run
        # until dry" by another name. It gets clamped back to empty - min_depth.
        self.assertEqual(cleaning.cleaning_cutoff(22.0, 12.9, 23.05, 5.0), 23.05 - 5.0)

    def test_clamp_does_not_raise_a_conservative_cutoff(self):
        # Clamping is a ceiling, never a floor: a stricter cutoff stays strict.
        self.assertEqual(cleaning.cleaning_cutoff(10.0, 12.9, 23.05, 5.0), 10.0)

    def test_clamp_skipped_when_the_calibration_cannot_support_it(self):
        # A tank shallower than the required depth is a configuration problem
        # this function cannot fix; inventing a number would be worse than
        # passing the request through.
        self.assertEqual(cleaning.cleaning_cutoff(18.0, 12.9, 4.0, 5.0), 18.0)

    def test_effective_cutoff_reads_config(self):
        with (
            patch.object(config, "CLEANING_CUTOFF_CM", 17.0),
            patch.object(config, "PUMP_CUTOFF_CM", 12.9),
            patch.object(config, "WATER_EMPTY_CM", 23.05),
            patch.object(config, "CLEANING_MIN_DEPTH_CM", 5.0),
        ):
            self.assertEqual(cleaning.effective_cutoff(), 17.0)


class CleaningDurationTestCase(unittest.TestCase):
    def test_default_applies_when_unspecified(self):
        with (
            patch.object(config, "CLEANING_DEFAULT_SECONDS", 3600),
            patch.object(config, "CLEANING_MAX_SECONDS", 7200),
        ):
            self.assertEqual(cleaning.validate_duration(None), 3600)

    def test_the_cap_itself_is_allowed(self):
        with patch.object(config, "CLEANING_MAX_SECONDS", 7200):
            self.assertEqual(cleaning.validate_duration(7200), 7200)

    def test_over_the_cap_is_rejected_not_clamped(self):
        # Rejecting is the point: silently honouring a 3-hour request as 2 hours
        # would leave somebody believing a run they did not get.
        with patch.object(config, "CLEANING_MAX_SECONDS", 7200):
            with self.assertRaises(ValueError):
                cleaning.validate_duration(7201)

    def test_zero_and_negative_are_rejected(self):
        for bad in (0, -1):
            with self.assertRaises(ValueError):
                cleaning.validate_duration(bad)

    def test_non_numeric_is_rejected(self):
        with self.assertRaises(ValueError):
            cleaning.validate_duration("soon")

    def test_cleaning_budget_is_separate_from_the_watering_cap(self):
        """The headline claim: cleaning goes past MAX_PUMP_RUN_SECONDS.

        If this ever fails, cleaning mode has silently become a normal run.
        """
        self.assertGreater(config.CLEANING_MAX_SECONDS, config.MAX_PUMP_RUN_SECONDS)
        self.assertEqual(
            cleaning.validate_duration(config.MAX_PUMP_RUN_SECONDS * 4),
            config.MAX_PUMP_RUN_SECONDS * 4,
        )


class CleaningSessionTestCase(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        os.unlink(self.path)
        self._patch = patch.object(config, "STATE_FILE", self.path)
        self._patch.start()
        self.addCleanup(self._patch.stop)
        self.addCleanup(lambda: os.path.exists(self.path) and os.unlink(self.path))

    def test_no_session_by_default(self):
        self.assertIsNone(cleaning.session())
        self.assertFalse(cleaning.is_active())
        self.assertFalse(cleaning.expired())

    def test_start_records_a_deadline_and_survives_a_reload(self):
        # The deadline is persisted rather than held in a timer precisely so a
        # different process -- or the same one after a restart -- can read it.
        now = datetime(2026, 9, 12, 10, 0, 0)
        cleaning.start(seconds=3600, speed=100, now=now)
        session = cleaning.session(now=now + timedelta(minutes=10))
        self.assertIsNotNone(session)
        self.assertEqual(session["remaining_seconds"], 3000)
        self.assertEqual(session["speed"], 100)

    def test_a_passed_deadline_reads_as_no_session(self):
        now = datetime(2026, 9, 12, 10, 0, 0)
        cleaning.start(seconds=60, now=now)
        self.assertIsNone(cleaning.session(now=now + timedelta(seconds=61)))
        self.assertFalse(cleaning.is_active(now=now + timedelta(seconds=61)))

    def test_expired_distinguishes_finished_from_never_started(self):
        # The signal an enforcing loop needs: session() alone cannot tell "no
        # run" from "a run that must be stopped right now".
        now = datetime(2026, 9, 12, 10, 0, 0)
        self.assertFalse(cleaning.expired(now=now))
        cleaning.start(seconds=60, now=now)
        self.assertFalse(cleaning.expired(now=now + timedelta(seconds=30)))
        self.assertTrue(cleaning.expired(now=now + timedelta(seconds=61)))

    def test_clear_ends_the_session_and_records_why(self):
        cleaning.start(seconds=600)
        cleaning.clear(cleaning.DONE_LOW_WATER)
        self.assertFalse(cleaning.is_active())
        self.assertEqual(cleaning.last_result(), cleaning.DONE_LOW_WATER)

    def test_clear_is_idempotent(self):
        # Every enforcing path calls it, sometimes more than once.
        cleaning.clear(cleaning.DONE_STOPPED)
        cleaning.clear(cleaning.DONE_STOPPED)
        self.assertFalse(cleaning.is_active())

    def test_a_corrupt_deadline_is_not_a_licence_to_run_forever(self):
        from app.lib import state as state_lib

        state_lib.save_state(**{cleaning.UNTIL_KEY: "not-a-timestamp"})
        self.assertIsNone(cleaning.session())
        # ...and it reads as expired, so the watchdog stops the pump rather than
        # leaving it running against an unreadable deadline.
        self.assertTrue(cleaning.expired())

    def test_an_aware_timestamp_does_not_raise(self):
        # Mixed naive/aware subtraction is a TypeError, and an exception here
        # would fail *open* -- a pump running against no deadline at all.
        from app.lib import state as state_lib

        state_lib.save_state(**{cleaning.UNTIL_KEY: "2099-01-01T00:00:00+00:00"})
        self.assertIsNotNone(cleaning.session())

    def test_start_rejects_an_impossible_speed(self):
        for bad in (0, 101):
            with self.assertRaises(ValueError):
                cleaning.start(seconds=60, speed=bad)


class CleaningWaterVerdictTestCase(unittest.TestCase):
    """The dry-run guard for a run measured in hours.

    Note the deliberate inversion versus ``app.lib.water_guard``: that one fails
    *open* so a stopped service cannot withhold water and kill the plants. This
    one fails *closed*, because refusing a manual cleaning run costs a retry
    while allowing one costs two hours of unwatched pumping.
    """

    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        os.unlink(self.path)
        self._patch = patch.object(config, "STATE_FILE", self.path)
        self._patch.start()
        self.addCleanup(self._patch.stop)
        self.addCleanup(lambda: os.path.exists(self.path) and os.unlink(self.path))
        self.now = datetime(2026, 9, 12, 10, 0, 0)

    def _state(self, airgap, age_seconds=0):
        return {
            "water_airgap_cm": airgap,
            "water_checked_at": (self.now - timedelta(seconds=age_seconds)).isoformat(),
        }

    def test_allows_a_tank_above_the_cleaning_cutoff(self):
        ok, reason = cleaning.water_verdict(state=self._state(14.0), now=self.now, cutoff=18.0)
        self.assertTrue(ok, reason)

    def test_refuses_a_tank_below_the_cleaning_cutoff(self):
        ok, reason = cleaning.water_verdict(state=self._state(19.0), now=self.now, cutoff=18.0)
        self.assertFalse(ok)
        self.assertIn("below the cleaning cutoff", reason)

    def test_allows_what_the_watering_cutoff_would_refuse(self):
        """The reason cleaning has its own cutoff at all.

        A 15 cm airgap is past the watering cutoff (12.9) and well inside the
        cleaning cutoff (18.0). Without the split, cleaning could never start on
        a drained tower -- which is the only kind of tower anyone cleans.
        """
        from app.lib.water import is_water_low

        self.assertTrue(is_water_low(15.0, 12.9), "precondition: watering would refuse this")
        ok, _ = cleaning.water_verdict(state=self._state(15.0), now=self.now, cutoff=18.0)
        self.assertTrue(ok)

    def test_fails_closed_on_a_stale_reading(self):
        with patch.object(config, "WATER_READING_MAX_AGE_SECONDS", 540):
            ok, reason = cleaning.water_verdict(
                state=self._state(10.0, age_seconds=10_000), now=self.now, cutoff=18.0
            )
        self.assertFalse(ok)
        self.assertIn("no recent water reading", reason)

    def test_fails_closed_with_no_reading_at_all(self):
        ok, reason = cleaning.water_verdict(state={}, now=self.now, cutoff=18.0)
        self.assertFalse(ok)

    def test_fails_closed_when_the_timestamp_is_fresh_but_the_value_is_missing(self):
        state = {"water_checked_at": self.now.isoformat(), "water_airgap_cm": None}
        ok, reason = cleaning.water_verdict(state=state, now=self.now, cutoff=18.0)
        self.assertFalse(ok)
        self.assertIn("blind", reason)

    def test_no_cutoff_configured_refuses_cleaning(self):
        ok, reason = cleaning.water_verdict(state={}, now=self.now, cutoff=None)
        self.assertFalse(ok)
        self.assertIn("no cleaning cutoff configured", reason)


if __name__ == "__main__":
    unittest.main()
