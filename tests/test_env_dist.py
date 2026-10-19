""".env-dist documents what config.py reads, and says what each setting does.

Three settings drifted: LOG_FILE looked like the services' log while both of
them name their own file; TELEGRAF_ENABLED read like a switch while nothing
consumes it; and MQTT_DEVICE_MODEL shipped as "gardyn 3.0", a generation the
code has no evidence for on a tower detection cannot place -- a Gardyn Home 1.0
among them.
"""

import importlib
import os
import re
import unittest
from pathlib import Path
from unittest.mock import patch

import config
from app.lib import hardware

ROOT = Path(__file__).resolve().parents[1]
ENV_DIST = (ROOT / ".env-dist").read_text(encoding="utf-8")
CONFIG_SOURCE = (ROOT / "config.py").read_text(encoding="utf-8")


def _documented_keys():
    """Keys .env-dist carries, set or commented out (``#KEY=`` / ``# KEY=``)."""
    return set(re.findall(r"^\s*#?\s*([A-Z][A-Z0-9_]*)=", ENV_DIST, flags=re.MULTILINE))


def _config_keys():
    return set(
        re.findall(r'(?:os\.getenv|_get_int|_get_float|_get_bool)\("([A-Z0-9_]+)"', CONFIG_SOURCE)
    )


class EnvDistCoverageTestCase(unittest.TestCase):
    def test_every_setting_config_reads_is_documented(self):
        missing = _config_keys() - _documented_keys()
        self.assertFalse(missing, f"config.py reads settings .env-dist never mentions: {missing}")

    def test_the_telemetry_interval_is_documented(self):
        self.assertIn("TELEMETRY_INTERVAL_SECONDS", _documented_keys())


class LogFileTestCase(unittest.TestCase):
    def test_both_services_name_their_own_log_file(self):
        """What the LOG_FILE note in .env-dist states: neither service uses it."""
        for entry in ("mqtt.py", "run.py"):
            with self.subTest(entry=entry):
                source = (ROOT / entry).read_text(encoding="utf-8")
                self.assertRegex(source, r'configure_logging\(log_file="[^"]+\.log"\)')


class DeviceModelTestCase(unittest.TestCase):
    def _reload(self, **env):
        for key, value in env.items():
            os.environ[key] = value
        try:
            return importlib.reload(config).MODEL
        finally:
            for key in env:
                os.environ.pop(key, None)
            importlib.reload(config)

    def test_the_default_claims_no_generation(self):
        self.assertEqual(self._reload(), "gardyn")
        self.assertEqual(self._reload(MQTT_DEVICE_MODEL="gardyn 1.0"), "gardyn 1.0")

    def test_env_dist_does_not_set_a_model(self):
        """A fresh install must not announce "gardyn 3.0" to Home Assistant."""
        active = re.findall(r"^MQTT_DEVICE_MODEL=.*$", ENV_DIST, flags=re.MULTILINE)
        self.assertEqual(active, [], "MQTT_DEVICE_MODEL is set, not just documented")

    @patch.object(config, "MODEL_OVERRIDE", None)
    @patch.object(config, "SENSOR_TYPE", None)
    @patch.object(config, "MODEL", "gardyn")
    @patch.object(hardware, "i2c_device_present", return_value=False)
    def test_detection_falls_back_to_the_configured_label(self, _present):
        self.assertEqual(hardware.detect_model(), "gardyn")

    @patch.object(config, "MODEL_OVERRIDE", "gardyn 1.0")
    @patch.object(config, "SENSOR_TYPE", "AM2320")
    @patch.object(hardware, "i2c_device_present", return_value=False)
    def test_the_override_labels_a_1_0_tower_detection_would_call_2_0(self, _present):
        self.assertEqual(hardware.detect_model(), "gardyn 1.0")


if __name__ == "__main__":
    unittest.main()
