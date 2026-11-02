from collections.abc import Mapping
from typing import Any

import pytest
from pydantic import Field

from people_archive.beeper_pull import (
    BeeperClient,
    chat_rows,
    message_row,
    participant_handle,
    pull_beeper,
)
from people_archive.errors import GatewayUnreachableError
from people_archive.store import ArchiveStore


class _FakeBeeper(BeeperClient):
    """Answers chat listing and history from memory; listed chat ids never answer."""

    chats: list[dict[str, Any]] = Field(default_factory=list)
    dead: set[str] = Field(default_factory=set)
    calls: list[str] = Field(default_factory=list)

    def model_post_init(self, context: Any) -> None:
        pass

    def get_json(self, path: str, params: Mapping[str, str], timeout: float | None = None) -> dict[str, Any]:
        self.calls.append(path)
        if self.base_url == "asleep":
            raise GatewayUnreachableError("Beeper unreachable: timed out")
        if path == "/v1/chats/search":
            return {"items": self.chats, "hasMore": False}
        chat_id = path.split("/")[3]
        if chat_id in self.dead:
            raise GatewayUnreachableError("Beeper unreachable: timed out")
        return {"items": [{"id": f"m-{chat_id}", "chatID": chat_id, "timestamp": "2026-10-01T10:00:00.000Z"}], "hasMore": False, "newestCursor": "n1"}


def _client(chats: list[dict[str, Any]], dead: frozenset[str] = frozenset(), base_url: str = "x") -> _FakeBeeper:
    return _FakeBeeper(base_url=base_url, proxy_url="p", token="t", chats=chats, dead=set(dead))


def _chat(chat_id: str, activity: str) -> dict[str, Any]:
    return {"id": chat_id, "network": "iMessage", "type": "single", "title": chat_id, "lastActivity": activity, "participants": {"items": []}}


def test_second_run_skips_unchanged_chats_and_retries_failed_ones(archive_store: ArchiveStore) -> None:
    chats = [_chat("a", "2026-10-01T00:00:00Z"), _chat("b", "2026-10-02T00:00:00Z")]
    first = pull_beeper(_client(chats, dead={"b"}), archive_store)
    assert first.fetched_count == 1 and first.failed_count == 1

    client = _client(chats)
    second = pull_beeper(client, archive_store)
    # "a" had nothing new; "b" failed last time, so it is fetched now.
    assert second.skipped_count == 1 and second.fetched_count == 1
    assert [c for c in client.calls if "/messages" in c] == ["/v1/chats/b/messages"]


def test_stops_after_beeper_goes_away_mid_run(archive_store: ArchiveStore) -> None:
    chats = [_chat(f"c{i}", f"2026-10-{i + 1:02d}T00:00:00Z") for i in range(20)]
    client = _client(chats, dead={c["id"] for c in chats})
    summary = pull_beeper(client, archive_store)
    assert summary.failed_count == 5
    assert len([c for c in client.calls if "/messages" in c]) == 5


def test_asleep_mac_fails_fast(archive_store: ArchiveStore) -> None:
    client = _client([_chat("a", "2026-10-01T00:00:00Z")], base_url="asleep")
    with pytest.raises(GatewayUnreachableError):
        pull_beeper(client, archive_store)
    assert client.calls == ["/v1/chats/search"]


def test_participant_handle_prefers_phone_then_email_then_username() -> None:
    assert str(participant_handle("WhatsApp", {"id": "@x", "phoneNumber": "+1 (555) 010-0001"})) == "phone:+15550100001"
    assert str(participant_handle("iMessage", {"id": "@y", "email": "Ada@Example.com"})) == "email:ada@example.com"
    assert str(participant_handle("Twitter/X", {"id": "@twitter_1:beeper.local", "username": "AdaL"})) == "x:adal"
    assert str(participant_handle("Facebook/Messenger", {"id": "@fb_9:local"})) == "facebook:@fb_9:local"


def test_participant_handle_ignores_short_codes_that_are_not_phone_numbers() -> None:
    assert str(participant_handle("iMessage", {"id": "@z", "phoneNumber": "787473"})) == "imessage:@z"


def test_chat_rows_project_participants_and_activity() -> None:
    raw = {
        "id": "imsg##thread:abc", "accountID": "imsg", "network": "iMessage", "type": "single", "title": "Ada",
        "lastActivity": "2026-09-01T10:00:00.000Z",
        "participants": {"items": [
            {"id": "p1", "phoneNumber": "+15550100001", "fullName": "Mira Vale", "isSelf": False},
            {"id": "me", "phoneNumber": "+15550100002", "isSelf": True},
        ]},
    }

    chat, participants = chat_rows(raw)

    assert chat.network == "iMessage" and chat.last_activity is not None and chat.last_activity.year == 2026
    assert [(p.participant_id, str(p.handle_key), p.is_self) for p in participants] == [("p1", "phone:+15550100001", False), ("me", "phone:+15550100002", True)]


def test_message_row_reads_direction_and_text() -> None:
    row = message_row({"id": "m1", "chatID": "c1", "timestamp": "2026-09-01T10:00:00.000Z", "senderID": "p1", "senderName": "Ada", "isSender": False, "text": "hi"})

    assert row.is_sender is False and row.text == "hi" and row.timestamp.year == 2026
