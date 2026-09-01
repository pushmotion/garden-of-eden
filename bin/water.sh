#!/usr/bin/env bash

# Script to control Gardyn water pump
# Usage: water <seconds|on|off>
# "on" defaults to 300 seconds (5 minutes), valid time range is 1 to 300 seconds
# (5 minutes) -- a hard safety cap; out-of-range input falls back to the default.

# -e exit immediately
# -u undefined variables trigger error
# -o exit with first piped failure
set -euo pipefail

# Constants
readonly TIME_DEFAULT=300    # 5 minutes in seconds
readonly TIME_MIN=1          # 1 second
readonly TIME_MAX=300        # 5 minutes in seconds (hard safety cap)
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
    "${GOE_PATH}/venv/bin/python" "${GOE_PATH}/app/sensors/pump/pump.py" --on --speed "${SPEED}"
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

# Function to handle exit signals, ensuring the water pump is turned off
clean_up() {
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

    case "$1" in
        off)
            turn_off_water
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
