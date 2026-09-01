from flask import Blueprint, jsonify

import config
from app.lib import water
from app.lib.hardware import detect_model

system_blueprint = Blueprint("system", __name__)


def _profile_for(model):
    """Resolve a hardware profile for a model string. Falls back to a prefix
    match so custom/suffixed names (e.g. 'gardyn 3.0 (simulated)') still map to
    the closest known profile instead of returning empty."""
    if model in config.MODELS:
        return config.MODELS[model]
    for key, profile in config.MODELS.items():
        if model and (model.startswith(key) or key in model):
            return profile
    return {}


@system_blueprint.route("", methods=["GET"])
def get_system():
    """Report identity, version, and the detected hardware model/profile."""
    model = detect_model()
    return jsonify(
        {
            "identifier": config.IDENTIFIER,
            "version": config.VERSION,
            "model": model,
            "profile": _profile_for(model),
            "sensor_type": config.SENSOR_TYPE,
            # Presentation default for clients that have no preference of their
            # own. Everything below is metric regardless -- see config.py.
            "display_units": config.DISPLAY_UNITS,
            "water_low_cm": config.WATER_LOW_CM,
            "pump_cutoff_cm": water.pump_cutoff(config.PUMP_CUTOFF_CM, config.WATER_LOW_CM),
            # Tank geometry, so clients can label a reading without holding
            # their own copy of the calibration. See app/lib/water.py.
            "water_full_cm": config.WATER_FULL_CM,
            "water_empty_cm": config.WATER_EMPTY_CM,
            "tank_capacity_gallons": config.TANK_CAPACITY_GALLONS,
            "pump_max_run_seconds": config.MAX_PUMP_RUN_SECONDS,
        }
    )
