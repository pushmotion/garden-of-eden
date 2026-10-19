"""The grow, pods and schedule files are edited under a lock, like the state file.

``app.lib.state`` serialises its read-modify-write through ``locking.file_lock``;
the grow-cycle state, the pod list and the schedule (plus the crontab it is
compiled into) were read, edited and written back by the REST API and the MQTT
service from separate processes with no lock at all, so each could silently
erase the other's edit. Every one of them now takes a lock file beside the
JSON it guards -- and never the pump lock, which ``mqtt.on_message`` already
holds when it gets there, so the ordering is pump lock first, these innermost.
"""

import contextlib
import multiprocessing
import os
import tempfile
import unittest
from unittest.mock import patch

import config
from app.lib import grow, locking, pods
from app.sensors.schedule import schedule as sched


def _ack_keys(path, prefix):
    config.GROW_STATE_FILE = path
    for index in range(20):
        grow.acknowledge_reminder(f"{prefix}_{index}")


def _name_pods(path, first, last):
    config.PODS_FILE = path
    for pod_id in range(first, last + 1):
        pods.set_pod(pod_id, name=f"pod {pod_id}")


def _run_two(target, *arg_sets):
    processes = [multiprocessing.Process(target=target, args=args) for args in arg_sets]
    for process in processes:
        process.start()
    for process in processes:
        process.join(20)
        if process.is_alive():
            process.terminate()
            process.join()
            raise AssertionError("writer deadlocked")
        if process.exitcode != 0:
            raise AssertionError(f"writer exited {process.exitcode}")


class TwoProcessTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def test_two_processes_preserve_each_others_grow_acknowledgements(self):
        path = os.path.join(self.temp.name, "grow.json")
        with patch.object(config, "GROW_STATE_FILE", path):
            _run_two(_ack_keys, (path, "a"), (path, "b"))
            acked = set(grow.load_state()["acknowledged"])
        for prefix in ("a", "b"):
            for index in range(20):
                self.assertIn(f"{prefix}_{index}", acked)

    def test_two_processes_preserve_each_others_pod_names(self):
        path = os.path.join(self.temp.name, "pods.json")
        with patch.object(config, "PODS_FILE", path), patch.object(config, "POD_COUNT", 30):
            _run_two(_name_pods, (path, 1, 15), (path, 16, 30))
            names = [pod["name"] for pod in pods.load_pods()]
        self.assertEqual(names, [f"pod {pod_id}" for pod_id in range(1, 31)])


class _LockRecorder:
    """Stands in for ``file_lock``: records every path taken, in order."""

    def __init__(self):
        self.paths = []

    @contextlib.contextmanager
    def __call__(self, path):
        self.paths.append(os.path.abspath(path))
        with locking.file_lock(path):
            yield


class LockDisciplineTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        for name in ("GROW_STATE_FILE", "PODS_FILE", "SCHEDULE_FILE", "STATE_FILE"):
            patcher = patch.object(config, name, os.path.join(self.temp.name, name + ".json"))
            patcher.start()
            self.addCleanup(patcher.stop)
        for name, stub in (("_read_crontab", lambda: []), ("_write_crontab", lambda lines: None)):
            patcher = patch.object(sched, name, stub)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _held(self, lock_path):
        return os.path.abspath(lock_path) in getattr(locking._held, "paths", {})

    def test_schedule_apply_holds_its_lock_across_the_crontab_rewrite(self):
        lock_path = config.SCHEDULE_FILE + ".lock"
        seen = []
        with patch.object(
            sched, "_write_crontab", lambda lines: seen.append(self._held(lock_path))
        ):
            sched.apply_schedule(sched.DEFAULT_SCHEDULE)
            sched.add_one_time_pump_run("12:00", 3)
            sched.refresh()
        self.assertTrue(seen, "nothing wrote the crontab")
        self.assertTrue(all(seen), "the crontab was rewritten without the schedule lock")

    def test_mqtt_schedule_setters_hold_the_lock_from_load_to_apply(self):
        """The window the lock closes: between the service's load and its apply."""
        import mqtt

        lock_path = config.SCHEDULE_FILE + ".lock"
        seen = []
        original = sched.load_schedule

        def observed_load():
            seen.append(self._held(lock_path))
            return original()

        with patch.object(sched, "load_schedule", observed_load):
            for setter, args in (
                (mqtt._set_schedule_flag, ("lights", True)),
                (mqtt._set_light_brightness, (70,)),
                (mqtt._set_light_first_window, ()),
                (mqtt._set_pump_duration, (3,)),
                (mqtt._set_pump_first_time, ("06:00",)),
            ):
                seen.clear()
                setter(_NullClient(), *args)
                self.assertTrue(seen and seen[0], f"{setter.__name__} loaded the schedule unlocked")

    def test_the_new_locks_never_take_the_pump_lock(self):
        """Lock order is pump lock first, these innermost -- never the reverse."""
        pump_lock = os.path.abspath(config.STATE_FILE + ".pump.lock")
        for module, call in (
            (grow, lambda: grow.acknowledge_reminder("thinning")),
            (grow, lambda: grow.change_stage("harvest")),
            (grow, lambda: grow.start_cycle()),
            (pods, lambda: pods.set_pod(1, name="Basil")),
            (sched, lambda: sched.apply_schedule(sched.DEFAULT_SCHEDULE)),
            (sched, lambda: sched.add_one_time_pump_run("12:00", 3)),
        ):
            recorder = _LockRecorder()
            with patch.object(module, "file_lock", recorder):
                call()
            self.assertTrue(recorder.paths, f"{module.__name__} took no lock")
            self.assertNotIn(pump_lock, recorder.paths)
            self.assertEqual(len(set(recorder.paths)), 1, "one lock file per store")


class _NullClient:
    def publish(self, *args, **kwargs):
        pass


if __name__ == "__main__":
    unittest.main()
