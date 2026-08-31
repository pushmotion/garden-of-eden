import unittest
from unittest.mock import patch

import config
from app.lib import hardware


class DetectModelTestCase(unittest.TestCase):
    @patch.object(config, "MODEL_OVERRIDE", "gardyn studio")
    def test_override_wins(self):
        self.assertEqual(hardware.detect_model(), "gardyn studio")

    @patch.object(config, "MODEL_OVERRIDE", None)
    @patch.object(config, "SENSOR_TYPE", "DHT20")
    @patch.object(hardware, "i2c_device_present", return_value=False)
    def test_dht20_implies_3_0(self, _present):
        self.assertEqual(hardware.detect_model(), "gardyn 3.0")

    @patch.object(config, "MODEL_OVERRIDE", None)
    @patch.object(config, "SENSOR_TYPE", "AM2320")
    @patch.object(hardware, "i2c_device_present", return_value=False)
    def test_am2320_implies_2_0(self, _present):
        self.assertEqual(hardware.detect_model(), "gardyn 2.0")


class SystemRouteTestCase(unittest.TestCase):
    def setUp(self):
        from app import create_app

        self.client = create_app("default").test_client()

    @patch("app.sensors.system.routes.detect_model", return_value="gardyn 3.0")
    def test_system_reports_model_and_profile(self, _model):
        resp = self.client.get("/system")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertEqual(body["model"], "gardyn 3.0")
        self.assertEqual(body["profile"], config.MODELS["gardyn 3.0"])

    @patch("app.sensors.system.routes.detect_model", return_value="gardyn 3.0 (simulated)")
    def test_profile_resolves_for_suffixed_model(self, _model):
        # Custom/suffixed model strings still resolve to the closest profile.
        body = self.client.get("/system").get_json()
        self.assertEqual(body["profile"], config.MODELS["gardyn 3.0"])


class CurrentDutyFractionTestCase(unittest.TestCase):
    """Constructing a driver must observe the pin, not reset it.

    PWMLED defaults to initial_value=0, so before this helper existed every
    start of the REST API wrote 0 to the light and pump pins and undid whatever
    the cron schedule had set.
    """

    class _Pi:
        def __init__(self, duty, span):
            self._duty, self._span = duty, span

        def get_PWM_dutycycle(self, _pin):
            if isinstance(self._duty, Exception):
                raise self._duty
            return self._duty

        def get_PWM_range(self, _pin):
            return self._span

    def test_reads_live_duty_as_a_fraction(self):
        self.assertAlmostEqual(hardware.current_duty_fraction(self._Pi(6500, 10000), 18), 0.65)

    def test_off_pin_reads_zero(self):
        self.assertEqual(hardware.current_duty_fraction(self._Pi(0, 10000), 18), 0)

    def test_no_pigpio_client_falls_back(self):
        self.assertEqual(hardware.current_duty_fraction(None, 18), 0)
        self.assertEqual(hardware.current_duty_fraction(None, 18, default=1), 1)

    def test_pin_not_in_pwm_mode_falls_back(self):
        pi = self._Pi(Exception("GPIO not set up for PWM"), 10000)
        self.assertEqual(hardware.current_duty_fraction(pi, 18), 0)

    def test_zero_range_falls_back(self):
        self.assertEqual(hardware.current_duty_fraction(self._Pi(100, 0), 18), 0)

    def test_result_is_clamped(self):
        self.assertEqual(hardware.current_duty_fraction(self._Pi(20000, 10000), 18), 1.0)


if __name__ == "__main__":
    unittest.main()
