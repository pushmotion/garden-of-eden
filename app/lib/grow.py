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

# One US teaspoon in millilitres. A dose gets mixed with a kitchen spoon at
# least as often as with a syringe, and "13 mL" means nothing at the drawer.
ML_PER_TEASPOON = 4.92892159375

# The Flora Series parts, in the order they MUST be poured. Micro first is not a
# preference: its calcium precipitates against Bloom's phosphate and sulfate if
# the concentrates meet before water has diluted them, and does not redissolve.
# ``order`` in the emitted plan is this sequence, so a consumer can render it
# without knowing the chemistry.
NUTRIENT_PARTS = (
    ("micro", "FloraMicro"),
    ("gro", "FloraGro"),
    ("bloom", "FloraBloom"),
)


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


def _ml_per_gallon(key):
    return {
        "micro": config.NUTRIENT_MICRO_ML_PER_GALLON,
        "gro": config.NUTRIENT_GRO_ML_PER_GALLON,
        "bloom": config.NUTRIENT_BLOOM_ML_PER_GALLON,
    }.get(key, 0.0)


def _spoon_text(ml):
    """``ml`` as teaspoons rounded to the nearest quarter, phrased for a spoon.

    Exact teaspoons are useless at the drawer -- nobody measures 2.64 tsp -- so
    this rounds to the nearest 1/4 tsp, which is the finest graduation a normal
    spoon set has. The unrounded figure travels beside it in the plan for anyone
    dosing with a syringe, so nothing is lost by rounding here.
    """
    quarters = int(round((ml / ML_PER_TEASPOON) * 4))
    if quarters <= 0:
        return "under 1/4 tsp"
    whole, rem = divmod(quarters, 4)
    fraction = {0: "", 1: "1/4", 2: "1/2", 3: "3/4"}[rem]
    if whole and fraction:
        return f"{whole} {fraction} tsp"
    if whole:
        return f"{whole} tsp"
    return f"{fraction} tsp"


def nutrient_plan(state, gallons=None):
    """How much of each Flora Series part the next feed needs, in mL and tsp.

    ``nutrient_dose`` says full or reduced; this turns that into the numbers a
    person actually pours, which is the difference between a reminder that says
    "add plant food" and one that can be acted on without going and looking
    something up.

    Volume comes from the caller. This module must never read the ultrasonic
    sensor itself -- see ``water.gallons_from_state`` -- so callers pass the MQTT
    service's last filtered figure, and an absent or nonsensical one falls back
    to the tank's rated capacity. That fallback is the right guess in practice
    because the documented routine is to top the reservoir up *before* dosing.

    Parts come back in pouring order, which is mandatory: see NUTRIENT_PARTS.
    """
    strength = nutrient_dose(state)
    fraction = 1.0 if strength == "full" else max(0.0, config.NUTRIENT_REDUCED_FRACTION)

    try:
        gallons = float(gallons)
    except (TypeError, ValueError):
        gallons = 0.0
    if gallons <= 0:
        gallons = float(config.TANK_CAPACITY_GALLONS or 0)

    parts = []
    for order, (key, label) in enumerate(NUTRIENT_PARTS, start=1):
        ml = _ml_per_gallon(key) * gallons * fraction
        parts.append(
            {
                "key": key,
                "label": label,
                "order": order,
                "ml": round(ml, 1),
                "tsp": round(ml / ML_PER_TEASPOON, 2),
                "spoons": _spoon_text(ml),
            }
        )

    plan = {
        "strength": strength,
        "fraction": round(fraction, 3),
        "gallons": round(gallons, 1),
        "parts": parts,
    }
    plan["summary"] = format_nutrient_plan(plan)
    return plan


def format_nutrient_plan(plan):
    """One line for the Home Assistant sensor: what to add, in order, both units.

    "then" rather than a comma because the order is load-bearing, and the string
    is the only part of the plan a glance at a notification will ever see.
    """
    parts = plan.get("parts") or []
    if not parts:
        return "no dose"
    return " then ".join(f"{p['label']} {p['ml']:g} mL ({p['spoons']})" for p in parts)


def set_stage(state, stage):
    if stage not in STAGES:
        raise ValueError(f"unknown stage {stage!r}; expected one of {STAGES}")
    state["stage"] = stage
    return state
