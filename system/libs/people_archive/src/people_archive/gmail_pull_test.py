from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from pydantic import Field

from people_archive.gmail_pull import is_full_pull_done, pull_gmail_full
from people_archive.interfaces import GoogleApiGatewayInterface
from people_archive.primitives import AccountEmail
from people_archive.store import ArchiveStore
from people_archive.testing import make_raw_gmail_message

_BULK_EXCLUSION = "-category:promotions -category:updates -category:forums -category:social"


class _FakeGmail(GoogleApiGatewayInterface):
    """Answers profile, one page of listing, and message fetches; records every listing query."""

    listing_queries: list[str] = Field(default_factory=list, description="The q= of every listing call, in order")

    def get_json(self, account: AccountEmail, url: str, params: Mapping[str, str] | Sequence[tuple[str, str]]) -> dict[str, Any]:
        params = dict(params.items()) if isinstance(params, Mapping) else dict(params)
        if url.endswith("/profile"):
            return {"historyId": "42"}
        if url.endswith("/messages"):
            self.listing_queries.append(params.get("q", ""))
            return {"messages": [{"id": "m1"}]}
        return make_raw_gmail_message(
            "m1", "Ada <ada@example.com>", "owner@example.com", "hello", "hi there", None, ("INBOX",), {}, 1_700_000_000_000,
        )


def test_full_walk_lists_only_mail_outside_the_bulk_categories(archive_store: ArchiveStore, tmp_path: Path) -> None:
    gateway = _FakeGmail()
    account = AccountEmail("owner@example.com")
    summary = pull_gmail_full(gateway, archive_store, account, 1, tmp_path / "ingest.jsonl")
    # One pass, and it excludes promotions/updates/forums/social: no unfiltered walk of the whole mailbox.
    assert gateway.listing_queries == [_BULK_EXCLUSION]
    assert summary.fetched_count == 1
    assert is_full_pull_done(archive_store, account)
