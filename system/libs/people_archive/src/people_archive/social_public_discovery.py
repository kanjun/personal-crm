"""Small, resumable Bright Data search pilot for missing social profiles.

The pilot has a durable 20-input ceiling. Search results are retained before any
profile is projected; uncertain matches remain candidates for human review.
"""

import fcntl
import hashlib
import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from people_archive.primitives import PersonId
from people_archive.social_enrichment import canonical_profile_url
from people_archive.store import (
    ArchiveStore,
    SocialProfileRow,
    open_existing_archive_store,
)

DATASET_ID = "gd_mfz5x93lmsjjjylob"
PILOT_LIMIT = 20
PLATFORMS = ("linkedin", "x", "instagram")
SEARCH_DOMAINS = {
    "linkedin": "linkedin.com/in",
    "x": "x.com",
    "instagram": "instagram.com",
}
ACTIVE_KEY = "social_public_pilot_active"
USED_KEY = "social_public_pilot_inputs_used"


def _request(method: str, url: str, payload: Any = None) -> tuple[int, Any]:
    command = ["latchkey", "curl", "-sS", "-X", method]
    if payload is not None:
        command += ["-H", "Content-Type: application/json", "--data-binary", "@-"]
    command += ["-w", "\n%{http_code}", url]
    response = subprocess.run(
        command,
        input=json.dumps(payload) if payload is not None else None,
        text=True,
        capture_output=True,
        check=False,
        timeout=120,
    )
    if response.returncode:
        raise RuntimeError(f"Search request failed: {response.stderr[-200:]}")
    body, separator, status = response.stdout.rpartition("\n")
    if not separator or not status.isdigit():
        raise RuntimeError("Search returned an unreadable response")
    try:
        decoded = json.loads(body)
    except json.JSONDecodeError:
        decoded = body
    return int(status), decoded


def _text_matches_name(name: str, text: str) -> bool:
    words = re.findall(r"[a-z0-9]+", name.lower())
    cleaned = re.sub(r"[^a-z0-9]+", " ", text.lower())
    return len(words) >= 2 and all(
        re.search(rf"\b{re.escape(word)}\b", cleaned) for word in words
    )


def _result_url(result: dict[str, Any]) -> str | None:
    link = str(result.get("link") or "")
    if link.startswith("https://www.google.com/goto?"):
        try:
            response = httpx.get(link, follow_redirects=False, timeout=10)
        except httpx.HTTPError:
            return None
        link = (
            response.headers.get("location", "")
            if response.status_code in {301, 302, 303}
            else ""
        )
    return link if canonical_profile_url(link) else None


def _next_target(
    store: ArchiveStore, platform: str
) -> tuple[str, str, str, str] | None:
    rows = store.execute_read(
        "SELECT p.person_id,p.canonical_name,p.organization,p.title "
        "FROM persons p WHERE p.is_human=1 AND length(p.canonical_name)>=5 "
        "AND length(p.organization)>=3 "
        "AND NOT EXISTS (SELECT 1 FROM social_profiles s WHERE s.person_id=p.person_id AND s.platform=?) "
        "AND NOT EXISTS (SELECT 1 FROM social_enrichment_metadata m WHERE m.key='social_public_pilot_attempt:'||p.person_id||':'||?) "
        "ORDER BY p.is_colleague DESC,p.tier ASC,p.score DESC LIMIT 1",
        [platform, platform],
    )
    return tuple(str(value) for value in rows[0]) if rows else None


def _save_results(
    store: ArchiveStore, active: dict[str, Any], raw: Any
) -> dict[str, Any]:
    person_id = PersonId(active["person_id"])
    platform = active["platform"]
    now = datetime.now(timezone.utc)
    source_id = hashlib.sha256(
        f"brightdata_google\0{active['snapshot_id']}".encode()
    ).hexdigest()
    search_url = "https://www.google.com/search?q=" + quote(active["query"])
    store.save_social_source(
        source_id=source_id,
        person_id=person_id,
        platform=platform,
        source_url=search_url,
        provider="brightdata_google_search",
        fetched_at=now,
        status="searched",
        raw={"input": active, "response": raw},
    )
    record = raw[0] if isinstance(raw, list) and raw else raw
    organic = record.get("organic", []) if isinstance(record, dict) else []
    accepted = 0
    uncertain = 0
    existing = {profile.platform for profile in store.social_profiles(person_id)}
    seen: set[str] = set()
    for result in organic[:15]:
        if not isinstance(result, dict):
            continue
        url = _result_url(result)
        canonical = canonical_profile_url(url or "")
        if canonical is None or canonical[0] != platform or canonical[1] in seen:
            continue
        seen.add(canonical[1])
        title = str(result.get("title") or "")
        description = str(result.get("description") or "")
        if not _text_matches_name(active["name"], title):
            continue
        organization = active["organization"].lower()
        # A matching name by itself is a lead, not identity proof. The company
        # must also appear in the result's own title or description.
        verified = organization in (title + " " + description).lower()
        state = "accepted" if verified and platform not in existing else "needs_review"
        reason = (
            "Public profile result matches the full name and CRM organization."
            if verified
            else "Public result matches the name; professional identity is not confirmed."
        )
        store.upsert_social_candidate(
            person_id=person_id,
            platform=platform,
            profile_url=canonical[1],
            username=canonical[2],
            discovered_via="brightdata_google_search",
            confidence=0.92 if verified else 0.5,
            reason=reason,
            state=state,
            seen_at=now,
        )
        if state == "accepted":
            store.save_social_profile(
                SocialProfileRow(
                    person_id=person_id,
                    platform=platform,
                    profile_url=canonical[1],
                    username=canonical[2],
                    confidence=0.92,
                    match_reason=reason,
                    discovered_via="brightdata_google_search",
                    observed_at=now,
                    source_id=source_id,
                )
            )
            existing.add(platform)
            accepted += 1
        else:
            uncertain += 1
    store.set_social_enrichment_metadata(ACTIVE_KEY, "")
    return {
        "state": "processed",
        "person_id": str(person_id),
        "platform": platform,
        "accepted": accepted,
        "uncertain": uncertain,
        "inputs_used": int(store.social_enrichment_metadata(USED_KEY) or "0"),
    }


def _save_search_event(
    store: ArchiveStore,
    active: dict[str, Any],
    *,
    stage: str,
    response: Any,
    http_status: int | None = None,
) -> None:
    """Keep unsuccessful provider responses linked to the attempted person."""
    source_id = hashlib.sha256(
        f"brightdata_google\0{active['person_id']}\0{active['submitted_at']}\0{stage}\0"
        f"{json.dumps(response, sort_keys=True, default=str)}".encode()
    ).hexdigest()
    store.save_social_source(
        source_id=source_id,
        person_id=PersonId(active["person_id"]),
        platform=active["platform"],
        source_url="https://www.google.com/search?q=" + quote(active["query"]),
        provider="brightdata_google_search",
        fetched_at=datetime.now(timezone.utc),
        status=stage,
        raw={"input": active, "response": response, "http_status": http_status},
    )


def run_public_pilot(store: ArchiveStore) -> dict[str, Any]:
    """Submit or finish one search, keeping the global paid-input cap across runs."""
    active_text = store.social_enrichment_metadata(ACTIVE_KEY)
    if active_text:
        active = json.loads(active_text)
        snapshot_id = active.get("snapshot_id")
        if not snapshot_id:
            # The request may have reached the provider before a crash. Keep
            # its reserved input spent and move on rather than risk a retry.
            _save_search_event(
                store,
                active,
                stage="submission_unknown",
                response=active.get("submission_response"),
            )
            store.set_social_enrichment_metadata(ACTIVE_KEY, "")
            return {
                "state": "submission_unknown",
                "inputs_used": int(store.social_enrichment_metadata(USED_KEY) or "0"),
            }
        status, progress = _request(
            "GET", f"https://api.brightdata.com/datasets/v3/progress/{snapshot_id}"
        )
        if status != 200 or not isinstance(progress, dict):
            _save_search_event(
                store,
                active,
                stage="progress_error",
                response=progress,
                http_status=status,
            )
            raise RuntimeError(f"Search progress returned HTTP {status}")
        if progress.get("status") == "failed":
            _save_search_event(
                store, active, stage="failed", response=progress, http_status=status
            )
            store.set_social_enrichment_metadata(ACTIVE_KEY, "")
            return {
                "state": "failed",
                "person_id": active["person_id"],
                "platform": active["platform"],
            }
        if progress.get("status") != "ready":
            return {
                "state": "pending",
                "person_id": active["person_id"],
                "platform": active["platform"],
            }
        status, raw = _request(
            "GET",
            f"https://api.brightdata.com/datasets/v3/snapshot/{snapshot_id}?format=json",
        )
        if status != 200:
            raise RuntimeError(f"Search snapshot returned HTTP {status}")
        return _save_results(store, active, raw)

    used = int(store.social_enrichment_metadata(USED_KEY) or "0")
    if used >= PILOT_LIMIT:
        return {"state": "pilot_complete", "inputs_used": used}
    platform = PLATFORMS[used % len(PLATFORMS)]
    target = _next_target(store, platform)
    if target is None:
        return {"state": "no_targets", "platform": platform, "inputs_used": used}
    person_id, name, organization, title = target
    query = f'"{name}" "{organization}" site:{SEARCH_DOMAINS[platform]}'
    active = {
        "person_id": person_id,
        "name": name,
        "organization": organization,
        "title": title,
        "platform": platform,
        "query": query,
        "submitted_at": datetime.now(timezone.utc).isoformat(),
    }
    # Reserve the input before sending. A crash cannot silently spend it twice.
    store.set_social_enrichment_metadata(USED_KEY, str(used + 1))
    store.set_social_enrichment_metadata(
        f"social_public_pilot_attempt:{person_id}:{platform}", active["submitted_at"]
    )
    store.set_social_enrichment_metadata(ACTIVE_KEY, json.dumps(active))
    payload = [
        {
            "url": "https://www.google.com/",
            "keyword": query,
            "language": "en",
            "uule": "",
            "brd_mobile": "",
            "tbs": "",
            "tbm": "",
            "nfpr": "",
            "index": "",
        }
    ]
    status, response = _request(
        "POST",
        f"https://api.brightdata.com/datasets/v3/scrape?dataset_id={DATASET_ID}&format=json&include_errors=true",
        payload,
    )
    if status == 202 and isinstance(response, dict) and response.get("snapshot_id"):
        active["snapshot_id"] = response["snapshot_id"]
        active["submission_response"] = response
        store.set_social_enrichment_metadata(ACTIVE_KEY, json.dumps(active))
        return {
            "state": "pending",
            "person_id": person_id,
            "platform": platform,
            "inputs_used": used + 1,
        }
    if status == 200:
        active["snapshot_id"] = f"sync_{person_id}_{platform}"
        return _save_results(store, active, response)
    _save_search_event(
        store, active, stage="submission_error", response=response, http_status=status
    )
    store.set_social_enrichment_metadata(ACTIVE_KEY, "")
    raise RuntimeError(
        f"Search submission returned HTTP {status}: {str(response)[:200]}"
    )


def run_public_pilot_locked(archive_path: Path) -> dict[str, Any]:
    """Serialize the entire read/reserve/request/save cycle for one archive."""
    archive_path = archive_path.resolve()
    lock_path = archive_path.with_suffix(".brightdata-discovery.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as discovery_lock:
        fcntl.flock(discovery_lock, fcntl.LOCK_EX)
        with open_existing_archive_store(archive_path) as store:
            return run_public_pilot(store)
