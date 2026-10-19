"""The physical button, the safety cap and startup restore report like MQTT.

The MQTT command paths go through ``commit_light_state`` / ``commit_pump_state``:
state and level published retained, the toggle flag synced from the real duty
cycle, and the state file updated. The button callbacks, the pump safety cap
and ``restore_actuator_state`` each hand-rolled a bare, unretained ``ON``/``OFF``
publish instead -- so Home Assistant showed the old brightness after a button
press, lost the state entirely after its own restart, and a double press with
the speed slider at 0 published ON, armed the cap and left the pump stopped.
"""

import os
import tempfile
import unittest
from unittest import mock


class FakeClient:
    def __init__(self):
        self.published = []

    def publish(self, topic, payload=None, **kwargs):
        self.published.append((topic, payload, bool(kwargs.get("retain"))))

    def last(self, topic):
        """``(payload, retained)`` of the most recent publish on ``topic``."""
        matches = [(p, r) for t, p, r in self.published if t == topic]
        return matches[-1] if matches else None


class ButtonPathTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import config
        import mqtt
        from app.lib import state as state_lib

        cls.mqtt = mqtt
        cls.config = config
        cls.state_lib = state_lib
        cls.base = mqtt.BASE_TOPIC

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        for name in ("STATE_FILE", "SCHEDULE_FILE"):
            patcher = mock.patch.object(
                self.config, name, os.path.join(self.tmp, name.lower() + ".json")
            )
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = FakeClient()
        # The button callbacks publish on the module-level client that
        # __main__ creates; stand one in for them.
        patcher = mock.patch.object(self.mqtt, "client", self.client, create=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in ("light_state", "pump_state", "brightness", "speed"):
            self.addCleanup(setattr, self.mqtt, name, getattr(self.mqtt, name))
        self.mqtt.light.off()
        self.mqtt.pump.off()
        self.mqtt.light_state = False
        self.mqtt.pump_state = False
        self.addCleanup(self.mqtt.pump.off)
        self.addCleanup(self.mqtt.light.off)
        self.addCleanup(self.mqtt._cancel_pump_safety)

    def _cap_pending(self):
        with self.mqtt._pump_timer_lock:
            return self.mqtt._pump_safety_pending_locked()

    def test_light_button_publishes_retained_state_and_brightness(self):
        self.mqtt.brightness = 65
        self.mqtt._toggle_light()
        self.assertEqual(self.client.last(f"{self.base}/light/state"), ("ON", True))
        self.assertEqual(self.client.last(f"{self.base}/light/brightness/state"), ("65", True))
        self.assertTrue(self.mqtt.light_state)
        saved = self.state_lib.load_state()
        self.assertTrue(saved["light_on"])
        self.assertEqual(saved["brightness"], 65)

        self.mqtt._toggle_light()
        self.assertEqual(self.client.last(f"{self.base}/light/state"), ("OFF", True))
        self.assertEqual(self.client.last(f"{self.base}/light/brightness/state"), ("0", True))
        self.assertFalse(self.mqtt.light_state)
        self.assertFalse(self.state_lib.load_state()["light_on"])

    def test_pump_button_with_the_slider_at_zero_runs_at_the_default_speed(self):
        """The whole point: ON must mean the pump is actually turning."""
        self.mqtt.speed = 0
        self.mqtt._toggle_pump()
        default = self.config.DEFAULT_PUMP_SPEED
        self.assertEqual(self.mqtt.pump.get_speed(), default, "the pump was left at 0%")
        self.assertEqual(self.mqtt.speed, default)
        self.assertEqual(self.client.last(f"{self.base}/pump/state"), ("ON", True))
        self.assertEqual(self.client.last(f"{self.base}/pump/speed/state"), (str(default), True))
        self.assertTrue(self.mqtt.pump_state)
        self.assertTrue(self._cap_pending())
        saved = self.state_lib.load_state()
        self.assertTrue(saved["pump_on"])
        self.assertEqual(saved["speed"], default)

    def test_pump_button_off_publishes_retained_off_and_clears_the_cap(self):
        self.mqtt.speed = 80
        self.mqtt._toggle_pump()
        self.assertTrue(self._cap_pending())
        self.mqtt._toggle_pump()
        self.assertEqual(self.mqtt.pump.get_speed(), 0)
        self.assertEqual(self.client.last(f"{self.base}/pump/state"), ("OFF", True))
        self.assertEqual(self.client.last(f"{self.base}/pump/speed/state"), ("0", True))
        self.assertFalse(self.mqtt.pump_state)
        self.assertFalse(self._cap_pending())
        self.assertFalse(self.state_lib.load_state()["pump_on"])

    def test_the_safety_cap_reports_retained_off_and_persists_it(self):
        self.mqtt.speed = 80
        self.mqtt._toggle_pump()
        self.mqtt._safety_pump_off()
        self.assertEqual(self.mqtt.pump.get_speed(), 0)
        self.assertEqual(self.client.last(f"{self.base}/pump/state"), ("OFF", True))
        self.assertEqual(self.client.last(f"{self.base}/pump/speed/state"), ("0", True))
        self.assertFalse(self.mqtt.pump_state)
        self.assertFalse(self.state_lib.load_state()["pump_on"])

    def test_restore_publishes_retained_state_and_level_for_both_actuators(self):
        self.state_lib.save_state(light_on=True, brightness=40, pump_on=True, speed=70)
        self.mqtt.restore_actuator_state(self.client)
        self.assertEqual(self.client.last(f"{self.base}/light/state"), ("ON", True))
        self.assertEqual(self.client.last(f"{self.base}/light/brightness/state"), ("40", True))
        self.assertEqual(self.client.last(f"{self.base}/pump/state"), ("ON", True))
        self.assertEqual(self.client.last(f"{self.base}/pump/speed/state"), ("70", True))
        self.assertTrue(self.mqtt.light_state)
        self.assertTrue(self.mqtt.pump_state)
        self.assertTrue(self._cap_pending(), "a restored run is still capped")

    def test_a_button_press_during_cleaning_ends_the_run_and_reports_off(self):
        from app.lib import cleaning
        from app.sensors.schedule import schedule

        cleaning.start(600)
        self.mqtt.pump.set_speed(100)
        with mock.patch.object(schedule, "expected_light_state", return_value=None):
            self.mqtt._toggle_pump()
        self.assertFalse(cleaning.is_active())
        self.assertEqual(self.mqtt.pump.get_speed(), 0)
        self.assertEqual(self.client.last(f"{self.base}/pump/state"), ("OFF", True))
        self.assertEqual(self.client.last(f"{self.base}/pump/clean/state"), ("OFF", True))
        self.assertFalse(self.mqtt.pump_state)


if __name__ == "__main__":
    unittest.main()
