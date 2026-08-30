import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

import config
from app.lib import grow


class GrowReminderTestCase(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 1, 1, 12, 0, 0)

    def _state(self, days_ago, acknowledged=None, last_ack=None):
        started = (self.now - timedelta(days=days_ago)).isoformat()
        return {
            "stage": "germination",
            "started": started,
            "acknowledged": acknowledged or [],
            "last_ack": last_ack or {},
        }

    def _ack_days_ago(self, days_ago):
        return (self.now - timedelta(days=days_ago)).isoformat()

    @patch.object(config, "THINNING_REMINDER_DAYS", 14)
    @patch.object(config, "ROOT_CHECK_REMINDER_DAYS", 21)
    @patch.object(config, "HARVEST_REMINDER_DAYS", 35)
    @patch.object(config, "NUTRIENT_REMINDER_DAYS", 7)
    def test_thinning_due_after_threshold(self):
        due = grow.due_reminders(self._state(15), now=self.now)
        self.assertIn("thinning", due)
        self.assertNotIn("root_check", due)

    @patch.object(config, "THINNING_REMINDER_DAYS", 14)
    @patch.object(config, "NUTRIENT_REMINDER_DAYS", 7)
    def test_nutrient_fires_on_cadence(self):
        due = grow.due_reminders(self._state(7), now=self.now)
        self.assertIn("nutrient", due)

    @patch.object(config, "NUTRIENT_REMINDER_DAYS", 7)
    def test_nutrient_not_due_before_cadence(self):
        self.assertNotIn("nutrient", grow.due_reminders(self._state(6), now=self.now))

    @patch.object(config, "NUTRIENT_REMINDER_DAYS", 7)
    def test_nutrient_still_due_past_the_exact_cadence_day(self):
        """Regression: the reminder used to fire only when days % cadence == 0,
        so missing its single day silently skipped a whole cadence."""
        for days in (8, 9, 13):
            with self.subTest(days=days):
                self.assertIn("nutrient", grow.due_reminders(self._state(days), now=self.now))

    @patch.object(config, "NUTRIENT_REMINDER_DAYS", 7)
    def test_nutrient_cadence_restarts_from_acknowledgement(self):
        recent = self._state(30, last_ack={"nutrient": self._ack_days_ago(3)})
        self.assertNotIn("nutrient", grow.due_reminders(recent, now=self.now))
        stale = self._state(30, last_ack={"nutrient": self._ack_days_ago(7)})
        self.assertIn("nutrient", grow.due_reminders(stale, now=self.now))

    @patch.object(config, "RESERVOIR_CHANGE_DAYS", 49)
    @patch.object(config, "NUTRIENT_REMINDER_DAYS", 0)
    def test_reservoir_change_is_recurring(self):
        self.assertNotIn("reservoir_change", grow.due_reminders(self._state(48), now=self.now))
        self.assertIn("reservoir_change", grow.due_reminders(self._state(49), now=self.now))

    @patch.object(config, "NUTRIENT_REMINDER_DAYS", 0)
    @patch.object(config, "RESERVOIR_CHANGE_DAYS", 0)
    def test_zero_cadence_disables_recurring(self):
        due = grow.due_reminders(self._state(400), now=self.now)
        self.assertNotIn("nutrient", due)
        self.assertNotIn("reservoir_change", due)

    def test_nutrient_dose_is_full_until_first_acknowledgement(self):
        self.assertEqual(grow.nutrient_dose(self._state(14)), "full")
        fed = self._state(14, last_ack={"nutrient": self._ack_days_ago(1)})
        self.assertEqual(grow.nutrient_dose(fed), "reduced")

    @patch.object(config, "THINNING_REMINDER_DAYS", 14)
    def test_acknowledged_not_repeated(self):
        state = self._state(15, acknowledged=["thinning"])
        self.assertNotIn("thinning", grow.due_reminders(state, now=self.now))

    def test_set_stage_validates(self):
        with self.assertRaises(ValueError):
            grow.set_stage({}, "bogus")
        self.assertEqual(grow.set_stage({}, "harvest")["stage"], "harvest")

    @patch.object(config, "NUTRIENT_REMINDER_DAYS", 7)
    def test_acknowledge_nutrient_records_when(self):
        state = self._state(7)
        grow.acknowledge(state, "nutrient", now=self.now)
        self.assertEqual(state["last_ack"]["nutrient"], self.now.isoformat())
        self.assertNotIn("nutrient", grow.due_reminders(state, now=self.now))

    @patch.object(config, "THINNING_REMINDER_DAYS", 14)
    def test_acknowledge_one_shot_still_uses_the_list(self):
        state = self._state(15)
        grow.acknowledge(state, "thinning", now=self.now)
        self.assertIn("thinning", state["acknowledged"])
        self.assertNotIn("thinning", grow.due_reminders(state, now=self.now))

    @patch.object(config, "NUTRIENT_REMINDER_DAYS", 7)
    def test_state_without_last_ack_is_tolerated(self):
        """States written before last_ack existed must still load."""
        legacy = {"stage": "germination", "started": self._ack_days_ago(9), "acknowledged": []}
        self.assertIn("nutrient", grow.due_reminders(legacy, now=self.now))


if __name__ == "__main__":
    unittest.main()
