"""A small in-memory Gmail for tests: messages with labels, a history of changes, and the
endpoints the sync and the pull call (profile, history, messages list/get, labels)."""

import base64
from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import Field

from people_archive.errors import UpstreamApiError
from people_archive.interfaces import GoogleApiGatewayInterface
from people_archive.primitives import AccountEmail

_BASE = "https://gmail.googleapis.com/gmail/v1/users/me/"


def _b64url(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


class FakeGmail(GoogleApiGatewayInterface):
    """One mailbox per account. Ids are hex so they sort like Gmail's; history ids count up."""

    mailboxes: dict[str, dict[str, dict[str, Any]]] = Field(default_factory=dict, description="account -> id -> message")
    history: dict[str, list[dict[str, Any]]] = Field(default_factory=dict, description="account -> history records")
    history_id: dict[str, int] = Field(default_factory=dict, description="account -> current history id")
    expired_before: dict[str, int] = Field(default_factory=dict, description="account -> oldest history id still kept")
    calls: list[str] = Field(default_factory=list, description="Every path requested, in order")
    page_size: int = Field(default=2, description="Listing page size, small so paging is exercised")

    # --- building the mailbox ---

    def _bump(self, account: str, record: dict[str, Any]) -> None:
        self.history_id[account] = self.history_id.get(account, 100) + 1
        if record:
            self.history.setdefault(account, []).append(dict(record, id=str(self.history_id[account])))

    def add(
        self,
        account: str,
        message_id: str,
        labels: Sequence[str],
        sender: str = "Ada <ada@example.com>",
        to: str = "owner@example.com",
        subject: str = "hello",
        body: str = "hi",
        thread_id: str | None = None,
        cc: str = "",
        bcc: str = "",
        is_recorded: bool = True,
        internal_ms: int | None = None,
    ) -> dict[str, Any]:
        headers = [{"name": "From", "value": sender}, {"name": "To", "value": to}, {"name": "Subject", "value": subject}]
        if cc:
            headers.append({"name": "Cc", "value": cc})
        if bcc:
            headers.append({"name": "Bcc", "value": bcc})
        headers.append({"name": "Date", "value": "Mon, 20 Oct 2026 10:00:00 +0000"})
        message = {
            "id": message_id,
            "threadId": thread_id or message_id,
            "labelIds": list(labels),
            "snippet": body[:40],
            "internalDate": str(internal_ms if internal_ms is not None else int(message_id, 16) * 1000),
            "payload": {"mimeType": "text/plain", "headers": headers, "body": {"data": _b64url(body)}},
        }
        self.mailboxes.setdefault(account, {})[message_id] = message
        self._bump(account, {"messagesAdded": [{"message": self._ref(message)}]} if is_recorded else {})
        return message

    def _ref(self, message: dict[str, Any]) -> dict[str, Any]:
        return {"id": message["id"], "threadId": message["threadId"], "labelIds": list(message["labelIds"])}

    def relabel(self, account: str, message_id: str, add: Sequence[str] = (), remove: Sequence[str] = (), is_recorded: bool = True) -> None:
        message = self.mailboxes[account][message_id]
        labels = [label for label in message["labelIds"] if label not in remove]
        labels += [label for label in add if label not in labels]
        message["labelIds"] = labels
        record: dict[str, Any] = {}
        if add:
            record["labelsAdded"] = [{"message": self._ref(message), "labelIds": list(add)}]
        if remove:
            record["labelsRemoved"] = [{"message": self._ref(message), "labelIds": list(remove)}]
        self._bump(account, record if is_recorded else {})

    def delete(self, account: str, message_id: str) -> None:
        message = self.mailboxes[account].pop(message_id)
        self._bump(account, {"messagesDeleted": [{"message": self._ref(message)}]})

    # --- the API ---

    def get_json(self, account: AccountEmail, url: str, params: Mapping[str, str] | Sequence[tuple[str, str]]) -> dict[str, Any]:
        pairs = list(params.items()) if isinstance(params, Mapping) else list(params)
        path = url.removeprefix(_BASE)
        self.calls.append(path)
        box = self.mailboxes.setdefault(str(account), {})
        if path == "profile":
            return {"historyId": str(self.history_id.get(str(account), 100))}
        if path == "history":
            return self._history(str(account), dict(pairs))
        if path == "messages":
            return self._list(box, pairs)
        if path.startswith("messages/"):
            message = box.get(path.removeprefix("messages/"))
            if message is None:
                raise UpstreamApiError(f"404 from {url}: not found")
            if dict(pairs).get("format") == "minimal":
                return {key: message[key] for key in ("id", "threadId", "labelIds", "internalDate", "snippet")}
            return dict(message)
        if path == "labels":
            names = sorted({label for m in box.values() for label in m["labelIds"]} | {"INBOX", "SENT"})
            return {"labels": [{"id": name, "name": name} for name in names]}
        if path.startswith("labels/"):
            label = path.removeprefix("labels/")
            return {"id": label, "messagesTotal": sum(1 for m in box.values() if label in m["labelIds"])}
        raise UpstreamApiError(f"404 from {url}: unknown path")

    def _history(self, account: str, params: dict[str, str]) -> dict[str, Any]:
        start = int(params["startHistoryId"])
        if start < self.expired_before.get(account, 0):
            raise UpstreamApiError("404 from history: Requested entity was not found.")
        records = [r for r in self.history.get(account, []) if int(r["id"]) > start]
        offset = int(params.get("pageToken") or 0)
        page = records[offset : offset + self.page_size]
        body: dict[str, Any] = {"history": page, "historyId": str(self.history_id.get(account, 100))}
        if offset + self.page_size < len(records):
            body["nextPageToken"] = str(offset + self.page_size)
        return body

    def _list(self, box: dict[str, dict[str, Any]], pairs: list[tuple[str, str]]) -> dict[str, Any]:
        labels = [value for name, value in pairs if name == "labelIds"]
        params = dict(pairs)
        include_hidden = params.get("includeSpamTrash") == "true" or bool({"TRASH", "SPAM"} & set(labels))
        query = params.get("q", "")
        after = int(query.removeprefix("after:")) * 1000 if query.startswith("after:") else 0
        matching = [
            m
            for m in box.values()
            if all(label in m["labelIds"] for label in labels)
            and (include_hidden or not ({"TRASH", "SPAM"} & set(m["labelIds"])))
            and int(m["internalDate"]) >= after
        ]
        matching.sort(key=lambda m: m["id"].rjust(20, "0"), reverse=True)
        offset = int(params.get("pageToken") or 0)
        page = matching[offset : offset + self.page_size]
        body: dict[str, Any] = {"messages": [{"id": m["id"], "threadId": m["threadId"]} for m in page], "resultSizeEstimate": len(matching)}
        if offset + self.page_size < len(matching):
            body["nextPageToken"] = str(offset + self.page_size)
        return body

    def inbox_ids(self, account: str) -> list[str]:
        box = self.mailboxes.get(account, {})
        return sorted((m["id"] for m in box.values() if "INBOX" in m["labelIds"]), key=lambda i: i.rjust(20, "0"), reverse=True)
