import pytest

"""Saved URLs survive merges, including an edit submitted from an older page."""

import sqlite3
from contextlib import closing
from pathlib import Path

from flask.testing import FlaskClient
from people_archive.cleanup_ledger import CleanupLedger


def test_merged_contact_links_preserve_query_and_post(client: FlaskClient, seeded_data_dir: Path) -> None:
    with closing(sqlite3.connect(seeded_data_dir / "archive.sqlite")) as connection:
        target = connection.execute("SELECT person_id FROM persons WHERE canonical_name='Mira Vale'").fetchone()[0]
    ledger = CleanupLedger(seeded_data_dir / "cleanup.sqlite")
    ledger.connection.execute("INSERT INTO aliases VALUES (?,?)", ("absorbed", target))
    ledger.connection.commit()
    ledger.close()

    response = client.get("/person/absorbed?queued=same")
    assert response.status_code == 307
    assert response.location == f"/person/{target}?queued=same"
    assert client.get("/person/absorbed", follow_redirects=True).status_code == 200
    evidence = client.get("/evidence/absorbed?kind=email&month=2026-02")
    assert evidence.location == f"/evidence/{target}?kind=email&month=2026-02"
    saved = client.post("/person/absorbed/edit", data={"field": "notes", "value": "Remember the engine demo"}, follow_redirects=True)
    assert saved.status_code == 200
    with closing(sqlite3.connect(seeded_data_dir / "archive.sqlite")) as connection:
        assert connection.execute("SELECT value FROM person_edits WHERE person_id=? AND field='notes'", (target,)).fetchone() == ("Remember the engine demo",)


def test_existing_contact_and_missing_target_do_not_redirect(client: FlaskClient, seeded_data_dir: Path) -> None:
    with closing(sqlite3.connect(seeded_data_dir / "archive.sqlite")) as connection:
        target = connection.execute("SELECT person_id FROM persons WHERE canonical_name='Mira Vale'").fetchone()[0]
    ledger = CleanupLedger(seeded_data_dir / "cleanup.sqlite")
    ledger.connection.executemany("INSERT INTO aliases VALUES (?,?)", [(target, "absent"), ("old", "absent")])
    ledger.connection.commit()
    ledger.close()

    assert client.get(f"/person/{target}").status_code == 200
    assert client.get("/person/old").status_code == 404


@pytest.fixture(autouse=True)
def enabled_optional_cleanup(monkeypatch):
    monkeypatch.setenv("CRM_CLEANUP_ENABLED", "true")
