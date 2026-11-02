"""Keep saved contact URLs usable after a reversible cleanup merge."""

import sqlite3
from collections.abc import Callable
from contextlib import closing
from pathlib import Path

from flask import Flask, Response, redirect, request, url_for
from people_archive.cleanup_ledger import resolve_alias
from people_archive.settings import load_settings


def register_cleanup_links(app: Flask, archive_path: Callable[[], Path]) -> None:
    """Redirect absorbed contact IDs while preserving queries and submitted edits."""

    @app.before_request
    def redirect_merged_contact() -> Response | None:
        if not load_settings().cleanup_enabled:
            return None
        if request.endpoint not in {"person_page", "person_edit", "evidence_panel"}:
            return None
        arguments = request.view_args or {}
        person_id = arguments.get("person_id", "")
        if not person_id:
            return None
        path = archive_path()
        target = resolve_alias(person_id, path.parent / "cleanup.sqlite")
        if target == person_id:
            return None
        with closing(sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)) as connection:
            # A restored original ID wins over an old redirect. Never send the user
            # to a target that a concurrent identity refresh has not published yet.
            original = connection.execute("SELECT 1 FROM persons WHERE person_id=?", (person_id,)).fetchone()
            destination = connection.execute("SELECT 1 FROM persons WHERE person_id=?", (target,)).fetchone()
        if original or not destination:
            return None
        location = url_for(request.endpoint, **{**arguments, "person_id": target})
        if request.query_string:
            location += "?" + request.query_string.decode("ascii")
        return redirect(location, code=307)
