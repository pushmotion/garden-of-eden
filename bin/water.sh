#!/usr/bin/env bash

# Script to control Gardyn water pump
# Usage: water <seconds|on|off>
# "on" runs for the full cap. Valid range is 1 second to MAX_PUMP_RUN_SECONDS
# from config.py (300 by default) -- a hard safety cap shared with the pump
# routes, the schedule compiler and Home Assistant. Out-of-range input falls
# back to the default rather than being clamped, so a typo cannot become a run.

# -e exit immediately
# -u undefined variables trigger error
# -o exit with first piped failure
set -euo pipefail

# Constants
readonly TIME_MIN=1          # 1 second
readonly SPEED=50
readonly WATER_BY_DEFAULT=true  # Whether to default to TIME_DEFAULT on invalid input

# Set by --override-low-water-level: run the pump even below the cutoff.
# Upstream iot-root#83 asked for this escape hatch alongside the safeguard.
OVERRIDE_LOW_WATER=false

NC=$(echo -e '\033[0m')
IT=$(echo -e '\033[3m')

# Get Garden of Eden path from script location
GOE_PATH=$(realpath "$(dirname "$(readlink -e "${0}")")/..")

# Put the repo root on PYTHONPATH so the driver scripts can `import config`
# regardless of the caller's working directory (cron, systemd, etc.).
export PYTHONPATH="${GOE_PATH}${PYTHONPATH:+:${PYTHONPATH}}"

# The hard safety cap comes from config.py (MAX_PUMP_RUN_SECONDS), the same value
# the pump routes, the schedule compiler and Home Assistant enforce. It used to be
# a literal 300 here, so raising the cap in .env left this path silently refusing
# anything longer -- cron and the CLI capped at five minutes while every other
# surface allowed more.
#
# Falls back to config.py's own default if python cannot answer: a broken venv
# must not make watering impossible, and the pump routes enforce the real cap
# regardless. The fallback is deliberately the *lower*, safer of the plausible
# values rather than unbounded.
read_pump_cap() {
    local cap
    cap=$("${GOE_PATH}/venv/bin/python" -c         'import config; print(int(config.MAX_PUMP_RUN_SECONDS))' 2>/dev/null) || cap=""
    if [[ "${cap}" =~ ^[0-9]+$ ]] && (( cap >= 1 )); then
        echo "${cap}"
    else
        echo 300
    fi
}

readonly TIME_MAX=$(read_pump_cap)
readonly TIME_DEFAULT="${TIME_MAX}"

# Turn off water pump
turn_off_water() {
    "${GOE_PATH}/venv/bin/python" "${GOE_PATH}/app/sensors/pump/pump.py" --off
}

# Refuse to run the pump when the tank is below the cutoff.
#
# The check reads the MQTT service's last median-filtered reading rather than
# taking its own: two processes triggering the ultrasonic sensor cross-talk and
# both come back wrong. A stale or missing reading counts as "no opinion" and
# the run proceeds, so a stopped service cannot withhold water indefinitely.
check_water_level() {
    if [[ "${OVERRIDE_LOW_WATER}" == true ]]; then
        echo "WARNING: --override-low-water-level set; skipping the dry-run guard."
        return 0
    fi
    if "${GOE_PATH}/venv/bin/python" -m app.lib.water_guard; then
        return 0
    fi
    echo "ERROR: refusing to water. Pass --override-low-water-level to force it." >&2
    return 1
}

# Turn on water pump
turn_on_water() {
    local extra=()
    if [[ "$OVERRIDE_LOW_WATER" == true ]]; then extra+=(--override-low-water-level); fi
    "${GOE_PATH}/venv/bin/python" "${GOE_PATH}/app/sensors/pump/pump.py" --on --speed "${SPEED}" "${extra[@]}"
}

# Function to water for a specified time, then turn off
water_for_time() {
    local time="$1"
    check_water_level || exit 1
	echo "Watering for ${time} seconds."
    turn_on_water
    sleep "${time}"
    # turn_off_water # turn off will be caught by the exit trap.
}

# True when a cleaning run currently owns the pump.
#
# Errors report "not cleaning" (see app/lib/cleaning_guard.py): a wrong answer
# in that direction costs a cleaning cycle somebody can restart, where the other
# direction silently suppresses watering for as long as the bad state lasts.
cleaning_in_progress() {
    "${GOE_PATH}/venv/bin/python" -m app.lib.cleaning_guard >/dev/null 2>&1
}

# Function to handle exit signals, ensuring the water pump is turned off
#
# ...unless a cleaning run owns the pump. This trap fires on *every* exit, so
# without the check a three-minute cron watering that happened to start during a
# two-hour clean would end the clean at its own three-minute mark -- and would
# do it silently, from the trap, long after the run appeared to succeed.
clean_up() {
    if cleaning_in_progress; then
        echo "Cleaning run in progress; leaving the pump alone."
        return 0
    fi
    turn_off_water
}

# Function to print usage instructions
usage() {
    cat << EOF
Usage: water [--override-low-water-level] <off|on|${IT}seconds${NC}>
Valid time range is ${TIME_MIN} to ${TIME_MAX} seconds; "on" defaults to ${TIME_DEFAULT} seconds.
Example: water 75
The pump is refused when the tank is below the cutoff; --override-low-water-level forces it.
EOF
}

# Trap signals to ensure water is turned off
trap clean_up EXIT

# Main logic
main() {
    # Strip the override flag from anywhere in the arg list before parsing the
    # duration, so `water --override-low-water-level 60` and `water 60
    # --override-low-water-level` both work.
    local args=()
    local arg
    for arg in "$@"; do
        if [[ "${arg}" == "--override-low-water-level" ]]; then
            OVERRIDE_LOW_WATER=true
        else
            args+=("${arg}")
        fi
    done
    set -- "${args[@]+"${args[@]}"}"

    if [[ $# -eq 0 ]]; then
        echo "ERROR: No arguments provided"
        usage
        exit 1
    fi

    local time

    # Scheduled watering is suspended for the duration of a cleaning run. The
    # pump is already circulating, at the cleaning duty cycle, under the
    # cleaning cutoff -- a watering run has nothing to add and everything to
    # interrupt. Exits 0 so cron records a no-op rather than a failure.
    #
    # "off" is exempt below: stopping the pump is safe from any source, always.
    if [[ "$1" != "off" ]] && cleaning_in_progress; then
        echo "Cleaning run in progress; skipping this watering run."
        exit 0
    fi

    case "$1" in
        off)
            # "off" ends a cleaning run rather than colliding with one. Clearing
            # the session first means the EXIT trap below sees no cleaning run
            # and lets the pump-off through; leaving it set would strand an
            # active session with a stopped pump, blocking every other path
            # until its deadline passed.
            "${GOE_PATH}/venv/bin/python" "${GOE_PATH}/app/sensors/pump/pump.py" --off --stop-cleaning
            exit 0
            ;;
        on)
            time="${TIME_DEFAULT}"
            ;;
        ''|*[!0-9]*)
            echo "ERROR: Unrecognized input format"
            usage
            exit 1
            ;;
        *)
            time="$1"
            ;;
    esac

    # Validate that the input time is within range
    if [[ "${time}" -ge "${TIME_MIN}" && "${time}" -le "${TIME_MAX}" ]]; then
        water_for_time "${time}"
    elif [[ "${WATER_BY_DEFAULT}" == true ]]; then
        water_for_time "${TIME_DEFAULT}"
    else
        echo "ERROR: Input must be between ${TIME_MIN} and ${TIME_MAX} seconds"
        usage
        exit 1
    fi
}

# Run main function
main "$@"
