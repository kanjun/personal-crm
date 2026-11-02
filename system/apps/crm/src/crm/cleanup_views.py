"""Read-only progress and source evidence for the overnight cleanup."""

import json
import sqlite3
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from urllib.parse import urlsplit

from flask import Flask, abort, render_template, request


def register_cleanup_views(app: Flask, archive_path: Callable[[], Path]) -> None:
    """Expose saved work without triggering research or changing any decisions."""

    @app.get("/cleanup")
    def cleanup_progress() -> str:
        folder = archive_path().parent
        ledger = folder / "cleanup.sqlite"
        counts = {"people": {}, "pairs": {}, "decisions": {}}
        entries = []
        enabled = False
        if ledger.exists():
            with closing(sqlite3.connect(f"file:{ledger.resolve()}?mode=ro", uri=True)) as connection:
                counts["people"] = dict(connection.execute("SELECT status,COUNT(*) FROM people GROUP BY status"))
                counts["pairs"] = dict(connection.execute("SELECT status,COUNT(*) FROM pairs GROUP BY status"))
                counts["decisions"] = dict(connection.execute("SELECT kind,COUNT(*) FROM decisions WHERE active=1 GROUP BY kind"))
                enabled = connection.execute("SELECT value FROM metadata WHERE key='apply_enabled'").fetchone() == ("true",)
                entries = [{"id": row[0], "name": json.loads(row[1])["name"], "status": row[2]} for row in connection.execute(
                    "SELECT id,profile,status FROM people WHERE extraction IS NOT NULL ORDER BY research IS NOT NULL DESC,rowid DESC LIMIT 100"
                )]
        status_file = folder / "cleanup-progress.json"
        state = json.loads(status_file.read_text()).get("state", "Preparing") if status_file.exists() else "Preparing"
        return render_template("cleanup.html", counts=counts, entries=entries, state=state, enabled=enabled)

    @app.get("/cleanup/person/<person_id>")
    def cleanup_person(person_id: str) -> str:
        ledger = archive_path().parent / "cleanup.sqlite"
        if not ledger.exists():
            abort(404)
        with closing(sqlite3.connect(f"file:{ledger.resolve()}?mode=ro", uri=True)) as connection:
            row = connection.execute("SELECT profile,evidence,extraction,research FROM people WHERE id=?", (person_id,)).fetchone()
        if row is None:
            abort(404)
        profile = json.loads(row[0])
        evidence = json.loads(row[1] or "[]")
        extracted = json.loads(row[2] or "{}")
        researched = json.loads(row[3] or "{}")
        for item in evidence:
            item["source_url"] = _web_url(item.get("source_url", ""))
        for fact in researched.get("facts", []):
            for citation in fact.get("citations", []):
                citation["source_url"] = _web_url(citation["source_id"])
        return render_template("cleanup_person.html", person=profile, evidence=evidence, extracted=extracted, researched=researched)

    @app.get("/cleanup/source")
    def cleanup_source() -> str:
        ledger = archive_path().parent / "cleanup.sqlite"
        if not ledger.exists():
            abort(404)
        with closing(sqlite3.connect(f"file:{ledger.resolve()}?mode=ro", uri=True)) as connection:
            row = connection.execute("SELECT source FROM evidence WHERE id=?", (request.args.get("id", ""),)).fetchone()
        if row is None:
            abort(404)
        source = json.loads(row[0])
        original = source.get("original_text", source.get("text", ""))
        try:
            original = json.dumps(json.loads(original), ensure_ascii=False, indent=2)
        except json.JSONDecodeError:
            pass
        return render_template("cleanup_source.html", source=source, original=original, source_url=_web_url(source.get("source_url", "")))


def _web_url(value: str) -> str:
    parsed = urlsplit(value)
    return value if parsed.scheme in {"http", "https"} and parsed.netloc else ""
