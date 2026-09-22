import os
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config
from app.sensors.pump.pump import Pump


class TestPump(unittest.TestCase):

    @patch("app.sensors.pump.pump.PWMLED")
    @patch("app.sensors.pump.pump.PiGPIOFactory")
    @patch("app.sensors.pump.pump.pigpio.pi")
    def setUp(self, MockPi, MockFactory, MockPWMLED):
        self.mock_pwm_pump = MockPWMLED()
        self.mock_pwm_pump.value = Mock()  # Set an initial value as a new Mock
        self.mock_pi = MockPi()
        self.pump = Pump(24)

    def test_on(self):
        self.pump.on()
        self.assertEqual(self.mock_pwm_pump.value, 1)

    def test_off(self):
        self.pump.off()
        self.assertEqual(self.mock_pwm_pump.value, 0)

    @patch.object(config, "PUMP_MAINTENANCE", True)
    def test_maintenance_refuses_all_positive_pwm_paths(self):
        for action in (
            self.pump.on,
            lambda: self.pump.set_speed(50),
            lambda: self.pump.set_duty_cycle(100),
        ):
            with self.assertRaisesRegex(RuntimeError, "maintenance"):
                action()
            self.assertEqual(self.mock_pwm_pump.value, 0)
        self.pump.off()
        self.pump.set_speed(0)
        self.assertEqual(self.mock_pwm_pump.value, 0)

    @patch.object(config, "PUMP_MAINTENANCE", True)
    @patch("app.sensors.pump.pump.PWMLED")
    @patch("app.sensors.pump.pump.PiGPIOFactory")
    @patch("app.sensors.pump.pump.pigpio.pi")
    def test_maintenance_initializes_off_despite_live_nonzero_duty(self, pi, factory, pwm):
        with patch("app.sensors.pump.pump.hardware.current_duty_fraction", return_value=1):
            Pump(24)
        self.assertEqual(pwm.call_args.kwargs["initial_value"], 0)

    def test_set_speed(self):
        self.pump.set_speed(50)
        self.assertEqual(self.mock_pwm_pump.value, 0.5)

    def test_set_frequency(self):
        freq = 1000
        self.pump.set_frequency(freq)
        self.mock_pi.set_PWM_frequency.assert_called_with(24, freq)

    def test_set_duty_cycle_valid(self):
        self.pump.set_duty_cycle(30)
        self.assertEqual(self.mock_pwm_pump.value, 0.3)

    def test_set_duty_cycle_invalid_low(self):
        with self.assertRaises(ValueError):
            self.pump.set_duty_cycle(-1)

    def test_set_duty_cycle_invalid_high(self):
        with self.assertRaises(ValueError):
            self.pump.set_duty_cycle(101)

    def test_get_duty_cycle(self):
        self.mock_pwm_pump.value = 0.7
        result = self.pump.get_duty_cycle()
        self.assertEqual(result, 70.0)

    def test_close(self):
        self.pump.close()
        self.mock_pwm_pump.close.assert_called_once()
        self.mock_pi.stop.assert_called_once()


if __name__ == "__main__":
    unittest.main()
