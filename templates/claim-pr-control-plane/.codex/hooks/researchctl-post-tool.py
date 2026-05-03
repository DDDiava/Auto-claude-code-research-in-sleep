#!/usr/bin/env python3
from __future__ import annotations

import subprocess
import sys


def main() -> int:
    payload = sys.stdin.read()
    proc = subprocess.run(
        [sys.executable, "-m", "researchctl", "hook", "post-tool"],
        input=payload,
        text=True,
    )
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
