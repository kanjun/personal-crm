import base64
import binascii
import email.utils
import html
import re
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any, Final

from imbue.imbue_common.pure import pure

from people_archive.data_types import (
    Direction,
    GmailLabelRow,
    MessageParticipant,
    ParsedEmail,
)
from people_archive.errors import InvalidArchiveValueError
from people_archive.primitives import (
    AccountEmail,
    EmailAddress,
    GmailMessageId,
    GmailThreadId,
)

# Bodies past this are truncated in the projection; the raw record keeps the whole thing.
MAX_BODY_TEXT_CHARS: Final[int] = 200_000

_SENT_LABEL: Final[str] = "SENT"
_AUTOMATED_PRECEDENCE: Final[frozenset[str]] = frozenset({"bulk", "list", "junk", "auto_reply"})
_AUTOMATED_SENDER_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^(no-?reply|do-?not-?reply|donotreply|notifications?|notify|alerts?|mailer-daemon|postmaster|bounce[s]?|"
    r"newsletter|news|updates?|info|hello|support|team|billing|receipts?|invoices?|calendar-notification|"
    r"drive-shares-noreply|comments-noreply|docs-noreply|meetings-noreply|hangouts-noreply)([+.-].*)?@",
    re.IGNORECASE,
)
_HTML_BLOCK_TAG_PATTERN: Final[re.Pattern[str]] = re.compile(r"</?(p|div|br|tr|li|h[1-6]|blockquote|table)[^>]*>", re.IGNORECASE)
_HTML_DROP_PATTERN: Final[re.Pattern[str]] = re.compile(r"<(script|style|head)[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
_HTML_TAG_PATTERN: Final[re.Pattern[str]] = re.compile(r"<[^>]+>")
_BLANK_LINES_PATTERN: Final[re.Pattern[str]] = re.compile(r"\n\s*\n\s*\n+")


@pure
def _headers_by_lowercase_name(payload: Mapping[str, Any]) -> dict[str, str]:
    """First value per header name; Gmail repeats some headers (Received), which we don't need."""
    result: dict[str, str] = {}
    for header in payload.get("headers", []) or []:
        name = str(header.get("name", "")).lower()
        if name and name not in result:
            result[name] = str(header.get("value", ""))
    return result


@pure
def parse_address_header(value: str) -> tuple[MessageParticipant, ...]:
    """Every parseable name/address pair in a From/To/Cc header; unparseable entries are dropped."""
    participants: list[MessageParticipant] = []
    for name, address in email.utils.getaddresses([value]):
        try:
            normalized = EmailAddress(address)
        except InvalidArchiveValueError:
            continue
        participants.append(MessageParticipant(name=name.strip().strip('"'), address=normalized))
    return tuple(participants)


@pure
def _decode_body_data(data: str) -> str:
    try:
        raw_bytes = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
    except (binascii.Error, ValueError):
        return ""
    return raw_bytes.decode("utf-8", errors="replace")


@pure
def html_to_text(markup: str) -> str:
    without_hidden = _HTML_DROP_PATTERN.sub(" ", markup)
    with_breaks = _HTML_BLOCK_TAG_PATTERN.sub("\n", without_hidden)
    stripped = _HTML_TAG_PATTERN.sub(" ", with_breaks)
    unescaped = html.unescape(stripped)
    lines = [re.sub(r"[ \t\r\f\v]+", " ", line).strip() for line in unescaped.split("\n")]
    return _BLANK_LINES_PATTERN.sub("\n\n", "\n".join(lines)).strip()


@pure
def _collect_body_parts(part: Mapping[str, Any], plain_parts: list[str], html_parts: list[str]) -> None:
    mime_type = str(part.get("mimeType", ""))
    body_data = (part.get("body") or {}).get("data")
    if body_data:
        if mime_type == "text/plain":
            plain_parts.append(_decode_body_data(str(body_data)))
        elif mime_type == "text/html":
            html_parts.append(_decode_body_data(str(body_data)))
        else:
            pass
    for child in part.get("parts", []) or []:
        _collect_body_parts(child, plain_parts, html_parts)


@pure
def extract_body_text(payload: Mapping[str, Any]) -> str:
    """Plain text of the message: the text/plain parts when present, else the HTML parts flattened."""
    plain_parts: list[str] = []
    html_parts: list[str] = []
    _collect_body_parts(payload, plain_parts, html_parts)
    plain = "\n".join(p for p in plain_parts if p.strip()).strip()
    text = plain if plain else html_to_text("\n".join(html_parts))
    return text[:MAX_BODY_TEXT_CHARS]


@pure
def is_automated_mail(headers: Mapping[str, str], sender_address: str) -> bool:
    """Bulk, list, or machine-generated mail: not evidence of a human relationship."""
    if "list-unsubscribe" in headers or "list-id" in headers:
        return True
    if headers.get("precedence", "").lower() in _AUTOMATED_PRECEDENCE:
        return True
    auto_submitted = headers.get("auto-submitted", "").lower()
    if auto_submitted and auto_submitted != "no":
        return True
    return bool(_AUTOMATED_SENDER_PATTERN.match(sender_address))


@pure
def parse_gmail_message(account: AccountEmail, raw: Mapping[str, Any]) -> ParsedEmail:
    """Project one ``messages.get?format=full`` response into the archive's message record."""
    payload = raw.get("payload") or {}
    headers = _headers_by_lowercase_name(payload)
    senders = parse_address_header(headers.get("from", ""))
    sender = senders[0] if senders else None
    label_ids: tuple[str, ...] = tuple(str(label) for label in raw.get("labelIds", []) or [])
    internal_date_ms = int(raw.get("internalDate", "0") or 0)
    return ParsedEmail(
        account=account,
        message_id=GmailMessageId(str(raw["id"])),
        thread_id=GmailThreadId(str(raw.get("threadId") or raw["id"])),
        timestamp=datetime.fromtimestamp(internal_date_ms / 1000, tz=timezone.utc),
        direction=Direction.SENT if _SENT_LABEL in label_ids else Direction.RECEIVED,
        sender=sender,
        to_recipients=parse_address_header(headers.get("to", "")),
        cc_recipients=parse_address_header(headers.get("cc", "")),
        subject=headers.get("subject", "").strip(),
        snippet=html.unescape(str(raw.get("snippet", ""))),
        text=extract_body_text(payload),
        label_ids=label_ids,
        is_automated=is_automated_mail(headers, str(sender.address) if sender else ""),
        rfc_message_id=headers.get("message-id", "").strip(),
        in_reply_to=headers.get("in-reply-to", "").strip(),
    )


@pure
def counterpart_addresses(message: ParsedEmail, own_addresses: Sequence[EmailAddress]) -> tuple[EmailAddress, ...]:
    """Everyone on the message other than the user, sender first."""
    own = {str(a) for a in own_addresses}
    ordered: list[EmailAddress] = []
    seen: set[str] = set()
    participants = ([message.sender] if message.sender else []) + list(message.to_recipients) + list(message.cc_recipients)
    for participant in participants:
        key = str(participant.address)
        if key in own or key in seen:
            continue
        seen.add(key)
        ordered.append(participant.address)
    return tuple(ordered)


@pure
def label_row_from_raw(account: AccountEmail, raw: dict[str, Any]) -> GmailLabelRow:
    """The label row a fetched message (full or minimal format) describes."""
    message_id = str(raw["id"])
    return GmailLabelRow(
        account=account,
        message_id=GmailMessageId(message_id),
        thread_id=GmailThreadId(str(raw.get("threadId") or message_id)),
        internal_ms=int(raw.get("internalDate", "0") or 0),
        label_ids=tuple(sorted({str(label) for label in raw.get("labelIds", []) or []})),
        is_deleted=False,
    )


@pure
def bcc_addresses(raw: Mapping[str, Any]) -> tuple[str, ...]:
    """Everyone a message was Bcc'd to (only the user's own copy of what the user sent carries the header)."""
    headers = _headers_by_lowercase_name(raw.get("payload") or {})
    return tuple(sorted({str(p.address) for p in parse_address_header(headers.get("bcc", ""))}))
