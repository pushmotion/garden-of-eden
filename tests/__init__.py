"""Test package bootstrap.

Installs hardware stubs before any ``app`` modules import real GPIO/I2C
libraries, so the suite runs off-Pi. Also ensures the repo root is importable.
"""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from . import _hwstub  # noqa: E402

_hwstub.install()
