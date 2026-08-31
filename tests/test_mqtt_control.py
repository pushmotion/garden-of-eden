"""Drive mqtt.on_message for the HA grow/schedule control topics (offline).

Verifies that commands published by Home Assistant mutate the persisted grow
state / schedule toggles and echo the new state back. crontab writes are stubbed
so nothing touches the host, and the state files live in a temp dir.
"""

import os
import tempfile
import unittest


class FakeClient:
    def __init__(self):
        self.published = []

    def publish(self, topic, payload=None, **kwargs):
        self.published.append((topic, payload))


class FakeMsg:
    def __init__(self, topic, payload):
        self.topic = topic
        self.payload = payload.encode() if isinstance(payload, str) else payload


class MqttControlTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import config
        import mqtt

        cls.mqtt = mqtt
        cls.config = config
        cls.base = mqtt.BASE_TOPIC

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.config.SCHEDULE_FILE = os.path.join(self.tmp, "sched.json")
        self.config.GROW_STATE_FILE = os.path.join(self.tmp, "grow.json")
        # Actuator state is persisted too; keep it out of the developer's home.
        self.config.STATE_FILE = os.path.join(self.tmp, "state.json")
        # Never touch the host crontab.
        self.mqtt.sched_lib._read_crontab = lambda: []
        self.mqtt.sched_lib._write_crontab = lambda lines: None
        self.client = FakeClient()

    def send(self, suffix, payload):
        self.mqtt.on_message(self.client, None, FakeMsg(f"{self.base}/{suffix}", payload))

    def published_for(self, suffix):
        topic = f"{self.base}/{suffix}"
        return [p for t, p in self.client.published if t == topic]

    # --- schedule toggles ---
    def test_lights_and_pump_and_vacation_toggle(self):
        self.send("schedule/lights/enabled/set", "ON")
        self.send("schedule/pump/enabled/set", "ON")
        self.send("schedule/vacation/enabled/set", "ON")

        sched = self.mqtt.sched_lib.load_schedule()
        self.assertTrue(sched["lights"]["enabled"])
        self.assertTrue(sched["pump"]["enabled"])
        self.assertTrue(sched["vacation"]["enabled"])
        self.assertIn("ON", self.published_for("schedule/lights/enabled"))
        self.assertIn("ON", self.published_for("schedule/vacation/enabled"))

    def test_lights_toggle_off(self):
        self.send("schedule/lights/enabled/set", "ON")
        self.send("schedule/lights/enabled/set", "OFF")
        self.assertFalse(self.mqtt.sched_lib.load_schedule()["lights"]["enabled"])
        self.assertIn("OFF", self.published_for("schedule/lights/enabled"))

    def test_toggle_preserves_per_day_windows(self):
        # A saved per-day schedule must survive an enable toggle from HA.
        self.mqtt.sched_lib.save_schedule(
            {
                "lights": {
                    "enabled": False,
                    "days": {"mon": [{"onTime": "23:00", "offTime": "07:00", "brightness": 80}]},
                },
                "pump": {"enabled": False, "days": {"mon": [{"time": "02:00", "duration": 5}]}},
            }
        )
        self.send("schedule/lights/enabled/set", "ON")
        sched = self.mqtt.sched_lib.load_schedule()
        self.assertTrue(sched["lights"]["enabled"])
        self.assertEqual(sched["lights"]["days"]["mon"][0]["onTime"], "23:00")
        self.assertEqual(sched["pump"]["days"]["mon"][0]["time"], "02:00")

    # --- everyday schedule setters (write one window/run to all 7 days) ---
    def test_set_everyday_light_window(self):
        self.send("schedule/lights/on/set", "23:00:00")
        self.send("schedule/lights/off/set", "07:00:00")
        self.send("schedule/lights/brightness/set", "80")

        days = self.mqtt.sched_lib.load_schedule()["lights"]["days"]
        for day in self.mqtt.sched_lib.DAYS:
            self.assertEqual(days[day][0]["onTime"], "23:00")
            self.assertEqual(days[day][0]["offTime"], "07:00")
            self.assertEqual(days[day][0]["brightness"], 80)
        self.assertIn("23:00:00", self.published_for("schedule/lights/on"))
        self.assertIn("80", self.published_for("schedule/lights/brightness"))

    def test_set_everyday_pump_run(self):
        self.send("schedule/pump/time/set", "02:30:00")
        self.send("schedule/pump/duration/set", "4")

        days = self.mqtt.sched_lib.load_schedule()["pump"]["days"]
        for day in self.mqtt.sched_lib.DAYS:
            self.assertEqual(days[day][0]["time"], "02:30")
            self.assertEqual(days[day][0]["duration"], 4)

    def test_pump_duration_clamped_to_safety_cap(self):
        self.send("schedule/pump/duration/set", "99")  # above the 5-min cap
        run = self.mqtt.sched_lib.load_schedule()["pump"]["days"]["mon"][0]
        self.assertEqual(run["duration"], 5)

    def test_setting_one_light_field_preserves_others(self):
        self.send("schedule/lights/on/set", "23:00:00")
        self.send("schedule/lights/brightness/set", "60")  # must keep onTime 23:00
        mon = self.mqtt.sched_lib.load_schedule()["lights"]["days"]["mon"][0]
        self.assertEqual(mon["onTime"], "23:00")
        self.assertEqual(mon["brightness"], 60)

    # --- a scalar from HA must never collapse a multi-window/multi-run day ---
    # These setters describe one window/run, but the schedule they edit can hold
    # several a day. Both used to write their single value over all seven days,
    # silently discarding the rest; each of the four is now surgical.
    def _save_two_windows_and_runs(self):
        self.mqtt.sched_lib.save_schedule(
            {
                "lights": {
                    "enabled": True,
                    "days": {
                        "mon": [
                            {"onTime": "05:00", "offTime": "11:00", "brightness": 65},
                            {"onTime": "15:00", "offTime": "21:00", "brightness": 40},
                        ]
                    },
                },
                "pump": {
                    "enabled": True,
                    "days": {
                        "mon": [
                            {"time": "06:00", "duration": 3},
                            {"time": "18:00", "duration": 3},
                        ]
                    },
                },
            }
        )

    def test_light_time_moves_only_the_first_window(self):
        self._save_two_windows_and_runs()
        self.send("schedule/lights/on/set", "04:00:00")

        mon = self.mqtt.sched_lib.load_schedule()["lights"]["days"]["mon"]
        self.assertEqual(len(mon), 2, "the second window must survive")
        self.assertEqual(mon[0]["onTime"], "04:00")
        self.assertEqual(mon[0]["offTime"], "11:00", "the first window keeps its off time")
        self.assertEqual(mon[1], {"onTime": "15:00", "offTime": "21:00", "brightness": 40})

    def test_light_brightness_applies_to_every_window(self):
        self._save_two_windows_and_runs()
        self.send("schedule/lights/brightness/set", "80")

        mon = self.mqtt.sched_lib.load_schedule()["lights"]["days"]["mon"]
        self.assertEqual(len(mon), 2)
        self.assertEqual([w["brightness"] for w in mon], [80, 80])
        # Times are a property of each window and must be left alone.
        self.assertEqual([w["onTime"] for w in mon], ["05:00", "15:00"])
        self.assertEqual([w["offTime"] for w in mon], ["11:00", "21:00"])

    def test_pump_time_moves_only_the_first_run(self):
        self._save_two_windows_and_runs()
        self.send("schedule/pump/time/set", "01:00:00")

        mon = self.mqtt.sched_lib.load_schedule()["pump"]["days"]["mon"]
        self.assertEqual(len(mon), 2, "the second run must survive")
        self.assertEqual(mon[0]["time"], "01:00")
        self.assertEqual(mon[1], {"time": "18:00", "duration": 3})

    def test_pump_duration_applies_to_every_run(self):
        self._save_two_windows_and_runs()
        self.send("schedule/pump/duration/set", "5")

        mon = self.mqtt.sched_lib.load_schedule()["pump"]["days"]["mon"]
        self.assertEqual(len(mon), 2)
        self.assertEqual([r["duration"] for r in mon], [5, 5])
        self.assertEqual([r["time"] for r in mon], ["06:00", "18:00"])

    # --- grow cycle ---
    def test_grow_stage_set(self):
        self.send("grow/stage/set", "thinning")
        self.assertEqual(self.mqtt.grow_lib.load_state()["stage"], "thinning")
        self.assertIn("thinning", self.published_for("grow/stage"))

    def test_grow_stage_invalid_is_rejected(self):
        self.send("grow/stage/set", "thinning")
        self.send("grow/stage/set", "not_a_stage")  # ValueError -> ignored
        self.assertEqual(self.mqtt.grow_lib.load_state()["stage"], "thinning")

    def test_grow_start_resets(self):
        self.send("grow/stage/set", "harvest")
        self.send("grow/start/set", "PRESS")
        self.assertEqual(self.mqtt.grow_lib.load_state()["stage"], "germination")

    # --- actuator commands are persisted and tracked ---
    # A command arriving over MQTT has to leave the same trace a physical button
    # press does, or power-loss recovery restores a stale value and the button's
    # next toggle fights whatever Home Assistant last did.
    def test_light_command_persists_and_tracks_state(self):
        self.send("light/command", "ON")
        self.assertTrue(self.mqtt.state_lib.load_state()["light_on"])
        self.assertTrue(self.mqtt.light_state)
        self.assertIn("ON", self.published_for("light/state"))

        self.send("light/command", "OFF")
        self.assertFalse(self.mqtt.state_lib.load_state()["light_on"])
        self.assertFalse(self.mqtt.light_state)

    def test_light_brightness_persists(self):
        self.send("light/brightness/set", "42")
        self.assertEqual(self.mqtt.state_lib.load_state()["brightness"], 42)

    def test_pump_speed_persists_and_tracks_state(self):
        self.send("pump/speed/set", "60")
        saved = self.mqtt.state_lib.load_state()
        self.assertEqual(saved["speed"], 60)
        self.assertTrue(saved["pump_on"])
        self.assertTrue(self.mqtt.pump_state)

        self.send("pump/speed/set", "0")
        self.assertFalse(self.mqtt.state_lib.load_state()["pump_on"])
        self.assertFalse(self.mqtt.pump_state)

    def test_pump_speed_set_publishes_on_off_state(self):
        """The slider must drive pump/state, not just pump/speed/state.

        Without this HA shows the pump OFF while it runs, and then refuses to
        send further brightness commands to what it believes is an off light.
        """
        self.send("pump/speed/set", "55")
        self.assertIn("ON", self.published_for("pump/state"))
        self.assertIn("55", self.published_for("pump/speed/state"))


if __name__ == "__main__":
    unittest.main()
