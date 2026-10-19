"""Water freshness and cleaning deadlines survive the DST fall-back night.

The verdict timestamp and the cleaning deadline were naive local time, and
``is_reading_fresh`` rejected any negative age. Local time repeats an hour on
the fall-back night, so a reading taken at 01:58 EDT and judged by cron at
01:03 EST looked fifty-five minutes in the future: stale for the whole hour,
which fails watering open and cleaning closed. A cleaning deadline gained an
hour the same way. Both are now written and judged in UTC; a naive stamp left
by an older build is read as local time.
"""

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import config
from app.lib import cleaning, water_guard
from app.lib import state as state_lib
from app.lib.water import as_utc, is_reading_fresh

EDT = timezone(timedelta(hours=-4))
EST = timezone(timedelta(hours=-5))
# 2026-11-01: the clock goes from 01:59:59 EDT back to 01:00:00 EST.
LAST_EDT_READING = datetime(2026, 11, 1, 1, 58, 0, tzinfo=EDT)  # 05:58 UTC
FIRST_EST_CRON = datetime(2026, 11, 1, 1, 3, 0, tzinfo=EST)  # 06:03 UTC, five minutes on


class _StateFileTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = patch.object(config, "STATE_FILE", os.path.join(self.tmp.name, "state.json"))
        patcher.start()
        self.addCleanup(patcher.stop)


class FallBackNightFreshnessTestCase(_StateFileTestCase):
    def test_a_reading_from_the_repeated_hour_is_fresh(self):
        """01:58 EDT judged at 01:03 EST is five minutes old, not minus fifty-five."""
        self.assertTrue(is_reading_fresh(LAST_EDT_READING.isoformat(), FIRST_EST_CRON, 540))

    def test_the_same_pair_as_utc_stamps(self):
        stamp = LAST_EDT_READING.astimezone(timezone.utc).isoformat(timespec="seconds")
        self.assertTrue(is_reading_fresh(stamp, FIRST_EST_CRON.astimezone(timezone.utc), 540))

    def test_a_reading_genuinely_past_the_window_is_still_stale(self):
        old = (FIRST_EST_CRON - timedelta(seconds=541)).isoformat()
        self.assertFalse(is_reading_fresh(old, FIRST_EST_CRON, 540))

    def test_spring_forward_does_not_age_a_reading_by_an_hour(self):
        # 2026-03-08: 01:59:59 EST -> 03:00:00 EDT.
        before = datetime(2026, 3, 8, 1, 58, 0, tzinfo=EST)
        after = datetime(2026, 3, 8, 3, 3, 0, tzinfo=EDT)
        self.assertTrue(is_reading_fresh(before.isoformat(), after, 540))

    def test_the_guard_honours_a_fresh_block_across_the_fall_back(self):
        """What the bug cost: a fresh "blocked" verdict was ignored for an hour."""
        allowed, reason = water_guard.pump_allowed(
            {
                "pump_blocked": True,
                "water_checked_at": LAST_EDT_READING.isoformat(),
                "water_airgap_cm": 19.2,
            },
            now=FIRST_EST_CRON,
        )
        self.assertFalse(allowed, reason)

    def test_naive_stamps_are_read_as_local_time(self):
        """Older builds wrote naive local stamps; they still compare."""
        now = datetime(2026, 8, 31, 12, 0, 0)
        self.assertEqual(as_utc(now), now.astimezone(timezone.utc))
        self.assertTrue(is_reading_fresh((now - timedelta(seconds=60)).isoformat(), now, 540))

    def test_the_service_stamps_readings_in_utc(self):
        import mqtt

        with patch.object(mqtt, "_pump_is_circulating", return_value=False):
            mqtt.record_water_reading(10.0)
        stamp = state_lib.load_state()["water_checked_at"]
        self.assertEqual(datetime.fromisoformat(stamp).utcoffset(), timedelta(0))
        self.assertTrue(is_reading_fresh(stamp, datetime.now(timezone.utc), 540))


class FallBackNightCleaningTestCase(_StateFileTestCase):
    def test_a_cleaning_run_does_not_gain_an_hour_at_fall_back(self):
        started = datetime(2026, 11, 1, 1, 30, 0, tzinfo=EDT)
        cleaning.start(seconds=3600, now=started)  # until 02:30 EDT, i.e. 01:30 EST
        session = cleaning.session(now=datetime(2026, 11, 1, 1, 20, 0, tzinfo=EST))
        self.assertEqual(session["remaining_seconds"], 600)
        self.assertTrue(cleaning.expired(now=datetime(2026, 11, 1, 1, 31, 0, tzinfo=EST)))

    def test_the_deadline_is_persisted_in_utc(self):
        cleaning.start(seconds=60, now=datetime(2026, 11, 1, 1, 30, 0, tzinfo=EDT))
        raw = state_lib.load_state()[cleaning.UNTIL_KEY]
        self.assertEqual(datetime.fromisoformat(raw).utcoffset(), timedelta(0))

    def test_a_naive_legacy_deadline_still_reads(self):
        now = datetime(2026, 9, 12, 10, 0, 0)
        state_lib.save_state(**{cleaning.UNTIL_KEY: (now + timedelta(minutes=5)).isoformat()})
        self.assertEqual(cleaning.session(now=now)["remaining_seconds"], 300)

    def test_status_reports_the_deadline_with_an_offset(self):
        cleaning.start(seconds=600)
        until = cleaning.status()["until"]
        self.assertIsNotNone(datetime.fromisoformat(until).utcoffset())


if __name__ == "__main__":
    unittest.main()
