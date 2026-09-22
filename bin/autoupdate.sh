#!/bin/bash
# Nightly auto-update (invoked by garden-autoupdate.timer).
#
# Fast-forwards the current branch from origin and, ONLY if something actually
# changed, refreshes deps and restarts the services. Designed to be safe to run
# unattended on live hardware:
#   * --ff-only: never rewrites history or discards local work; if the branch
#     can't fast-forward (e.g. it was force-pushed) it logs and bails.
#   * no-op when already up to date, so it doesn't cycle the light/pump for
#     nothing.
#   * a service restart ends with the pump OFF (graceful_shutdown), so an
#     interrupted watering run fails safe.
set -euo pipefail

BIN_DIR=$(dirname "$(readlink -f "$0")")
INSTALL_DIR=$(realpath "$BIN_DIR/..")
cd "$INSTALL_DIR" || exit 1

log() { echo "[garden-autoupdate] $*"; }

branch=$(git rev-parse --abbrev-ref HEAD 2>/dev/null) || { log "not a git repo; abort"; exit 1; }
if [ "$branch" != "feat/gardyn-tower-local" ]; then
    log "not on the production build branch; automatic update disabled"
    exit 0
fi
if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
    log "tracked files have local changes; update deferred"
    exit 1
fi
if [ ! -x "$INSTALL_DIR/venv/bin/python" ]; then
    log "runtime venv missing; run setup"
    exit 1
fi

if ! git fetch --quiet origin "$branch"; then
    log "git fetch failed (offline?); will retry next run"
    exit 0
fi

local_rev=$(git rev-parse HEAD)
remote_rev=$(git rev-parse "origin/$branch" 2>/dev/null) || { log "no origin/$branch; abort"; exit 0; }

if [ "$local_rev" = "$remote_rev" ] && [ ! -f "$INSTALL_DIR/.update-pending" ]; then
    log "already up to date ($branch @ ${local_rev:0:7})"
    exit 0
fi

log "update available on $branch: ${local_rev:0:7} -> ${remote_rev:0:7}"
if ! "$INSTALL_DIR/venv/bin/python" "$BIN_DIR/check-update.py" "$remote_rev" pushmotion/garden-of-eden; then
    exit 0
fi
if "$INSTALL_DIR/venv/bin/python" -m app.lib.cleaning_guard; then
    log "cleaning is active; update deferred"
    exit 0
fi

if ! git merge-base --is-ancestor "$local_rev" "$remote_rev"; then
    log "cannot fast-forward (branch diverged/force-pushed); skipping to stay safe"
    exit 0
fi

# Install candidate requirements before activating candidate code. On failure,
# leave HEAD unchanged so the next attempt retries instead of declaring success.
candidate_requirements=$(mktemp)
trap 'rm -f "$candidate_requirements"' EXIT
git show "$remote_rev:requirements.txt" > "$candidate_requirements"
if ! "$INSTALL_DIR/venv/bin/python" -m pip install --quiet -r "$candidate_requirements"; then
    log "dependency installation failed; code activation and restarts aborted"
    exit 1
fi
printf '%s\n' "$remote_rev" > "$INSTALL_DIR/.update-pending"
if ! git merge --ff-only --quiet "$remote_rev"; then
    log "git pull --ff-only failed; skipping"
    exit 0
fi
log "pulled to $(git rev-parse --short HEAD)"

# Restart only the installed services. This is reached only when code changed.
"$INSTALL_DIR/venv/bin/python" -c 'from app.sensors.schedule import schedule; schedule.refresh()'
for svc in mqtt.service garden-api.service; do
    if systemctl list-unit-files | grep -q "^${svc}"; then
        if ! sudo systemctl restart "$svc"; then
            log "restart failed for $svc; activation remains pending for retry"
            exit 1
        fi
        log "restarted $svc"
    fi
done
rm -f "$INSTALL_DIR/.update-pending"

log "update complete -> $(git rev-parse --short HEAD)"
