from pathlib import Path

from people_archive.resolve_lock import is_resolve_running, resolve_lock


def test_lock_is_seen_as_running_only_while_held(tmp_path: Path) -> None:
    store_path = tmp_path / "archive.sqlite"

    assert not is_resolve_running(store_path)
    with resolve_lock(store_path):
        assert is_resolve_running(store_path)
    assert not is_resolve_running(store_path)
