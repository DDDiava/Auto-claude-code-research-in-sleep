#!/usr/bin/env python3
from __future__ import annotations

import subprocess
import sys


if __name__ == "__main__":
    raise SystemExit(
        subprocess.run(
        [sys.executable, "-m", "researchctl", "hook", "pre-tool", "--platform", "claude"],
            input=sys.stdin.read(),
            text=True,
        ).returncode
    )
