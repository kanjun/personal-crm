from datetime import datetime
from enum import auto

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from pydantic import Field

from people_archive.primitives import (
    AccountEmail,
    CalendarEventId,
    ContactResourceName,
    Direction,
    EmailAddress,
    GmailMessageId,
    GmailThreadId,
    HandleKey,
    InteractionKind,
    PersonId,
    PhoneNumber,
    SourceKind,
)


class MessageParticipant(FrozenModel):
    """One name/address pair from an email header."""

    name: str = Field(description="Display name as written in the header; may be empty")
    address: EmailAddress = Field(description="The normalized address")


class ParsedEmail(FrozenModel):
    """The projection of one Gmail message the archive keeps alongside the raw record."""

    account: AccountEmail = Field(description="Which of the user's inboxes this came from")
    message_id: GmailMessageId = Field(description="Gmail message id")
    thread_id: GmailThreadId = Field(description="Gmail thread id")
    timestamp: datetime = Field(description="When Gmail received it (internalDate), UTC")
    direction: Direction = Field(description="SENT when the user wrote it, else RECEIVED")
    sender: MessageParticipant | None = Field(description="From header, when parseable")
    to_recipients: tuple[MessageParticipant, ...] = Field(description="To header")
    cc_recipients: tuple[MessageParticipant, ...] = Field(description="Cc header")
    subject: str = Field(description="Subject header; may be empty")
    snippet: str = Field(description="Gmail's short preview")
    text: str = Field(description="Best-effort plain text of the body")
    label_ids: tuple[str, ...] = Field(description="Gmail label ids on the message")
    is_automated: bool = Field(description="Bulk, list, or auto-generated mail rather than a person writing")
    rfc_message_id: str = Field(description="Message-ID header; may be empty")
    in_reply_to: str = Field(description="In-Reply-To header; may be empty")


class ContactCard(FrozenModel):
    """One Google Contacts card, projected."""

    account: AccountEmail = Field(description="Which Google account holds the card")
    resource_name: ContactResourceName = Field(description="People API resource name")
    display_name: str = Field(description="Primary display name; may be empty")
    emails: tuple[EmailAddress, ...] = Field(description="All email addresses on the card")
    phones: tuple[PhoneNumber, ...] = Field(description="All phone numbers on the card")
    organization: str = Field(description="Company name on the card; may be empty")
    title: str = Field(description="Job title on the card; may be empty")


class EventAttendee(FrozenModel):
    """One attendee line on a calendar event."""

    address: EmailAddress = Field(description="Attendee email")
    name: str = Field(description="Display name if Google provided one")
    response_status: str = Field(description="accepted / declined / tentative / needsAction")
    is_self: bool = Field(description="True when this attendee is the account owner")


class CalendarEvent(FrozenModel):
    """One Google Calendar event, projected."""

    account: AccountEmail = Field(description="Which calendar it came from")
    event_id: CalendarEventId = Field(description="Google's event id")
    series_id: CalendarEventId = Field(description="The recurring series' id, or the event's own id for a one-off")
    summary: str = Field(description="Event title; may be empty")
    start: datetime = Field(description="Start, UTC")
    end: datetime = Field(description="End, UTC")
    organizer: EmailAddress | None = Field(description="Organizer email when present")
    attendees: tuple[EventAttendee, ...] = Field(description="Everyone on the invite")
    is_organized_by_owner: bool = Field(description="True when the user is the organizer")


class Interaction(FrozenModel):
    """One row of the unified, searchable index: an email or a meeting, with who was on the other end."""

    kind: InteractionKind = Field(description="Email or meeting")
    source: SourceKind = Field(description="Which system it came from")
    source_id: str = Field(description="The source's own id for the record")
    account: AccountEmail = Field(description="Which of the user's accounts saw it")
    timestamp: datetime = Field(description="When it happened, UTC")
    direction: Direction = Field(description="Sent, received, or observed")
    title: str = Field(description="Subject or event title")
    text: str = Field(description="Searchable body text")
    counterpart_handles: tuple[HandleKey, ...] = Field(description="Handles of everyone other than the user")


class EvidenceKind(UpperCaseStrEnum):
    """How strongly a piece of evidence ties two handles to one person."""

    # Hard: auto-merge.
    CONTACT_CARD = auto()
    # Soft: propose only.
    SAME_DISPLAY_NAME = auto()


class HandleEvidence(FrozenModel):
    """Evidence that two handles belong to the same person."""

    handle_a: HandleKey = Field(description="One handle")
    handle_b: HandleKey = Field(description="The other handle")
    kind: EvidenceKind = Field(description="What kind of evidence links them")
    source: SourceKind = Field(description="Where the evidence was seen")
    source_id: str = Field(description="The record that carries it")


class HandleObservation(FrozenModel):
    """A handle as seen in one place, with the display name attached to it there."""

    handle: HandleKey = Field(description="The handle")
    display_name: str = Field(description="Name seen alongside it; may be empty")
    source: SourceKind = Field(description="Where it was seen")


class Person(FrozenModel):
    """One resolved person: a cluster of handles under a stable id."""

    person_id: PersonId = Field(description="Stable id")
    canonical_name: str = Field(description="Best display name")
    aliases: tuple[str, ...] = Field(description="Other names seen for the same handles")
    handles: tuple[HandleKey, ...] = Field(description="Every handle in the cluster")


class PullSummary(FrozenModel):
    """What one pull run did."""

    source: SourceKind = Field(description="Which source was pulled")
    account: AccountEmail = Field(description="Which account")
    fetched_count: int = Field(description="Records newly fetched and stored")
    skipped_count: int = Field(description="Records already present and left alone")
    failed_count: int = Field(description="Records that could not be fetched after retries")


class GmailLabelRow(FrozenModel):
    """What the Gmail sync knows about one message's labels right now."""

    account: AccountEmail = Field(description="Mailbox the message is in")
    message_id: GmailMessageId = Field(description="Gmail's message id")
    thread_id: GmailThreadId = Field(description="Gmail's thread id")
    internal_ms: int = Field(description="Gmail's internalDate in milliseconds; 0 when unknown")
    label_ids: tuple[str, ...] = Field(description="Current label ids, sorted")
    is_deleted: bool = Field(description="Gmail reported the message deleted")


class GmailChangeKind(UpperCaseStrEnum):
    """What happened to a message, as recorded in the change feed."""

    ADDED = auto()
    LABELS = auto()
    DELETED = auto()
