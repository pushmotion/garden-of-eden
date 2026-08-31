"""Lights/pump scheduling that survives reboots and PC shutdown by writing the
Pi's crontab (issue #32).

The schedule is persisted as JSON and compiled into crontab lines tagged with a
marker comment so we can rewrite only our own entries. Cron invokes the
``light`` and ``water`` CLI symlinks installed by bin/setup.sh.

Each of ``lights`` and ``pump`` holds a ``days`` map keyed by weekday
(``mon``..``sun``); every weekday carries a list of entries, so you can set as
many light windows and pump runs per day as you like:

    {
      "lights": {"enabled": true, "days": {
        "mon": [{"onTime": "06:00", "offTime": "22:00", "brightness": 70}], ...
      }},
      "pump": {"enabled": true, "days": {
        "mon": [{"time": "08:00", "duration": 5}, {"time": "16:00", "duration": 5}], ...
      }}
    }

Older single-window schedules (``lights.onTime`` / ``pump.runs``) are migrated
to this shape on load, so existing saved schedules and clients keep working.
"""

import datetime
import json
import logging
import os
import subprocess

import config
from app.lib.persist import write_json_atomic

logger = logging.getLogger(__name__)

CRON_MARKER = "# garden-of-eden"
# One-shot pump runs live under their own marker so apply_schedule() never
# rewrites or removes them, and so a one-off can never be mistaken for part of
# the recurring schedule. Note it *contains* CRON_MARKER, so every filter on the
# recurring marker has to exclude this one explicitly.
ONCE_MARKER = "# garden-of-eden-once"
LIGHT_CMD = "/usr/local/bin/light"
WATER_CMD = "/usr/local/bin/water"

# schedule-refresh CLI (absolute path): run nightly so vacation mode auto-expires.
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
REFRESH_CMD = os.path.join(_REPO_ROOT, "bin", "schedule-refresh.sh")

# Reduced light/water applied to every day while Vacation mode is active.
VACATION_PROFILE = {
    "lights": [{"onTime": "10:00", "offTime": "16:00", "brightness": 50}],
    "pump": [{"time": "12:00", "duration": 3}],
}

# Weekday order and the cron day-of-week number each maps to (cron: Sun=0).
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
DAY_TO_CRON = {"mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6, "sun": 0}

DEFAULT_SCHEDULE = {
    "lights": {"enabled": False, "days": {d: [] for d in DAYS}},
    "pump": {"enabled": False, "days": {d: [] for d in DAYS}},
    "vacation": {"enabled": False, "until": None},
}


def _empty_days():
    return {d: [] for d in DAYS}


def normalize_schedule(schedule):
    """Coerce any accepted schedule shape into the canonical per-day form.

    Accepts the current ``days`` layout, the legacy single-window layout
    (``lights.onTime``/``offTime``/``brightness`` and ``pump.runs``), or a mix,
    and always returns a dict with full ``mon``..``sun`` day maps.
    """
    schedule = schedule or {}
    lights = dict(schedule.get("lights") or {})
    pump = dict(schedule.get("pump") or {})

    # --- lights ---
    light_days = _empty_days()
    if isinstance(lights.get("days"), dict):
        for day, entries in lights["days"].items():
            if day in light_days and isinstance(entries, list):
                light_days[day] = [dict(e) for e in entries]
    elif lights.get("onTime") or lights.get("offTime"):
        # Legacy: one window applied to every day.
        window = {
            "onTime": lights.get("onTime", "08:00"),
            "offTime": lights.get("offTime", "22:00"),
            "brightness": int(lights.get("brightness", 70)),
        }
        light_days = {d: [dict(window)] for d in DAYS}

    # --- pump ---
    pump_days = _empty_days()
    if isinstance(pump.get("days"), dict):
        for day, entries in pump["days"].items():
            if day in pump_days and isinstance(entries, list):
                pump_days[day] = [dict(e) for e in entries]
    elif isinstance(pump.get("runs"), list):
        # Legacy: same set of runs applied to every day.
        runs = [dict(r) for r in pump["runs"]]
        pump_days = {d: [dict(r) for r in runs] for d in DAYS}

    vacation = dict(schedule.get("vacation") or {})

    return {
        "lights": {"enabled": bool(lights.get("enabled")), "days": light_days},
        "pump": {"enabled": bool(pump.get("enabled")), "days": pump_days},
        "vacation": {
            "enabled": bool(vacation.get("enabled")),
            "until": vacation.get("until") or None,
        },
    }


def load_schedule():
    """Return the saved schedule (normalized), or defaults if none exists."""
    try:
        with open(config.SCHEDULE_FILE) as fh:
            return normalize_schedule(json.load(fh))
    except (FileNotFoundError, ValueError):
        return normalize_schedule(DEFAULT_SCHEDULE)


def save_schedule(schedule):
    write_json_atomic(config.SCHEDULE_FILE, schedule)


def _hh_mm(value):
    """Parse "HH:MM" -> (minute, hour) for cron, or raise ValueError."""
    hour, minute = value.split(":")
    h, m = int(hour), int(minute)
    if not (0 <= h < 24 and 0 <= m < 60):
        raise ValueError(f"invalid time {value!r}")
    return m, h


def _pump_seconds(duration_minutes):
    """Minutes -> seconds, clamped to the hard safety cap (never > 5 min)."""
    seconds = int(duration_minutes) * 60
    if seconds < 1:
        raise ValueError(f"invalid pump duration {duration_minutes!r}")
    return min(seconds, config.MAX_PUMP_RUN_SECONDS)


def is_vacation_active(schedule, today=None):
    """True when Vacation mode is on and not past its end date (``until``)."""
    vacation = schedule.get("vacation") or {}
    if not vacation.get("enabled"):
        return False
    until = vacation.get("until")
    if not until:
        return True  # no end date -> active until manually turned off
    today = today or datetime.date.today()
    try:
        return today <= datetime.date.fromisoformat(until)
    except ValueError:
        return True


def _vacation_cron_lines():
    """Minimal keep-alive light/water for every day, plus a nightly refresh so
    vacation mode reverts to the normal schedule once its end date passes."""
    lines = []
    for day in DAYS:
        dow = DAY_TO_CRON[day]
        for window in VACATION_PROFILE["lights"]:
            on_m, on_h = _hh_mm(window["onTime"])
            off_m, off_h = _hh_mm(window["offTime"])
            brightness = int(window["brightness"])
            lines.append(f"{on_m} {on_h} * * {dow} {LIGHT_CMD} {brightness} {CRON_MARKER}")
            lines.append(f"{off_m} {off_h} * * {dow} {LIGHT_CMD} off {CRON_MARKER}")
        for run in VACATION_PROFILE["pump"]:
            run_m, run_h = _hh_mm(run["time"])
            seconds = _pump_seconds(run["duration"])
            lines.append(f"{run_m} {run_h} * * {dow} {WATER_CMD} {seconds} {CRON_MARKER}")
    lines.append(f"2 0 * * * {REFRESH_CMD} {CRON_MARKER}")
    return lines


def build_cron_lines(schedule):
    """Compile a (normalized) schedule dict into a list of marked crontab lines."""
    schedule = normalize_schedule(schedule)

    # Vacation mode overrides the normal schedule with a reduced keep-alive one.
    if is_vacation_active(schedule):
        return _vacation_cron_lines()

    lines = []

    lights = schedule["lights"]
    if lights["enabled"]:
        for day in DAYS:
            dow = DAY_TO_CRON[day]
            for window in lights["days"][day]:
                brightness = int(window.get("brightness", 70))
                ramp = int(window.get("rampMinutes", 0) or 0)
                on_m, on_h = _hh_mm(window.get("onTime", "08:00"))
                off_m, off_h = _hh_mm(window.get("offTime", "22:00"))
                # light.sh takes positional args: `light <brightness|off>`, or
                # `light ramp <brightness> <minutes>` for a sunrise/sunset fade.
                if ramp > 0:
                    on_cmd = f"{LIGHT_CMD} ramp {brightness} {ramp}"
                    off_cmd = f"{LIGHT_CMD} ramp 0 {ramp}"
                else:
                    on_cmd = f"{LIGHT_CMD} {brightness}"
                    off_cmd = f"{LIGHT_CMD} off"
                lines.append(f"{on_m} {on_h} * * {dow} {on_cmd} {CRON_MARKER}")
                lines.append(f"{off_m} {off_h} * * {dow} {off_cmd} {CRON_MARKER}")

    pump = schedule["pump"]
    if pump["enabled"]:
        for day in DAYS:
            dow = DAY_TO_CRON[day]
            for run in pump["days"][day]:
                run_m, run_h = _hh_mm(run.get("time", "12:00"))
                seconds = _pump_seconds(run.get("duration", 5))
                lines.append(f"{run_m} {run_h} * * {dow} {WATER_CMD} {seconds} {CRON_MARKER}")

    return lines


def _read_crontab():
    result = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    if result.returncode != 0:
        return []
    return result.stdout.splitlines()


def _write_crontab(lines):
    payload = "\n".join(lines) + "\n"
    subprocess.run(["crontab", "-"], input=payload, text=True, check=True)


def _effective_days(schedule, kind, now):
    """The day map in force for ``kind`` ("lights"/"pump"), honouring Vacation
    mode, or None when that half of the schedule is switched off."""
    if is_vacation_active(schedule, today=now.date()):
        return {day: list(VACATION_PROFILE[kind]) for day in DAYS}
    section = schedule.get(kind) or {}
    if not section.get("enabled"):
        return None
    return section.get("days") or _empty_days()


def expected_light_state(schedule, now=None, lookback_days=8):
    """What the compiled schedule has the light doing at ``now``.

    Returns ``(on, brightness)``, or ``None`` when the schedule expresses no
    opinion (lights disabled, or no windows at all) so callers leave the pin be.

    This deliberately replays the crontab's own semantics -- the most recent
    on/off event at or before ``now`` wins -- rather than interpreting each
    window as an interval. Windows that run past midnight, several windows in
    one day, and deliberate gaps then all agree with what cron actually did.
    """
    now = now or datetime.datetime.now()
    schedule = normalize_schedule(schedule)
    days = _effective_days(schedule, "lights", now)
    if days is None:
        return None

    latest = None
    for delta in range(lookback_days):
        date = (now - datetime.timedelta(days=delta)).date()
        for window in days.get(DAYS[date.weekday()], []):
            brightness = int(window.get("brightness", 70))
            try:
                on_m, on_h = _hh_mm(window.get("onTime", "08:00"))
                off_m, off_h = _hh_mm(window.get("offTime", "22:00"))
            except (ValueError, AttributeError):
                continue
            for stamp, on, level in (
                (datetime.datetime.combine(date, datetime.time(on_h, on_m)), True, brightness),
                (datetime.datetime.combine(date, datetime.time(off_h, off_m)), False, 0),
            ):
                if stamp <= now and (latest is None or stamp > latest[0]):
                    latest = (stamp, on, level)

    if latest is None:
        return None
    return latest[1], latest[2]


def expected_pump_run(schedule, now=None):
    """Seconds still owed to a pump run that should be under way at ``now``.

    Returns ``None`` when no run is in progress. Reported rather than acted on
    automatically: a service that crash-loops would otherwise restart the pump
    on every boot, and a missed run self-heals at the next scheduled one.
    """
    now = now or datetime.datetime.now()
    schedule = normalize_schedule(schedule)
    days = _effective_days(schedule, "pump", now)
    if days is None:
        return None

    for delta in (0, 1):  # today, and yesterday for a run spanning midnight
        date = (now - datetime.timedelta(days=delta)).date()
        for run in days.get(DAYS[date.weekday()], []):
            try:
                run_m, run_h = _hh_mm(run.get("time", "12:00"))
                seconds = _pump_seconds(run.get("duration", 5))
            except (ValueError, AttributeError, TypeError):
                continue
            start = datetime.datetime.combine(date, datetime.time(run_h, run_m))
            remaining = (start + datetime.timedelta(seconds=seconds) - now).total_seconds()
            if 0 < remaining <= seconds:
                return int(remaining)
    return None


def next_light_change(schedule, now=None, lookahead_days=8):
    """The next light transition after ``now`` as ``(when, on, brightness)``.

    The mirror of expected_light_state, which looks backwards: this walks forward
    to the soonest on/off event, so a caller can say "on at 65% until 21:00".
    Returns None when the light schedule is disabled or has no windows.
    """
    now = now or datetime.datetime.now()
    schedule = normalize_schedule(schedule)
    days = _effective_days(schedule, "lights", now)
    if days is None:
        return None

    for delta in range(lookahead_days):
        date = (now + datetime.timedelta(days=delta)).date()
        soonest = None
        for window in days.get(DAYS[date.weekday()], []):
            brightness = int(window.get("brightness", 70))
            try:
                on_m, on_h = _hh_mm(window.get("onTime", "08:00"))
                off_m, off_h = _hh_mm(window.get("offTime", "22:00"))
            except (ValueError, AttributeError):
                continue
            for stamp, on, level in (
                (datetime.datetime.combine(date, datetime.time(on_h, on_m)), True, brightness),
                (datetime.datetime.combine(date, datetime.time(off_h, off_m)), False, 0),
            ):
                if stamp > now and (soonest is None or stamp < soonest[0]):
                    soonest = (stamp, on, level)
        if soonest is not None:
            return soonest
    return None


def installed_cron_lines():
    """The marked cron lines currently installed -- i.e. what will actually run.

    Reads the live crontab rather than recompiling the saved schedule, so a
    client can verify the two agree. Returns [] when crontab is unavailable.
    """
    return [line for line in _read_crontab() if CRON_MARKER in line and ONCE_MARKER not in line]


def next_pump_run(schedule, now=None, lookahead_days=8):
    """When the next scheduled pump run starts, as a naive local datetime.

    Returns None when the pump schedule is disabled or has no runs. This exists
    because the writable "Pump Run Time" entity can only express a single run per
    day: on a multi-cycle schedule it shows the first run and never moves, so a
    dashboard reading it appears frozen.
    """
    now = now or datetime.datetime.now()
    schedule = normalize_schedule(schedule)
    days = _effective_days(schedule, "pump", now)
    if days is None:
        return None

    for delta in range(lookahead_days):
        date = (now + datetime.timedelta(days=delta)).date()
        soonest = None
        for run in days.get(DAYS[date.weekday()], []):
            try:
                run_m, run_h = _hh_mm(run.get("time", "12:00"))
            except (ValueError, AttributeError):
                continue
            start = datetime.datetime.combine(date, datetime.time(run_h, run_m))
            if start > now and (soonest is None or start < soonest):
                soonest = start
        if soonest is not None:
            return soonest
    return None


def apply_schedule(schedule):
    """Persist the schedule and replace our crontab entries with its compiled form."""
    # Validate/compile first so a bad schedule never touches crontab.
    schedule = normalize_schedule(schedule)
    cron_lines = build_cron_lines(schedule)
    save_schedule(schedule)

    # Keep foreign lines, and keep one-shot runs: adding or applying a recurring
    # schedule must never cancel a pending one-off.
    existing = [ln for ln in _read_crontab() if CRON_MARKER not in ln or ONCE_MARKER in ln]
    _write_crontab(existing + cron_lines)
    logger.info("Applied schedule with %d cron entries", len(cron_lines))
    return schedule


def refresh():
    """Re-apply the saved schedule, turning Vacation mode off once its end date
    has passed. Run nightly via cron so vacation auto-expires to the normal
    schedule."""
    schedule = load_schedule()
    vacation = schedule.get("vacation") or {}
    if vacation.get("enabled") and not is_vacation_active(schedule):
        vacation["enabled"] = False
        schedule["vacation"] = vacation
        logger.info("Vacation mode expired; reverting to the normal schedule")
    applied = apply_schedule(schedule)
    # A dated one-shot entry carries no year, so a spent one would fire again in
    # twelve months. This nightly pass is where they get cleaned up.
    prune_one_time_pump_runs()
    return applied


# --- One-shot pump runs -------------------------------------------------------
# A dated cron entry ("M H D Mon *") fires on a specific day, which is how a
# single run survives a reboot without a daemon holding a timer. Cron has no
# concept of "once", though, so the entry would fire again next year: each line
# carries its full ISO timestamp after the marker, and prune_one_time_pump_runs()
# drops the ones that have passed.


def _parse_once_line(line):
    """Extract ``(when, seconds)`` from a one-shot cron line, or None."""
    if ONCE_MARKER not in line:
        return None
    head, _, tail = line.partition(ONCE_MARKER)
    stamp = tail.split()[0] if tail.split() else ""
    try:
        when = datetime.datetime.fromisoformat(stamp)
    except ValueError:
        return None
    fields = head.split()
    try:
        # M H D Mon DOW CMD SECONDS
        seconds = int(fields[6])
    except (IndexError, ValueError):
        return None
    return when, seconds


def next_occurrence(hhmm, now=None):
    """The next datetime matching ``"HH:MM"`` -- today if still ahead, else tomorrow."""
    now = now or datetime.datetime.now()
    minute, hour = _hh_mm(hhmm)
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= now:
        candidate += datetime.timedelta(days=1)
    return candidate


def one_time_pump_runs(now=None):
    """Pending one-shot pump runs, soonest first. Expired entries are excluded."""
    now = now or datetime.datetime.now()
    runs = []
    for line in _read_crontab():
        parsed = _parse_once_line(line)
        if parsed and parsed[0] > now:
            runs.append({"at": parsed[0], "seconds": parsed[1]})
    return sorted(runs, key=lambda run: run["at"])


def prune_one_time_pump_runs(now=None, write=True):
    """Drop one-shot lines whose time has passed; returns the lines to keep.

    ``write=False`` computes the survivors without touching crontab, so a caller
    that is about to write anyway does not need two passes.
    """
    now = now or datetime.datetime.now()
    lines = _read_crontab()
    if not lines:
        # crontab unreadable (or genuinely empty) -- never write [] over it.
        return []
    kept = []
    for line in lines:
        parsed = _parse_once_line(line)
        if parsed and parsed[0] <= now:
            logger.info("Pruning expired one-time pump run: %s", parsed[0].isoformat())
            continue
        kept.append(line)
    if write and len(kept) != len(lines):
        _write_crontab(kept)
    return kept


def add_one_time_pump_run(hhmm, duration_minutes, now=None):
    """Install one dated cron entry for a single pump run.

    Deliberately does **not** go through apply_schedule(): adding a one-off must
    never rewrite the recurring schedule. Duration is clamped by the same
    MAX_PUMP_RUN_SECONDS cap as every other path.
    """
    when = next_occurrence(hhmm, now)
    seconds = _pump_seconds(duration_minutes)
    line = (
        f"{when.minute} {when.hour} {when.day} {when.month} * "
        f"{WATER_CMD} {seconds} {ONCE_MARKER} {when.isoformat()}"
    )
    kept = prune_one_time_pump_runs(now=now, write=False)
    _write_crontab(kept + [line])
    logger.info("Scheduled a one-time pump run at %s for %ss", when.isoformat(), seconds)
    return {"at": when, "seconds": seconds}


def clear_one_time_pump_runs():
    """Remove every pending one-shot run. Returns how many lines were removed."""
    lines = _read_crontab()
    if not lines:
        return 0
    kept = [line for line in lines if ONCE_MARKER not in line]
    removed = len(lines) - len(kept)
    if removed:
        _write_crontab(kept)
    return removed


def default_pump_duration(schedule=None):
    """Duration (minutes) to use for a run when the caller does not specify one.

    The shortest run already in the schedule, so a one-off inherits the rhythm
    the tower is already on rather than a hardcoded guess. Falls back to 3.
    """
    schedule = normalize_schedule(schedule or load_schedule())
    durations = [
        int(run.get("duration", 0))
        for day in DAYS
        for run in schedule["pump"]["days"].get(day, [])
        if int(run.get("duration", 0)) > 0
    ]
    return min(durations) if durations else 3
