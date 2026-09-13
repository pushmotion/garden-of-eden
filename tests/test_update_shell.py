"""Exercise updater failure paths with local commands; never contact a tower."""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


@unittest.skipUnless(os.name == "posix" and shutil.which("bash"), "requires bash")
class UpdateShellTests(unittest.TestCase):
    def test_failed_ci_or_dependencies_cannot_activate_or_restart(self):
        source = Path(__file__).resolve().parents[1] / "bin/autoupdate.sh"
        for gate in (False, True):
            with self.subTest(ci_passed=gate), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / "bin").mkdir()
                (root / "venv/bin").mkdir(parents=True)
                (root / "fake").mkdir()
                script = root / "bin/autoupdate.sh"
                script.write_text(source.read_text())
                commands = {
                    "fake/git": """#!/bin/bash
echo "git $*" >> "$TRACE"
case "$*" in
  'rev-parse --abbrev-ref HEAD') echo feat/gardyn-tower-local ;;
  'rev-parse HEAD') printf '%040d\\n' 1 ;;
  'rev-parse origin/feat/gardyn-tower-local') printf '%040d\\n' 2 ;;
  status*|fetch*|merge-base*) exit 0 ;;
  show*) echo 'example-package==1' ;;
  *) exit 99 ;;
esac
""",
                    "venv/bin/python": """#!/bin/bash
echo "python $*" >> "$TRACE"
case "$*" in
  *check-update.py*) exit "$GATE" ;;
  '-m app.lib.cleaning_guard') exit 1 ;;
  '-m pip '*) exit 1 ;;
  *) exit 99 ;;
esac
""",
                    "fake/systemctl": '#!/bin/bash\necho systemctl >> "$TRACE"\nexit 99\n',
                    "fake/sudo": '#!/bin/bash\necho sudo >> "$TRACE"\nexit 99\n',
                }
                for relative, body in commands.items():
                    path = root / relative
                    path.write_text(body)
                    path.chmod(0o755)
                trace = root / "trace"
                env = dict(
                    os.environ,
                    PATH=str(root / "fake") + ":" + os.environ["PATH"],
                    TRACE=str(trace),
                    GATE="0" if gate else "1",
                )
                result = subprocess.run(
                    ["bash", str(script)], env=env, capture_output=True, text=True
                )
                self.assertEqual(result.returncode, 1 if gate else 0, result.stderr)
                calls = trace.read_text()
                self.assertNotIn("git merge --ff-only", calls)
                self.assertNotIn("systemctl", calls)
                self.assertNotIn("sudo", calls)
                self.assertEqual("-m pip install" in calls, gate)
                self.assertFalse((root / ".update-pending").exists())
