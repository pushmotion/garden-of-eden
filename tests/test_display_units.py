"""DISPLAY_UNITS changes what is shown and nothing else.

The tower measures, stores, publishes and calibrates in metric regardless -- the
same arrangement temperature has always had, where the tower publishes Celsius
and Home Assistant displays Fahrenheit because HA converts. A unit preference
that could move a threshold would be a safety bug, so these tests pin that it
cannot.
"""

import importlib
import os
import unittest
from unittest.mock import patch

import config
from app import create_app


def reload_config(value):
    """Re-read config.py with DISPLAY_UNITS set, then restore it."""
    env = dict(os.environ)
    if value is None:
        env.pop("DISPLAY_UNITS", None)
    else:
        env["DISPLAY_UNITS"] = value
    with patch.dict(os.environ, env, clear=True):
        return importlib.reload(config)


class DisplayUnitsParsingTestCase(unittest.TestCase):
    def tearDown(self):
        importlib.reload(config)

    def test_default_is_metric(self):
        self.assertEqual("metric", reload_config(None).DISPLAY_UNITS)

    def test_imperial_spellings_are_accepted(self):
        for value in ("imperial", "IMPERIAL", " Imperial ", "us", "customary"):
            with self.subTest(value=value):
                self.assertEqual("imperial", reload_config(value).DISPLAY_UNITS)

    def test_anything_unrecognised_falls_back_to_metric(self):
        """Never guess. An unreadable setting must not silently reinterpret a tank."""
        for value in ("", "metric", "in", "inches", "nonsense"):
            with self.subTest(value=value):
                self.assertEqual("metric", reload_config(value).DISPLAY_UNITS)


class DisplayUnitsIsPresentationOnlyTestCase(unittest.TestCase):
    def tearDown(self):
        importlib.reload(config)

    def test_imperial_does_not_move_any_threshold_or_calibration(self):
        metric = reload_config("metric")
        before = (
            metric.WATER_LOW_CM,
            metric.PUMP_CUTOFF_CM,
            metric.WATER_FULL_CM,
            metric.WATER_EMPTY_CM,
            metric.TANK_CAPACITY_GALLONS,
            metric.MAX_PUMP_RUN_SECONDS,
        )
        imperial = reload_config("imperial")
        after = (
            imperial.WATER_LOW_CM,
            imperial.PUMP_CUTOFF_CM,
            imperial.WATER_FULL_CM,
            imperial.WATER_EMPTY_CM,
            imperial.TANK_CAPACITY_GALLONS,
            imperial.MAX_PUMP_RUN_SECONDS,
        )
        self.assertEqual(before, after)


class SystemPublishesDisplayUnitsTestCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()

    def test_the_preference_is_published_for_clients(self):
        body = self.client.get("/system").get_json()
        self.assertEqual(config.DISPLAY_UNITS, body["display_units"])
        self.assertIn(body["display_units"], ("metric", "imperial"))

    def test_the_readings_alongside_it_stay_metric(self):
        """A client converts for display; it is never handed pre-converted values."""
        with patch.object(config, "DISPLAY_UNITS", "imperial"):
            body = self.client.get("/system").get_json()
        self.assertEqual(config.WATER_FULL_CM, body["water_full_cm"])
        self.assertEqual(config.WATER_EMPTY_CM, body["water_empty_cm"])


class MqttStaysMetricTestCase(unittest.TestCase):
    """HA converts for itself, so publishing inches would fight its unit system."""

    def test_every_distance_entity_is_published_in_cm_with_a_device_class(self):
        import json

        import mqtt

        published = []

        class Capture:
            def publish(self, topic, payload=None, **kwargs):
                published.append(payload)

        mqtt.send_discovery_messages(Capture())
        distances = [
            json.loads(p) for p in published if p and '"unit_of_measurement": "cm"' in str(p)
        ]
        self.assertGreaterEqual(len(distances), 4)
        for entity in distances:
            with self.subTest(entity=entity["unique_id"]):
                # device_class is what lets HA offer inches without us sending them.
                self.assertEqual("distance", entity["device_class"])


if __name__ == "__main__":
    unittest.main()
