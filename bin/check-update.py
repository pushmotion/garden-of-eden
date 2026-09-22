#!/usr/bin/env python3
"""Allow only an explicitly approved SHA or a successful CI workflow for that SHA."""

import json
import os
import re
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import config  # noqa: F401 -- loads the operator's .env


def approved(sha, repository, opener=urllib.request.urlopen):
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        return False
    pinned = os.getenv("GARDEN_APPROVED_REV")
    if pinned:
        return sha == pinned
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        return False
    request = urllib.request.Request(
        f"https://api.github.com/repos/{repository}/actions/workflows/ci.yml/runs?head_sha={sha}&per_page=100",
        headers={"Accept": "application/vnd.github+json", "User-Agent": "garden-of-eden-updater"},
    )
    with opener(request, timeout=20) as response:
        runs = json.load(response).get("workflow_runs", [])
    runs = [run for run in runs if run.get("head_sha") == sha and run.get("event") == "push"]
    if not runs:
        return False
    latest = max(runs, key=lambda run: (run.get("run_number", 0), run.get("run_attempt", 0)))
    return latest.get("status") == "completed" and latest.get("conclusion") == "success"


if __name__ == "__main__":
    try:
        ok = approved(sys.argv[1], sys.argv[2])
    except Exception:
        ok = False
    print(
        "Update approved"
        if ok
        else "Update deferred: exact revision lacks successful CI or explicit approval"
    )
    sys.exit(0 if ok else 1)
