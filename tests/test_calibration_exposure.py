"""Every client gets the calibration from the tower, never from its own copy.

The web UI used to hold a second copy of the tank geometry and the pump cap as
literals. It agreed with Home Assistant only while a tower ran the config
defaults, and this tower does not: calibrating it made the two surfaces report
different fill levels for the same water.

These tests pin the contract that replaced that -- /system publishes the
calibration and the caps, /distance publishes what was derived from them -- so a
client has no reason to hardcode either.
"""

import unittest
from unittest.mock import patch

import config
from app import create_app


class SystemExposesCalibrationTestCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()

    def payload(self):
        response = self.client.get("/system")
        self.assertEqual(200, response.status_code)
        return response.get_json()

    def test_tank_geometry_is_published(self):
        """Without these three a client cannot turn an airgap into a percentage."""
        body = self.payload()
        self.assertEqual(config.WATER_FULL_CM, body["water_full_cm"])
        self.assertEqual(config.WATER_EMPTY_CM, body["water_empty_cm"])
        self.assertEqual(config.TANK_CAPACITY_GALLONS, body["tank_capacity_gallons"])

    def test_both_water_thresholds_are_published(self):
        """The alert and the interlock are different numbers and both are shown."""
        body = self.payload()
        self.assertIn("water_low_cm", body)
        self.assertIn("pump_cutoff_cm", body)

    def test_the_cutoff_is_the_resolved_one_not_the_raw_setting(self):
        """PUMP_CUTOFF_CM unset falls back to the alert; clients see the effective value."""
        with (
            patch.object(config, "PUMP_CUTOFF_CM", None),
            patch.object(config, "WATER_LOW_CM", 9.1),
        ):
            self.assertEqual(9.1, self.payload()["pump_cutoff_cm"])

    def test_the_pump_cap_is_published(self):
        self.assertEqual(config.MAX_PUMP_RUN_SECONDS, self.payload()["pump_max_run_seconds"])


class MqttDurationCapTestCase(unittest.TestCase):
    """Home Assistant's minute control is derived, not written down twice."""

    def test_minutes_derive_from_the_seconds_cap(self):
        import mqtt

        self.assertEqual(max(1, config.MAX_PUMP_RUN_SECONDS // 60), mqtt.MAX_PUMP_MINUTES)

    def _duration_entity(self):
        import json

        import mqtt

        published = []

        class Capture:
            def publish(self, topic, payload=None, **kwargs):
                published.append((topic, payload))

        mqtt.send_discovery_messages(Capture())
        found = [
            json.loads(p)
            for t, p in published
            if p and "sched_pump_duration" in str(p) and "number" in t
        ]
        self.assertTrue(found, "Pump Run Duration entity was not discovered")
        return found[0]

    def test_the_discovery_payload_offers_no_more_than_the_cap(self):
        import mqtt

        self.assertEqual(mqtt.MAX_PUMP_MINUTES, self._duration_entity()["max"])

    def test_a_lower_cap_actually_narrows_the_control(self):
        """The test that fails if this is ever written down as a literal again.

        On the default 300s cap a hardcoded 5 is indistinguishable from a derived
        one, which is how the literal survived. Lowering the cap separates them.
        """
        import mqtt

        with patch.object(mqtt, "MAX_PUMP_MINUTES", 4):
            self.assertEqual(4, self._duration_entity()["max"])


if __name__ == "__main__":
    unittest.main()
