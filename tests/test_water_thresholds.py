"""Both water thresholds must reach Home Assistant, and survive a restart.

The number entity used to read *unknown* until somebody happened to write it --
a control with no value, governing a guard already running -- and a value set
from HA was never persisted, so the next restart reverted to the .env setting
while HA kept showing the retained one. Neither side could notice.
"""

import os
import tempfile
import unittest
from unittest import mock


class FakeClient:
    def __init__(self):
        self.published = []

    def publish(self, topic, payload=None, **kwargs):
        self.published.append((topic, payload))


class WaterThresholdTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import config
        import mqtt

        cls.mqtt = mqtt
        cls.config = config
        cls.base = mqtt.BASE_TOPIC

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._orig_state = self.config.STATE_FILE
        self.config.STATE_FILE = os.path.join(self.tmp, "state.json")
        self.addCleanup(setattr, self.config, "STATE_FILE", self._orig_state)

        self._orig_low = self.mqtt.WATER_LOW_CM
        self.addCleanup(setattr, self.mqtt, "WATER_LOW_CM", self._orig_low)
        self.client = FakeClient()

    def published_for(self, suffix):
        topic = f"{self.base}/{suffix}"
        return [p for t, p in self.client.published if t == topic]

    def test_both_thresholds_are_published(self):
        """A dashboard should never have to guess either number."""
        self.mqtt.WATER_LOW_CM = 13.0
        self.mqtt.publish_water_thresholds(self.client)

        self.assertEqual(self.published_for("water/low/cm"), ["13.00"])
        self.assertEqual(len(self.published_for("water/pump/cutoff")), 1)

    def test_the_cutoff_falls_back_to_the_alert_when_unset(self):
        self.mqtt.WATER_LOW_CM = 13.0
        with mock.patch.object(self.mqtt, "PUMP_CUTOFF_CM", None):
            self.mqtt.publish_water_thresholds(self.client)
        self.assertEqual(self.published_for("water/pump/cutoff"), ["13.00"])

    def test_the_cutoff_is_published_when_set(self):
        self.mqtt.WATER_LOW_CM = 13.0
        with mock.patch.object(self.mqtt, "PUMP_CUTOFF_CM", 17.6):
            self.mqtt.publish_water_thresholds(self.client)
        self.assertEqual(self.published_for("water/pump/cutoff"), ["17.60"])

    def test_alerting_disabled_publishes_no_threshold(self):
        """Both go out as "unavailable" so HA greys the control instead of blanking it."""
        self.mqtt.WATER_LOW_CM = None
        with mock.patch.object(self.mqtt, "PUMP_CUTOFF_CM", None):
            self.mqtt.publish_water_thresholds(self.client)
        self.assertEqual(self.published_for("water/low/cm"), ["unavailable"])
        self.assertEqual(self.published_for("water/pump/cutoff"), ["unavailable"])

    def test_a_threshold_set_from_ha_survives_a_restart(self):
        """The whole point: the service and the dashboard must not diverge."""
        from app.lib import state as state_lib

        state_lib.save_state(water_low_cm=16.5)

        self.mqtt.WATER_LOW_CM = 11.0  # stands in for the .env value after a restart
        self.mqtt.restore_water_threshold()
        self.assertEqual(self.mqtt.WATER_LOW_CM, 16.5)

    def test_nothing_persisted_leaves_the_env_value_alone(self):
        """.env stays authoritative on a tower that never touched the control."""
        self.mqtt.WATER_LOW_CM = 11.0
        self.mqtt.restore_water_threshold()
        self.assertEqual(self.mqtt.WATER_LOW_CM, 11.0)

    def test_an_unusable_persisted_value_is_ignored(self):
        from app.lib import state as state_lib

        state_lib.save_state(water_low_cm="not-a-number")
        self.mqtt.WATER_LOW_CM = 11.0
        self.mqtt.restore_water_threshold()
        self.assertEqual(self.mqtt.WATER_LOW_CM, 11.0, "a corrupt value must not brick the guard")


if __name__ == "__main__":
    unittest.main()
