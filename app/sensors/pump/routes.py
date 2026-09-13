import logging
import threading

from flask import Blueprint, jsonify, request

import config
from app.lib import cleaning as cleaning_lib
from app.lib import state as state_lib
from app.lib.hardware import get_pin_factory
from app.lib.lib import check_sensor_guard, parse_level
from app.lib.locking import pump_locked
from app.lib.water_guard import pump_allowed

from .pump import Pump as PumpControl
from .pump_power import fetch_ina219_data

logger = logging.getLogger(__name__)

pump_blueprint = Blueprint("pump", __name__)

try:
    pump_control = PumpControl(
        pin=config.PUMP_PIN,
        frequency=config.PUMP_FREQUENCY,
        pin_factory=get_pin_factory(),
    )
except Exception as exc:
    logger.error("Failed to initialize Pump: %s", exc)
    pump_control = None

check_sensor = check_sensor_guard(sensor=pump_control, sensor_name="Pump")

# Tracks the pending auto-off timer so repeated calls don't stack and so the
# pump is *always* armed with a safety shut-off. Whenever the pump is energized
# (on/run/speed>0) we (re)arm a timer; turning it off cancels it.
_run_timer = None
_run_lock = threading.Lock()


@pump_locked
def _safety_off():
    """Stop the pump and record it off, so persisted state stays accurate."""
    if cleaning_lib.is_active():
        return
    pump_control.off()
    state_lib.save_state(pump_on=False)


def _arm_auto_off(seconds):
    """(Re)arm the single auto-off timer to stop the pump after ``seconds``."""
    global _run_timer
    with _run_lock:
        if _run_timer is not None:
            _run_timer.cancel()  # supersede any in-flight run
        _run_timer = threading.Timer(seconds, _safety_off)
        _run_timer.daemon = True
        _run_timer.start()


def _cancel_auto_off():
    global _run_timer
    with _run_lock:
        if _run_timer is not None:
            _run_timer.cancel()
            _run_timer = None


# ---------------------------------------------------------------------------
# Cleaning mode
#
# A cleaning run is the one path allowed past MAX_PUMP_RUN_SECONDS, so it brings
# its own enforcement rather than borrowing the timer above. The watchdog below
# is deliberately a loop and not a single long Timer: a two-hour Timer would fire
# exactly once, at the end, and learn nothing about the tank in between.
#
# These helpers are module-level rather than inline in the routes because
# ``mqtt.py`` drives the same cleaning run from Home Assistant and must not grow
# a second, subtly different copy of this logic. It already imports
# ``pump_control`` from here; it calls ``start_cleaning``/``stop_cleaning`` too.
# ---------------------------------------------------------------------------
_clean_stop = threading.Event()
_clean_thread = None
_clean_thread_lock = threading.Lock()
_clean_session_id = None


class CleaningRefused(RuntimeError):
    """A cleaning run cannot start or continue (low water, stale reading)."""


# The MQTT service enforces the same deadline on its reconcile loop, so two
# watchdogs can race to end one run. The loser must exit quietly rather than
# call stop_cleaning() again: a second call would overwrite the winner's reason,
# and "completed" turning into "stopped" is exactly the detail somebody reads
# the sensor to find out.
_ENDED_ELSEWHERE = "ended elsewhere"


def _cleaning_tick_reason(expected_id=None):
    """Why the current cleaning run must stop now, or ``None`` to keep going.

    One state-file read serves both checks, so the tank verdict costs nothing
    beyond the deadline check that has to happen anyway.
    """
    state = state_lib.load_state()
    actual_id = state.get(cleaning_lib.ID_KEY) or state.get(cleaning_lib.UNTIL_KEY)
    if expected_id and actual_id and actual_id != expected_id:
        return _ENDED_ELSEWHERE
    if not state.get(cleaning_lib.UNTIL_KEY):
        return (
            _ENDED_ELSEWHERE if state.get(cleaning_lib.REASON_KEY) else cleaning_lib.DONE_NO_READING
        )
    if cleaning_lib.expired(state=state):
        return cleaning_lib.DONE_COMPLETED

    ok, reason = cleaning_lib.water_verdict(state=state)
    if not ok:
        logger.warning("Cleaning run stopping: %s", reason)
        return cleaning_lib.DONE_NO_READING if "reading" in reason else cleaning_lib.DONE_LOW_WATER
    return None


def _clean_watchdog(stop_event, session_id):
    """Hold the pump on for the run, and end it the moment it should end."""
    while not stop_event.wait(max(1, config.CLEANING_TICK_SECONDS)):
        try:
            reason = _cleaning_tick_reason(session_id)
        except Exception:
            # Never let a watchdog crash leave the pump running. An unreadable
            # state file is exactly the situation where the deadline can no
            # longer be trusted, so end the run rather than assume it is fine.
            logger.exception("Cleaning watchdog failed; stopping the run")
            reason = cleaning_lib.DONE_NO_READING
        if reason is _ENDED_ELSEWHERE:
            return
        if reason is not None:
            stop_cleaning(reason, expected_id=session_id)
            return


def _ensure_watchdog():
    global _clean_thread, _clean_stop, _clean_session_id
    active = cleaning_lib.session()
    if active is None:
        return
    with _clean_thread_lock:
        if (
            _clean_thread is not None
            and _clean_thread.is_alive()
            and _clean_session_id == active["id"]
        ):
            return
        _clean_stop.set()
        _clean_stop = threading.Event()
        _clean_session_id = active["id"]
        _clean_thread = threading.Thread(
            target=_clean_watchdog,
            args=(_clean_stop, active["id"]),
            name="cleaning-watchdog",
            daemon=True,
        )
        _clean_thread.start()


@pump_locked
def start_cleaning(seconds=None, speed=None):
    """Begin a cleaning run. Raises ``ValueError``/``CleaningRefused``.

    The tank is checked *before* the pump is energized and then on every tick;
    the pre-check is what makes a refusal cheap, and the ticks are what make a
    two-hour run safe.
    """
    if pump_control is None:
        raise CleaningRefused("pump unavailable")
    if cleaning_lib.is_active():
        raise CleaningRefused("A cleaning run is already active; stop it first")

    seconds = cleaning_lib.validate_duration(seconds)
    ok, reason = cleaning_lib.water_verdict()
    if not ok:
        raise CleaningRefused(reason)

    session = cleaning_lib.start(seconds=seconds, speed=speed)

    # The five-minute cap belongs to ordinary runs. Leaving it armed would stop
    # the clean at 5:00 with no explanation anywhere.
    _cancel_auto_off()
    try:
        pump_control.on()
        pump_control.set_speed(session["speed"])
        state_lib.save_state(strict=True, pump_on=True)
    except Exception:
        stop_cleaning(cleaning_lib.DONE_STOPPED)
        raise
    # Deliberately not persisting `speed=`: that field is the *normal* pump
    # speed the MQTT toggle restores, and overwriting it with the cleaning duty
    # cycle would silently re-tune every future watering run.
    _ensure_watchdog()
    logger.info("Cleaning run under way for %ss at %s%%", seconds, session["speed"])
    return session


@pump_locked
def stop_cleaning(reason=cleaning_lib.DONE_STOPPED, expected_id=None):
    """End a cleaning run and stop the pump. Safe to call when none is running."""
    state = state_lib.load_state()
    actual_id = state.get(cleaning_lib.ID_KEY) or state.get(cleaning_lib.UNTIL_KEY)
    if expected_id and actual_id and actual_id != expected_id:
        return
    if expected_id and not actual_id and state.get(cleaning_lib.REASON_KEY):
        return
    _clean_stop.set()
    _cancel_auto_off()
    try:
        if pump_control is not None:
            pump_control.off()
            state_lib.save_state(pump_on=False)
    finally:
        # Clear the session even if stopping the pump failed. A session that
        # outlives a failed stop would keep every safety cap suppressed, which
        # is strictly worse than a pump that needs stopping by hand.
        cleaning_lib.clear(reason)
        if actual_id:
            restore_scheduled_lighting()


def restore_scheduled_lighting():
    """Resume today's lighting, leaving manual lighting alone if unscheduled."""
    from app.lib.light_schedule import restore
    from app.sensors.light.routes import light_control

    restore(light_control)


@pump_locked
def resume_cleaning_if_active():
    """Re-arm enforcement for a run that outlived the process that started it.

    Called on startup. A restart mid-run leaves a persisted deadline and, quite
    possibly, a pump still spinning -- with every in-memory timer gone. Either
    the run is picked back up under a fresh watchdog, or it is already past its
    deadline and gets stopped here.
    """
    session = cleaning_lib.session()
    if session is None:
        if cleaning_lib.expired():
            logger.warning("Found an expired cleaning run at startup; stopping the pump")
            stop_cleaning(cleaning_lib.DONE_COMPLETED)
        return None
    ok, reason = cleaning_lib.water_verdict()
    if not ok:
        logger.warning("Not resuming cleaning: %s", reason)
        stop_cleaning(
            cleaning_lib.DONE_NO_READING if "reading" in reason else cleaning_lib.DONE_LOW_WATER
        )
        return None
    logger.info("Resuming a cleaning run with %ss left", session["remaining_seconds"])
    _ensure_watchdog()
    return session


def cleaning_active():
    return cleaning_lib.is_active()


def _busy_cleaning():
    """A 409 for the ordinary pump paths while a cleaning run owns the pump.

    Without this, any of them would re-arm the five-minute cap and cut the clean
    short -- the exact failure this whole feature exists to avoid. Refusing is
    also the honest answer: the pump is already running, for something else.
    """
    remaining = cleaning_lib.remaining_seconds()
    return (
        jsonify(
            message=(
                "A cleaning run is in progress "
                f"({remaining}s left). Stop it first, or POST /pump/off."
            ),
            cleaning=cleaning_lib.status(),
        ),
        409,
    )


@pump_blueprint.route("/on", methods=["POST"])
@check_sensor
@pump_locked
def turn_on():
    if cleaning_lib.is_active():
        return _busy_cleaning()
    allowed, reason = pump_allowed()
    if not allowed:
        return jsonify(message=reason), 409
    pump_control.on()
    # Safety: never leave the pump running longer than the hard cap, even if
    # nobody calls /off.
    _arm_auto_off(config.MAX_PUMP_RUN_SECONDS)
    state_lib.save_state(pump_on=True)
    return jsonify(message="Pump turned on!"), 200


@pump_blueprint.route("/off", methods=["POST"])
@check_sensor
@pump_locked
def turn_off():
    # "Off" is never refused. It is the one instruction that is safe from any
    # source at any time, so it ends a cleaning run rather than colliding with
    # one -- and clearing the session is what stops the watchdog re-energizing
    # the pump on its next tick.
    if state_lib.load_state().get(cleaning_lib.UNTIL_KEY):
        stop_cleaning(cleaning_lib.DONE_STOPPED)
        return jsonify(message="Cleaning run stopped; pump turned off!"), 200
    _cancel_auto_off()
    pump_control.off()
    state_lib.save_state(pump_on=False)
    return jsonify(message="Pump turned off!"), 200


@pump_blueprint.route("/speed", methods=["POST"])
@check_sensor
@pump_locked
def adjust_speed():
    data = request.get_json(silent=True) or {}
    speed_value = parse_level(data, default=config.DEFAULT_PUMP_SPEED)
    if speed_value == 0 and state_lib.load_state().get(cleaning_lib.UNTIL_KEY):
        stop_cleaning()
        return jsonify(message="Cleaning run stopped; pump turned off!"), 200
    # A zero speed is a stop, and stops are always allowed; anything else would
    # re-arm the five-minute cap over a run that is entitled to hours.
    if cleaning_lib.is_active():
        if speed_value > 0:
            return _busy_cleaning()
        # Speed 0 is /off by another name, so it ends the run outright. Merely
        # idling the pump would leave the session holding the pump hostage --
        # blocking every other path for the rest of its two hours over a run
        # that is no longer happening.
        stop_cleaning(cleaning_lib.DONE_STOPPED)
        return jsonify(message="Cleaning run stopped; pump turned off!"), 200
    allowed, reason = pump_allowed()
    if speed_value > 0 and not allowed:
        return jsonify(message=reason), 409
    pump_control.set_speed(speed_value)
    # Setting a non-zero speed energizes the pump, so arm the safety shut-off too.
    if speed_value > 0:
        _arm_auto_off(config.MAX_PUMP_RUN_SECONDS)
    else:
        _cancel_auto_off()
    state_lib.save_state(pump_on=speed_value > 0, speed=speed_value)
    return jsonify(message=f"Pump adjusted to {speed_value}% speed!"), 200


@pump_blueprint.route("/speed", methods=["GET"])
@check_sensor
def get_speed():
    current_speed = pump_control.get_speed()
    return jsonify(value=current_speed), 200


@pump_blueprint.route("/run", methods=["POST"])
@check_sensor
@pump_locked
def run_for():
    """Run the pump for a fixed number of seconds, then stop. Non-blocking:
    schedules the stop on a background timer and returns immediately."""
    if cleaning_lib.is_active():
        return _busy_cleaning()
    data = request.get_json(silent=True) or {}
    try:
        seconds = int(data.get("seconds", config.MAX_PUMP_RUN_SECONDS))
    except (TypeError, ValueError):
        return jsonify(message="seconds must be an integer"), 400
    if not (1 <= seconds <= config.MAX_PUMP_RUN_SECONDS):
        return (
            jsonify(message=f"seconds must be between 1 and {config.MAX_PUMP_RUN_SECONDS}"),
            400,
        )

    allowed, reason = pump_allowed()
    if not allowed:
        return jsonify(message=reason), 409
    pump_control.on()
    _arm_auto_off(seconds)
    return jsonify(message=f"Pump running for {seconds}s"), 200


@pump_blueprint.route("/clean", methods=["GET"])
def get_cleaning():
    """Cleaning status. Deliberately not behind ``check_sensor``: a tower whose
    pump failed to initialize should still be able to report that no cleaning
    run is happening, rather than returning 400 to a status poll."""
    return jsonify(cleaning_lib.status()), 200


@pump_blueprint.route("/clean", methods=["POST"])
@check_sensor
def start_cleaning_run():
    """Start a cleaning run: hours, not minutes, under its own safety budget.

    ``seconds`` defaults to ``CLEANING_DEFAULT_SECONDS`` and is capped by
    ``CLEANING_MAX_SECONDS`` -- a separate budget from ``MAX_PUMP_RUN_SECONDS``,
    which every ordinary path still obeys.
    """
    if cleaning_lib.is_active():
        return _busy_cleaning()

    data = request.get_json(silent=True) or {}
    seconds = data.get("seconds")
    if seconds is None and data.get("minutes") is not None:
        # Every surface that a person actually touches talks in minutes; only
        # the wire talks in seconds.
        try:
            seconds = int(data["minutes"]) * 60
        except (TypeError, ValueError):
            return jsonify(message="minutes must be an integer"), 400

    try:
        session = start_cleaning(seconds=seconds, speed=data.get("speed"))
    except ValueError as exc:
        return jsonify(message=str(exc)), 400
    except CleaningRefused as exc:
        # 409, not 400: the request was well-formed, the tower just will not do
        # it right now. The reason is the useful part -- it says which threshold
        # refused and what the tank actually read.
        return jsonify(message=str(exc), cleaning=cleaning_lib.status()), 409

    return (
        jsonify(
            message=f"Cleaning run started for {session['seconds']}s",
            cleaning=cleaning_lib.status(),
        ),
        200,
    )


@pump_blueprint.route("/clean/stop", methods=["POST"])
@check_sensor
@pump_locked
def stop_cleaning_run():
    if not state_lib.load_state().get(cleaning_lib.UNTIL_KEY):
        return jsonify(message="No cleaning run in progress", cleaning=cleaning_lib.status()), 200
    stop_cleaning(cleaning_lib.DONE_STOPPED)
    return jsonify(message="Cleaning run stopped", cleaning=cleaning_lib.status()), 200


@pump_blueprint.route("/stats", methods=["GET"])
@check_sensor
def get_pump_data():
    data = fetch_ina219_data()
    return jsonify(data)
