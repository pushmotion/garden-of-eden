"""The cleaning REST surface, and the guards that keep a long run alive.

Two of these cover failures that would make cleaning mode *look* implemented and
silently not work: an ordinary pump call re-arming the five-minute cap over a
two-hour run, and the MQTT reconcile loop doing the same from a background
thread with nothing in the UI to explain it.
"""

import os
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

import config
from app import create_app
from app.lib import cleaning
from app.lib import state as state_lib
from app.sensors.pump import routes as pump_routes


class CleaningApiTestCase(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        os.unlink(self.path)
        self._patches = [
            patch.object(config, "STATE_FILE", self.path),
            patch.object(config, "CLEANING_MAX_SECONDS", 7200),
            patch.object(config, "CLEANING_DEFAULT_SECONDS", 3600),
            patch.object(config, "CLEANING_CUTOFF_CM", 18.0),
            patch.object(config, "PUMP_CUTOFF_CM", 12.9),
            patch.object(config, "WATER_EMPTY_CM", 23.05),
            patch.object(config, "CLEANING_MIN_DEPTH_CM", 2.0),
            # The watchdog is exercised directly below; a live thread would just
            # race these assertions.
            patch.object(pump_routes, "_ensure_watchdog", lambda: None),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(lambda: os.path.exists(self.path) and os.unlink(self.path))
        self.addCleanup(lambda: cleaning.clear(cleaning.DONE_STOPPED))

        self.client = create_app("default").test_client()
        self._fill_tank(14.0)

    def _fill_tank(self, airgap):
        """Record a fresh water reading, as the MQTT service would."""
        state_lib.save_state(
            water_airgap_cm=airgap,
            water_checked_at=datetime.now().isoformat(timespec="seconds"),
        )

    # -- starting and stopping ------------------------------------------------

    @patch("app.sensors.pump.routes.pump_control.set_speed")
    @patch("app.sensors.pump.routes.pump_control.on")
    def test_start_runs_the_pump_past_the_normal_cap(self, mock_on, mock_speed):
        resp = self.client.post("/pump/clean", json={"minutes": 90})
        self.assertEqual(resp.status_code, 200, resp.get_json())
        mock_on.assert_called_once()
        body = resp.get_json()["cleaning"]
        self.assertTrue(body["active"])
        # 90 minutes is 18x the five-minute cap every other path obeys.
        self.assertGreater(body["seconds"], config.MAX_PUMP_RUN_SECONDS)

    @patch("app.sensors.pump.routes.pump_control.set_speed")
    @patch("app.sensors.pump.routes.pump_control.on")
    def test_start_accepts_seconds_too(self, mock_on, mock_speed):
        resp = self.client.post("/pump/clean", json={"seconds": 1800})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["cleaning"]["seconds"], 1800)

    def test_start_rejects_a_duration_over_the_cleaning_cap(self):
        resp = self.client.post("/pump/clean", json={"seconds": 7201})
        self.assertEqual(resp.status_code, 400)

    @patch("app.sensors.pump.routes.pump_control.on")
    def test_start_refused_when_the_tank_is_below_the_cleaning_cutoff(self, mock_on):
        self._fill_tank(19.0)  # past the 18.0 cleaning cutoff
        resp = self.client.post("/pump/clean", json={"minutes": 60})
        # 409, not 400: the request was fine, the tower will not do it now.
        self.assertEqual(resp.status_code, 409)
        mock_on.assert_not_called()
        self.assertFalse(cleaning.is_active())

    @patch("app.sensors.pump.routes.pump_control.set_speed")
    @patch("app.sensors.pump.routes.pump_control.on")
    def test_start_allowed_where_a_watering_run_would_be_refused(self, mock_on, mock_speed):
        """The reason for a separate cutoff: cleaning happens on a drained tower."""
        self._fill_tank(15.0)  # past PUMP_CUTOFF_CM (12.9), inside 18.0
        self.assertEqual(self.client.post("/pump/clean", json={"minutes": 60}).status_code, 200)

    @patch("app.sensors.pump.routes.pump_control.off")
    @patch("app.sensors.pump.routes.pump_control.set_speed")
    @patch("app.sensors.pump.routes.pump_control.on")
    def test_stop_ends_the_run(self, mock_on, mock_speed, mock_off):
        self.client.post("/pump/clean", json={"minutes": 60})
        resp = self.client.post("/pump/clean/stop")
        self.assertEqual(resp.status_code, 200)
        mock_off.assert_called()
        self.assertFalse(cleaning.is_active())

    def test_stopping_when_nothing_is_running_is_not_an_error(self):
        self.assertEqual(self.client.post("/pump/clean/stop").status_code, 200)

    @patch("app.sensors.pump.routes.pump_control.set_speed")
    @patch("app.sensors.pump.routes.pump_control.on")
    def test_status_reports_the_run(self, mock_on, mock_speed):
        self.client.post("/pump/clean", json={"minutes": 60})
        body = self.client.get("/pump/clean").get_json()
        self.assertTrue(body["active"])
        self.assertGreater(body["remaining_seconds"], 3500)
        self.assertEqual(body["cutoff_cm"], 18.0)

    # -- the guards that keep the run alive -----------------------------------

    @patch("app.sensors.pump.routes.pump_control.set_speed")
    @patch("app.sensors.pump.routes.pump_control.on")
    def test_ordinary_pump_calls_are_refused_during_a_clean(self, mock_on, mock_speed):
        """Each of these would otherwise re-arm the five-minute auto-off and end
        a two-hour run at 5:00, from a background timer, with no explanation."""
        self.client.post("/pump/clean", json={"minutes": 60})
        self.assertEqual(self.client.post("/pump/on").status_code, 409)
        self.assertEqual(self.client.post("/pump/run", json={"seconds": 60}).status_code, 409)
        self.assertEqual(self.client.post("/pump/speed", json={"value": 50}).status_code, 409)
        self.assertTrue(cleaning.is_active())

    @patch("app.sensors.pump.routes.pump_control.off")
    @patch("app.sensors.pump.routes.pump_control.set_speed")
    @patch("app.sensors.pump.routes.pump_control.on")
    def test_off_always_works_and_ends_the_run(self, mock_on, mock_speed, mock_off):
        # "Off" is the one instruction that is safe from any source at any time.
        self.client.post("/pump/clean", json={"minutes": 60})
        self.assertEqual(self.client.post("/pump/off").status_code, 200)
        self.assertFalse(cleaning.is_active())

    @patch("app.sensors.pump.routes.pump_control.off")
    @patch("app.sensors.pump.routes.pump_control.set_speed")
    @patch("app.sensors.pump.routes.pump_control.on")
    def test_speed_zero_ends_the_run_rather_than_stranding_it(self, mock_on, mock_speed, mock_off):
        # Idling the pump but leaving the session would block every other path
        # for the rest of the two hours over a run that is not happening.
        self.client.post("/pump/clean", json={"minutes": 60})
        self.assertEqual(self.client.post("/pump/speed", json={"value": 0}).status_code, 200)
        self.assertFalse(cleaning.is_active())

    @patch("app.sensors.pump.routes.pump_control.set_speed")
    @patch("app.sensors.pump.routes.pump_control.on")
    def test_cleaning_does_not_overwrite_the_normal_pump_speed(self, mock_on, mock_speed):
        """``speed`` is what the MQTT toggle restores for ordinary runs.

        Writing the cleaning duty cycle into it would silently re-tune every
        future watering run to 100%.
        """
        state_lib.save_state(speed=45)
        self.client.post("/pump/clean", json={"minutes": 60, "speed": 100})
        self.assertEqual(state_lib.load_state()["speed"], 45)

    @patch("app.sensors.pump.routes.pump_control.set_speed")
    @patch("app.sensors.pump.routes.pump_control.on")
    def test_starting_twice_is_refused_rather_than_extending(self, mock_on, mock_speed):
        self.client.post("/pump/clean", json={"minutes": 60})
        self.assertEqual(self.client.post("/pump/clean", json={"minutes": 120}).status_code, 409)


class CleaningWatchdogTestCase(unittest.TestCase):
    """The tick logic, driven directly -- the watchdog itself is a sleep loop."""

    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        os.unlink(self.path)
        for p in (
            patch.object(config, "STATE_FILE", self.path),
            patch.object(config, "CLEANING_CUTOFF_CM", 18.0),
            patch.object(config, "WATER_EMPTY_CM", 23.05),
            patch.object(config, "CLEANING_MIN_DEPTH_CM", 2.0),
        ):
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(lambda: os.path.exists(self.path) and os.unlink(self.path))

    def _reading(self, airgap):
        state_lib.save_state(
            water_airgap_cm=airgap,
            water_checked_at=datetime.now().isoformat(timespec="seconds"),
        )

    def test_keeps_running_while_the_tank_holds(self):
        cleaning.start(seconds=3600)
        self._reading(14.0)
        self.assertIsNone(pump_routes._cleaning_tick_reason())

    def test_stops_when_the_tank_drops_mid_run(self):
        """A start-only check would miss this entirely -- which is the whole
        argument for re-checking a two-hour run."""
        cleaning.start(seconds=3600)
        self._reading(20.0)
        self.assertEqual(pump_routes._cleaning_tick_reason(), cleaning.DONE_LOW_WATER)

    def test_stops_when_the_deadline_passes(self):
        cleaning.start(seconds=1, now=datetime.now() - timedelta(seconds=10))
        self._reading(14.0)
        self.assertEqual(pump_routes._cleaning_tick_reason(), cleaning.DONE_COMPLETED)

    def test_stops_when_the_water_reading_goes_stale(self):
        cleaning.start(seconds=3600)
        state_lib.save_state(
            water_airgap_cm=14.0,
            water_checked_at=(datetime.now() - timedelta(hours=5)).isoformat(),
        )
        self.assertEqual(pump_routes._cleaning_tick_reason(), cleaning.DONE_NO_READING)

    def test_a_run_ended_elsewhere_exits_without_clobbering_the_reason(self):
        """Both processes watchdog the same run, so they race to end it.

        The loser must not call stop_cleaning() again: that would overwrite the
        winner's reason, and "completed" becoming "stopped" is exactly the
        detail somebody reads the sensor to find out.
        """
        cleaning.clear(cleaning.DONE_COMPLETED)
        self.assertEqual(pump_routes._cleaning_tick_reason(), pump_routes._ENDED_ELSEWHERE)
        self.assertEqual(cleaning.last_result(), cleaning.DONE_COMPLETED)

    @patch("app.sensors.pump.routes.pump_control.off")
    def test_resume_stops_a_run_that_expired_while_the_process_was_gone(self, mock_off):
        cleaning.start(seconds=1, now=datetime.now() - timedelta(hours=3))
        self.assertIsNone(pump_routes.resume_cleaning_if_active())
        mock_off.assert_called()
        self.assertEqual(cleaning.last_result(), cleaning.DONE_COMPLETED)

    @patch.object(pump_routes, "_ensure_watchdog", lambda: None)
    def test_resume_picks_up_a_run_that_still_has_time(self):
        self._reading(14.0)
        cleaning.start(seconds=3600)
        resumed = pump_routes.resume_cleaning_if_active()
        self.assertIsNotNone(resumed)
        self.assertGreater(resumed["remaining_seconds"], 3500)
        cleaning.clear(cleaning.DONE_STOPPED)


class CleaningSafetyCapInteractionTestCase(unittest.TestCase):
    """The MQTT reconcile loop must not cap a cleaning run at five minutes.

    Without this, cleaning mode fails in the least debuggable way possible: the
    pump stops at 5:00 from a background thread in a different process, and
    nothing in Home Assistant or the web UI says why.
    """

    @classmethod
    def setUpClass(cls):
        import mqtt

        cls.mqtt = mqtt

    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        os.unlink(self.path)
        p = patch.object(config, "STATE_FILE", self.path)
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(lambda: os.path.exists(self.path) and os.unlink(self.path))
        self.mqtt._cancel_pump_safety()
        self.addCleanup(self.mqtt._cancel_pump_safety)

    def _pending(self):
        with self.mqtt._pump_timer_lock:
            return self.mqtt._pump_safety_pending_locked()

    def test_the_cap_is_still_armed_for_an_ordinary_run(self):
        # The existing guarantee must survive this feature.
        self.assertTrue(self.mqtt._ensure_pump_safety_armed())
        self.assertTrue(self._pending())

    def test_the_cap_is_not_armed_over_a_cleaning_run(self):
        cleaning.start(seconds=3600)
        self.addCleanup(lambda: cleaning.clear(cleaning.DONE_STOPPED))
        self.assertFalse(self.mqtt._ensure_pump_safety_armed())
        self.assertFalse(self._pending())


if __name__ == "__main__":
    unittest.main()
