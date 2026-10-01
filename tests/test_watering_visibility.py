"""A refused watering run must be visible somewhere other than a state file.

Found on tower 1, 2026-10-01: the tank sat past the pump cutoff and the dry-run
guard refused six scheduled runs in a row -- correctly -- but cron discarded
``bin/water.sh``'s output (no MTA on the Pi), the MQTT log said nothing, and
Home Assistant showed only "Water Low", which had been on all week. The plants
went 22 hours without water and nothing anywhere said so.
"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import config
from app.lib import state as state_lib


class FakeClient:
    def __init__(self):
        self.published = []

    def publish(self, topic, payload=None, **kwargs):
        self.published.append((topic, payload))

    def last(self, suffix):
        vals = [p for t, p in self.published if t.endswith(suffix)]
        return vals[-1] if vals else None


class _TempState(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        os.unlink(self.path)
        patcher = mock.patch.object(config, "STATE_FILE", self.path)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(lambda: os.path.exists(self.path) and os.unlink(self.path))


class GuardRecordsRefusalTestCase(_TempState):
    @classmethod
    def setUpClass(cls):
        from app.lib import water_guard

        cls.guard = water_guard

    def test_a_refusal_is_persisted_with_its_reason(self):
        with mock.patch.object(self.guard, "pump_allowed", return_value=(False, "too low")):
            self.assertEqual(self.guard.REFUSE, self.guard.main())
        state = state_lib.load_state()
        self.assertEqual(state["watering_refused_reason"], "too low")
        # Offset-aware: Home Assistant's timestamp sensors reject naive times.
        self.assertRegex(state["watering_refused_at"], r"[+-]\d\d:\d\d$|Z$")

    def test_an_allowed_run_records_nothing(self):
        with mock.patch.object(self.guard, "pump_allowed", return_value=(True, "fine")):
            self.assertEqual(self.guard.ALLOW, self.guard.main())
        self.assertNotIn("watering_refused_at", state_lib.load_state())

    def test_failing_to_record_does_not_change_the_verdict(self):
        refuse = mock.patch.object(self.guard, "pump_allowed", return_value=(False, "too low"))
        broken = mock.patch.object(state_lib, "save_state", side_effect=OSError("disk"))
        with refuse, broken:
            self.assertEqual(self.guard.REFUSE, self.guard.main())


class MqttWateringVisibilityTestCase(_TempState):
    @classmethod
    def setUpClass(cls):
        import mqtt

        cls.mqtt = mqtt

    def setUp(self):
        super().setUp()
        for name, value in (("WATER_LOW_CM", 9.08), ("PUMP_CUTOFF_CM", 12.89)):
            patcher = mock.patch.object(self.mqtt, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = FakeClient()

    def _circulating(self, value):
        return mock.patch.object(self.mqtt, "_pump_is_circulating", return_value=value)

    def test_blocked_reports_on_past_the_cutoff(self):
        with self._circulating(False):
            self.mqtt.publish_watering_blocked(self.client, 13.73)
        self.assertEqual(self.client.last("/water/pump_blocked/state"), "ON")

    def test_blocked_is_off_between_the_alert_and_the_cutoff(self):
        # "Water Low" is on here; watering is not blocked. That gap is the point.
        with self._circulating(False):
            self.mqtt.publish_watering_blocked(self.client, 11.0)
        self.assertEqual(self.client.last("/water/pump_blocked/state"), "OFF")

    def test_blocked_is_held_while_the_pump_runs(self):
        # Mid-run the level dips past the cutoff with water up in the tower.
        with self._circulating(True):
            self.mqtt.publish_watering_blocked(self.client, 13.73)
        self.assertIsNone(self.client.last("/water/pump_blocked/state"))

    def test_evaluate_water_low_publishes_the_blocked_state(self):
        it = iter([13.73] * 5)
        reads = mock.patch.object(self.mqtt, "safe_distance_measure", side_effect=lambda: next(it))
        with self._circulating(False), reads:
            self.mqtt.evaluate_water_low(self.client)
        self.assertEqual(self.client.last("/water/pump_blocked/state"), "ON")

    def test_a_new_refusal_is_logged_and_published(self):
        state_lib.save_state(
            watering_refused_at="2026-09-30T15:00:02-04:00",
            watering_refused_reason="water below the pump cutoff",
        )
        with self.assertLogs(self.mqtt.logger, "WARNING") as logs:
            mark = self.mqtt.sync_watering_skipped(self.client, None)
        self.assertEqual(mark, "2026-09-30T15:00:02-04:00")
        self.assertIn("Watering run refused", logs.output[0])
        self.assertEqual(self.client.last("/water/skipped/last"), mark)
        attrs = json.loads(self.client.last("/water/skipped/attributes"))
        self.assertEqual(attrs["reason"], "water below the pump cutoff")

    def test_an_already_reported_refusal_is_not_repeated(self):
        state_lib.save_state(watering_refused_at="2026-09-30T15:00:02-04:00")
        mark = self.mqtt.sync_watering_skipped(self.client, "2026-09-30T15:00:02-04:00")
        self.assertEqual(mark, "2026-09-30T15:00:02-04:00")
        self.assertEqual(self.client.published, [])

    def test_no_refusal_ever_publishes_nothing(self):
        self.assertIsNone(self.mqtt.sync_watering_skipped(self.client, None))
        self.assertIsNone(self.mqtt.publish_watering_skipped(self.client))
        self.assertEqual(self.client.published, [])

    def test_both_entities_are_discovered(self):
        self.mqtt.send_discovery_messages(self.client)
        configs = {t: json.loads(p) for t, p in self.client.published if t.endswith("/config")}
        blocked = next(v for t, v in configs.items() if t.endswith("_watering_blocked/config"))
        skipped = next(v for t, v in configs.items() if t.endswith("_watering_skipped/config"))
        self.assertEqual(blocked["device_class"], "problem")
        self.assertTrue(blocked["state_topic"].endswith("/water/pump_blocked/state"))
        self.assertEqual(skipped["device_class"], "timestamp")
        self.assertTrue(skipped["state_topic"].endswith("/water/skipped/last"))


class WaterShLogsToJournalTestCase(unittest.TestCase):
    """cron discards water.sh's output, so a refusal must reach the journal."""

    def test_refusal_goes_through_the_journal_logger(self):
        script = (Path(__file__).resolve().parents[1] / "bin" / "water.sh").read_text()
        self.assertIn("logger -t garden-water", script)
        self.assertRegex(script, r'log_event err "ERROR: refusing to water')


if __name__ == "__main__":
    unittest.main()
