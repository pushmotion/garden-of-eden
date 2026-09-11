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


@patch.object(config, "NUTRIENT_MICRO_ML_PER_GALLON", 5.0)
@patch.object(config, "NUTRIENT_GRO_ML_PER_GALLON", 5.0)
@patch.object(config, "NUTRIENT_BLOOM_ML_PER_GALLON", 3.0)
@patch.object(config, "NUTRIENT_REDUCED_FRACTION", 0.5)
@patch.object(config, "TANK_CAPACITY_GALLONS", 5.0)
class NutrientPlanTestCase(unittest.TestCase):
    """The reminder has to carry millilitres, not just raise an alarm.

    "Add Plant Food" with no dose is what sent a real feed three days late: the
    alarm fired, the numbers lived somewhere else, and nothing got poured.
    """

    UNFED = {"stage": "germination", "started": "2026-01-01T00:00:00", "last_ack": {}}
    FED = {
        "stage": "germination",
        "started": "2026-01-01T00:00:00",
        "last_ack": {"nutrient": "2026-01-08T00:00:00"},
    }

    def _ml(self, plan):
        return {p["key"]: p["ml"] for p in plan["parts"]}

    def test_full_dose_matches_the_bottle_ratio(self):
        plan = grow.nutrient_plan(self.UNFED, gallons=5)
        self.assertEqual(plan["strength"], "full")
        self.assertEqual(self._ml(plan), {"micro": 25.0, "gro": 25.0, "bloom": 15.0})

    def test_reduced_dose_is_halved(self):
        plan = grow.nutrient_plan(self.FED, gallons=5)
        self.assertEqual(plan["strength"], "reduced")
        self.assertEqual(self._ml(plan), {"micro": 12.5, "gro": 12.5, "bloom": 7.5})

    def test_dose_scales_with_the_reservoir(self):
        """A part-full tank must not get a full tank's dose."""
        plan = grow.nutrient_plan(self.UNFED, gallons=2.5)
        self.assertEqual(self._ml(plan), {"micro": 12.5, "gro": 12.5, "bloom": 7.5})

    def test_parts_come_back_in_pouring_order(self):
        """Micro before Bloom is chemistry, not presentation: a consumer that
        renders the list in order must not have to know that."""
        plan = grow.nutrient_plan(self.UNFED, gallons=5)
        self.assertEqual([p["key"] for p in plan["parts"]], ["micro", "gro", "bloom"])
        self.assertEqual([p["order"] for p in plan["parts"]], [1, 2, 3])

    def test_teaspoons_accompany_every_millilitre_figure(self):
        plan = grow.nutrient_plan(self.UNFED, gallons=5)
        micro = plan["parts"][0]
        self.assertAlmostEqual(micro["tsp"], 25.0 / grow.ML_PER_TEASPOON, places=2)
        self.assertEqual(micro["spoons"], "5 tsp")

    def test_spoon_text_rounds_to_the_nearest_quarter(self):
        cases = {
            0.0: "under 1/4 tsp",
            1.25: "1/4 tsp",
            2.5: "1/2 tsp",
            3.7: "3/4 tsp",
            4.93: "1 tsp",
            12.3: "2 1/2 tsp",
        }
        for ml, expected in cases.items():
            with self.subTest(ml=ml):
                self.assertEqual(grow._spoon_text(ml), expected)

    def test_unusable_volume_falls_back_to_tank_capacity(self):
        """None/0/garbage means "no reading", not "empty tank" -- guessing empty
        would under-dose a full reservoir. Capacity is right because the routine
        is to top up before dosing."""
        for gallons in (None, 0, -1, "", "nonsense"):
            with self.subTest(gallons=gallons):
                plan = grow.nutrient_plan(self.UNFED, gallons=gallons)
                self.assertEqual(plan["gallons"], 5.0)
                self.assertEqual(self._ml(plan)["micro"], 25.0)

    def test_summary_names_the_bottle_the_amount_and_the_order(self):
        plan = grow.nutrient_plan(self.UNFED, gallons=5)
        self.assertEqual(
            plan["summary"],
            "FloraMicro 25 mL (5 tsp) then FloraGro 25 mL (5 tsp) " "then FloraBloom 15 mL (3 tsp)",
        )

    def test_summary_fits_a_home_assistant_state_string(self):
        """HA truncates a state payload past 255 characters."""
        plan = grow.nutrient_plan(self.UNFED, gallons=5)
        self.assertLessEqual(len(plan["summary"]), 255)


if __name__ == "__main__":
    unittest.main()
