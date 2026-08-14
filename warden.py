#!/usr/bin/env python3
"""Launcher for the n8n_warden package in ./src.

Kept so `./warden.py ...` works from a source checkout. For servers, build the
single-file bundle instead:

    ./build.sh          # produces n8n-warden.pyz
    scp n8n-warden.pyz you@host:
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from n8n_warden.cli import run  # noqa: E402

if __name__ == "__main__":
    sys.exit(run())
