"""Independent, reversible machine decisions. Never writes the raw archive or user assertions."""
import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from people_archive.cleanup_models import Citation


def ledger_path() -> Path:
    return Path(os.environ.get("CRM_DATA_DIR", "data/.apps/crm")) / "cleanup.sqlite"


class CleanupLedger:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path, timeout=30)
        self.connection.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS people(id TEXT PRIMARY KEY, profile TEXT NOT NULL, evidence TEXT,
          extraction TEXT, research TEXT, status TEXT NOT NULL DEFAULT 'pending');
        CREATE TABLE IF NOT EXISTS pairs(a TEXT,b TEXT, judgment TEXT, status TEXT NOT NULL DEFAULT 'pending',
          PRIMARY KEY(a,b));
        CREATE TABLE IF NOT EXISTS calls(id INTEGER PRIMARY KEY, phase TEXT, item TEXT, usage TEXT, events TEXT);
        CREATE TABLE IF NOT EXISTS decisions(id INTEGER PRIMARY KEY, kind TEXT, handles_a TEXT, handles_b TEXT,
          value TEXT, reason TEXT, active INTEGER NOT NULL DEFAULT 1);
        CREATE TABLE IF NOT EXISTS aliases(old_id TEXT PRIMARY KEY, new_id TEXT);
        CREATE TABLE IF NOT EXISTS evidence(id TEXT PRIMARY KEY, source TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT);
        """)
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    def inventory(self, people: list[dict[str, Any]]) -> None:
        if self.connection.execute("SELECT value FROM metadata WHERE key='inventory_initialized'").fetchone():
            return
        self.connection.executemany("INSERT OR IGNORE INTO people(id,profile) VALUES (?,?)",
                                    [(p["id"], json.dumps(p)) for p in people])
        self.connection.execute("INSERT INTO metadata VALUES ('inventory_initialized','true')")
        self.connection.commit()

    def profiles(self) -> dict[str, dict[str, Any]]:
        return {row[0]: json.loads(row[1]) for row in self.connection.execute("SELECT id,profile FROM people")}

    def save_evidence(self, items: list[dict[str, Any]]) -> None:
        self.connection.executemany("INSERT OR REPLACE INTO evidence VALUES (?,?)", [(item["id"],json.dumps(item)) for item in items])
        self.connection.commit()

    def save_call(self, phase: str, item: str, usage: dict[str, int], events: str) -> None:
        self.connection.execute("INSERT INTO calls(phase,item,usage,events) VALUES (?,?,?,?)", (phase,item,json.dumps(usage),events))
        self.connection.commit()

    def decision(self, kind: str, a: list[str], b: list[str], value: str, reason: str) -> None:
        self.connection.execute("INSERT INTO decisions(kind,handles_a,handles_b,value,reason) VALUES (?,?,?,?,?)",
                                (kind,json.dumps(a),json.dumps(b),value,reason))

    def progress(self) -> dict[str, Any]:
        people = dict(self.connection.execute("SELECT status,COUNT(*) FROM people GROUP BY status"))
        pairs = dict(self.connection.execute("SELECT status,COUNT(*) FROM pairs GROUP BY status"))
        decisions = dict(self.connection.execute("SELECT kind,COUNT(*) FROM decisions WHERE active=1 GROUP BY kind"))
        usage: dict[str,int] = {}
        for (raw,) in self.connection.execute("SELECT usage FROM calls"):
            for key, value in json.loads(raw).items():
                usage[key] = usage.get(key,0) + value
        return {"people":people,"pairs":pairs,"accepted_decisions":decisions,"usage":usage,"dollar_cost":None}


def valid_citations(citations: list[Citation], evidence: list[dict[str, Any]]) -> bool:
    by_id = {item["id"]: item["text"] for item in evidence}
    return bool(citations) and all(c.source_id in by_id and " ".join(c.quote.split()) in " ".join(by_id[c.source_id].split()) for c in citations)


def load_overlay(path: Path | None = None) -> list[dict[str, Any]]:
    target = path or ledger_path()
    if not target.exists():
        return []
    with closing(sqlite3.connect(f"file:{target.resolve()}?mode=ro", uri=True)) as connection:
        enabled = connection.execute("SELECT value FROM metadata WHERE key='apply_enabled'").fetchone()
        if enabled is None or enabled[0] != "true":
            return []
        return [{"id":r[0],"kind":r[1],"a":json.loads(r[2]),"b":json.loads(r[3]),"value":r[4],"reason":r[5]}
                for r in connection.execute("SELECT id,kind,handles_a,handles_b,value,reason FROM decisions WHERE active=1 ORDER BY id")]


def resolve_alias(person_id: str, path: Path | None = None) -> str:
    target = path or ledger_path()
    if not target.exists():
        return person_id
    with closing(sqlite3.connect(f"file:{target.resolve()}?mode=ro", uri=True)) as connection:
        seen: set[str] = set()
        current = person_id
        while current not in seen:
            seen.add(current)
            row = connection.execute("SELECT new_id FROM aliases WHERE old_id=?", (current,)).fetchone()
            if row is None:
                return current
            current = row[0]
        return person_id


def save_aliases(existing: dict[str,str], resolved: dict[str,str], path: Path | None = None) -> None:
    target = path or ledger_path()
    if not target.exists():
        return
    destinations: dict[str,set[str]] = {}
    for handle, old_id in existing.items():
        if handle in resolved:
            destinations.setdefault(old_id,set()).add(resolved[handle])
    with closing(sqlite3.connect(target, timeout=30)) as connection:
        for old_id, ids in destinations.items():
            if len(ids) == 1 and old_id not in ids:
                connection.execute("INSERT OR REPLACE INTO aliases VALUES (?,?)", (old_id,next(iter(ids))))
            else:
                connection.execute("DELETE FROM aliases WHERE old_id=?", (old_id,))

        connection.commit()


def original_ids_for_undo(path: Path) -> dict[str,str]:
    if not path.exists():
        return {}
    with closing(sqlite3.connect(f"file:{path.resolve()}?mode=ro",uri=True)) as connection:
        enabled = connection.execute("SELECT value FROM metadata WHERE key='restore_original_ids'").fetchone()
        if enabled is None or enabled[0] != "true":
            return {}
        return {handle:row[0] for row in connection.execute("SELECT id,profile FROM people") for handle in json.loads(row[1])["handles"]}
