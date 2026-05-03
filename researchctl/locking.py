from __future__ import annotations

import os
import re
import time
from contextlib import contextmanager
from pathlib import Path

from .paths import aris_dir


class LockError(RuntimeError):
    pass


_HELD_LOCKS: set[Path] = set()


def _lock_path(root: Path, name: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", name).strip("_") or "state"
    return aris_dir(root) / ".runtime" / "locks" / f"{safe}.lock"


@contextmanager
def file_lock(root: Path, name: str, timeout: float = 10.0):
    path = _lock_path(root, name).resolve()
    if path in _HELD_LOCKS:
        yield
        return

    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    fd: int | None = None
    while fd is None:
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, f"pid={os.getpid()} ts={time.time()}\n".encode("utf-8"))
        except FileExistsError as exc:
            if time.monotonic() >= deadline:
                raise LockError(f"timed out waiting for lock: {path}") from exc
            time.sleep(0.05)

    _HELD_LOCKS.add(path)
    try:
        yield
    finally:
        _HELD_LOCKS.discard(path)
        if fd is not None:
            os.close(fd)
        try:
            path.unlink()
        except FileNotFoundError:
            pass
