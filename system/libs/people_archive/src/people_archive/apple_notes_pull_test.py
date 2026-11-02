import gzip
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path

from people_archive.apple_notes_pull import EssaySelection, essay_notes, note_body_text


def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _field(number: int, payload: bytes) -> bytes:
    return _varint(number << 3 | 2) + _varint(len(payload)) + payload


def _body(text: str) -> bytes:
    """Apple's layout: Document(field 2) > Note(field 3) > text(field 2), gzipped."""
    note = _varint(1 << 3 | 0) + _varint(0) + _field(2, text.encode())
    return gzip.compress(_field(2, _varint(1 << 3 | 0) + _varint(0) + _field(3, note)))


def _apple_time(day: date) -> float:
    return datetime(day.year, day.month, day.day, 12, tzinfo=timezone.utc).timestamp() - 978307200


def _store(path: Path) -> Path:
    db = path / "NoteStore.sqlite"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE ZICCLOUDSYNCINGOBJECT (Z_PK INTEGER, ZTITLE1 TEXT, ZTITLE2 TEXT, ZFOLDER INTEGER, ZMODIFICATIONDATE1 REAL, ZMARKEDFORDELETION INTEGER)")
    c.execute("CREATE TABLE ZICNOTEDATA (ZNOTE INTEGER, ZDATA BLOB)")
    c.executemany("INSERT INTO ZICCLOUDSYNCINGOBJECT (Z_PK, ZTITLE2) VALUES (?, ?)", [(3, "Notes"), (4, "Personal")])
    notes = [
        (10, "Luddites", 3, _apple_time(date(2026, 8, 8)), 0, "Luddites\nThe reason digital technology feels bad"),
        (11, "Itemized list", 3, _apple_time(date(2026, 10, 18)), 0, "Account balance"),
        (12, "Old essay", 3, _apple_time(date(2025, 2, 1)), 0, "too old"),
        (13, "Private", 4, _apple_time(date(2026, 8, 9)), 0, "other folder"),
        (14, "Deleted", 3, _apple_time(date(2026, 8, 10)), 1, "in the bin"),
    ]
    for pk, title, folder, modified, deleted, text in notes:
        c.execute(
            "INSERT INTO ZICCLOUDSYNCINGOBJECT (Z_PK, ZTITLE1, ZFOLDER, ZMODIFICATIONDATE1, ZMARKEDFORDELETION) VALUES (?, ?, ?, ?, ?)",
            (pk, title, folder, modified, deleted),
        )
        c.execute("INSERT INTO ZICNOTEDATA VALUES (?, ?)", (pk, _body(text)))
    c.commit()
    c.close()
    return db


def test_body_text_reads_apple_layout() -> None:
    assert note_body_text(_body("Hello\nworld")) == "Hello\nworld"


def test_selection_keeps_recent_essays_in_the_folder_only(tmp_path: Path) -> None:
    selection = EssaySelection(device="d", folder="Notes", since=date(2026, 8, 1), exclude_ids=(11,))
    notes = essay_notes(_store(tmp_path), selection)
    assert [(n.note_id, n.title) for n in notes] == [(10, "Luddites")]
    assert notes[0].text.startswith("Luddites\nThe reason")
