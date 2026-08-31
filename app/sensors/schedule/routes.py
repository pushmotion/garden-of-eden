import datetime
import logging

from flask import Blueprint, jsonify, request

from . import schedule as sched

logger = logging.getLogger(__name__)

schedule_blueprint = Blueprint("schedule", __name__)


def _iso(when):
    """Local datetime -> ISO 8601 with a UTC offset, or None."""
    return when.astimezone().isoformat() if when else None


@schedule_blueprint.route("", methods=["GET"])
def get_schedule():
    return jsonify(sched.load_schedule())


@schedule_blueprint.route("", methods=["POST"])
def set_schedule():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify(error="expected a JSON schedule object"), 400
    try:
        applied = sched.apply_schedule(data)
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    except FileNotFoundError:
        # crontab binary missing (e.g. off-Pi) — schedule is still saved.
        return jsonify(error="crontab not available on this host"), 503
    return jsonify(applied)


@schedule_blueprint.route("/state", methods=["GET"])
def get_schedule_state():
    """What the schedule says should be active right now.

    The MQTT service already applies this on connect; exposing it over REST lets
    the bundled web UI show the same "what is running now" view instead of only
    the stored times.
    """
    schedule = sched.load_schedule()
    light = sched.expected_light_state(schedule)
    owed = sched.expected_pump_run(schedule)
    return jsonify(
        {
            "now": datetime.datetime.now().astimezone().isoformat(),
            "vacation": sched.is_vacation_active(schedule),
            "light": None if light is None else {"on": light[0], "brightness": light[1]},
            "pump": {"running": owed is not None, "seconds_remaining": owed},
        }
    )


@schedule_blueprint.route("/next", methods=["GET"])
def get_schedule_next():
    """The next light transition and the next pump run."""
    schedule = sched.load_schedule()
    change = sched.next_light_change(schedule)
    return jsonify(
        {
            "light": (
                None
                if change is None
                else {"at": _iso(change[0]), "on": change[1], "brightness": change[2]}
            ),
            "pump": {"at": _iso(sched.next_pump_run(schedule))},
        }
    )


@schedule_blueprint.route("/validate", methods=["POST"])
def validate_schedule():
    """Compile a schedule without applying it.

    POST /schedule rewrites the live crontab as a side effect, so there was no
    way to check a payload first. Returns the cron lines the schedule would
    install, so a client can diff them against GET /schedule/cron.
    """
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify(error="expected a JSON schedule object"), 400
    try:
        normalized = sched.normalize_schedule(data)
        lines = sched.build_cron_lines(normalized)
    except (ValueError, TypeError) as exc:
        return jsonify(valid=False, error=str(exc)), 400
    return jsonify(valid=True, count=len(lines), cron_lines=lines, schedule=normalized)


@schedule_blueprint.route("/cron", methods=["GET"])
def get_installed_cron():
    """The marked cron lines actually installed, for verification.

    Answers "is what is running the same as what is saved?" without shelling
    into the Pi -- the saved schedule and the installed crontab can diverge if
    the crontab is edited by hand or an apply failed partway.
    """
    lines = sched.installed_cron_lines()
    return jsonify(count=len(lines), cron_lines=lines)


@schedule_blueprint.route("/pump/once", methods=["GET"])
def get_one_time_pump_runs():
    """Pending one-shot pump runs (soonest first)."""
    runs = sched.one_time_pump_runs()
    return jsonify(
        count=len(runs),
        runs=[{"at": _iso(r["at"]), "seconds": r["seconds"]} for r in runs],
    )


@schedule_blueprint.route("/pump/once", methods=["POST"])
def add_one_time_pump_run():
    """Schedule a single pump run at ``time`` ("HH:MM"), today or tomorrow.

    Installs one dated cron entry under its own marker. It does not go through
    apply_schedule(), so adding a one-off cannot disturb the recurring schedule
    -- the whole point of having it. ``duration`` defaults to the shortest run
    already in the schedule, and is capped by MAX_PUMP_RUN_SECONDS either way.
    """
    data = request.get_json(silent=True) or {}
    when = data.get("time")
    if not isinstance(when, str):
        return jsonify(error='expected {"time": "HH:MM"}'), 400
    duration = data.get("duration", sched.default_pump_duration())
    try:
        run = sched.add_one_time_pump_run(when, duration)
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    except FileNotFoundError:
        return jsonify(error="crontab not available on this host"), 503
    return jsonify(at=_iso(run["at"]), seconds=run["seconds"]), 201


@schedule_blueprint.route("/pump/once", methods=["DELETE"])
def clear_one_time_pump_runs():
    """Cancel every pending one-shot pump run."""
    try:
        removed = sched.clear_one_time_pump_runs()
    except FileNotFoundError:
        return jsonify(error="crontab not available on this host"), 503
    return jsonify(removed=removed)
