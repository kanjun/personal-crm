from people_archive.contacts_pull import parse_contact_card
from people_archive.testing import WORK_ACCOUNT


def test_parse_contact_card_normalizes_and_dedupes_handles() -> None:
    raw = {
        "resourceName": "people/c1",
        "names": [{"displayName": "Mira Vale"}],
        "emailAddresses": [{"value": "Ada@Example.com"}, {"value": "ada@example.com"}, {"value": "bad"}],
        "phoneNumbers": [{"value": "(555) 010-0001", "canonicalForm": "+15550100001"}, {"value": "x"}],
        "organizations": [{"name": "Example Workshop", "title": "Founder"}],
    }

    card = parse_contact_card(WORK_ACCOUNT, raw)

    assert card.display_name == "Mira Vale"
    assert card.emails == ("ada@example.com",)
    assert card.phones == ("+15550100001",)
    assert (card.organization, card.title) == ("Example Workshop", "Founder")


def test_parse_contact_card_tolerates_a_bare_card() -> None:
    card = parse_contact_card(WORK_ACCOUNT, {"resourceName": "people/c2"})

    assert card.display_name == "" and card.emails == () and card.phones == ()
