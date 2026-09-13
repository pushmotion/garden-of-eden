"""Read MQTT's water snapshot without creating a second ultrasonic sensor."""

from datetime import datetime

from flask import Blueprint, jsonify

import config
from app.lib import state as state_lib
from app.lib.water import is_reading_fresh, tank_readings

distance_blueprint = Blueprint("distance", __name__)


@distance_blueprint.route("", methods=["GET"])
@distance_blueprint.route("/measure", methods=["GET"])
def get_distance():
    state = state_lib.load_state()
    checked = state.get("water_checked_at")
    fresh = is_reading_fresh(checked, datetime.now(), config.WATER_READING_MAX_AGE_SECONDS)
    distance = state.get("water_airgap_cm") if fresh else None
    readings = tank_readings(
        distance, config.WATER_FULL_CM, config.WATER_EMPTY_CM, config.TANK_CAPACITY_GALLONS
    )
    return jsonify(
        distance=distance,
        depth=readings["depth_cm"],
        percent=readings["percent"],
        gallons=readings["gallons"],
        checked_at=checked,
        fresh=fresh,
    )
