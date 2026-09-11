from flask import Blueprint, jsonify, request

import config
from app.lib import grow as grow_lib
from app.lib import state as state_lib
from app.lib.water import gallons_from_state

grow_blueprint = Blueprint("grow", __name__)


def _reservoir_gallons():
    """The MQTT service's last filtered reservoir figure, or None if it has none.

    Deliberately does not take a reading of its own: two processes on the
    ultrasonic sensor cross-talk and both come back wrong. A None here makes
    ``nutrient_plan`` fall back to the tank's rated capacity.
    """
    try:
        return gallons_from_state(
            state_lib.load_state(),
            config.WATER_FULL_CM,
            config.WATER_EMPTY_CM,
            config.TANK_CAPACITY_GALLONS,
        )
    except Exception:  # pragma: no cover - a state-file problem must not 500 /grow
        return None


@grow_blueprint.route("", methods=["GET"])
def get_grow():
    state = grow_lib.load_state()
    return jsonify(
        {
            **state,
            "due": grow_lib.due_reminders(state),
            "nutrient_dose": grow_lib.nutrient_dose(state),
            "nutrient_plan": grow_lib.nutrient_plan(state, gallons=_reservoir_gallons()),
        }
    )


@grow_blueprint.route("/start", methods=["POST"])
def start_grow():
    return jsonify(grow_lib.start_cycle())


@grow_blueprint.route("/stage", methods=["POST"])
def set_stage():
    data = request.get_json(silent=True) or {}
    try:
        state = grow_lib.set_stage(grow_lib.load_state(), data.get("stage"))
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    grow_lib.save_state(state)
    return jsonify(state)


@grow_blueprint.route("/acknowledge", methods=["POST"])
def acknowledge():
    data = request.get_json(silent=True) or {}
    key = data.get("key")
    if not key:
        return jsonify(error="missing 'key'"), 400
    state = grow_lib.acknowledge(grow_lib.load_state(), key)
    grow_lib.save_state(state)
    return jsonify(state)
