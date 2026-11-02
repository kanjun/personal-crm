from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest
from flask.testing import FlaskClient
from people_archive.data_types import Direction
from people_archive.identity import resolve_and_store_people
from people_archive.store import open_archive_store
from people_archive.testing import (
    WORK_ACCOUNT,
    make_contact_card,
    make_parsed_email,
    utc,
)

from crm.runner import app


@pytest.fixture
def seeded_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A data dir holding a small archive: one close person, one robot, one meeting."""
    data_dir = tmp_path / f"crm-{uuid4().hex}"
    data_dir.mkdir()
    # Keep identity resolution's relative contact and assertion paths isolated too.
    monkeypatch.chdir(tmp_path)
    with open_archive_store(data_dir / "archive.sqlite") as store:
        with store.transaction():
            for idx in range(8):
                store.upsert_message(make_parsed_email(f"s{idx}", "owner@example.com", ("ada@example.com",), Direction.SENT, utc(2026, 1, idx + 1), f"Engine plan {idx}", "about the difference engine", False, ""))
            store.upsert_message(make_parsed_email("r1", "ada@example.com", ("owner@example.com",), Direction.RECEIVED, utc(2026, 2, 1), "Re: Engine plan", "sounds good", False, "Mira Vale"))
            store.upsert_message(make_parsed_email("n1", "no-reply@shop.com", ("owner@example.com",), Direction.RECEIVED, utc(2026, 2, 2), "Receipt", "thanks", True, "Shop"))
            store.upsert_contact(make_contact_card("people/1", "Mira Vale", ("ada@example.com",), ("+15550100001",), "Example Workshop"))
        resolve_and_store_people(store, [WORK_ACCOUNT])
    monkeypatch.setenv("CRM_DATA_DIR", str(data_dir))
    return data_dir


@pytest.fixture
def client(seeded_data_dir: Path) -> Iterator[FlaskClient]:
    app.config["TESTING"] = True
    with app.test_client() as test_client:
        yield test_client
