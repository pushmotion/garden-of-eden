"""mqtt.graceful_shutdown tears the hardware down in a safe order (offline).

gpiozero's DistanceSensor runs a queue-filling thread through pigpio. Closing
the pump drops that connection, so a sensor left open had its thread die with
"OSError: [Errno 9] Bad file descriptor" in the journal at every SIGTERM. The
light must still never be closed: gpiozero writes 0 to the pin on close, which
switched the grow light off on every `systemctl restart`.
"""

import unittest
from unittest import mock


class GracefulShutdownTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import mqtt

        cls.mqtt = mqtt

    def _shutdown(self, distance_sensor):
        """Run graceful_shutdown against mocks that share one call recorder."""
        calls = mock.Mock()
        pump, light = calls.pump, calls.light
        sensor = calls.distance_sensor if distance_sensor == "present" else distance_sensor
        with (
            mock.patch.object(self.mqtt, "pump", pump),
            mock.patch.object(self.mqtt, "light", light),
            mock.patch.object(self.mqtt, "distance_sensor", sensor),
            mock.patch.object(self.mqtt.state_lib, "save_state"),
            mock.patch.object(self.mqtt.cleaning_lib, "is_active", return_value=False),
        ):
            with self.assertRaises(SystemExit) as raised:
                self.mqtt.graceful_shutdown(15, None)
        self.assertEqual(raised.exception.code, 0)
        return calls

    @staticmethod
    def _names(calls):
        return [name for name, _args, _kwargs in calls.mock_calls]

    def test_closes_the_distance_sensor_before_the_pump_and_never_the_light(self):
        calls = self._shutdown("present")
        names = self._names(calls)
        self.assertIn("distance_sensor.close", names)
        self.assertIn("pump.close", names)
        self.assertLess(names.index("distance_sensor.close"), names.index("pump.close"))
        self.assertLess(names.index("pump.off"), names.index("distance_sensor.close"))
        self.assertFalse(
            [n for n in names if n.startswith("light.")], "the light must be left alone"
        )

    def test_a_failing_sensor_close_does_not_skip_the_pump_close(self):
        calls = mock.Mock()
        calls.distance_sensor.close.side_effect = OSError(9, "Bad file descriptor")
        with (
            mock.patch.object(self.mqtt, "pump", calls.pump),
            mock.patch.object(self.mqtt, "light", calls.light),
            mock.patch.object(self.mqtt, "distance_sensor", calls.distance_sensor),
            mock.patch.object(self.mqtt.state_lib, "save_state"),
            mock.patch.object(self.mqtt.cleaning_lib, "is_active", return_value=False),
        ):
            with self.assertRaises(SystemExit):
                self.mqtt.graceful_shutdown(15, None)
        calls.pump.close.assert_called_once_with()
        calls.light.close.assert_not_called()

    def test_a_tower_whose_sensor_never_initialised_still_shuts_down(self):
        calls = self._shutdown(None)
        names = self._names(calls)
        self.assertIn("pump.close", names)
        self.assertFalse([n for n in names if n.startswith("light.")])


if __name__ == "__main__":
    unittest.main()
