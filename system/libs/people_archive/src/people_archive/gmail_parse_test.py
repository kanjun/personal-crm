import pytest

from people_archive.data_types import Direction
from people_archive.gmail_parse import (
    counterpart_addresses,
    extract_body_text,
    html_to_text,
    is_automated_mail,
    parse_address_header,
    parse_gmail_message,
)
from people_archive.primitives import EmailAddress
from people_archive.testing import WORK_ACCOUNT, make_raw_gmail_message


def test_parse_address_header_keeps_names_and_normalizes_addresses() -> None:
    participants = parse_address_header('"Mira Vale" <Ada@Example.COM>, bob@example.com, not-an-address')

    assert [(p.name, str(p.address)) for p in participants] == [("Mira Vale", "ada@example.com"), ("", "bob@example.com")]


def test_parse_gmail_message_prefers_plain_text_and_reads_headers() -> None:
    raw = make_raw_gmail_message(
        message_id="m1", from_header="Mira Vale <ada@example.com>", to_header="owner@example.com",
        subject="Hello", plain_body="Plain body here", html_body="<p>HTML body</p>",
        label_ids=("INBOX",), extra_headers={"Message-ID": "<abc@x>"}, internal_date_ms=1_700_000_000_000,
    )

    parsed = parse_gmail_message(WORK_ACCOUNT, raw)

    assert parsed.sender is not None and parsed.sender.name == "Mira Vale"
    assert parsed.text == "Plain body here"
    assert parsed.direction == Direction.RECEIVED
    assert parsed.timestamp.year == 2023
    assert parsed.rfc_message_id == "<abc@x>"
    assert parsed.is_automated is False


def test_parse_gmail_message_marks_sent_and_falls_back_to_html() -> None:
    raw = make_raw_gmail_message(
        message_id="m2", from_header="owner@example.com", to_header="ada@example.com", subject="Re: Hello",
        plain_body=None, html_body="<div>Line one<br>Line <b>two</b></div><style>p{}</style>",
        label_ids=("SENT",), extra_headers={}, internal_date_ms=1_700_000_000_000,
    )

    parsed = parse_gmail_message(WORK_ACCOUNT, raw)

    assert parsed.direction == Direction.SENT
    assert parsed.text == "Line one\nLine two"


@pytest.mark.parametrize(
    ("headers", "sender", "expected"),
    [
        ({"list-unsubscribe": "<mailto:x>"}, "news@brand.com", True),
        ({"precedence": "bulk"}, "someone@example.com", True),
        ({"auto-submitted": "auto-generated"}, "someone@example.com", True),
        ({}, "no-reply@service.com", True),
        ({}, "notifications@github.com", True),
        ({"auto-submitted": "no"}, "ada@example.com", False),
        ({}, "ada@example.com", False),
    ],
)
def test_is_automated_mail_detects_bulk_and_robot_senders(headers: dict[str, str], sender: str, expected: bool) -> None:
    assert is_automated_mail(headers, sender) is expected


def test_html_to_text_strips_tags_and_collapses_blank_lines() -> None:
    assert html_to_text("<p>a</p>\n\n\n<p>b &amp; c</p>") == "a\n\nb & c"


def test_extract_body_text_handles_nested_parts() -> None:
    payload = {"mimeType": "multipart/mixed", "parts": [
        {"mimeType": "multipart/alternative", "parts": [{"mimeType": "text/plain", "body": {"data": "aGVsbG8"}}]},
        {"mimeType": "image/png", "body": {"data": "aWdub3JlZA"}},
    ]}

    assert extract_body_text(payload) == "hello"


def test_counterpart_addresses_excludes_owner_and_dedupes() -> None:
    raw = make_raw_gmail_message(
        message_id="m3", from_header="ada@example.com", to_header="owner@example.com, bob@example.com, ada@example.com",
        subject="s", plain_body="x", html_body=None, label_ids=("INBOX",), extra_headers={}, internal_date_ms=1_700_000_000_000,
    )

    parsed = parse_gmail_message(WORK_ACCOUNT, raw)

    assert counterpart_addresses(parsed, [EmailAddress("owner@example.com")]) == (EmailAddress("ada@example.com"), EmailAddress("bob@example.com"))
