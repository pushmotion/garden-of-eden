"""Grow-cycle tracking and time-based reminders.

Covers the grow-cycle notification issues: thinning (#6), root check (#5),
harvest/trim (#62), and nutrient/"food" reminders (#4). A cycle has a start
date; reminders become "due" once enough days have elapsed since the start.

Pure logic with an injectable ``now`` so it can be unit-tested deterministically.
The MQTT service persists/loads state via load_state/save_state and publishes
due reminders.
"""

import json
from datetime import datetime

import config
from app.lib.persist import write_json_atomic

STAGES = ["germination", "thinning", "root_check", "harvest"]

# Reminders that come back round after round, keyed to the config cadence that
# drives them. Unlike the one-shots these are anchored to the last acknowledgement
# rather than to the cycle start, so acting late shifts the next one instead of
# silently skipping a whole cadence.
RECURRING = ("nutrient", "reservoir_change")


def _cadence(key):
    return {
        "nutrient": config.NUTRIENT_REMINDER_DAYS,
        "reservoir_change": config.RESERVOIR_CHANGE_DAYS,
    }.get(key)


def default_state(now=None):
    now = now or datetime.now()
    return {
        "stage": "germination",
        "started": now.isoformat(),
        "acknowledged": [],
        "last_ack": {},
    }


def load_state():
    try:
        with open(config.GROW_STATE_FILE) as fh:
            return json.load(fh)
    except (FileNotFoundError, ValueError):
        return default_state()


def save_state(state):
    write_json_atomic(config.GROW_STATE_FILE, state)


def start_cycle(now=None):
    """Begin a fresh grow cycle (resets stage, start date, acknowledgements)."""
    state = default_state(now)
    save_state(state)
    return state


def _days_since(started_iso, now):
    try:
        started = datetime.fromisoformat(started_iso)
    except (TypeError, ValueError):
        return 0
    return (now - started).days


def due_reminders(state, now=None):
    """Return reminder keys that are due and not yet acknowledged.

    One-shot grow-stage reminders fire once their day threshold passes; the
    recurring nutrient reminder fires every NUTRIENT_REMINDER_DAYS.
    """
    now = now or datetime.now()
    days = _days_since(state.get("started"), now)
    acked = set(state.get("acknowledged", []))
    due = []

    one_shot = {
        "thinning": config.THINNING_REMINDER_DAYS,
        "root_check": config.ROOT_CHECK_REMINDER_DAYS,
        "harvest": config.HARVEST_REMINDER_DAYS,
    }
    for key, threshold in one_shot.items():
        if threshold and days >= threshold and key not in acked:
            due.append(key)

    # Recurring reminders are due once a full cadence has passed since they were
    # last acknowledged (or since the cycle started, if never). The older
    # ``days % cadence == 0`` test only held for a single day, so a reminder
    # missed on its exact day vanished until the next multiple.
    last_ack = state.get("last_ack") or {}
    for key in RECURRING:
        cadence = _cadence(key)
        if not cadence:
            continue
        previous = last_ack.get(key)
        since = _days_since(previous, now) if previous else days
        if days >= cadence and since >= cadence:
            due.append(key)

    return due


def acknowledge(state, key, now=None):
    """Mark a reminder handled so it stops firing.

    Recurring reminders record *when* they were handled and restart their
    cadence from that moment; one-shots are simply marked done.
    """
    now = now or datetime.now()
    if key in RECURRING:
        last_ack = dict(state.get("last_ack") or {})
        last_ack[key] = now.isoformat()
        state["last_ack"] = last_ack
    else:
        acked = set(state.get("acknowledged", []))
        acked.add(key)
        state["acknowledged"] = sorted(acked)
    return state


def nutrient_dose(state):
    """Whether the next feed should be a full or a reduced dose.

    The first feed of a cycle goes into what is effectively plain water, so it
    is full strength. Later feeds land on top of whatever the previous one left
    behind — plain-water top-offs dilute but do not clear it — so they are cut
    back to avoid stacking salts in a reservoir nothing on the unit can measure.
    """
    return "reduced" if (state.get("last_ack") or {}).get("nutrient") else "full"


def set_stage(state, stage):
    if stage not in STAGES:
        raise ValueError(f"unknown stage {stage!r}; expected one of {STAGES}")
    state["stage"] = stage
    return state
