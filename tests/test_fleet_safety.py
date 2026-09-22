"""Regressions across MQTT, REST and the shared persisted pump state."""

import importlib.util
import json
import multiprocessing
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import config
import mqtt
from app import create_app
from app.lib import cleaning, state
from app.sensors.pump import routes


def _write_keys(path, prefix):
    config.STATE_FILE = path
    for index in range(20):
        state.save_state(strict=True, **{f"{prefix}_{index}": index})


class FleetSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = os.path.join(self.temp.name, "state.json")
        for name, value in (
            ("STATE_FILE", self.path),
            ("CLEANING_CUTOFF_CM", 18.0),
            ("WATER_EMPTY_CM", 24.0),
        ):
            p = patch.object(config, name, value)
            p.start()
            self.addCleanup(p.stop)
        watchdog = patch.object(routes, "_ensure_watchdog")
        watchdog.start()
        self.addCleanup(watchdog.stop)
        self.addCleanup(mqtt._cancel_pump_safety)
        self.addCleanup(routes._cancel_auto_off)
        self.client = MagicMock()
        self.api = create_app().test_client()
        self.reading()

    def reading(self, airgap=10):
        state.save_state(
            water_airgap_cm=airgap,
            water_checked_at=datetime.now().isoformat(),
            pump_blocked=airgap > 18,
        )

    def send(self, topic, payload):
        mqtt.on_message(
            self.client,
            None,
            MagicMock(topic=mqtt.BASE_TOPIC + "/" + topic, payload=payload.encode()),
        )

    def test_rest_refuses_all_positive_commands_on_fresh_low_water(self):
        self.reading(23)
        with (
            patch.object(routes.pump_control, "on") as on,
            patch.object(routes.pump_control, "set_speed") as speed,
        ):
            for endpoint, data in (("on", {}), ("run", {"seconds": 30}), ("speed", {"value": 50})):
                self.assertEqual(self.api.post("/pump/" + endpoint, json=data).status_code, 409)
            on.assert_not_called()
            speed.assert_not_called()
            self.assertEqual(self.api.post("/pump/off").status_code, 200)

    def test_cleaning_start_fails_before_gpio_when_disk_write_fails(self):
        with (
            patch.object(state, "write_json_atomic", side_effect=OSError("disk full")),
            patch.object(routes.pump_control, "on") as on,
        ):
            self.assertEqual(self.api.post("/pump/clean", json={"minutes": 60}).status_code, 503)
            on.assert_not_called()

    def test_old_mqtt_and_rest_caps_cannot_stop_cleaning(self):
        cleaning.start(3600)
        with (
            patch.object(mqtt.pump, "off") as off,
            patch.object(routes.pump_control, "off") as rest_off,
        ):
            mqtt._safety_pump_off()
            routes._safety_off()
            off.assert_not_called()
            rest_off.assert_not_called()

    def test_mqtt_stop_clears_session_and_slider_cannot_arm_short_cap(self):
        cleaning.start(3600)
        with patch.object(mqtt, "_arm_pump_safety") as arm:
            self.send("pump/speed/set", "50")
            self.send("pump/command", "ON")
            arm.assert_not_called()
        self.send("pump/command", "OFF")
        self.assertFalse(cleaning.is_active())

    def test_expired_session_is_not_restored_as_ordinary_run(self):
        cleaning.start(1, now=datetime.now() - timedelta(hours=1))
        state.save_state(pump_on=True)
        with patch.object(mqtt.pump, "set_speed") as start:
            mqtt.restore_actuator_state(self.client)
            start.assert_not_called()
        self.assertFalse(state.load_state()["pump_on"])

    def test_old_watchdog_cannot_stop_new_session(self):
        old = cleaning.start(60)
        cleaning.clear()
        new = cleaning.start(60)
        with patch.object(routes.pump_control, "off") as off:
            routes.stop_cleaning(expected_id=old["id"])
            off.assert_not_called()
        self.assertEqual(cleaning.session()["id"], new["id"])

    def test_resume_refuses_stale_water(self):
        cleaning.start(3600)
        state.save_state(water_checked_at="2000-01-01T00:00:00")
        self.assertIsNone(routes.resume_cleaning_if_active())
        self.assertFalse(cleaning.is_active())

    def test_cleaning_skips_scheduled_lights_but_allows_manual_lighting(self):
        from app.sensors.light.light import ramp_to

        cleaning.start(3600)
        light = MagicMock()
        light.get_brightness.return_value = 40
        ramp_to(light, 80, 0, scheduled=True)
        light.set_brightness.assert_not_called()
        ramp_to(light, 65, 0)
        light.set_brightness.assert_called_once_with(65)
        with patch.object(mqtt.light, "set_duty_cycle") as pwm:
            mqtt.apply_scheduled_state(self.client)
            pwm.assert_not_called()
        self.assertEqual(self.api.post("/light/brightness", json={"value": 55}).status_code, 200)

    def test_running_light_fade_cannot_resume_after_cleaning(self):
        from app.sensors.light.light import ramp_to

        light = MagicMock()
        light.get_brightness.return_value = 0

        def clean_between_steps(_delay):
            cleaning.start(60)
            cleaning.clear()

        with patch("app.sensors.light.light.time.sleep", side_effect=clean_between_steps):
            ramp_to(light, 100, 1, scheduled=True)
        self.assertEqual(light.set_brightness.call_count, 1)

    def test_cleaning_end_restores_current_light_schedule_without_replaying_water(self):
        from app.sensors.light import routes as lights
        from app.sensors.schedule import schedule

        for expected in ((True, 65), (False, 0), None):
            with self.subTest(expected=expected):
                cleaning.start(60)
                with (
                    patch.object(schedule, "expected_light_state", return_value=expected),
                    patch.object(lights.light_control, "set_brightness") as brightness,
                    patch.object(routes.pump_control, "on") as pump_on,
                ):
                    routes.stop_cleaning()
                    if expected is None:
                        brightness.assert_not_called()
                    else:
                        brightness.assert_called_once_with(expected[1])
                    pump_on.assert_not_called()

    def test_api_returns_cached_reading_and_marks_stale_data(self):
        self.assertEqual(self.api.get("/distance").get_json()["distance"], 10)
        state.save_state(water_checked_at="2000-01-01T00:00:00")
        data = self.api.get("/distance").get_json()
        self.assertFalse(data["fresh"])
        self.assertIsNone(data["distance"])

    def test_two_processes_preserve_each_others_updates(self):
        processes = [
            multiprocessing.Process(target=_write_keys, args=(self.path, prefix))
            for prefix in ("a", "b")
        ]
        for process in processes:
            process.start()
        for process in processes:
            process.join(10)
            if process.is_alive():
                process.terminate()
                process.join()
                self.fail("State writer deadlocked")
            self.assertEqual(process.exitcode, 0)
        data = state.load_state()
        for prefix in ("a", "b"):
            for index in range(20):
                self.assertEqual(data[f"{prefix}_{index}"], index)


def _script(name):
    spec = importlib.util.spec_from_file_location(
        name, Path(__file__).resolve().parents[1] / "bin" / (name + ".py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ProvisioningTests(unittest.TestCase):
    def test_dashboard_covers_discovery_for_three_distinct_views(self):
        generator = _script("ha-dashboard")
        identifiers = ["gardyn_01", "gardyn_02", "gardyn_03"]
        output = generator.generate(identifiers)
        client = MagicMock()
        mqtt.send_discovery_messages(client)
        for identifier in identifiers:
            self.assertIn("path: " + identifier.replace("_", "-"), output)
            for call in client.publish.call_args_list:
                entity_id = json.loads(call.args[1])["default_entity_id"]
                self.assertIn(entity_id.replace(mqtt.IDENTIFIER, identifier), output)
        with self.assertRaises(ValueError):
            generator.generate(["gardyn_01", "gardyn_01"])

    def test_update_requires_success_for_exact_revision(self):
        updater = _script("check-update")
        sha = "a" * 40

        def opener_for(runs):
            from io import BytesIO

            return lambda *a, **kw: BytesIO(json.dumps({"workflow_runs": runs}).encode())

        run = dict(
            head_sha=sha, event="push", status="completed", conclusion="success", run_number=1
        )
        with patch.dict(os.environ, {}, clear=True):
            self.assertTrue(updater.approved(sha, "pushmotion/garden-of-eden", opener_for([run])))
            self.assertFalse(
                updater.approved("b" * 40, "pushmotion/garden-of-eden", opener_for([run]))
            )
            self.assertFalse(
                updater.approved(
                    sha, "pushmotion/garden-of-eden", opener_for([dict(run, conclusion="failure")])
                )
            )
            self.assertFalse(updater.approved(sha, "pushmotion/garden-of-eden", opener_for([])))
