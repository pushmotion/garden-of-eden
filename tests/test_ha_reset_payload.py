"""Every "nothing to report" publish uses Home Assistant's MQTT reset sentinel.

Home Assistant's MQTT number and sensor platforms treat the literal payload
``"None"`` (``PAYLOAD_NONE`` in homeassistant/components/mqtt/const.py) as
"reset this entity to unknown". The service used to send ``"unavailable"``,
which is a *state* HA derives from the availability topic, never a payload it
accepts: the water-threshold number could not parse it, and the two timestamp
sensors logged an invalid-state warning every time the schedule was empty.
"""

import contextlib
import os
import tempfile
import unittest
from unittest import mock


class FakeClient:
    def __init__(self):
        self.published = []

    def publish(self, topic, payload=None, **kwargs):
        self.published.append((topic, payload))


class ResetPayloadTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import config
        import mqtt

        cls.mqtt = mqtt
        cls.config = config
        cls.base = mqtt.BASE_TOPIC

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        for name in ("STATE_FILE", "SCHEDULE_FILE"):
            patcher = mock.patch.object(
                self.config, name, os.path.join(self.tmp, name.lower() + ".json")
            )
            patcher.start()
            self.addCleanup(patcher.stop)
        # Never touch the host crontab.
        for name, stub in (("_read_crontab", lambda: []), ("_write_crontab", lambda lines: None)):
            patcher = mock.patch.object(self.mqtt.sched_lib, name, stub)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(setattr, self.mqtt, "WATER_LOW_CM", self.mqtt.WATER_LOW_CM)
        self.client = FakeClient()

    def published_for(self, suffix):
        topic = f"{self.base}/{suffix}"
        return [p for t, p in self.client.published if t == topic]

    def test_the_sentinel_is_home_assistants(self):
        self.assertEqual(self.mqtt.PAYLOAD_NONE, "None")

    def test_disabled_thresholds_reset_both_entities(self):
        self.mqtt.WATER_LOW_CM = None
        with mock.patch.object(self.mqtt, "PUMP_CUTOFF_CM", None):
            self.mqtt.publish_water_thresholds(self.client)
        self.assertEqual(self.published_for("water/low/cm"), ["None"])
        self.assertEqual(self.published_for("water/pump/cutoff"), ["None"])

    def test_a_disabled_pump_schedule_resets_the_next_run_sensor(self):
        """A timestamp sensor given a non-timestamp warns on every publish."""
        self.mqtt.sched_lib.save_schedule(self.mqtt.sched_lib.DEFAULT_SCHEDULE)
        self.mqtt.publish_next_pump_run(self.client)
        self.assertEqual(self.published_for("schedule/pump/next"), ["None"])

    def test_no_pending_one_off_resets_the_pending_sensor(self):
        self.mqtt.publish_one_time_state(self.client)
        self.assertEqual(self.published_for("schedule/pump/once/next"), ["None"])

    def _no_cutoffs(self):
        """Neither cutoff configured, in the service and in config alike."""
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.object(self.mqtt, "PUMP_CUTOFF_CM", None))
        stack.enter_context(mock.patch.object(self.config, "CLEANING_CUTOFF_CM", None))
        stack.enter_context(mock.patch.object(self.config, "PUMP_CUTOFF_CM", None))
        return stack

    def test_no_cleaning_cutoff_resets_the_cutoff_sensor(self):
        with self._no_cutoffs():
            self.mqtt.publish_cleaning_state(self.client)
        self.assertEqual(self.published_for("pump/clean/cutoff"), ["None"])

    def test_the_old_string_is_gone_from_every_reset_path(self):
        self.mqtt.WATER_LOW_CM = None
        self.mqtt.sched_lib.save_schedule(self.mqtt.sched_lib.DEFAULT_SCHEDULE)
        with self._no_cutoffs():
            self.mqtt.publish_water_thresholds(self.client)
            self.mqtt.publish_next_pump_run(self.client)
            self.mqtt.publish_one_time_state(self.client)
            self.mqtt.publish_cleaning_state(self.client)
        self.assertNotIn("unavailable", [p for _, p in self.client.published])


if __name__ == "__main__":
    unittest.main()
