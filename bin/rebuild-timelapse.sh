#!/usr/bin/env bash
#
# Rebuild the timelapse clips from the archived frames. Intended for cron.
#
# Usage: rebuild-timelapse.sh [cam ...]      (default: upper lower)
#
# Deliberately does NOT go through the REST API. `POST /camera/timelapse/<cam>`
# encodes synchronously inside the request handler, so on a Pi Zero W a full
# archive occupies a waitress worker for minutes and dies with the service:
# a `systemctl restart` during a build SIGKILLed ffmpeg mid-write and left a
# truncated mp4 with no moov atom. Running it here keeps encoding out of the
# service's lifetime entirely.
#
# Safe to import from: app/sensors/camera/camera.py pulls in only config and
# app.lib.locking, no GPIO. Importing anything that builds a driver would open a
# second handle on the ultrasonic sensor and corrupt the MQTT service's readings.
set -uo pipefail

GOE_PATH=$(realpath "$(dirname "$(readlink -e "${0}")")/..")
export PYTHONPATH="${GOE_PATH}${PYTHONPATH:+:${PYTHONPATH}}"
PY="${GOE_PATH}/venv/bin/python"

PRUNE_DARK=false
ARGS=()
for a in "$@"; do
    case "$a" in
        # One-off, for archives captured before the brightness check existed.
        # Ordinary captures are filtered at archive time, so the weekly run does
        # not need this and should not pay to decode every frame.
        --prune-dark) PRUNE_DARK=true ;;
        *) ARGS+=("$a") ;;
    esac
done
CAMS=("${ARGS[@]+"${ARGS[@]}"}")
[ ${#CAMS[@]} -eq 0 ] && CAMS=(upper lower)

if [ "${PRUNE_DARK}" = true ]; then
    for cam in "${CAMS[@]}"; do
        n=$("${PY}" -c "
from app.sensors.camera import camera
print(camera.prune_dark_frames('${cam}'))
" 2>/dev/null) || n="?"
        echo "$(date '+%Y-%m-%d %H:%M:%S') ${cam}: quarantined ${n} dark frames"
    done
fi

# nice: encoding is the heaviest thing this box ever does, and watering cron and
# the MQTT service both matter more than a timelapse finishing promptly.
for cam in "${CAMS[@]}"; do
    start=$(date +%s)
    # stderr goes to a separate file, not into $out: generate_timelapse logs to
    # stderr, so folding it in here made the captured "path" a multi-line blob
    # and every success reported 0 bytes.
    err=$(mktemp)
    if out=$(nice -n 10 "${PY}" -c "
from app.sensors.camera import camera
print(camera.generate_timelapse('${cam}'))
" 2>"${err}"); then
        size=$(stat -c%s "${out}" 2>/dev/null || echo 0)
        echo "$(date '+%Y-%m-%d %H:%M:%S') ${cam}: ok, ${size} bytes in $(( $(date +%s) - start ))s"
    else
        # Non-zero here is worth seeing: generate_timelapse() now verifies the
        # artifact, so a failure means no video was produced -- not merely that
        # ffmpeg grumbled.
        echo "$(date '+%Y-%m-%d %H:%M:%S') ${cam}: FAILED after $(( $(date +%s) - start ))s: $(tail -3 "${err}")" >&2
    fi
    rm -f "${err}"
done
