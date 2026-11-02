import pytest

"""Cleanup progress exposes sources while treating records as untrusted text."""

import json
from pathlib import Path

from flask.testing import FlaskClient
from people_archive.cleanup_ledger import CleanupLedger


def test_cleanup_progress_before_first_run(client: FlaskClient) -> None:
    response = client.get("/cleanup")
    assert response.status_code == 200
    assert b"0 entries in this run" in response.data
    assert b"Changes are held for verification" in response.data
    assert client.get("/cleanup/person/missing").status_code == 404


def test_cleanup_findings_link_sources_and_escape_records(client: FlaskClient, seeded_data_dir: Path) -> None:
    ledger = CleanupLedger(seeded_data_dir / "cleanup.sqlite")
    ledger.inventory([{"id": "sample", "name": "Mira Vale", "handles": ["email:ada@example.com"]}])
    evidence = [{"kind": "email", "context": "Original sender", "source_url": "javascript:alert(1)", "text": "<script>alert(1)</script>", "truncated": False}]
    research = {"facts": [{"field": "title", "value": "Mathematician", "confidence": "high", "citations": [{"source_id": "https://example.org/ada", "quote": "Ada built the algorithm"}]}], "reason": "Source found"}
    ledger.connection.execute("UPDATE people SET evidence=?,extraction=?,research=?,status='researched'", (json.dumps(evidence), '{"facts": []}', json.dumps(research)))
    ledger.connection.commit()
    ledger.close()

    progress = client.get("/cleanup")
    assert b"1 entries in this run" in progress.data
    assert b"/cleanup/person/sample" in progress.data
    detail = client.get("/cleanup/person/sample")
    assert detail.status_code == 200
    assert b"Mathematician" in detail.data
    assert b'href="https://example.org/ada"' in detail.data
    assert b"&lt;script&gt;alert(1)&lt;/script&gt;" in detail.data
    assert b'href="javascript:' not in detail.data
    assert b"<script>alert(1)</script>" not in detail.data


def test_saved_cleanup_source_preserves_full_original(client: FlaskClient, seeded_data_dir: Path) -> None:
    ledger = CleanupLedger(seeded_data_dir / "cleanup.sqlite")
    ledger.save_evidence([{"id": "original", "kind": "email", "text": "Excerpt", "original_text": '{"body":"Full original <script>text</script>"}', "source_url": "https://example.org/message"}])
    ledger.close()
    response = client.get("/cleanup/source", query_string={"id": "original"})
    assert response.status_code == 200
    assert b"Full original &lt;script&gt;text&lt;/script&gt;" in response.data
    assert b'href="https://example.org/message"' in response.data
    assert client.get("/cleanup/source", query_string={"id": "missing"}).status_code == 404


@pytest.fixture(autouse=True)
def enabled_optional_cleanup(monkeypatch):
    monkeypatch.setenv("CRM_CLEANUP_ENABLED", "true")
