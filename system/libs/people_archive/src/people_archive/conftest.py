from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest

from people_archive.store import ArchiveStore, open_archive_store


@pytest.fixture
def archive_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[ArchiveStore]:
    # Relative contact and assertion paths must belong to the test, not the workspace.
    monkeypatch.chdir(tmp_path)
    with open_archive_store(tmp_path / f"archive-{uuid4().hex}.sqlite") as store:
        yield store
