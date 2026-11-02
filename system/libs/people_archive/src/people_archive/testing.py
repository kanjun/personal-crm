import base64
from datetime import datetime, timezone
from typing import Any

from people_archive.data_types import (
    ContactCard,
    Direction,
    MessageParticipant,
    ParsedEmail,
)
from people_archive.primitives import (
    AccountEmail,
    ContactResourceName,
    EmailAddress,
    GmailMessageId,
    GmailThreadId,
    PhoneNumber,
)

WORK_ACCOUNT = AccountEmail("owner@example.com")


def make_parsed_email(
    message_id: str,
    sender: str,
    recipients: tuple[str, ...],
    direction: Direction,
    timestamp: datetime,
    subject: str,
    text: str,
    is_automated: bool,
    sender_name: str,
) -> ParsedEmail:
    return ParsedEmail(
        account=WORK_ACCOUNT,
        message_id=GmailMessageId(message_id),
        thread_id=GmailThreadId(f"thread-{message_id}"),
        timestamp=timestamp,
        direction=direction,
        sender=MessageParticipant(name=sender_name, address=EmailAddress(sender)),
        to_recipients=tuple(MessageParticipant(name="", address=EmailAddress(r)) for r in recipients),
        cc_recipients=(),
        subject=subject,
        snippet=text[:60],
        text=text,
        label_ids=("SENT",) if direction == Direction.SENT else ("INBOX",),
        is_automated=is_automated,
        rfc_message_id=f"<{message_id}@example.com>",
        in_reply_to="",
    )


def make_contact_card(resource: str, name: str, emails: tuple[str, ...], phones: tuple[str, ...], organization: str) -> ContactCard:
    return ContactCard(
        account=WORK_ACCOUNT,
        resource_name=ContactResourceName(resource),
        display_name=name,
        emails=tuple(EmailAddress(e) for e in emails),
        phones=tuple(PhoneNumber(p) for p in phones),
        organization=organization,
        title="",
    )


def _b64url(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def make_raw_gmail_message(
    message_id: str,
    from_header: str,
    to_header: str,
    subject: str,
    plain_body: str | None,
    html_body: str | None,
    label_ids: tuple[str, ...],
    extra_headers: dict[str, str],
    internal_date_ms: int,
) -> dict[str, Any]:
    """A ``messages.get?format=full`` response shaped like Gmail's, with the given parts."""
    headers = [{"name": "From", "value": from_header}, {"name": "To", "value": to_header}, {"name": "Subject", "value": subject}]
    headers += [{"name": name, "value": value} for name, value in extra_headers.items()]
    parts: list[dict[str, Any]] = []
    if plain_body is not None:
        parts.append({"mimeType": "text/plain", "body": {"data": _b64url(plain_body)}})
    if html_body is not None:
        parts.append({"mimeType": "text/html", "body": {"data": _b64url(html_body)}})
    return {
        "id": message_id,
        "threadId": f"thread-{message_id}",
        "labelIds": list(label_ids),
        "snippet": (plain_body or html_body or "")[:40],
        "internalDate": str(internal_date_ms),
        "payload": {"mimeType": "multipart/alternative", "headers": headers, "parts": parts},
    }


def utc(year: int, month: int, day: int) -> datetime:
    return datetime(year, month, day, 12, 0, tzinfo=timezone.utc)
