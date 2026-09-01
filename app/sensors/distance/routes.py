import logging

from flask import Blueprint, jsonify

import config
from app.lib.hardware import get_pin_factory
from app.lib.lib import check_sensor_guard
from app.lib.water import tank_readings

from .distance import Distance as DistanceControl

logger = logging.getLogger(__name__)

distance_blueprint = Blueprint("distance", __name__)

try:
    distance_control = DistanceControl(pin_factory=get_pin_factory())
except Exception as exc:
    logger.error("Failed to initialize Distance: %s", exc)
    distance_control = None

check_sensor = check_sensor_guard(sensor=distance_control, sensor_name="Distance")


@distance_blueprint.route("", methods=["GET"])
@distance_blueprint.route("/measure", methods=["GET"])
@check_sensor
def get_distance():
    """The raw airgap plus every figure derived from it.

    Depth, percent and gallons come from the same ``tank_readings`` the MQTT
    service publishes, so the web UI can render them instead of re-deriving the
    tank geometry in JavaScript -- which is how the page came to disagree with
    Home Assistant on any tower that had actually been calibrated.
    """
    distance_value = distance_control.measure_once()
    readings = tank_readings(
        distance_value,
        config.WATER_FULL_CM,
        config.WATER_EMPTY_CM,
        config.TANK_CAPACITY_GALLONS,
    )
    return (
        jsonify(
            distance=distance_value,
            depth=readings["depth_cm"],
            percent=readings["percent"],
            gallons=readings["gallons"],
        ),
        200,
    )
