"""One identity rebuild at a time per archive.

The rebuild rewrites every derived people table, so two of them running at once fight over the
database. Every caller (the CLI, the CRM's background runs, the scheduled refresh) takes this lock
first and waits its turn.
"""

import fcntl
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


def resolve_lock_path(store_path: Path) -> Path:
    return store_path.with_name(store_path.name + ".resolve.lock")


@contextmanager
def resolve_lock(store_path: Path) -> Iterator[None]:
    """Hold the archive's rebuild lock, waiting for any run already in progress."""
    lock_path = resolve_lock_path(store_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def is_resolve_running(store_path: Path) -> bool:
    """Whether some process currently holds the rebuild lock."""
    lock_path = resolve_lock_path(store_path)
    if not lock_path.exists():
        return False
    with lock_path.open("w") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(handle, fcntl.LOCK_UN)
        return False
