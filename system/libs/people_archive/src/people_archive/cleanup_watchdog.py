"""Recover the supervised CRM cleanup when it stops or becomes unresponsive."""
import json
import os
import signal
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import click

from people_archive.cleanup_models import CleanupError
from people_archive.settings import require_feature

PROGRESS_PATH = Path("data/.apps/crm/cleanup-progress.json")
SERVICE_NAME = "crm_cleanup"
STALE_AFTER = timedelta(minutes=25)
ACTIVE_STATES = frozenset({"RUNNING", "STARTING"})


class CleanupWatchdogError(CleanupError, RuntimeError):
    """The watchdog could not inspect or recover the CRM cleanup service."""


def watchdog_action(
    progress: dict[str, Any],
    service_state: str,
    now: datetime,
    *,
    stale_after: timedelta = STALE_AFTER,
) -> str:
    """Choose the smallest recovery action for the current saved state."""
    if progress.get("state") == "complete":
        return "complete"
    if service_state not in ACTIVE_STATES:
        return "start"
    updated_at = progress.get("updated_at")
    if not isinstance(updated_at, str):
        return "restart"
    updated = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
    if updated.tzinfo is None:
        updated = updated.replace(tzinfo=timezone.utc)
    return "restart" if now - updated > stale_after else "healthy"


def _service_state() -> str:
    completed = subprocess.run(
        ["supervisorctl", "status", SERVICE_NAME],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if completed.returncode and not completed.stdout:
        raise CleanupWatchdogError(completed.stderr.strip() or "Could not read CRM cleanup service state")
    fields = completed.stdout.split()
    if len(fields) < 2:
        raise CleanupWatchdogError("Could not parse CRM cleanup service state")
    return fields[1]


def _supervisor(action: str) -> None:
    completed = subprocess.run(
        ["supervisorctl", action, SERVICE_NAME],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if completed.returncode:
        raise CleanupWatchdogError(completed.stderr.strip() or completed.stdout.strip())


def crm_gpt_process_groups(process_listing: str) -> set[int]:
    """Find process groups belonging to temporary CRM GPT calls."""
    groups: set[int] = set()
    for line in process_listing.splitlines():
        fields = line.strip().split(maxsplit=2)
        if len(fields) != 3 or "/tmp/crm-gpt-" not in fields[2]:
            continue
        try:
            pid, group = int(fields[0]), int(fields[1])
        except ValueError:
            continue
        if pid != os.getpid() and group > 1:
            groups.add(group)
    return groups


def _terminate_leftover_gpt_processes() -> int:
    completed = subprocess.run(
        ["ps", "-axo", "pid=,pgid=,command="],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    groups = crm_gpt_process_groups(completed.stdout)
    for group in groups:
        try:
            os.killpg(group, signal.SIGKILL)
        except ProcessLookupError:
            pass
    return len(groups)


def main() -> None:
    require_feature("cleanup")
    progress = json.loads(PROGRESS_PATH.read_text()) if PROGRESS_PATH.exists() else {}
    service_state = _service_state()
    action = watchdog_action(progress, service_state, datetime.now(timezone.utc))
    if action in {"complete", "healthy"}:
        click.echo(f"crm-cleanup watchdog: {action}")
        return
    if action == "restart":
        _supervisor("stop")
    terminated = _terminate_leftover_gpt_processes()
    _supervisor("start")
    new_state = _service_state()
    if new_state not in ACTIVE_STATES:
        raise CleanupWatchdogError(f"CRM cleanup did not recover: {new_state}")
    click.echo(f"crm-cleanup watchdog: {action}ed service; removed {terminated} leftover GPT process groups")


if __name__ == "__main__":
    main()
