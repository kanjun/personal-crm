"""The CRM: people, their history, and search across every archived source.

Services run from /home/user/workspace (the repo root). Conventions:

- Persistent state (anything written and read across runs -- cursors,
  caches, snapshots, user records): read and write it under ``DATA_DIR``
  (defined below), never a hardcoded ``data/.apps/crm/`` at the
  call site. ``DATA_DIR`` defaults to ``data/.apps/crm/`` but
  honors the ``CRM_DATA_DIR`` env var, so an editing agent can point a
  throwaway instance at a *copy* of the data instead of the live store
  (see the update-app skill). Do NOT use ``Path(__file__)``-based
  paths for state.
- Static assets shipped alongside this file (templates, default
  configs, bundled JSON): ``Path(__file__).parent / "assets/..."`` is
  fine and is the right pattern.
- Listen port: bind ``PORT`` (defined below), which defaults to this
  app's assigned port but honors the ``CRM_PORT`` env var, so
  an editing agent can boot a throwaway instance on a *spare* port
  alongside the live one (see the update-app skill). Never hardcode
  the port at the ``run_simple`` call.

This is a synchronous Flask app served by the threaded Werkzeug server. The app
owns its own browser origin (the forwarder routes ``http://crm.<workspace-host>/``
straight to this port), so it serves at ``/`` and root-absolute URLs work unmodified.

The data behind every page is the people-archive SQLite store (``archive.sqlite``
under DATA_DIR), which the ``people-archive`` CLI fills and the identity pass
derives the people tables in. This app only reads it.
"""

import json
import os
import re
import subprocess
import threading
import time
import urllib.parse
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Final

from flask import Flask, Response, abort, redirect, render_template, request
from markupsafe import escape
from people_archive.assertions import (
    Decision,
    append_assertion,
    assertions_path,
    make_assertion,
)
from people_archive.primitives import PersonId
from people_archive.resolve_lock import is_resolve_running
from people_archive.settings import display_timezone, load_settings
from people_archive.store import (
    EDITABLE_FIELDS,
    ArchiveStore,
    open_archive_store,
)
from werkzeug.serving import run_simple

from crm.cleanup_links import register_cleanup_links
from crm.cleanup_views import register_cleanup_views
from crm.queries import (
    GROUP_BY_OPTIONS,
    LAUNCH_LIST_NAME,
    HeaderCounts,
    PeopleListFilters,
    build_launch_list,
    chat_messages_for,
    evidence_for_person,
    get_person,
    get_raw_record,
    handles_of,
    header_counts,
    list_merge_candidates,
    list_people,
    search_everything,
)

# Persistent state for this app lives under DATA_DIR. It defaults to
# ``data/.apps/crm/`` but is overridable via the ``CRM_DATA_DIR`` env var
# so a throwaway instance can run against a *copy* of the data while editing --
# see the update-app skill.
DATA_DIR = Path(os.environ.get("CRM_DATA_DIR", "data/.apps/crm"))
_ARCHIVE_FILENAME: Final[str] = "archive.sqlite"


def archive_path() -> Path:
    """The archive this process reads; resolved per request so a test can point it elsewhere."""
    return Path(os.environ.get("CRM_DATA_DIR", str(DATA_DIR))) / _ARCHIVE_FILENAME

# Listen port. Defaults to this app's assigned port but is overridable via
# the ``CRM_PORT`` env var so an editing agent can boot a throwaway
# instance on a spare port next to the live one (see the update-app skill).
PORT = int(os.environ.get("CRM_PORT", "8091"))

ASSETS = Path(__file__).parent / "assets"

# One connection per archive, kept open across requests: opening a fresh one per click applied the
# schema and started with an empty page cache, so every tab re-read the people tables from disk.
# Requests take turns on it; a page's queries are milliseconds once the cache is warm.
# A search for a common word touches more than the library's default cache holds, so the shared
# connection gets twice that and a second search is served from memory.
SHARED_STORE_CACHE_KIB: Final[int] = 65536
_store_lock = threading.Lock()
_stores: dict[Path, ArchiveStore] = {}


@contextmanager
def archive_store() -> Iterator[ArchiveStore]:
    """The shared, warm connection to the archive this request reads (and, for an edit, writes)."""
    path = archive_path()
    with _store_lock:
        store = _stores.get(path)
        if store is None:
            store = ArchiveStore(path=path)
            store.open(cache_kib=SHARED_STORE_CACHE_KIB)
            _stores[path] = store
        yield store


# The header's counts are read from indexes, but on a cold disk cache that is still seconds of
# reading, so they are computed at most this often and a stale value is served while a
# background thread refreshes it.
STATUS_TTL_SECONDS: Final[float] = 3600.0
_status_lock = threading.Lock()
_status_cache: dict[Path, tuple[HeaderCounts, float]] = {}
_status_refreshing: set[Path] = set()


def _compute_status(path: Path) -> None:
    try:
        with open_archive_store(path) as store:
            counts = header_counts(store)
        with _status_lock:
            _status_cache[path] = (counts, time.monotonic())
    finally:
        with _status_lock:
            _status_refreshing.discard(path)


def archive_status(path: Path) -> HeaderCounts:
    """The header counts: fresh within STATUS_TTL_SECONDS, else the last value while a refresh runs."""
    with _status_lock:
        cached = _status_cache.get(path)
        fresh = cached is not None and time.monotonic() - cached[1] < STATUS_TTL_SECONDS
        start_refresh = cached is not None and not fresh and path not in _status_refreshing
        if start_refresh:
            _status_refreshing.add(path)
    if start_refresh:
        threading.Thread(target=_compute_status, args=(path,), name="crm-status-refresh", daemon=True).start()
    if cached is not None:
        return cached[0]
    with _status_lock:
        _status_refreshing.add(path)
    _compute_status(path)
    with _status_lock:
        return _status_cache[path][0]


app = Flask("crm", static_folder=str(ASSETS / "static"), static_url_path="/static", template_folder=str(ASSETS / "templates"))
register_cleanup_links(app, archive_path)
register_cleanup_views(app, archive_path)

@app.context_processor
def crm_settings():
    return {"cleanup_enabled": load_settings().cleanup_enabled}

@app.before_request
def hide_optional_cleanup():
    if request.path.startswith("/cleanup") and not load_settings().cleanup_enabled:
        abort(404)

@app.template_filter("localtime")
def localtime(value):
    return value.astimezone(display_timezone()) if value is not None else value



@app.template_filter("gmail_url")
def gmail_url(account: str, thread_id: str) -> str:
    # Gmail resolves a thread id in the #all anchor; ?authuser selects the account.
    return f"https://mail.google.com/mail/u/0/?authuser={urllib.parse.quote(account, safe='')}#all/{thread_id}"


# A bullet's evidence is its trailing parenthetical, e.g. "(2023-08 note)" or "(2026-06/07 emails, meeting)".
_CITATION_PATTERN = re.compile(r"\(([^()]*\b(?:19|20)\d{2}[^()]*)\)\s*$")
_CITATION_KIND_PATTERN = re.compile(r"\b(email|note|meeting|text|call)s?\b", re.IGNORECASE)


@app.template_filter("cite")
def cite(value: str) -> str:
    """Escape a bullet and turn its trailing (date kind) citation into a small colored chip."""
    escaped = str(escape(value))

    def chip(match: re.Match[str]) -> str:
        kind_match = _CITATION_KIND_PATTERN.search(match.group(1))
        kind = kind_match.group(1).lower() if kind_match else "other"
        month_match = re.search(r"(?:19|20)\d{2}(?:-\d{2})?", match.group(1))
        month = month_match.group(0) if month_match else ""
        return f'<span class="cite cite--{kind}" data-kind="{kind}" data-month="{month}" role="button" tabindex="0">{match.group(1)}</span>'

    return _CITATION_PATTERN.sub(chip, escaped)


@app.template_filter("when")
def when(value: object) -> str:
    return value.astimezone(display_timezone()).strftime("%Y-%m-%d") if hasattr(value, "astimezone") else ""


@app.route("/")
def people_page() -> str:
    tier_raw = request.args.get("tier", "")
    filters = PeopleListFilters(
        query=request.args.get("q", "").strip(),
        tier=int(tier_raw) if tier_raw in ("1", "2", "3") else None,
        group=request.args.get("group", "") if request.args.get("group", "") in ("colleagues", "network") else "",
        sort="recent" if request.args.get("sort", "") == "recent" else "score",
        page=max(0, int(request.args.get("page", "0") or 0)),
        group_by=request.args.get("by", "") if request.args.get("by", "") in GROUP_BY_OPTIONS else "",
        group_value=request.args.get("in", "").strip(),
    )
    with archive_store() as store:
        page = list_people(store, filters)
    status = archive_status(archive_path())
    return render_template("people.html", page=page, filters=filters, status=status, group_by_options=GROUP_BY_OPTIONS)


@app.route("/person/<person_id>")
def person_page(person_id: str) -> str:
    with archive_store() as store:
        detail = get_person(store, person_id)
    if detail is None:
        abort(404)
    return render_template("person.html", detail=detail)


@app.route("/person/<person_id>/chat")
def person_chat(person_id: str) -> str:
    chat_id = request.args.get("chat_id", "")
    if not chat_id:
        abort(404)
    with archive_store() as store:
        messages = chat_messages_for(store, person_id, chat_id)
    if messages is None:
        abort(404)
    return render_template("_chat_messages.html", messages=messages)


@app.route("/social-source/<source_id>")
def social_source(source_id: str) -> Response:
    """Show the preserved provider response behind one social profile observation."""
    with archive_store() as store:
        source = store.get_social_source(source_id)
    if source is None:
        abort(404)
    return Response(json.dumps(source, ensure_ascii=False, indent=2), content_type="application/json; charset=utf-8")


@app.route("/person/<person_id>/edit", methods=["POST"])
def person_edit(person_id: str) -> Response:
    """Save one field the user typed on the page; an empty value goes back to the model's or the sources' text."""
    field = request.form.get("field", "")
    if field not in EDITABLE_FIELDS:
        abort(400)
    with archive_store() as store:
        if not store.execute_read("SELECT 1 FROM persons WHERE person_id = ?", [person_id]):
            abort(404)
        store.set_person_edit(PersonId(person_id), field, request.form.get("value", ""))
    return Response('{"ok": true}', mimetype="application/json")


@app.route("/search")
def search_page() -> str:
    query = request.args.get("q", "").strip()
    hits = []
    if query:
        with archive_store() as store:
            hits = search_everything(store, query)
    return render_template("search.html", query=query, hits=hits)


@app.route("/record/<source>/<account>/<record_id>")
def record_page(source: str, account: str, record_id: str) -> str:
    with archive_store() as store:
        view = get_raw_record(store, source, account, record_id)
    if view is None:
        abort(404)
    return render_template("record.html", view=view)


# The identity pass that applies a decision runs in the background; the page shows it as queued.
def _resolve_pending_marker() -> Path:
    return archive_path().parent / "resolve.pending"


def _queue_resolve() -> None:
    # Tests and throwaway instances set this so a click does not spawn a rebuild of the live archive.
    if os.environ.get("CRM_SKIP_BACKGROUND_RESOLVE"):
        return
    if _resolve_pending_marker().exists():
        return
    resolve_log = archive_path().parent / "logs/resolve.log"
    resolve_log.parent.mkdir(parents=True, exist_ok=True)
    _resolve_pending_marker().touch()
    command = ["uv", "run", "--no-sync", "people-archive", "resolve", "--store", str(archive_path()),
               *[part for account in load_settings().accounts for part in ("--account", account)],
               "--claim-marker", str(_resolve_pending_marker())]
    with resolve_log.open("a") as log:
        subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, cwd=Path.cwd(), start_new_session=True)


def _resolve_state() -> str:
    """'queued' when a rebuild is waiting to start, 'running' while one is rewriting the people tables, else ''."""
    if _resolve_pending_marker().exists():
        return "queued"
    return "running" if is_resolve_running(archive_path()) else ""


@app.route("/merge", methods=["POST"])
def merge_decision() -> Response:
    """Record that two people are the same (or different) and rebuild identity in the background."""
    person_a = request.form.get("a", "")
    person_b = request.form.get("b", "")
    decision_raw = request.form.get("decision", "")
    if not person_a or not person_b or decision_raw not in ("same", "different"):
        abort(400)
    with archive_store() as store:
        handles_a = handles_of(store, person_a)
        handles_b = handles_of(store, person_b)
    if not handles_a or not handles_b:
        abort(404)
    decision = Decision.SAME if decision_raw == "same" else Decision.DIFFERENT
    append_assertion(assertions_path(), make_assertion(decision, handles_a, handles_b, note=request.form.get("note", "")))
    _queue_resolve()
    back_to = request.form.get("back", f"/person/{person_a}")
    return redirect(f"{back_to}{'&' if '?' in back_to else '?'}queued={decision_raw}")


@app.route("/lists/outreach")
def launch_list_page() -> str:
    with archive_store() as store:
        launch = build_launch_list(store)
    return render_template("launch.html", launch=launch)


@app.route("/lists/outreach/decide", methods=["POST"])
def launch_list_decide() -> Response:
    person_id = request.form.get("person_id", "")
    decision = request.form.get("decision", "")
    if not person_id or decision not in ("include", "exclude", ""):
        abort(400)
    with archive_store() as store:
        store.set_list_decision(LAUNCH_LIST_NAME, PersonId(person_id), decision)
    return Response('{"ok": true}', mimetype="application/json")


@app.route("/lists/outreach.csv")
def launch_list_csv() -> Response:
    """Everyone marked include, as a spreadsheet: name, email, X, group, why."""
    with archive_store() as store:
        launch = build_launch_list(store)
    lines = ["name,email,x,organization,group,why"]
    for _key, label, people in launch.groups:
        for person in people:
            if person.decision == "include":
                cells = [person.name, person.primary_email, person.x_username, person.organization, label, person.why]
                lines.append(",".join('"' + cell.replace('"', '""') + '"' for cell in cells))
    return Response("\n".join(lines) + "\n", mimetype="text/csv", headers={"Content-Disposition": "attachment; filename=outreach.csv"})


@app.route("/evidence/<person_id>")
def evidence_panel(person_id: str) -> str:
    """HTML fragment for the side panel: the records behind a citation (kind + month) for one person."""
    kind = request.args.get("kind", "email")
    month = request.args.get("month", "")
    with archive_store() as store:
        items = evidence_for_person(store, person_id, month, kind if kind in ("email", "text", "meeting", "note") else "email", 12)
    return render_template("_evidence.html", items=items, kind=kind, month=month)


@app.route("/merges")
def merges_page() -> str:
    with archive_store() as store:
        review = list_merge_candidates(store)
    return render_template("merges.html", review=review, pairs=review.pairs, resolve_state=_resolve_state())


@app.route("/health")
def health() -> Response:
    return Response('{"status": "ok"}', mimetype="application/json")


def _warm_up() -> None:
    """Fill the shared connection's cache and the header counts so the first click is as quick as the rest."""
    with archive_store() as store:
        list_people(store, PeopleListFilters(query="", tier=None, group="", sort="score", page=0))
    archive_status(archive_path())


def main() -> None:
    threading.Thread(target=_warm_up, name="crm-warm-up", daemon=True).start()
    run_simple("127.0.0.1", PORT, app, threaded=True, use_reloader=False, use_debugger=False)


if __name__ == "__main__":
    main()
