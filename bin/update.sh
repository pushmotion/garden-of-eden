#!/bin/bash
# Manual updates use the same exact-revision gate as the nightly updater.
set -euo pipefail
BIN_DIR=$(dirname "$(readlink -f "$0")")
exec bash "$BIN_DIR/autoupdate.sh"
