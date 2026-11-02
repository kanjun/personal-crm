import json
import os
import urllib.parse
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any, Final

import httpx
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
from people_archive.primitives import (
    AccountEmail,
    EmailAddress,
    HandleKey,
    HandleNetwork,
    PhoneNumber,
    SourceKind,
    handle_key,
)
from people_archive.settings import load_settings, require_feature
from people_archive.store import (
    ArchiveStore,
    BeeperChatRow,
    BeeperMessageRow,
    BeeperParticipantRow,
)

DEFAULT_BEEPER_BASE_URL: Final[str] = ""
DEFAULT_TAILSCALE_PROXY: Final[str] = ""
_CHAT_PAGE_SIZE: Final[str] = "200"
_MESSAGE_PAGE_SIZE: Final[str] = "200"
_HARD_TIMEOUT_SECONDS: Final[float] = 60.0
# A quick check that Beeper answers at all before walking thousands of chats: when the Mac
# is asleep every request would otherwise wait out the full timeout.
_PROBE_TIMEOUT_SECONDS: Final[float] = 10.0
# This many chats failing in a row means Beeper went away mid-run: stop, and pick up next run.
_MAX_CONSECUTIVE_FAILURES: Final[int] = 5
# Beeper's accounts are pseudo-accounts here; the archive keys raw records by this label.
BEEPER_ACCOUNT: Final[AccountEmail] = AccountEmail("beeper@local")

_NETWORK_BY_LABEL: Final[dict[str, HandleNetwork]] = {
    "iMessage": HandleNetwork.IMESSAGE, "WhatsApp": HandleNetwork.WHATSAPP, "Signal": HandleNetwork.SIGNAL,
    "Telegram": HandleNetwork.TELEGRAM, "Twitter/X": HandleNetwork.X, "Instagram": HandleNetwork.INSTAGRAM,
    "Facebook/Messenger": HandleNetwork.FACEBOOK, "Beeper (Matrix)": HandleNetwork.MATRIX,
}


class BeeperClient(MutableModel):
    """Calls the optional Beeper Desktop API over a configured network route."""

    base_url: str = Field(frozen=True, description="Beeper's API base as seen from this machine")
    proxy_url: str = Field(frozen=True, description="Optional HTTP proxy")
    token: str = Field(frozen=True, description="Beeper access token")
    _client: httpx.Client = PrivateAttr()

    def model_post_init(self, context: Any) -> None:
        self._client = httpx.Client(proxy=self.proxy_url or None, headers={"Authorization": f"Bearer {self.token}"}, timeout=_HARD_TIMEOUT_SECONDS)

    @classmethod
    def from_environment(cls) -> "BeeperClient":
        require_feature("beeper")
        settings = load_settings()
        base = os.environ.get("BEEPER_BASE_URL", "") or (f"{settings.beeper_scheme}://{settings.beeper_host}:{settings.beeper_port}" if settings.beeper_host else "")
        if not base or not settings.beeper_token:
            raise GatewayUnreachableError("Set beeper_host and beeper_token before enabling Beeper")
        return cls(
            base_url=base,
            proxy_url=os.environ.get("BEEPER_PROXY_URL", settings.beeper_proxy_url),
            token=settings.beeper_token,
        )

    def get_json(self, path: str, params: Mapping[str, str], timeout: float | None = None) -> dict[str, Any]:
        try:
            response = self._client.get(
                f"{self.base_url}{path}", params=dict(params), timeout=timeout or _HARD_TIMEOUT_SECONDS
            )
        except httpx.HTTPError as e:
            raise GatewayUnreachableError(f"Beeper unreachable: {e}") from e
        if response.status_code >= 400:
            raise UpstreamApiError(f"Beeper {response.status_code} on {path}: {response.text[:200]}")
        body = response.json()
        if not isinstance(body, dict):
            raise UpstreamApiError(f"Unexpected Beeper response shape on {path}")
        return body


@pure
def participant_handle(network_label: str, participant: Mapping[str, Any]) -> HandleKey:
    """Phone first (it joins the contact cards), then email, then the network's username or id."""
    phone_raw = str(participant.get("phoneNumber") or "")
    if phone_raw:
        try:
            return handle_key(HandleNetwork.PHONE, PhoneNumber(phone_raw))
        except InvalidArchiveValueError:
            pass
    email_raw = str(participant.get("email") or "")
    if email_raw:
        try:
            return handle_key(HandleNetwork.EMAIL, EmailAddress(email_raw))
        except InvalidArchiveValueError:
            pass
    network = _NETWORK_BY_LABEL.get(network_label, HandleNetwork.OTHER_CHAT)
    username = str(participant.get("username") or "")
    return handle_key(network, (username or str(participant.get("id") or "")).lower())


@pure
def _parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


@pure
def chat_rows(raw: Mapping[str, Any]) -> tuple[BeeperChatRow, list[BeeperParticipantRow]]:
    chat_id = str(raw["id"])
    participants_raw = (raw.get("participants") or {}).get("items", []) or []
    participants = [
        BeeperParticipantRow(
            chat_id=chat_id, participant_id=str(p.get("id") or ""), handle_key=participant_handle(str(raw.get("network", "")), p),
            full_name=str(p.get("fullName") or ""), phone=str(p.get("phoneNumber") or ""), username=str(p.get("username") or ""),
            is_self=bool(p.get("isSelf", False)),
        )
        for p in participants_raw
        if p.get("id")
    ]
    last_activity = raw.get("lastActivity")
    return (
        BeeperChatRow(
            chat_id=chat_id, account_id=str(raw.get("accountID") or ""), network=str(raw.get("network") or ""),
            chat_type=str(raw.get("type") or ""), title=str(raw.get("title") or ""),
            participants_json=json.dumps(participants_raw), last_activity=_parse_timestamp(str(last_activity)) if last_activity else None,
        ),
        participants,
    )


@pure
def message_row(raw: Mapping[str, Any]) -> BeeperMessageRow:
    return BeeperMessageRow(
        message_id=str(raw["id"]), chat_id=str(raw.get("chatID") or ""), timestamp=_parse_timestamp(str(raw["timestamp"])),
        sender_id=str(raw.get("senderID") or ""), sender_name=str(raw.get("senderName") or ""),
        is_sender=bool(raw.get("isSender", False)), text=str(raw.get("text") or ""),
    )


def _list_all_chats(client: BeeperClient) -> list[dict[str, Any]]:
    chats: list[dict[str, Any]] = []
    cursor: str | None = ""
    while cursor is not None:
        params = {"limit": _CHAT_PAGE_SIZE}
        if cursor:
            params["cursor"] = cursor
            params["direction"] = "before"
        page = client.get_json("/v1/chats/search", params)
        items = page.get("items", []) or []
        chats.extend(items)
        cursor = page.get("oldestCursor") if page.get("hasMore") and items else None
    return chats


def _pull_chat_history(client: BeeperClient, store: ArchiveStore, chat_id: str) -> int:
    """Walk one chat's history to the beginning (or fetch only what is newer than last time)."""
    path = f"/v1/chats/{urllib.parse.quote(chat_id, safe='')}/messages"
    sync = store.get_beeper_chat_sync(chat_id)
    stored = 0
    if sync is not None and sync[1]:
        # History done before: fetch only newer messages, forward from the recorded cursor.
        cursor: str | None = sync[0]
        newest = sync[0]
        while cursor is not None:
            page = client.get_json(path, {"limit": _MESSAGE_PAGE_SIZE, "cursor": cursor, "direction": "after"})
            items = page.get("items", []) or []
            if items:
                with store.transaction():
                    store.upsert_beeper_messages([message_row(m) for m in items])
                stored += len(items)
                newest = str(page.get("newestCursor") or newest)
            cursor = page.get("newestCursor") if page.get("hasMore") and items else None
        with store.transaction():
            store.set_beeper_chat_sync(chat_id, newest, True)
        return stored
    # First time: newest page first, then keep paging back until the beginning.
    cursor = ""
    newest_cursor = ""
    while cursor is not None:
        params = {"limit": _MESSAGE_PAGE_SIZE}
        if cursor:
            params["cursor"] = cursor
            params["direction"] = "before"
        page = client.get_json(path, params)
        items = page.get("items", []) or []
        if items:
            with store.transaction():
                store.upsert_beeper_messages([message_row(m) for m in items])
            stored += len(items)
        if not newest_cursor:
            newest_cursor = str(page.get("newestCursor") or "")
        cursor = page.get("oldestCursor") if page.get("hasMore") and items else None
    with store.transaction():
        store.set_beeper_chat_sync(chat_id, newest_cursor, True)
    return stored


def pull_beeper(client: BeeperClient, store: ArchiveStore) -> PullSummary:
    """Every chat's participants and messages; the first run walks all history, later runs only the new."""
    # Raises GatewayUnreachableError within seconds when the Mac is asleep or offline.
    client.get_json("/v1/chats/search", {"limit": "1"}, timeout=_PROBE_TIMEOUT_SECONDS)
    chats = _list_all_chats(client)
    fetched_at = datetime.now(timezone.utc)
    # A chat whose last activity is unchanged since the previous run has nothing new to fetch.
    previous_activity = {
        str(raw["id"]): (store.get_raw_record(SourceKind.BEEPER, BEEPER_ACCOUNT, f"chat:{raw['id']}") or {}).get("lastActivity")
        for raw in chats
    }
    logger.info("Beeper: {} chats listed", len(chats))
    to_pull = chats_to_pull(chats, previous_activity, store)
    # A chat's record (and with it the activity mark compared next run) is saved only once its
    # messages are in, so a chat that fails or is not reached this run is fetched next time.
    pending = {str(raw["id"]) for raw in to_pull}
    _save_chats([raw for raw in chats if str(raw["id"]) not in pending], store, fetched_at)
    skipped = len(chats) - len(to_pull)
    logger.info("Beeper: {} chats have new activity, {} unchanged", len(to_pull), skipped)
    messages_stored = 0
    failed = 0
    consecutive_unreachable = 0
    for idx, raw in enumerate(to_pull, start=1):
        try:
            messages_stored += _pull_chat_history(client, store, str(raw["id"]))
            _save_chats([raw], store, fetched_at)
            consecutive_unreachable = 0
        except UpstreamApiError as e:
            failed += 1
            logger.warning("Beeper chat {} skipped: {}", raw.get("title") or raw["id"], e)
        except GatewayUnreachableError as e:
            failed += 1
            consecutive_unreachable += 1
            logger.warning("Beeper chat {} skipped: {}", raw.get("title") or raw["id"], e)
            if consecutive_unreachable >= _MAX_CONSECUTIVE_FAILURES:
                logger.warning("Beeper stopped answering ({} chats in a row); stopping, the rest wait for the next run", consecutive_unreachable)
                break
        if idx % 100 == 0:
            logger.info("Beeper: {} of {} chats done, {} messages stored so far", idx, len(to_pull), messages_stored)
    logger.info("Beeper: {} messages stored, {} chats failed", messages_stored, failed)
    return PullSummary(source=SourceKind.BEEPER, account=BEEPER_ACCOUNT, fetched_count=messages_stored, skipped_count=skipped, failed_count=failed)


def _save_chats(chats: list[dict[str, Any]], store: ArchiveStore, fetched_at: datetime) -> None:
    with store.transaction():
        for raw in chats:
            store.upsert_raw_record(SourceKind.BEEPER, BEEPER_ACCOUNT, f"chat:{raw['id']}", raw, fetched_at)
            chat, participants = chat_rows(raw)
            store.upsert_beeper_chat(chat, participants)


def chats_to_pull(chats: list[dict[str, Any]], previous_activity: Mapping[str, Any], store: ArchiveStore) -> list[dict[str, Any]]:
    """Chats with activity since the last run, or whose history was never finished; newest first."""
    out = []
    for raw in chats:
        chat_id = str(raw["id"])
        sync = store.get_beeper_chat_sync(chat_id)
        finished = sync is not None and sync[1]
        if finished and raw.get("lastActivity") and raw.get("lastActivity") == previous_activity.get(chat_id):
            continue
        out.append(raw)
    return sorted(out, key=lambda r: str(r.get("lastActivity") or ""), reverse=True)
