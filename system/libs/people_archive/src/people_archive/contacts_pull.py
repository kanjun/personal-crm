from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any, Final

from imbue.imbue_common.pure import pure
from loguru import logger

from people_archive.data_types import ContactCard, PullSummary
from people_archive.errors import InvalidArchiveValueError
from people_archive.interfaces import GoogleApiGatewayInterface
from people_archive.primitives import (
    AccountEmail,
    ContactResourceName,
    EmailAddress,
    PhoneNumber,
    SourceKind,
)
from people_archive.store import ArchiveStore

_PEOPLE_BASE: Final[str] = "https://people.googleapis.com/v1"
_PERSON_FIELDS: Final[str] = "names,emailAddresses,phoneNumbers,organizations"
_PAGE_SIZE: Final[str] = "1000"


@pure
def parse_contact_card(account: AccountEmail, raw: Mapping[str, Any]) -> ContactCard:
    """Project one People API person into a contact card; unparseable addresses are dropped."""
    names = raw.get("names") or []
    display_name = str(names[0].get("displayName", "")) if names else ""
    emails: list[EmailAddress] = []
    for entry in raw.get("emailAddresses") or []:
        try:
            emails.append(EmailAddress(str(entry.get("value", ""))))
        except InvalidArchiveValueError:
            continue
    phones: list[PhoneNumber] = []
    for entry in raw.get("phoneNumbers") or []:
        candidate = str(entry.get("canonicalForm") or entry.get("value") or "")
        try:
            phones.append(PhoneNumber(candidate))
        except InvalidArchiveValueError:
            continue
    organizations = raw.get("organizations") or []
    first_org = organizations[0] if organizations else {}
    return ContactCard(
        account=account,
        resource_name=ContactResourceName(str(raw["resourceName"])),
        display_name=display_name,
        emails=tuple(dict.fromkeys(emails)),
        phones=tuple(dict.fromkeys(phones)),
        organization=str(first_org.get("name", "")),
        title=str(first_org.get("title", "")),
    )


def pull_contacts(gateway: GoogleApiGatewayInterface, store: ArchiveStore, account: AccountEmail) -> PullSummary:
    """Re-read the whole address book (a few thousand cards; cheap) and upsert every card."""
    fetched = 0
    page_token: str | None = ""
    fetched_at = datetime.now(timezone.utc)
    while page_token is not None:
        params = {"personFields": _PERSON_FIELDS, "pageSize": _PAGE_SIZE}
        if page_token:
            params["pageToken"] = page_token
        page = gateway.get_json(account, f"{_PEOPLE_BASE}/people/me/connections", params)
        with store.transaction():
            for raw in page.get("connections", []) or []:
                store.upsert_raw_record(SourceKind.GOOGLE_CONTACTS, account, str(raw["resourceName"]), raw, fetched_at)
                store.upsert_contact(parse_contact_card(account, raw))
                fetched += 1
        page_token = page.get("nextPageToken") or None
    logger.info("Contacts {}: {} cards stored", account, fetched)
    return PullSummary(source=SourceKind.GOOGLE_CONTACTS, account=account, fetched_count=fetched, skipped_count=0, failed_count=0)
