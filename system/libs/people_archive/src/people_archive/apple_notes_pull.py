"""Read selected Apple Notes through an adopter-configured file-access connection.

Only selected note records are persisted in the raw archive. The downloaded
SQLite database and write-ahead log are removed after extraction.
"""

import gzip
import json
import shutil
import sqlite3
import tempfile
import urllib.parse
from collections.abc import Iterator
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Final

import httpx
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure
from loguru import logger
from pydantic import Field, PrivateAttr

from people_archive.data_types import PullSummary
from people_archive.errors import GatewayUnreachableError, UpstreamApiError
from people_archive.gateway import gateway_settings_from_environment
from people_archive.notes_pull import NOTES_ACCOUNT, parse_note
from people_archive.primitives import SourceKind
from people_archive.settings import load_settings
from people_archive.store import ArchiveStore

DEFAULT_SELECTION_PATH: Final[Path] = Path("data/.apps/crm/apple_notes_essays.json")
_FILES_ROOT: Final[str] = "/minds-api-proxy/api/v1/files"
_CONTAINER: Final[str] = ""
_DATABASE_FILES: Final[tuple[str, ...]] = ("NoteStore.sqlite", "NoteStore.sqlite-wal", "NoteStore.sqlite-shm")
# Apple stores dates as seconds since 2001-01-01.
_APPLE_EPOCH_OFFSET: Final[float] = 978307200.0
SOURCE_LABEL: Final[str] = "apple-notes"


class EssaySelection(FrozenModel):
    """Which Apple Notes count as essay topics."""

    device: str = Field(description="Imbue Studio device id of the Mac holding the notes")
    folder: str = Field(description="Apple Notes folder the essays are in")
    since: date = Field(description="Only notes last edited on or after this day")
    exclude_ids: tuple[int, ...] = Field(description="Notes in range that are not essays")

    @classmethod
    def load(cls, path: Path) -> "EssaySelection":
        return cls.model_validate(json.loads(path.read_text()))


class AppleNote(FrozenModel):
    note_id: int = Field(description="The note's row id in NoteStore.sqlite")
    title: str = Field(description="The note's title")
    folder: str = Field(description="Its folder")
    modified: datetime = Field(description="When it was last edited")
    text: str = Field(description="Plain text of the note")


class AppleNotesClient(MutableModel):
    """Downloads the Notes database files from the Mac through the gateway's file route."""

    gateway_base_url: str = Field(frozen=True, description="Gateway base")
    auth_headers: dict[str, str] = Field(frozen=True, description="Gateway auth headers")
    device: str = Field(frozen=True, description="Device id of the Mac")
    _client: httpx.Client = PrivateAttr()

    def model_post_init(self, context: Any) -> None:
        self._client = httpx.Client(headers={**self.auth_headers, "X-Latchkey-Device": self.device}, timeout=120)

    @classmethod
    def from_environment(cls, device: str) -> "AppleNotesClient":
        base, headers = gateway_settings_from_environment()
        return cls(gateway_base_url=base, auth_headers=headers, device=device)

    def download(self, name: str, destination: Path) -> bool:
        """Save one file of the Notes container; False when it does not exist (no -wal yet)."""
        url = f"{self.gateway_base_url}{_FILES_ROOT}/{self.device}/{urllib.parse.quote(load_settings().apple_notes_container)}/{name}"
        try:
            response = self._client.get(url)
        except httpx.HTTPError as e:
            raise GatewayUnreachableError(f"Mac unreachable for Apple Notes: {e}") from e
        if response.status_code == 404:
            return False
        if response.status_code != 200:
            raise UpstreamApiError(f"Apple Notes {name}: {response.status_code} {response.text[:120]}")
        destination.write_bytes(response.content)
        return True


def _varint(data: bytes, index: int) -> tuple[int, int]:
    result = shift = 0
    while True:
        byte = data[index]
        index += 1
        result |= (byte & 0x7F) << shift
        shift += 7
        if byte < 0x80:
            return result, index


def _fields(data: bytes) -> Iterator[tuple[int, Any]]:
    index = 0
    while index < len(data):
        key, index = _varint(data, index)
        number, wire = key >> 3, key & 7
        if wire == 0:
            value, index = _varint(data, index)
        elif wire == 2:
            length, index = _varint(data, index)
            value = data[index : index + length]
            index += length
        elif wire == 1:
            value, index = data[index : index + 8], index + 8
        elif wire == 5:
            value, index = data[index : index + 4], index + 4
        else:
            raise UpstreamApiError(f"Unreadable Apple Notes body (wire type {wire})")
        yield number, value


@pure
def note_body_text(blob: bytes) -> str:
    """The plain text of a note from its gzipped protobuf body."""
    document = dict(_fields(gzip.decompress(blob)))[2]
    note = dict(_fields(document))[3]
    return dict(_fields(note))[2].decode("utf-8", "replace")


def essay_notes(database: Path, selection: EssaySelection) -> list[AppleNote]:
    """The notes the selection picks, newest first, read from a copy of NoteStore.sqlite."""
    since = datetime.combine(selection.since, datetime.min.time(), tzinfo=timezone.utc).timestamp() - _APPLE_EPOCH_OFFSET
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        rows = connection.execute(
            """SELECT n.Z_PK, n.ZTITLE1, f.ZTITLE2, n.ZMODIFICATIONDATE1, d.ZDATA
               FROM ZICCLOUDSYNCINGOBJECT n
               JOIN ZICCLOUDSYNCINGOBJECT f ON n.ZFOLDER = f.Z_PK
               JOIN ZICNOTEDATA d ON d.ZNOTE = n.Z_PK
               WHERE n.ZTITLE1 IS NOT NULL AND f.ZTITLE2 = ? AND n.ZMODIFICATIONDATE1 >= ?
                 AND COALESCE(n.ZMARKEDFORDELETION, 0) = 0
               ORDER BY n.ZMODIFICATIONDATE1 DESC""",
            (selection.folder, since),
        ).fetchall()
    finally:
        connection.close()
    notes = []
    for note_id, title, folder, modified, blob in rows:
        if note_id in selection.exclude_ids or blob is None:
            continue
        notes.append(
            AppleNote(
                note_id=int(note_id), title=str(title), folder=str(folder),
                modified=datetime.fromtimestamp(float(modified) + _APPLE_EPOCH_OFFSET, tz=timezone.utc),
                text=note_body_text(bytes(blob)),
            )
        )
    return notes


def pull_apple_notes(client: AppleNotesClient, store: ArchiveStore, selection: EssaySelection) -> PullSummary:
    """Store the selected Apple Notes as notes, alongside the Obsidian and Logseq ones."""
    workdir = Path(tempfile.mkdtemp(prefix="apple-notes-", dir="/var/tmp"))
    try:
        workdir.chmod(0o700)
        if not client.download(_DATABASE_FILES[0], workdir / _DATABASE_FILES[0]):
            raise UpstreamApiError("No NoteStore.sqlite in the Apple Notes container")
        for name in _DATABASE_FILES[1:]:
            client.download(name, workdir / name)
        notes = essay_notes(workdir / _DATABASE_FILES[0], selection)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    fetched_at = datetime.now(timezone.utc)
    with store.transaction():
        for note in notes:
            path = f"Apple Notes/{note.folder}/{note.title.replace('/', '-')}.md"
            raw = {"note_id": note.note_id, "title": note.title, "folder": note.folder, "modified": note.modified.isoformat(), "text": note.text}
            store.upsert_raw_record(SourceKind.NOTES, NOTES_ACCOUNT, f"{SOURCE_LABEL}:{note.note_id}", raw, fetched_at)
            store.upsert_note(parse_note(SOURCE_LABEL, path, note.text))
    logger.info("Apple Notes: {} essay notes stored", len(notes))
    return PullSummary(source=SourceKind.NOTES, account=NOTES_ACCOUNT, fetched_count=len(notes), skipped_count=0, failed_count=0)
