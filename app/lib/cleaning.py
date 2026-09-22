"""Cleaning mode: one deliberate, long, manual pump run.

Flushing the tower with a cleaning solution needs the pump circulating for an
hour or two. Every other path through this codebase is capped at
``MAX_PUMP_RUN_SECONDS`` (5 minutes), and that cap is not negotiable -- it is
what guarantees that a stuck schedule, a dropped API call or a killed CLI cannot
leave the pump running. Cleaning therefore does not raise the cap; it opens a
second, explicitly-bounded budget that only this module can spend.

Three things make that safe:

**The deadline is persisted, not held in a timer.** A ``threading.Timer`` dies
with its process, and a service restart 20 minutes into a two-hour run would
otherwise leave the pump energized with nobody holding a deadline for it. The
deadline lives in the state file, so every process can independently see when
the run must end -- and any of them can end it.

**Both processes enforce it.** The REST API and the MQTT service each arm their
own timer *and* re-check the persisted deadline on their polling loop. Whichever
notices expiry first stops the pump and clears the session; the other then sees
no session and does nothing. They cannot fight, because they agree on one
timestamp rather than each tracking their own.

**The tank is re-checked for the whole run.** A start-only dry-run check is
adequate for five minutes and reckless for two hours -- a slow leak, a siphon
back into the reservoir, or simply the volume the tower holds while running can
drop the level well after the run was cleared to start.

The cutoff it re-checks against is deliberately *not* ``PUMP_CUTOFF_CM``. See
``cleaning_cutoff``.
"""

import logging
import uuid
from datetime import datetime, timedelta

import config
from app.lib import state as state_lib
from app.lib.locking import pump_locked
from app.lib.water import is_reading_fresh, is_water_low

logger = logging.getLogger(__name__)

# State-file keys. Namespaced so load_state()'s merge cannot collide with the
# actuator fields, and so clearing a session is an explicit set-to-None rather
# than a key deletion the merge would undo.
UNTIL_KEY = "cleaning_until"
STARTED_KEY = "cleaning_started_at"
SECONDS_KEY = "cleaning_seconds"
SPEED_KEY = "cleaning_speed"
REASON_KEY = "cleaning_last_result"
ID_KEY = "cleaning_id"
GENERATION_KEY = "cleaning_generation"

# Why a run ended, surfaced to Home Assistant and the web UI. "Why did my clean
# stop early?" is otherwise only answerable from the logs.
DONE_COMPLETED = "completed"
DONE_STOPPED = "stopped"
DONE_LOW_WATER = "stopped: low water"
DONE_NO_READING = "stopped: no water reading"


def cleaning_cutoff(cleaning_cm=None, pump_cutoff_cm=None, empty_cm=None, min_depth_cm=None):
    """The airgap (cm) at which a cleaning run is refused or stopped.

    Airgaps grow as the tank drains, so a *larger* cutoff tolerates *less* water.

    Kept separate from ``PUMP_CUTOFF_CM`` because the two runs are not the same
    job. Watering protects a full tower of plants and can afford to stop early,
    at 4" of water. Cleaning is done on a drained tower with a shallow pool of
    solution -- the watering cutoff sits at roughly half a tank, so it would
    refuse a cleaning run outright before it ever started.

    Unset falls back to the watering cutoff, which is exactly today's behaviour:
    a tower that never configures cleaning is no less protected than it is now.

    The result is then clamped so it can never demand less than ``min_depth_cm``
    of water above the tank floor. A configured cutoff at or past ``empty_cm``
    means "run until dry", which is precisely what a cutoff exists to prevent,
    and a two-hour run has ample time to get there. The clamp is skipped when the
    calibration cannot support it -- a tank shallower than the required depth is
    a configuration problem this function cannot fix, and inventing a number for
    it would be worse than passing the request through.
    """
    cutoff = cleaning_cm or pump_cutoff_cm
    if not cutoff:
        # No cutoff configured anywhere: no interlock, matching watering. The
        # caller decides whether to allow that; see water_verdict().
        return None
    if empty_cm and min_depth_cm:
        floor = empty_cm - min_depth_cm
        if floor > 0:
            return min(cutoff, floor)
    return cutoff


def effective_cutoff():
    """``cleaning_cutoff`` resolved from config."""
    return cleaning_cutoff(
        config.CLEANING_CUTOFF_CM,
        config.PUMP_CUTOFF_CM,
        config.WATER_EMPTY_CM,
        config.CLEANING_MIN_DEPTH_CM,
    )


def validate_duration(seconds, default=None, maximum=None):
    """Return a validated run length in seconds, or raise ``ValueError``.

    Rejects rather than clamps, mirroring ``/pump/run``: a request for three
    hours under a two-hour cap is a misunderstanding worth reporting, not
    something to quietly honour as two.
    """
    default = config.CLEANING_DEFAULT_SECONDS if default is None else default
    maximum = config.CLEANING_MAX_SECONDS if maximum is None else maximum
    if seconds is None or seconds == "":
        seconds = default
    try:
        seconds = int(seconds)
    except (TypeError, ValueError):
        raise ValueError("seconds must be an integer")
    if not (1 <= seconds <= maximum):
        raise ValueError(f"seconds must be between 1 and {maximum}")
    return seconds


def water_verdict(state=None, now=None, cutoff=None):
    """Return ``(ok, reason)`` for running a cleaning pump right now.

    Reads the MQTT service's last recorded reading rather than the sensor: the
    ultrasonic sensor cannot be read by two processes at once without both
    cross-talking and coming back wrong, so the service polls and everything
    else acts on what it recorded. Same rule as ``bin/water.sh``.

    **This fails closed, which is the opposite of the watering guard.** There, a
    stale reading allows the run, because a stopped service must not be able to
    withhold water indefinitely and kill the plants. Here, nothing is at stake in
    refusing: cleaning is a manual action with a person standing at the tower,
    who can retry. What *is* at stake is two hours of pumping with nothing
    watching the tank -- and a stale reading means the service that would notice
    the level dropping is not running.
    """
    state = state_lib.load_state() if state is None else state
    now = now or datetime.now()
    cutoff = effective_cutoff() if cutoff is None else cutoff

    if not cutoff:
        return False, "no cleaning cutoff configured; configure and verify the intake depth first"

    checked_at = state.get("water_checked_at")
    if not is_reading_fresh(checked_at, now, config.WATER_READING_MAX_AGE_SECONDS):
        return False, (
            f"no recent water reading (last: {checked_at or 'never'}); refusing to "
            "run a long cleaning cycle with nothing watching the tank"
        )

    airgap = state.get("water_airgap_cm")
    if airgap is None:
        return False, "no water reading recorded; refusing to run the pump blind"

    if is_water_low(airgap, cutoff):
        return False, (
            f"water below the cleaning cutoff (airgap {airgap} cm > {cutoff:.2f} cm); "
            "refusing to run the pump dry"
        )
    return True, f"water above the cleaning cutoff (airgap {airgap} cm)"


def session(state=None, now=None):
    """The active cleaning session, or ``None``.

    An expired session reads as ``None`` so that callers cannot act on a run
    whose deadline has passed. Clearing the persisted keys is the enforcing
    caller's job -- this is a read.
    """
    state = state_lib.load_state() if state is None else state
    now = now or datetime.now()

    raw = state.get(UNTIL_KEY)
    if not raw:
        return None
    try:
        until = datetime.fromisoformat(str(raw))
        if until.tzinfo is not None:
            until = until.astimezone().replace(tzinfo=None)
    except (TypeError, ValueError):
        # A corrupt timestamp is not a licence to run the pump forever.
        logger.warning("Ignoring unparseable cleaning deadline: %r", raw)
        return None

    remaining = (until - now).total_seconds()
    if remaining <= 0:
        return None

    return {
        "id": state.get(ID_KEY) or str(raw),
        "until": until,
        "remaining_seconds": int(remaining),
        "started_at": state.get(STARTED_KEY),
        "seconds": state.get(SECONDS_KEY),
        "speed": state.get(SPEED_KEY) or config.CLEANING_PUMP_SPEED,
    }


def is_active(state=None, now=None):
    return session(state=state, now=now) is not None


def remaining_seconds(state=None, now=None):
    active = session(state=state, now=now)
    return active["remaining_seconds"] if active else 0


def expired(state=None, now=None):
    """True when a session is recorded but its deadline has passed.

    The signal an enforcing loop needs: ``session()`` alone cannot distinguish
    "no run" from "a run that must be stopped right now".
    """
    state = state_lib.load_state() if state is None else state
    if not state.get(UNTIL_KEY):
        return False
    return session(state=state, now=now) is None


@pump_locked
def start(seconds=None, speed=None, now=None):
    """Record the start of a cleaning run and return the session.

    Persisting the deadline *before* the pump is energized is deliberate: a crash
    between the two leaves a deadline with no pump (harmless, and the next poll
    clears it), where the reverse would leave a pump with no deadline.
    """
    seconds = validate_duration(seconds)
    speed = config.CLEANING_PUMP_SPEED if speed is None else int(speed)
    if not (1 <= speed <= 100):
        raise ValueError("speed must be between 1 and 100")

    now = now or datetime.now()
    until = now + timedelta(seconds=seconds)
    session_id = uuid.uuid4().hex
    state_lib.save_state(
        strict=True,
        **{
            ID_KEY: session_id,
            GENERATION_KEY: session_id,
            UNTIL_KEY: until.isoformat(timespec="seconds"),
            STARTED_KEY: now.isoformat(timespec="seconds"),
            SECONDS_KEY: seconds,
            SPEED_KEY: speed,
            REASON_KEY: None,
        },
    )
    logger.info("Cleaning run started: %ss at %s%%, until %s", seconds, speed, until)
    return {
        "id": session_id,
        "until": until,
        "remaining_seconds": seconds,
        "started_at": now.isoformat(timespec="seconds"),
        "seconds": seconds,
        "speed": speed,
    }


@pump_locked
def clear(reason=DONE_STOPPED):
    """End the session. Idempotent, so every enforcing path can call it freely."""
    state_lib.save_state(
        strict=True,
        **{
            ID_KEY: None,
            UNTIL_KEY: None,
            STARTED_KEY: None,
            SECONDS_KEY: None,
            SPEED_KEY: None,
            REASON_KEY: reason,
        },
    )
    logger.info("Cleaning run ended: %s", reason)


def last_result(state=None):
    state = state_lib.load_state() if state is None else state
    return state.get(REASON_KEY)


def status(state=None, now=None):
    """A serialisable snapshot for the REST API, the web UI and MQTT."""
    state = state_lib.load_state() if state is None else state
    now = now or datetime.now()
    active = session(state=state, now=now)
    cutoff = effective_cutoff()
    return {
        "active": active is not None,
        "remaining_seconds": active["remaining_seconds"] if active else 0,
        "until": active["until"].isoformat(timespec="seconds") if active else None,
        "started_at": active["started_at"] if active else None,
        "seconds": active["seconds"] if active else None,
        "speed": active["speed"] if active else None,
        "cutoff_cm": cutoff,
        "max_seconds": config.CLEANING_MAX_SECONDS,
        "default_seconds": config.CLEANING_DEFAULT_SECONDS,
        "last_result": state.get(REASON_KEY),
    }


if __name__ == "__main__":
    # Bookkeeping only -- this never touches the pump. ``--stop`` ends the
    # *session*, which is what stops the watchdogs in the other processes from
    # holding the pump on; ``bin/water.sh`` pairs it with an actual pump-off.
    # Keeping the two separate is what lets `water off` be correct without this
    # module needing a pin factory or a running pigpiod.
    import argparse
    import json as _json

    parser = argparse.ArgumentParser(description="Inspect or end a cleaning run.")
    parser.add_argument("--status", action="store_true", help="Print cleaning status as JSON.")
    parser.add_argument("--stop", action="store_true", help="End the cleaning session.")
    args = parser.parse_args()

    if args.stop:
        clear(DONE_STOPPED)
        print("Cleaning session cleared.")
    elif args.status:
        print(_json.dumps(status(), indent=2, default=str))
    else:
        print(_json.dumps(status(), indent=2, default=str))
