"""Slow telemetry is retained and paced by one config value.

Home Assistant drops its MQTT state on restart. Temperature, humidity, PCB
temperature and the derived water figures were published without ``retain``,
so after an HA restart every one of them read *unknown* until the next poll --
up to half an hour for the ambient readings. That half hour was also a
hard-coded ``sleep(30 * 60)`` in three loops while ``publish_frequency`` held
the same number and only the grow loop read it; ``TELEMETRY_INTERVAL_SECONDS``
now drives all of them.
"""

import contextlib
import importlib
import os
import tempfile
import unittest
from unittest import mock


class FakeClient:
    def __init__(self):
        self.published = []

    def publish(self, topic, payload=None, **kwargs):
        self.published.append((topic, payload, bool(kwargs.get("retain"))))

    def retained(self, topic):
        return [r for t, _p, r in self.published if t == topic]


class _StopLoop(Exception):
    """Raised from the patched sleep to leave a ``while True`` publisher."""


class TelemetryCadenceTestCase(unittest.TestCase):
    def _interval(self, **env):
        """config.TELEMETRY_INTERVAL_SECONDS as parsed from ``env``, then restored."""
        import config

        for key, value in env.items():
            os.environ[key] = value
        try:
            return importlib.reload(config).TELEMETRY_INTERVAL_SECONDS
        finally:
            for key in env:
                os.environ.pop(key, None)
            importlib.reload(config)

    def test_the_interval_is_configurable_and_defaults_to_half_an_hour(self):
        self.assertEqual(self._interval(TELEMETRY_INTERVAL_SECONDS="600"), 600)
        self.assertEqual(self._interval(), 1800)

    def test_the_service_reads_its_cadence_from_config(self):
        import config
        import mqtt

        self.assertEqual(mqtt.publish_frequency, config.TELEMETRY_INTERVAL_SECONDS)


class TelemetryRetainTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import config
        import mqtt

        cls.mqtt = mqtt
        cls.config = config

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        for name in ("STATE_FILE", "GROW_STATE_FILE"):
            patcher = mock.patch.object(
                self.config, name, os.path.join(self.tmp, name.lower() + ".json")
            )
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = FakeClient()

    def _run_loop(self, loop, interval=123):
        """Run one pass of a publisher loop; the patched sleep ends it."""
        sleep = mock.patch.object(self.mqtt, "sleep", side_effect=_StopLoop)
        frequency = mock.patch.object(self.mqtt, "publish_frequency", interval)
        with sleep as slept, frequency:
            with self.assertRaises(_StopLoop):
                loop(self.client)
        slept.assert_called_once_with(interval)

    def test_each_slow_loop_publishes_retained_and_sleeps_the_configured_interval(self):
        loops = (
            ("temperature", self.mqtt.publish_temperature, "temperature_sensor"),
            ("humidity", self.mqtt.publish_humidity, "humidity_sensor"),
        )
        for suffix, loop, sensor in loops:
            with self.subTest(suffix=suffix):
                self.client = FakeClient()
                with mock.patch.object(getattr(self.mqtt, sensor), "read", return_value=21.5):
                    self._run_loop(loop)
                self.assertEqual(self.client.retained(f"{self.mqtt.BASE_TOPIC}/{suffix}"), [True])

        self.client = FakeClient()
        with mock.patch.object(self.mqtt, "get_pcb_temperature", return_value=41.0):
            self._run_loop(self.mqtt.publish_pcb_temperature)
        self.assertEqual(self.client.retained(f"{self.mqtt.BASE_TOPIC}/pcb/temperature"), [True])

    def test_the_grow_loop_sleeps_the_same_interval(self):
        self._run_loop(self.mqtt.publish_grow_reminders, interval=77)
        self.assertEqual(self.client.retained(f"{self.mqtt.BASE_TOPIC}/grow/stage"), [True, True])

    def test_derived_water_readings_are_retained(self):
        with mock.patch.object(self.mqtt, "WATER_FULL_CM", 5.0):
            with mock.patch.object(self.mqtt, "WATER_EMPTY_CM", 20.0):
                self.mqtt.publish_water_readings(self.client, 10.0)
        for suffix in ("water/level", "water/depth", "water/percent", "water/gallons"):
            with self.subTest(suffix=suffix):
                self.assertEqual(self.client.retained(f"{self.mqtt.BASE_TOPIC}/{suffix}"), [True])

    def test_on_demand_reads_are_retained_too(self):
        """A later non-retained publish would not replace the retained one, so
        HA could come back after a restart to a value older than the last read."""

        def send(suffix):
            msg = mock.Mock(topic=f"{self.mqtt.BASE_TOPIC}/{suffix}", payload=b"")
            self.mqtt.on_message(self.client, None, msg)

        with contextlib.ExitStack() as stack:
            stack.enter_context(
                mock.patch.object(self.mqtt.temperature_sensor, "read", return_value=21.5)
            )
            stack.enter_context(
                mock.patch.object(self.mqtt.humidity_sensor, "read", return_value=55.0)
            )
            stack.enter_context(
                mock.patch.object(self.mqtt, "get_pcb_temperature", return_value=41.0)
            )
            send("temperature/get")
            send("humidity/get")
            send("pcb/temperature/get")
        for suffix in ("temperature", "humidity", "pcb/temperature"):
            with self.subTest(suffix=suffix):
                self.assertEqual(self.client.retained(f"{self.mqtt.BASE_TOPIC}/{suffix}"), [True])


if __name__ == "__main__":
    unittest.main()
