import json
import os
import tempfile
import unittest
from unittest.mock import patch

import config
from app.lib import state


class ActuatorStateTestCase(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        os.unlink(self.path)  # start absent
        self._patch = patch.object(config, "STATE_FILE", self.path)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        if os.path.exists(self.path):
            os.unlink(self.path)

    def test_defaults_when_absent(self):
        s = state.load_state()
        self.assertFalse(s["light_on"])
        self.assertFalse(s["pump_on"])

    def test_save_and_reload_roundtrip(self):
        state.save_state(light_on=True, brightness=42)
        s = state.load_state()
        self.assertTrue(s["light_on"])
        self.assertEqual(s["brightness"], 42)
        # File is valid JSON on disk.
        with open(self.path) as fh:
            self.assertEqual(json.load(fh)["brightness"], 42)

    def test_partial_update_preserves_other_fields(self):
        state.save_state(pump_on=True, speed=80)
        state.save_state(light_on=True)
        s = state.load_state()
        self.assertTrue(s["pump_on"])
        self.assertEqual(s["speed"], 80)
        self.assertTrue(s["light_on"])


class StateRoundTripTestCase(unittest.TestCase):
    """Keys outside DEFAULT_STATE must survive a save/load round trip.

    load_state() used to keep only the four actuator fields, so anything else a
    caller persisted was written and then silently discarded on the next read.
    That is why the One-Time Pump Run time reset to 12:00 on every restart, and
    it would have swallowed the water verdict bin/water.sh depends on.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._orig = config.STATE_FILE
        config.STATE_FILE = os.path.join(self.tmp, "state.json")
        self.addCleanup(setattr, config, "STATE_FILE", self._orig)

    def test_extra_keys_survive_a_round_trip(self):
        state.save_state(manual_pump_time="06:30", pump_blocked=True)
        loaded = state.load_state()
        self.assertEqual(loaded["manual_pump_time"], "06:30")
        self.assertTrue(loaded["pump_blocked"])

    def test_defaults_still_fill_missing_keys(self):
        state.save_state(manual_pump_time="06:30")
        loaded = state.load_state()
        for key, value in state.DEFAULT_STATE.items():
            self.assertEqual(loaded[key], value, f"{key} should fall back to its default")

    def test_a_non_object_state_file_falls_back_to_defaults(self):
        with open(config.STATE_FILE, "w") as fh:
            fh.write("[1, 2, 3]")
        self.assertEqual(state.load_state(), dict(state.DEFAULT_STATE))


if __name__ == "__main__":
    unittest.main()
