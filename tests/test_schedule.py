import datetime
import unittest

from app.sensors.schedule import schedule as sched


class BuildCronLinesTestCase(unittest.TestCase):
    def test_lights_per_day_emits_on_and_off_with_dow(self):
        s = {
            "lights": {
                "enabled": True,
                "days": {"mon": [{"onTime": "08:30", "offTime": "22:15", "brightness": 60}]},
            },
            "pump": {"enabled": False},
        }
        lines = sched.build_cron_lines(s)
        self.assertEqual(len(lines), 2)
        # Monday -> cron day-of-week 1; light.sh takes positional args.
        self.assertIn("30 8 * * 1 /usr/local/bin/light 60", lines[0])
        self.assertIn("15 22 * * 1 /usr/local/bin/light off", lines[1])
        self.assertTrue(all(sched.CRON_MARKER in ln for ln in lines))

    def test_light_ramp_emits_ramp_commands(self):
        s = {
            "lights": {
                "enabled": True,
                "days": {
                    "mon": [
                        {
                            "onTime": "06:00",
                            "offTime": "22:00",
                            "brightness": 70,
                            "rampMinutes": 30,
                        }
                    ]
                },
            }
        }
        lines = sched.build_cron_lines(s)
        self.assertEqual(len(lines), 2)
        self.assertIn("0 6 * * 1 /usr/local/bin/light ramp 70 30", lines[0])
        self.assertIn("0 22 * * 1 /usr/local/bin/light ramp 0 30", lines[1])

    def test_multiple_entries_per_day(self):
        s = {
            "lights": {
                "enabled": True,
                "days": {
                    "fri": [
                        {"onTime": "06:00", "offTime": "09:00", "brightness": 35},
                        {"onTime": "18:00", "offTime": "22:00", "brightness": 70},
                    ]
                },
            }
        }
        lines = sched.build_cron_lines(s)
        self.assertEqual(len(lines), 4)  # two windows -> two on + two off
        self.assertTrue(all(" * * 5 " in ln for ln in lines))  # all on Friday

    def test_legacy_single_window_migrates_to_every_day(self):
        s = {
            "lights": {"enabled": True, "onTime": "08:00", "offTime": "22:00", "brightness": 70},
            "pump": {"enabled": False, "runs": []},
        }
        lines = sched.build_cron_lines(s)
        self.assertEqual(len(lines), 14)  # 7 days x (on + off)
        dows = {ln.split()[4] for ln in lines}
        self.assertEqual(dows, {"0", "1", "2", "3", "4", "5", "6"})

    def test_pump_runs_convert_minutes_to_seconds_with_dow(self):
        s = {"pump": {"enabled": True, "days": {"tue": [{"time": "06:30", "duration": 3}]}}}
        lines = sched.build_cron_lines(s)
        self.assertEqual(len(lines), 1)
        self.assertIn("30 6 * * 2 /usr/local/bin/water 180", lines[0])

    def test_pump_duration_clamped_to_safety_cap(self):
        # 10 minutes requested, but the hard cap is 5 minutes (300s).
        s = {"pump": {"enabled": True, "days": {"wed": [{"time": "12:00", "duration": 10}]}}}
        lines = sched.build_cron_lines(s)
        self.assertEqual(len(lines), 1)
        self.assertIn(f"/usr/local/bin/water {sched.config.MAX_PUMP_RUN_SECONDS} ", lines[0])
        self.assertNotIn("water 600", lines[0])

    def test_disabled_emits_nothing(self):
        self.assertEqual(sched.build_cron_lines(sched.DEFAULT_SCHEDULE), [])

    def test_invalid_time_raises(self):
        s = {
            "lights": {"enabled": True, "days": {"mon": [{"onTime": "99:99", "offTime": "22:00"}]}}
        }
        with self.assertRaises(ValueError):
            sched.build_cron_lines(s)


class VacationModeTestCase(unittest.TestCase):
    def test_is_vacation_active_respects_until(self):
        base = {"vacation": {"enabled": True, "until": "2026-06-30"}}
        self.assertTrue(sched.is_vacation_active(base, today=datetime.date(2026, 6, 28)))
        self.assertFalse(sched.is_vacation_active(base, today=datetime.date(2026, 7, 1)))
        self.assertFalse(sched.is_vacation_active({"vacation": {"enabled": False}}))
        # Enabled with no end date stays active.
        self.assertTrue(sched.is_vacation_active({"vacation": {"enabled": True}}))

    def test_active_overrides_with_reduced_profile_and_refresh(self):
        s = {
            "lights": {
                "enabled": True,
                "days": {"mon": [{"onTime": "08:00", "offTime": "22:00", "brightness": 70}]},
            },
            "pump": {"enabled": False},
            "vacation": {"enabled": True, "until": "2999-12-31"},
        }
        lines = sched.build_cron_lines(s)
        joined = "\n".join(lines)
        self.assertIn("/usr/local/bin/light 50", joined)  # reduced brightness
        self.assertNotIn("/usr/local/bin/light 70", joined)  # normal overridden
        self.assertTrue(any("schedule-refresh.sh" in ln for ln in lines))  # nightly expiry

    def test_expired_falls_back_to_normal(self):
        s = {
            "lights": {
                "enabled": True,
                "days": {"mon": [{"onTime": "08:00", "offTime": "22:00", "brightness": 70}]},
            },
            "vacation": {"enabled": True, "until": "2000-01-01"},
        }
        lines = sched.build_cron_lines(s)
        joined = "\n".join(lines)
        self.assertIn("/usr/local/bin/light 70", joined)
        self.assertFalse(any("schedule-refresh.sh" in ln for ln in lines))


class NormalizeScheduleTestCase(unittest.TestCase):
    def test_legacy_pump_runs_apply_to_all_days(self):
        s = {"pump": {"enabled": True, "runs": [{"time": "12:00", "duration": 5}]}}
        norm = sched.normalize_schedule(s)
        self.assertEqual(set(norm["pump"]["days"]), set(sched.DAYS))
        for day in sched.DAYS:
            self.assertEqual(norm["pump"]["days"][day], [{"time": "12:00", "duration": 5}])

    def test_fills_missing_days(self):
        norm = sched.normalize_schedule({"lights": {"enabled": True, "days": {"mon": []}}})
        self.assertEqual(set(norm["lights"]["days"]), set(sched.DAYS))
        self.assertEqual(norm["lights"]["days"]["sun"], [])


if __name__ == "__main__":
    unittest.main()


class ExpectedStateTestCase(unittest.TestCase):
    """What the schedule says should be running right now.

    Used on startup to recover from a power cut mid-photoperiod: cron drives the
    actuators through the CLIs, which never update the persisted actuator state,
    so without this the tower stays dark until the next scheduled on-time.
    """

    # 2026-08-31 is a Monday.
    def _at(self, hour, minute=0, day=31):
        return datetime.datetime(2026, 8, day, hour, minute)

    def _daily(self, windows=None, runs=None, lights=True, pump=True):
        return {
            "lights": {"enabled": lights, "days": {d: list(windows or []) for d in sched.DAYS}},
            "pump": {"enabled": pump, "days": {d: list(runs or []) for d in sched.DAYS}},
        }

    def test_inside_window_is_on_at_that_brightness(self):
        s = self._daily([{"onTime": "05:00", "offTime": "21:00", "brightness": 65}])
        self.assertEqual(sched.expected_light_state(s, now=self._at(9)), (True, 65))

    def test_after_off_time_is_off(self):
        s = self._daily([{"onTime": "05:00", "offTime": "21:00", "brightness": 65}])
        self.assertEqual(sched.expected_light_state(s, now=self._at(22)), (False, 0))

    def test_before_first_on_time_uses_yesterdays_off(self):
        s = self._daily([{"onTime": "05:00", "offTime": "21:00", "brightness": 65}])
        self.assertEqual(sched.expected_light_state(s, now=self._at(3)), (False, 0))

    def test_window_spanning_midnight_follows_cron_semantics(self):
        # Compiled as "on 23:00" and "off 07:00" on the same weekday, so at 02:00
        # the most recent event is the previous day's 23:00 on.
        s = self._daily([{"onTime": "23:00", "offTime": "07:00", "brightness": 80}])
        self.assertEqual(sched.expected_light_state(s, now=self._at(2)), (True, 80))
        self.assertEqual(sched.expected_light_state(s, now=self._at(8)), (False, 0))

    def test_latest_of_several_windows_wins(self):
        s = self._daily(
            [
                {"onTime": "05:00", "offTime": "09:00", "brightness": 40},
                {"onTime": "17:00", "offTime": "21:00", "brightness": 90},
            ]
        )
        self.assertEqual(sched.expected_light_state(s, now=self._at(18)), (True, 90))
        self.assertEqual(sched.expected_light_state(s, now=self._at(12)), (False, 0))

    def test_disabled_or_empty_expresses_no_opinion(self):
        self.assertIsNone(sched.expected_light_state(self._daily(lights=False), now=self._at(9)))
        self.assertIsNone(sched.expected_light_state(self._daily([]), now=self._at(9)))

    def test_vacation_profile_overrides(self):
        s = self._daily([{"onTime": "05:00", "offTime": "21:00", "brightness": 65}])
        s["vacation"] = {"enabled": True, "until": None}
        # Vacation keeps lights 10:00-16:00 at 50%.
        self.assertEqual(sched.expected_light_state(s, now=self._at(12)), (True, 50))
        self.assertEqual(sched.expected_light_state(s, now=self._at(9)), (False, 0))

    def test_pump_run_in_progress_reports_remaining_seconds(self):
        s = self._daily(runs=[{"time": "09:00", "duration": 3}])
        self.assertEqual(sched.expected_pump_run(s, now=self._at(9, 1)), 120)

    def test_pump_run_outside_window_is_none(self):
        s = self._daily(runs=[{"time": "09:00", "duration": 3}])
        self.assertIsNone(sched.expected_pump_run(s, now=self._at(9, 5)))
        self.assertIsNone(sched.expected_pump_run(s, now=self._at(8, 59)))

    def test_pump_disabled_is_none(self):
        s = self._daily(runs=[{"time": "09:00", "duration": 3}], pump=False)
        self.assertIsNone(sched.expected_pump_run(s, now=self._at(9, 1)))
