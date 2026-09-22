#!/usr/bin/env python3
"""Validate tower identity before setup starts services. Never drives hardware."""

import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import config


def errors():
    problems = []
    identifier = os.getenv("MQTT_IDENTIFIER", "")
    if not re.fullmatch(r"[a-z][a-z0-9_]*", identifier):
        problems.append("Set MQTT_IDENTIFIER explicitly to a unique lowercase tower identifier")
    if config.BASE_TOPIC != identifier:
        problems.append(
            "MQTT_BASETOPIC must match MQTT_IDENTIFIER for fleet provisioning; remove the override"
        )
    if config.BROKER in ("", "localhost", "127.0.0.1"):
        problems.append("Set MQTT_BROKER to the shared broker for this fleet")
    if config.SENSOR_TYPE not in ("AM2320", "DHT20"):
        problems.append("Set SENSOR_TYPE from the installed sensor, not the product name")
    if not (0 < config.WATER_FULL_CM < config.WATER_EMPTY_CM):
        problems.append("Water calibration requires 0 < WATER_FULL_CM < WATER_EMPTY_CM")
    return problems


if __name__ == "__main__":
    problems = errors()
    for problem in problems:
        print(problem)
    sys.exit(1 if problems else 0)
