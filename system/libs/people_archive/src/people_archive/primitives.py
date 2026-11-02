import re
from enum import auto
from typing import Any, Final, Self

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.ids import RandomId
from imbue.imbue_common.primitives import NonEmptyStr
from imbue.imbue_common.pure import pure
from pydantic import GetCoreSchemaHandler
from pydantic_core import CoreSchema, core_schema

from people_archive.errors import InvalidArchiveValueError

_EMAIL_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
# Digits only after normalization; 7-15 digits is the E.164 range plus short local numbers.
_PHONE_DIGITS_PATTERN: Final[re.Pattern[str]] = re.compile(r"^\+?\d{7,15}$")
_NON_PHONE_CHARS_PATTERN: Final[re.Pattern[str]] = re.compile(r"[^\d+]")


class SourceKind(UpperCaseStrEnum):
    """Which system a raw record or interaction came from."""

    GMAIL = auto()
    GOOGLE_CONTACTS = auto()
    GOOGLE_CALENDAR = auto()
    BEEPER = auto()
    NOTES = auto()
    GRANOLA = auto()


class InteractionKind(UpperCaseStrEnum):
    """What sort of interaction a row in the unified index is."""

    EMAIL = auto()
    MEETING = auto()


class Direction(UpperCaseStrEnum):
    """Whether the user sent the interaction, received it, or merely observed it."""

    SENT = auto()
    RECEIVED = auto()
    OBSERVED = auto()


class HandleNetwork(UpperCaseStrEnum):
    """The namespace an address lives in; a handle is unique within its network."""

    EMAIL = auto()
    PHONE = auto()
    # Chat networks whose participants have no phone or email: keyed by username or opaque id.
    IMESSAGE = auto()
    WHATSAPP = auto()
    SIGNAL = auto()
    TELEGRAM = auto()
    X = auto()
    INSTAGRAM = auto()
    FACEBOOK = auto()
    MATRIX = auto()
    OTHER_CHAT = auto()


class AccountEmail(NonEmptyStr):
    """The email address of one of the user's own connected Google accounts."""


class EmailAddress(str):
    """An email address, lowercased and stripped; the identity of an email handle."""

    def __new__(cls, value: str) -> Self:
        normalized = value.strip().lower()
        if not _EMAIL_PATTERN.match(normalized):
            raise InvalidArchiveValueError(f"Not an email address: {value!r}")
        return super().__new__(cls, normalized)

    @classmethod
    def __get_pydantic_core_schema__(cls, source_type: Any, handler: GetCoreSchemaHandler) -> CoreSchema:
        return core_schema.no_info_after_validator_function(cls, core_schema.str_schema())


class PhoneNumber(str):
    """A phone number reduced to digits (leading + kept); the identity of a phone handle."""

    def __new__(cls, value: str) -> Self:
        normalized = _NON_PHONE_CHARS_PATTERN.sub("", value)
        if not _PHONE_DIGITS_PATTERN.match(normalized):
            raise InvalidArchiveValueError(f"Not a phone number: {value!r}")
        return super().__new__(cls, normalized)

    @classmethod
    def __get_pydantic_core_schema__(cls, source_type: Any, handler: GetCoreSchemaHandler) -> CoreSchema:
        return core_schema.no_info_after_validator_function(cls, core_schema.str_schema())


class GmailMessageId(NonEmptyStr):
    """Gmail's id for one message, unique within one account."""


class GmailThreadId(NonEmptyStr):
    """Gmail's id for one thread, unique within one account."""


class CalendarEventId(NonEmptyStr):
    """Google Calendar's id for one event."""


class ContactResourceName(NonEmptyStr):
    """The People API resource name of one contact (``people/c123``)."""


class PersonId(RandomId):
    """Stable id of one resolved person; assigned once, never reused."""


class HandleKey(NonEmptyStr):
    """``<network>:<address>`` -- the string key of one handle across the archive."""


@pure
def handle_key(network: HandleNetwork, address: str) -> HandleKey:
    return HandleKey(f"{network.value.lower()}:{address}")
