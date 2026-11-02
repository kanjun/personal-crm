from pathlib import Path
from typing import Final

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from pydantic import Field

from people_archive.errors import InvalidArchiveValueError
from people_archive.primitives import EmailAddress

# Optional hand-kept contacts: one entry per line,
# ``<email-or-domain> TAB <name / role> TAB <category> TAB <notes>``. Several emails may sit
# in the first column separated by commas. Domain-wide rules (no ``@``) carry no person.
DEFAULT_HAND_KEPT_CONTACTS_PATH: Final[Path | None] = None
_COLUMN_SEPARATOR: Final[str] = "\t"


class HandKeptContact(FrozenModel):
    """One person from the user's hand-kept contacts file: the user's own words about who someone is."""

    emails: tuple[EmailAddress, ...] = Field(description="Addresses on the entry")
    name: str = Field(description="Name as the user wrote it (a parenthetical org may follow)")
    category: str = Field(description="the user's category for the classifier, kept as a tag")
    notes: str = Field(description="the user's free-text notes; may be empty")


@pure
def _split_name_and_organization(raw_name: str) -> tuple[str, str]:
    """``"Mira Vale (Example Studio)"`` -> ``("Mira Vale", "Example Studio")``."""
    if raw_name.endswith(")") and "(" in raw_name:
        name, organization = raw_name[:-1].rsplit("(", 1)
        return name.strip(), organization.strip()
    return raw_name.strip(), ""


@pure
def parse_hand_kept_contacts(text: str) -> list[HandKeptContact]:
    contacts: list[HandKeptContact] = []
    for line in text.splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        columns = line.split(_COLUMN_SEPARATOR)
        if len(columns) < 3 or "@" not in columns[0]:
            continue
        emails: list[EmailAddress] = []
        for candidate in columns[0].split(","):
            try:
                emails.append(EmailAddress(candidate))
            except InvalidArchiveValueError:
                continue
        if not emails:
            continue
        contacts.append(HandKeptContact(
            emails=tuple(emails), name=columns[1].strip(), category=columns[2].strip(),
            notes=columns[3].strip() if len(columns) > 3 else "",
        ))
    return contacts


def load_hand_kept_contacts(path: Path | None) -> list[HandKeptContact]:
    if path is None or not path.is_file():
        return []
    return parse_hand_kept_contacts(path.read_text())


@pure
def hand_kept_name_and_organization(contact: HandKeptContact) -> tuple[str, str]:
    return _split_name_and_organization(contact.name)
