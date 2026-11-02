"""Resumable, zero-cost discovery from a sender's own archived messages."""

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any

from people_archive.primitives import PersonId
from people_archive.social_enrichment import (
    extract_profile_urls,
    profile_username_matches_person,
)
from people_archive.store import ArchiveStore, SocialProfileRow

CURSOR_KEY = "social_private_message_rowid"
COLLEAGUE_CURSOR_KEY = "social_private_colleague_backfill_rowid"
_QUOTED = re.compile(
    r"(?im)^(?:\s*>|\s*On .{5,220}wrote:\s*$|\s*From:\s|"
    r"\s*-{2,}\s*(?:Forwarded message|Original Message)|\s*_{5,})"
)


def own_message_text(text: str) -> str:
    """Strip quoted conversations before looking for the sender's signature."""
    match = _QUOTED.search(text)
    return text[: match.start()] if match else text


def _source_id(account: str, message_id: str, platform: str, url: str) -> str:
    material = f"crm_message\0{account}\0{message_id}\0{platform}\0{url}"
    return hashlib.sha256(material.encode()).hexdigest()


def _plausible_candidate(name: str, aliases_json: str, username: str) -> bool:
    """Avoid queuing unrelated people linked in an email body for review."""
    try:
        variants = [name, *json.loads(aliases_json)]
    except (ValueError, TypeError):
        variants = [name]
    compact = re.sub(r"[^a-z0-9]", "", username.lower())
    for variant in variants:
        parts = re.findall(r"[a-z0-9]+", str(variant).lower())
        if parts and len(parts[-1]) >= 4 and parts[-1] in compact:
            return True
    return False


def _mentions_linked_person_as_third_party(text: str, url: str) -> bool:
    """Treat a name-bearing introduction as ambiguous, even from a matching sender."""
    position = text.lower().find(url.lower())
    if position < 0:
        return False
    preceding = re.sub(r"\s+", " ", text[max(0, position - 180) : position].lower())
    return bool(
        re.search(
            r"(?:introduce you to|my (?:good )?friend|meet my friend|i recommend)\b",
            preceding,
        )
    )


def run_private_discovery(
    store: ArchiveStore, *, limit: int = 1000, colleague_backfill: bool = False
) -> dict[str, Any]:
    """Process a bounded message batch and durably advance only after each message."""
    cursor_key = COLLEAGUE_CURSOR_KEY if colleague_backfill else CURSOR_KEY
    cursor = int(store.social_enrichment_metadata(cursor_key) or "0")
    rows = store.execute_read(
        "SELECT rowid, account, message_id, from_addr, text, direction, is_automated FROM messages "
        "WHERE rowid>? ORDER BY rowid LIMIT ?",
        [cursor, max(1, limit)],
    )
    people: set[str] = set()
    accepted = {"linkedin": 0, "x": 0, "instagram": 0}
    candidates = {"linkedin": 0, "x": 0, "instagram": 0}
    for rowid, account, message_id, from_addr, text, direction, is_automated in rows:
        cursor = int(rowid)
        own_text = (
            own_message_text(str(text or ""))
            if direction == "RECEIVED" and not is_automated
            else ""
        )
        urls = extract_profile_urls(own_text[-1200:])
        if urls:
            identities = store.execute_read(
                "SELECT p.person_id, p.canonical_name, p.aliases_json, p.is_colleague "
                "FROM person_handles h JOIN persons p ON p.person_id=h.person_id "
                "WHERE h.handle_key=? AND p.is_human=1",
                ["email:" + str(from_addr).lower()],
            )
            if len(identities) == 1:
                person_id, name, aliases_json, is_colleague = identities[0]
                if colleague_backfill and not is_colleague:
                    store.set_social_enrichment_metadata(cursor_key, str(cursor))
                    continue
                pid = PersonId(str(person_id))
                people.add(str(pid))
                existing = {p.platform for p in store.social_profiles(pid)}
                for platform, url, username in urls:
                    if platform in existing:
                        continue
                    matched = profile_username_matches_person(
                        str(name), str(aliases_json), username
                    ) and not _mentions_linked_person_as_third_party(own_text, url)
                    if not matched and not _plausible_candidate(
                        str(name), str(aliases_json), username
                    ):
                        continue
                    state = "accepted" if matched else "needs_review"
                    reason = (
                        "Linked in the sender's own message and the username matches their full name."
                        if matched
                        else "Linked in the sender's own message; identity needs review."
                    )
                    now = datetime.now(timezone.utc)
                    source_id = _source_id(str(account), str(message_id), platform, url)
                    store.save_social_source(
                        source_id=source_id,
                        person_id=pid,
                        platform=platform,
                        source_url=url,
                        provider="crm_message",
                        fetched_at=now,
                        status=state,
                        raw={
                            "account": str(account),
                            "message_id": str(message_id),
                            "from_addr": str(from_addr),
                            "text": str(text or ""),
                            "profile_url": url,
                        },
                    )
                    store.upsert_social_candidate(
                        person_id=pid,
                        platform=platform,
                        profile_url=url,
                        username=username,
                        discovered_via="crm_message",
                        confidence=0.95 if matched else 0.5,
                        reason=reason,
                        state=state,
                        seen_at=now,
                    )
                    if matched:
                        store.save_social_profile(
                            SocialProfileRow(
                                person_id=pid,
                                platform=platform,
                                profile_url=url,
                                username=username,
                                confidence=0.95,
                                match_reason=reason,
                                discovered_via="crm_message",
                                observed_at=now,
                                source_id=source_id,
                            )
                        )
                        existing.add(platform)
                        accepted[platform] += 1
                    else:
                        candidates[platform] += 1
        store.set_social_enrichment_metadata(cursor_key, str(cursor))
    return {
        "messages_scanned": len(rows),
        "people_with_direct_links": len(people),
        "accepted": accepted,
        "candidates": candidates,
        "cursor": cursor,
        "complete": len(rows) < max(1, limit),
        "cost_usd": 0,
    }
