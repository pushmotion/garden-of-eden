"""Water-low alert robustness in the MQTT service (offline).

Covers the false-alarm fixes: a single ultrasonic spike must not trip the alert
(median rejection), and a genuine low reading must be confirmed (debounce)
before the retained state flips to ON.
"""

import unittest
from unittest import mock


class FakeClient:
    def __init__(self):
        self.published = []

    def publish(self, topic, payload=None, **kwargs):
        self.published.append((topic, payload))

    def last_low(self):
        vals = [p for t, p in self.published if t.endswith("/water/low/state")]
        return vals[-1] if vals else None


class WaterLowTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import mqtt

        cls.mqtt = mqtt

    def setUp(self):
        self.mqtt.WATER_LOW_CM = 11.0
        self.mqtt._water_low_streak = 0
        self.client = FakeClient()

    def _readings(self, values):
        """Patch safe_distance_measure to yield the given per-call values."""
        it = iter(values)
        return mock.patch.object(self.mqtt, "safe_distance_measure", side_effect=lambda: next(it))

    def test_single_spike_does_not_trip_alert(self):
        # Four good full-tank reads (5cm) and one spurious spike (99cm): the
        # median is well under threshold, so the tank is NOT reported low.
        with self._readings([5, 5, 99, 5, 5]):
            self.mqtt.evaluate_water_low(self.client)
        self.assertEqual(self.client.last_low(), "OFF")

    def test_genuine_low_needs_confirmation_then_trips(self):
        # Consistently high distance (tank low). First pass arms the debounce,
        # second confirms and flips the alert ON.
        with self._readings([18] * 5):
            self.mqtt.evaluate_water_low(self.client)
        self.assertEqual(self.client.last_low(), "OFF")  # debounced on first hit
        with self._readings([18] * 5):
            self.mqtt.evaluate_water_low(self.client)
        self.assertEqual(self.client.last_low(), "ON")

    def test_recovery_clears_immediately(self):
        self.mqtt._water_low_streak = 5  # pretend it was low
        with self._readings([5] * 5):  # tank refilled
            self.mqtt.evaluate_water_low(self.client)
        self.assertEqual(self.client.last_low(), "OFF")

    def test_disabled_threshold_reports_not_low(self):
        self.mqtt.WATER_LOW_CM = None
        with self._readings([5] * 5):
            self.mqtt.evaluate_water_low(self.client)
        self.assertEqual(self.client.last_low(), "OFF")

    def test_disabled_alert_still_records_the_pump_verdict(self):
        """WATER_LOW_CM=0 used to return before reading the tank at all.

        The cron guard then saw only stale verdicts and failed open on every
        run, "Watering Blocked" froze, and cleaning (fails closed) was refused.
        """
        import os
        import tempfile

        import config
        from app.lib import state as state_lib

        self.mqtt.WATER_LOW_CM = None
        tmp = tempfile.mkdtemp()
        with (
            mock.patch.object(config, "STATE_FILE", os.path.join(tmp, "state.json")),
            mock.patch.object(self.mqtt, "PUMP_CUTOFF_CM", 12.89),
            mock.patch.object(self.mqtt, "_pump_is_circulating", return_value=False),
            self._readings([13.73] * 5),
        ):
            result = self.mqtt.evaluate_water_low(self.client)
            state = state_lib.load_state()
        self.assertEqual(result, 13.73)
        self.assertEqual(self.client.last_low(), "OFF")
        self.assertTrue(state["pump_blocked"], "the verdict must be recorded with the alert off")
        self.assertEqual(state["water_airgap_cm"], 13.73)
        blocked = [p for t, p in self.client.published if t.endswith("/water/pump_blocked/state")]
        self.assertEqual(blocked, ["ON"])

    def test_a_missing_echo_is_not_a_dry_tank(self):
        """Five saturated reads (100.00 cm) must leave the verdict alone.

        Recording them as a dry tank refused watering on the one hardware fault
        the guard is documented to fail open on; dropping them lets the stored
        verdict go stale, which is that fail-open path.
        """
        with (
            self._readings([100.0] * 5),
            mock.patch.object(self.mqtt, "record_water_reading") as rec,
        ):
            result = self.mqtt.evaluate_water_low(self.client)
        self.assertIsNone(result)
        rec.assert_not_called()
        self.assertIsNone(self.client.last_low())

    def test_all_reads_failing_publishes_nothing(self):
        with self._readings([None] * 5):
            result = self.mqtt.evaluate_water_low(self.client)
        self.assertIsNone(result)
        self.assertIsNone(self.client.last_low())


if __name__ == "__main__":
    unittest.main()
