import json
import re
import urllib.parse
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Final

import httpx
import yaml
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure
from loguru import logger
from pydantic import Field, PrivateAttr

from people_archive.data_types import PullSummary
from people_archive.errors import (
    GatewayUnreachableError,
    InvalidArchiveValueError,
    UpstreamApiError,
)
from people_archive.gateway import gateway_settings_from_environment
from people_archive.primitives import (
    AccountEmail,
    EmailAddress,
    HandleKey,
    PhoneNumber,
    SourceKind,
)
from people_archive.settings import load_settings
from people_archive.store import ArchiveStore, NoteRow

# The user's Mac, reached through the Mind app's file access (WebDAV behind the latchkey
# gateway). Read-only: this module never writes to either vault.
_FILES_ROOT: Final[str] = "/minds-api-proxy/api/v1/files"
_DAV_NAMESPACE_HREF: Final[re.Pattern[str]] = re.compile(r"<ns0:href>([^<]+)</ns0:href>")
_WORKERS: Final[int] = 8
NOTES_ACCOUNT: Final[AccountEmail] = AccountEmail("notes@local")

# [[Page]] and #[[Page]] links; #tag; Logseq `key:: value` properties; Obsidian frontmatter.
_WIKILINK_PATTERN: Final[re.Pattern[str]] = re.compile(r"\[\[([^\]|#]+)(?:[|#][^\]]*)?\]\]")
_HASHTAG_PATTERN: Final[re.Pattern[str]] = re.compile(r"(?<![\w/])#([A-Za-z][\w-]{1,40})")
_LOGSEQ_PROPERTY_PATTERN: Final[re.Pattern[str]] = re.compile(r"^\s*-?\s*([A-Za-z][\w-]*)::\s*(.+?)\s*$", re.MULTILINE)
_FRONTMATTER_PATTERN: Final[re.Pattern[str]] = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)
_JOURNAL_DATE_PATTERN: Final[re.Pattern[str]] = re.compile(r"(\d{4})[_-](\d{2})[_-](\d{2})")
# Folders in the vaults that hold no notes worth reading.
_SKIPPED_FOLDERS: Final[tuple[str, ...]] = ("/.obsidian/", "/.claude/", "/logseq/bak/", "/logseq/version-files/", "/backup_duplicates/", "/.trash/", "/assets/")


class VaultFolder(FrozenModel):
    """One folder on the Mac to read notes from."""

    label: str = Field(description="obsidian, or logseq:<graph>")
    path_under_users: str = Field(description="Path below /Users, unencoded")


class MacFilesClient(MutableModel):
    """Reads files from the user's Mac through the gateway's file-access route."""

    gateway_base_url: str = Field(frozen=True, description="Gateway base")
    auth_headers: dict[str, str] = Field(frozen=True, description="Gateway auth headers")
    _client: httpx.Client = PrivateAttr()

    def model_post_init(self, context: Any) -> None:
        self._client = httpx.Client(headers=self.auth_headers, timeout=120)

    @classmethod
    def from_environment(cls) -> "MacFilesClient":
        base, headers = gateway_settings_from_environment()
        return cls(gateway_base_url=base, auth_headers={**headers, **({"X-Latchkey-Device": load_settings().notes_device} if load_settings().notes_device else {})})

    def _url(self, path_under_users: str) -> str:
        return f"{self.gateway_base_url}{_FILES_ROOT}/Users/{urllib.parse.quote(path_under_users, safe='/()')}"

    def list_markdown_files(self, folder_under_users: str) -> list[str]:
        """Every .md path (unencoded, below /Users) under the folder, recursively."""
        response = self._client.request("PROPFIND", self._url(folder_under_users) + "/", headers={"Depth": "infinity"})
        if response.status_code != 207:
            raise UpstreamApiError(f"Listing {folder_under_users} failed: {response.status_code} {response.text[:120]}")
        paths: list[str] = []
        for href in _DAV_NAMESPACE_HREF.findall(response.text):
            decoded = urllib.parse.unquote(href)
            if not decoded.endswith(".md"):
                continue
            if any(skipped in decoded.lower() for skipped in _SKIPPED_FOLDERS):
                continue
            paths.append(decoded.split("/users/", 1)[1] if "/users/" in decoded else decoded.split("/Users/", 1)[1])
        return paths

    def read_text(self, path_under_users: str) -> str:
        response = self._client.get(self._url(path_under_users))
        if response.status_code != 200:
            raise UpstreamApiError(f"Reading {path_under_users} failed: {response.status_code}")
        return response.text


@pure
def _journal_date(path: str) -> datetime | None:
    match = _JOURNAL_DATE_PATTERN.search(path.rsplit("/", 1)[-1])
    if match is None:
        return None
    year, month, day = (int(group) for group in match.groups())
    try:
        return datetime(year, month, day, tzinfo=timezone.utc)
    except ValueError:
        return None


@pure
def parse_note(label: str, path: str, text: str) -> NoteRow:
    """A note's mentions ([[links]] and #tags), its properties, and whether it is a journal day."""
    links = [name.strip() for name in _WIKILINK_PATTERN.findall(text) if name.strip()]
    tags = [tag for tag in _HASHTAG_PATTERN.findall(text)]
    properties: dict[str, str] = {}
    frontmatter = _FRONTMATTER_PATTERN.match(text)
    if frontmatter is not None:
        try:
            loaded = yaml.safe_load(frontmatter.group(1)) or {}
        except yaml.YAMLError:
            loaded = {}
        if isinstance(loaded, dict):
            properties.update({str(k): str(v) for k, v in loaded.items() if isinstance(v, (str, int, float))})
    for key, value in _LOGSEQ_PROPERTY_PATTERN.findall(text):
        properties.setdefault(key.lower(), value)
    is_journal = "/journals/" in path or "/daily notes/" in path.lower()
    title = path.rsplit("/", 1)[-1][:-3]
    return NoteRow(
        source=label, path=path, title=title, is_journal=is_journal, note_date=_journal_date(path) if is_journal else None,
        text=text, mentions=tuple(dict.fromkeys(links)), tags=tuple(dict.fromkeys(tags)), properties=properties,
    )


@pure
def property_handles(properties: dict[str, str]) -> list[HandleKey]:
    """Hard evidence a page carries about who it is: an email or phone property."""
    handles: list[HandleKey] = []
    for key in ("email", "emails", "e-mail"):
        for candidate in re.split(r"[,\s]+", properties.get(key, "")):
            try:
                handles.append(HandleKey(f"email:{EmailAddress(candidate)}"))
            except InvalidArchiveValueError:
                continue
    for key in ("phone", "phones", "mobile", "cell"):
        for candidate in re.split(r"[,;]+", properties.get(key, "")):
            try:
                handles.append(HandleKey(f"phone:{PhoneNumber(candidate)}"))
            except InvalidArchiveValueError:
                continue
    return handles


def pull_notes(client: MacFilesClient, store: ArchiveStore, folders: Sequence[VaultFolder]) -> PullSummary:
    """Read every Markdown note in the given folders and store it with its mentions."""
    fetched_at = datetime.now(timezone.utc)
    stored = 0
    failed = 0
    for folder in folders:
        try:
            paths = client.list_markdown_files(folder.path_under_users)
        except (UpstreamApiError, GatewayUnreachableError, httpx.HTTPError) as e:
            logger.warning("Notes folder {} skipped: {}", folder.label, e)
            failed += 1
            continue

        def fetch(path: str) -> tuple[str, str | None]:
            try:
                return path, client.read_text(path)
            except (UpstreamApiError, httpx.HTTPError):
                return path, None

        with ThreadPoolExecutor(max_workers=_WORKERS) as pool:
            results = list(pool.map(fetch, paths))
        with store.transaction():
            for path, text in results:
                if text is None:
                    failed += 1
                    continue
                note = parse_note(folder.label, path, text)
                store.upsert_raw_record(SourceKind.NOTES, NOTES_ACCOUNT, f"{folder.label}:{path}", {"path": path, "text": text}, fetched_at)
                store.upsert_note(note)
                stored += 1
        logger.info("Notes {}: {} files stored", folder.label, len(results))
    return PullSummary(source=SourceKind.NOTES, account=NOTES_ACCOUNT, fetched_count=stored, skipped_count=0, failed_count=failed)


def default_vault_folders() -> list[VaultFolder]:
    return [VaultFolder.model_validate(folder) for folder in load_settings().notes_folders]



@pure
def mention_handle(name: str) -> HandleKey:
    normalized = re.sub(r"\s+", " ", name.strip()).lower()
    return HandleKey(f"note:{normalized}")


def note_mentions_as_json(notes: Sequence[NoteRow]) -> str:
    return json.dumps([{"path": n.path, "mentions": list(n.mentions)} for n in notes])
