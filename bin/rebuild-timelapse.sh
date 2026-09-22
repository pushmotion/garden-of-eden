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

CAMS=("$@")
[ ${#CAMS[@]} -eq 0 ] && CAMS=(upper lower)

# nice: encoding is the heaviest thing this box ever does, and watering cron and
# the MQTT service both matter more than a timelapse finishing promptly.
for cam in "${CAMS[@]}"; do
    start=$(date +%s)
    if out=$(nice -n 10 "${PY}" -c "
from app.sensors.camera import camera
print(camera.generate_timelapse('${cam}'))
" 2>&1); then
        size=$(stat -c%s "${out}" 2>/dev/null || echo 0)
        echo "$(date '+%Y-%m-%d %H:%M:%S') ${cam}: ok, ${size} bytes in $(( $(date +%s) - start ))s"
    else
        # Non-zero here is worth seeing: generate_timelapse() now verifies the
        # artifact, so a failure means no video was produced -- not merely that
        # ffmpeg grumbled.
        echo "$(date '+%Y-%m-%d %H:%M:%S') ${cam}: FAILED after $(( $(date +%s) - start ))s: ${out}" >&2
    fi
done
