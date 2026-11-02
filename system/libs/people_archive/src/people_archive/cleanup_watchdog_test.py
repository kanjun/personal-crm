from datetime import datetime, timedelta, timezone

from people_archive.cleanup_watchdog import crm_gpt_process_groups, watchdog_action

NOW = datetime(2026, 9, 23, 7, 0, tzinfo=timezone.utc)


def test_watchdog_leaves_active_cleanup_alone() -> None:
    progress = {"state": "running", "updated_at": (NOW - timedelta(minutes=20)).isoformat()}
    assert watchdog_action(progress, "RUNNING", NOW) == "healthy"


def test_watchdog_restarts_stale_cleanup() -> None:
    progress = {"state": "running", "updated_at": (NOW - timedelta(minutes=26)).isoformat()}
    assert watchdog_action(progress, "RUNNING", NOW) == "restart"


def test_watchdog_starts_safely_paused_cleanup() -> None:
    progress = {"state": "paused_error: provider unavailable", "updated_at": NOW.isoformat()}
    assert watchdog_action(progress, "EXITED", NOW) == "start"


def test_watchdog_does_not_restart_completed_cleanup() -> None:
    assert watchdog_action({"state": "complete"}, "EXITED", NOW) == "complete"


def test_watchdog_finds_only_crm_gpt_process_groups() -> None:
    listing = """
      10  10 codex exec -o /tmp/crm-gpt-one/answer.json
      11  10 /usr/bin/codex worker /tmp/crm-gpt-one/schema.json
      12  12 codex exec -o /tmp/other-job/answer.json
      bad data codex exec -o /tmp/crm-gpt-bad/answer.json
    """
    assert crm_gpt_process_groups(listing) == {10}
