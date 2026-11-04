"""PyInstaller entry script for the frozen GUI shell.

A tiny module-level launcher (no relative imports — the frozen entry
point is analyzed as a plain script): boots the same ``main`` the
``drcode-desktop`` console script runs.
"""

from __future__ import annotations

import sys

from drcodedesktop.app import main

if __name__ == "__main__":
    sys.exit(main())
