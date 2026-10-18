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


class _StopPolling(Exception):
    """Raised from the patched sleep to end publish_water_level after one pass."""


class StartupEvaluationTestCase(unittest.TestCase):
    """A restart must evaluate the tank once, not once from on_connect and once
    from the poll thread.

    Both used to evaluate within the same second against one cached sensor
    value, which met the two-in-a-row debounce instantly: every restart or
    broker reconnect could raise the alert on a single reading.
    """

    # Everything on_connect publishes besides the water verdict: unrelated to
    # the debounce, and several of them read state files or hardware.
    OTHER_CONNECT_PUBLISHERS = (
        "send_discovery_messages",
        "publish_water_thresholds",
        "publish_water_low_mode",
        "publish_watering_skipped",
        "publish_over_temp_state",
        "publish_grow_state",
        "publish_schedule_state",
        "publish_cleaning_state",
        "publish_light_state",
        "publish_pump_state",
    )

    @classmethod
    def setUpClass(cls):
        import mqtt

        cls.mqtt = mqtt

    def setUp(self):
        self.mqtt.WATER_LOW_CM = 11.0
        self.mqtt._water_low_streak = 0
        self.mqtt._last_water_distance = None
        self.client = FakeClient()
        self.client.subscribe = mock.Mock()

    def _start_service(self, distance):
        """Run on_connect and the poll thread's first pass, in that order.

        The poll thread is the real ``publish_water_level`` loop, stopped at its
        first sleep. Returns the mock wrapping ``evaluate_water_low``.
        """
        import contextlib
        import os
        import tempfile

        import config

        with contextlib.ExitStack() as stack:
            tmp = stack.enter_context(tempfile.TemporaryDirectory())
            stack.enter_context(
                mock.patch.object(config, "STATE_FILE", os.path.join(tmp, "state.json"))
            )
            stack.enter_context(mock.patch.object(self.mqtt, "over_temp_alert", None))
            stack.enter_context(
                mock.patch.object(self.mqtt, "_pump_is_circulating", return_value=False)
            )
            stack.enter_context(
                mock.patch.object(self.mqtt, "measure_distance_median", return_value=distance)
            )
            stack.enter_context(mock.patch.object(self.mqtt, "publish_water_readings"))
            stack.enter_context(mock.patch.object(self.mqtt, "sleep", side_effect=_StopPolling))
            for name in self.OTHER_CONNECT_PUBLISHERS:
                stack.enter_context(mock.patch.object(self.mqtt, name))
            evaluate = stack.enter_context(
                mock.patch.object(
                    self.mqtt, "evaluate_water_low", wraps=self.mqtt.evaluate_water_low
                )
            )

            self.mqtt.on_connect(self.client, None, None, 0)
            with self.assertRaises(_StopPolling):
                self.mqtt.publish_water_level(self.client)
        return evaluate

    def test_connect_then_first_poll_evaluates_once(self):
        # A low tank, so a doubled evaluation would show as streak 2 and "ON".
        evaluate = self._start_service(18.0)
        self.assertEqual(evaluate.call_count, 1)
        self.assertEqual(self.mqtt._water_low_streak, 1)
        self.assertEqual(self.client.last_low(), "OFF")

    def test_connect_clears_a_stale_retained_on_before_the_first_poll(self):
        with mock.patch.object(self.mqtt, "evaluate_water_low") as evaluate:
            self.mqtt.republish_water_verdict(self.client)
        evaluate.assert_not_called()
        self.assertEqual(self.client.last_low(), "OFF")

    def test_reconnect_mid_alert_keeps_it_on_without_a_new_reading(self):
        self.mqtt._water_low_streak = 2
        with mock.patch.object(self.mqtt, "evaluate_water_low") as evaluate:
            self.mqtt.republish_water_verdict(self.client)
        evaluate.assert_not_called()
        self.assertEqual(self.client.last_low(), "ON")
        self.assertEqual(self.mqtt._water_low_streak, 2)

    def test_reconnect_repeats_the_pump_verdict_from_the_last_reading(self):
        self.mqtt._last_water_distance = 18.0
        with (
            mock.patch.object(self.mqtt, "_pump_is_circulating", return_value=False),
            mock.patch.object(self.mqtt, "PUMP_CUTOFF_CM", 12.89),
        ):
            self.mqtt.republish_water_verdict(self.client)
        blocked = [p for t, p in self.client.published if t.endswith("/water/pump_blocked/state")]
        self.assertEqual(blocked, ["ON"])

    def test_an_on_demand_refresh_still_resets_the_debounce(self):
        # update_water_low_state is for a threshold change: it must keep taking a
        # fresh reading against a cleared streak, so a low tank that had the
        # alert on reads as streak 1 (OFF) here, not 6 (ON).
        self.mqtt._water_low_streak = 5
        with (
            mock.patch.object(self.mqtt, "measure_distance_median", return_value=18.0) as measure,
            mock.patch.object(self.mqtt, "record_water_reading"),
            mock.patch.object(self.mqtt, "publish_watering_blocked"),
        ):
            self.mqtt.update_water_low_state(self.client)
        measure.assert_called_once()
        self.assertEqual(self.mqtt._water_low_streak, 1)
        self.assertEqual(self.client.last_low(), "OFF")


if __name__ == "__main__":
    unittest.main()
