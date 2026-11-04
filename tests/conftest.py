"""Test fixtures for the GUI shell.

The drcode user-config directory is resolved at IMPORT time (see
drcode.config), so the sandbox redirect must be in the environment
before any drcode import — done here at conftest import, before the
test modules (and through them the shell) are collected.
"""

from __future__ import annotations

import os
import tempfile

_SANDBOX = tempfile.mkdtemp(prefix="drcode-desktop-tests-")
os.environ["DRCODE_USER_CONFIG_DIR"] = os.path.join(_SANDBOX, "drcode-config")
os.environ["DRCODE_DESKTOP_CONFIG_DIR"] = os.path.join(_SANDBOX, "gui-config")
# The GUI tests must never open a real window: the offscreen platform
# provides a working event loop + timers without a display.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
