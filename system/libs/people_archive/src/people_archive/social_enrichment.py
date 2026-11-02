"""Resumable public social-profile enrichment for CRM people.

Exact handles and repeated signature links are accepted without a web search. Ambiguous
links remain candidates for a GPT identity judgment. Bright Data is called only for accepted
profile URLs, and every response is preserved before projected follower counts are written.
"""

import hashlib
import json
import re
import sqlite3
import subprocess
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from typing import Any, Final
from urllib.parse import quote, unquote, urlsplit, urlunsplit

from imbue.imbue_common.frozen_model import FrozenModel
from pydantic import Field, ValidationError

from people_archive.cleanup_gpt import SOL_MODEL, ask_gpt
from people_archive.primitives import PersonId
from people_archive.store import ArchiveStore, SocialProfileRow

PLATFORMS: Final[tuple[str, ...]] = ("linkedin", "x", "instagram")
DATASET_BY_PLATFORM: Final[dict[str, str]] = {
    "linkedin": "gd_l1viktl72bvl7bjuj0",
    "x": "gd_lwxmeb2u1cniijd7t4",
    "instagram": "gd_l1vikfch901nx3by4",
}
_PROFILE_URL = re.compile(
    r"https?://(?:www\.)?(?:linkedin\.com/in/[^\s<>'\"()]+|(?:x|twitter)\.com/[^\s<>'\"()]+|instagram\.com/[^\s<>'\"()]+)",
    re.IGNORECASE,
)
_TRAILING_PUNCTUATION = ".,;:!?]}"
_IGNORED_SLUGS: Final[frozenset[str]] = frozenset(
    {
        "home",
        "intent",
        "share",
        "search",
        "explore",
        "reel",
        "reels",
        "p",
        "stories",
        "i",
        "login",
    }
)


class BrightDataError(RuntimeError):
    """Bright Data refused or could not complete a request."""


class BrightDataInactive(BrightDataError):
    """The key is valid but the Bright Data customer has not been activated."""


class BrightDataPending(BrightDataError):
    """Bright Data has accepted the request and will deliver a snapshot later."""

    def __init__(self, snapshot_id: str) -> None:
        super().__init__(f"Bright Data snapshot {snapshot_id} is pending")
        self.snapshot_id = snapshot_id


class BrightDataSnapshotFailed(BrightDataError):
    """Bright Data finished a delayed request without a usable snapshot."""


class ParsedSocialProfile(FrozenModel):
    """Network-neutral projection of one Bright Data profile response."""

    platform: str
    profile_url: str
    username: str
    display_name: str = ""
    follower_count: int | None = None
    following_count: int | None = None
    connection_count: int | None = None
    biography: str = ""
    headline: str = ""
    is_verified: bool | None = None


class EnrichmentRunSummary(FrozenModel):
    """What one bounded pass accomplished."""

    queued_now: int = 0
    exact_profiles_added: int = 0
    signature_candidates_added: int = 0
    profiles_fetched: int = 0
    profiles_errored: int = 0
    requests_uncertain: int = 0
    state: str = "running"
    detail: str = ""
    totals: dict[str, Any] = Field(default_factory=dict)


class DiscoveredProfile(FrozenModel):
    platform: str
    profile_url: str
    confidence: float
    match_reason: str
    evidence_url: str


class PersonSocialDiscovery(FrozenModel):
    person_id: str
    profiles: tuple[DiscoveredProfile, ...]
    summary: str


class SocialDiscoveryBatch(FrozenModel):
    people: tuple[PersonSocialDiscovery, ...]


CommandRunner = Callable[..., subprocess.CompletedProcess[str]]


def _integer(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    text = str(value).strip().lower().replace(",", "").rstrip("+")
    multiplier = 1
    if text.endswith("k"):
        multiplier, text = 1_000, text[:-1]
    elif text.endswith("m"):
        multiplier, text = 1_000_000, text[:-1]
    try:
        return int(float(text) * multiplier)
    except ValueError:
        return None


def canonical_profile_url(value: str) -> tuple[str, str, str] | None:
    """Return ``(platform, canonical_url, username)`` for a profile URL, else None."""
    cleaned = value.strip().rstrip(_TRAILING_PUNCTUATION)
    try:
        parsed = urlsplit(cleaned)
    except ValueError:
        return None
    host = (parsed.hostname or "").lower()
    segments = [part for part in parsed.path.split("/") if part]
    if (
        (host == "linkedin.com" or host.endswith(".linkedin.com"))
        and len(segments) >= 2
        and segments[0].lower() == "in"
    ):
        platform, username = "linkedin", segments[1]
        decoded_username = unquote(username)
        if not re.fullmatch(r"[\w.-]{3,100}", decoded_username):
            return None
        username = quote(decoded_username, safe="-_.")
        path = f"/in/{username}"
        host = "www.linkedin.com"
    elif (
        host in {"x.com", "www.x.com", "twitter.com", "www.twitter.com"}
        and len(segments) == 1
    ):
        platform, username = "x", segments[0].lstrip("@")
        path, host = f"/{username}", "x.com"
    elif host in {"instagram.com", "www.instagram.com"} and len(segments) == 1:
        platform, username = "instagram", segments[0].lstrip("@")
        path, host = f"/{username}", "www.instagram.com"
    else:
        return None
    valid_username = {
        "linkedin": r"[A-Za-z0-9_%.\-]{3,300}",
        "x": r"[A-Za-z0-9_]{1,15}",
        "instagram": r"[A-Za-z0-9._]{1,30}",
    }[platform]
    if not re.fullmatch(valid_username, username) or username.lower() in _IGNORED_SLUGS:
        return None
    return platform, urlunsplit(("https", host, path, "", "")), username


def extract_profile_urls(text: str) -> tuple[tuple[str, str, str], ...]:
    """Extract unique profile URLs while excluding posts, shares, reels, and other non-profiles."""
    found: dict[tuple[str, str], tuple[str, str, str]] = {}
    for match in _PROFILE_URL.finditer(text):
        parsed = canonical_profile_url(match.group(0))
        if parsed is not None:
            found[(parsed[0], parsed[1])] = parsed
    return tuple(found.values())


def profile_from_handle(platform: str, username: str) -> tuple[str, str, str]:
    host = {"x": "x.com", "instagram": "www.instagram.com"}[platform]
    return platform, f"https://{host}/{username.lstrip('@')}", username.lstrip("@")


def parse_brightdata_profile(
    platform: str, requested_url: str, raw: dict[str, Any]
) -> ParsedSocialProfile:
    """Project the fields shared by Bright Data's three profile scrapers."""
    canonical = canonical_profile_url(
        str(raw.get("url") or raw.get("profile_url") or requested_url)
    )
    if canonical is None:
        canonical = canonical_profile_url(requested_url)
    if canonical is None:
        raise BrightDataError(f"Response has no valid {platform} profile URL")
    _, profile_url, username = canonical
    follower_count = _integer(
        raw.get("followers")
        if raw.get("followers") is not None
        else raw.get("followers_count")
    )
    following_count = _integer(
        raw.get("following")
        if raw.get("following") is not None
        else raw.get("following_count")
    )
    connection_count = _integer(
        raw.get("connections")
        if raw.get("connections") is not None
        else raw.get("connections_count")
    )
    verified = raw.get("is_verified")
    return ParsedSocialProfile(
        platform=platform,
        profile_url=profile_url,
        username=str(raw.get("account") or username).lstrip("@"),
        display_name=str(raw.get("profile_name") or raw.get("name") or ""),
        follower_count=follower_count,
        following_count=following_count,
        connection_count=connection_count,
        biography=str(raw.get("biography") or raw.get("bio") or raw.get("about") or ""),
        headline=str(
            raw.get("headline") or raw.get("position") or raw.get("subtitle") or ""
        ),
        is_verified=verified if isinstance(verified, bool) else None,
    )


def _source_id(
    provider: str, person_id: str, platform: str, url: str, observed_at: datetime
) -> str:
    material = f"{provider}\0{person_id}\0{platform}\0{url}\0{observed_at.isoformat()}".encode()
    return hashlib.sha256(material).hexdigest()


def save_profile_observation(
    store: ArchiveStore,
    *,
    person_id: PersonId,
    platform: str,
    profile_url: str,
    raw: dict[str, Any],
    provider: str,
    confidence: float,
    match_reason: str,
    discovered_via: str,
    observed_at: datetime | None = None,
) -> SocialProfileRow:
    """Preserve one raw public observation and project it onto the accepted profile."""
    when = observed_at or datetime.now(timezone.utc)
    source_id = _source_id(provider, str(person_id), platform, profile_url, when)
    store.save_social_source(
        source_id=source_id,
        person_id=person_id,
        platform=platform,
        source_url=profile_url,
        provider=provider,
        fetched_at=when,
        status="fetched",
        raw=raw,
    )
    parsed = parse_brightdata_profile(platform, profile_url, raw)
    row = SocialProfileRow(
        person_id=person_id,
        platform=platform,
        profile_url=parsed.profile_url,
        username=parsed.username,
        display_name=parsed.display_name,
        follower_count=parsed.follower_count,
        following_count=parsed.following_count,
        connection_count=parsed.connection_count,
        biography=parsed.biography,
        headline=parsed.headline,
        is_verified=parsed.is_verified,
        confidence=confidence,
        match_reason=match_reason,
        discovered_via=discovered_via,
        observed_at=when,
        source_id=source_id,
    )
    store.upsert_social_candidate(
        person_id=person_id,
        platform=platform,
        profile_url=parsed.profile_url,
        username=parsed.username,
        discovered_via=discovered_via,
        confidence=confidence,
        reason=match_reason,
        state="accepted",
        seen_at=when,
    )
    store.save_social_profile(row)
    return row


def profile_username_matches_person(
    name: str, aliases_json: str, username: str
) -> bool:
    """Require both ends of a person's name before trusting a signature profile URL."""
    try:
        aliases = json.loads(aliases_json)
    except json.JSONDecodeError:
        aliases = []
    variants = [name, *(str(alias) for alias in aliases if isinstance(alias, str))]
    compact_username = re.sub(r"[^a-z0-9]", "", username.lower())
    if len(compact_username) < 5:
        return False
    for variant in variants:
        tokens = [
            token
            for token in re.sub(r"[^a-z0-9]+", " ", variant.lower()).split()
            if len(token) >= 2
        ]
        if len(tokens) < 2:
            continue
        compact_name = "".join(tokens)
        if compact_username == compact_name:
            return True
        first, last = tokens[0], tokens[-1]
        if (
            len(first) >= 3
            and len(last) >= 3
            and first in compact_username
            and last in compact_username
        ):
            return True
    return False


def harvest_known_profiles(
    store: ArchiveStore, now: datetime | None = None
) -> tuple[int, int]:
    """Accept exact CRM handles and queue signature links with evidence-based confidence."""
    observed_at = now or datetime.now(timezone.utc)
    exact_added = 0
    for person_id, handle_key in store.execute_read(
        "SELECT p.person_id, h.handle_key FROM persons p JOIN person_handles h USING(person_id) "
        "WHERE p.is_human=1 AND p.is_colleague=0 AND (h.handle_key LIKE 'x:%' OR h.handle_key LIKE 'instagram:%')",
        [],
    ):
        platform, username = str(handle_key).split(":", 1)
        _, profile_url, username = profile_from_handle(platform, username)
        store.upsert_social_candidate(
            person_id=PersonId(str(person_id)),
            platform=platform,
            profile_url=profile_url,
            username=username,
            discovered_via="crm_handle",
            confidence=1.0,
            reason="Exact social handle already linked to this CRM identity.",
            state="accepted",
            seen_at=observed_at,
        )
        store.save_social_profile(
            SocialProfileRow(
                person_id=PersonId(str(person_id)),
                platform=platform,
                profile_url=profile_url,
                username=username,
                confidence=1.0,
                match_reason="Exact social handle already linked to this CRM identity.",
                discovered_via="crm_handle",
                observed_at=observed_at,
            )
        )
        exact_added += 1

    store.reset_email_signature_social_candidates()
    occurrences: Counter[tuple[str, str, str, str]] = Counter()
    identities: dict[str, tuple[str, str]] = {}
    for person_id, name, aliases_json, text in store.execute_read(
        "SELECT h.person_id, p.canonical_name, p.aliases_json, m.text FROM messages m "
        "JOIN person_handles h ON h.handle_key='email:' || lower(m.from_addr) "
        "JOIN persons p ON p.person_id=h.person_id "
        "WHERE p.is_human=1 AND p.is_colleague=0 AND m.direction='RECEIVED' AND m.is_automated=0 "
        "AND (m.text LIKE '%linkedin.com/in/%' OR m.text LIKE '%twitter.com/%' OR m.text LIKE '%x.com/%' OR m.text LIKE '%instagram.com/%')",
        [],
    ):
        pid = str(person_id)
        identities[pid] = (str(name), str(aliases_json))
        for platform, url, username in extract_profile_urls(str(text)[-1500:]):
            occurrences[(pid, platform, url, username)] += 1

    candidates_added = 0
    ranked: dict[tuple[str, str], list[tuple[bool, int, str, str]]] = {}
    for (person_id, platform, url, username), count in occurrences.items():
        name_match = profile_username_matches_person(*identities[person_id], username)
        if not name_match and count < 2:
            continue
        ranked.setdefault((person_id, platform), []).append(
            (name_match, count, url, username)
        )
    for (person_id, platform), candidates in ranked.items():
        for name_match, count, url, username in sorted(candidates, reverse=True)[:3]:
            confidence = (
                0.96 if name_match and count >= 2 else (0.91 if name_match else 0.65)
            )
            state = "accepted" if name_match else "needs_review"
            reason = (
                f"Profile username matches this person's full name and appeared in {count} received message(s)."
                if name_match
                else f"Profile link appeared in {count} received messages; GPT identity review is required."
            )
            store.upsert_social_candidate(
                person_id=PersonId(person_id),
                platform=platform,
                profile_url=url,
                username=username,
                discovered_via="email_signature",
                confidence=confidence,
                reason=reason,
                state=state,
                seen_at=observed_at,
            )
            if state == "accepted":
                store.save_social_profile(
                    SocialProfileRow(
                        person_id=PersonId(person_id),
                        platform=platform,
                        profile_url=url,
                        username=username,
                        confidence=confidence,
                        match_reason=reason,
                        discovered_via="email_signature",
                        observed_at=observed_at,
                    )
                )
            candidates_added += 1
    return exact_added, candidates_added


def _person_discovery_context(
    store: ArchiveStore, row: tuple[Any, ...]
) -> dict[str, Any]:
    person_id, name, aliases_json, emails_json, organization, title, tier, score = row
    try:
        emails = [str(value).lower() for value in json.loads(str(emails_json)) if value]
    except json.JSONDecodeError:
        emails = []
    recent_messages: list[dict[str, str]] = []
    if emails:
        placeholders = ",".join("?" for _ in emails)
        for subject, snippet, direction in store.execute_read(
            "SELECT DISTINCT m.subject, m.snippet, mp.direction FROM message_participants mp "
            "JOIN messages m ON m.account=mp.account AND m.message_id=mp.message_id "
            f"WHERE lower(mp.address) IN ({placeholders}) AND m.is_automated=0 "
            "ORDER BY mp.ts DESC LIMIT 8",
            emails,
        ):
            recent_messages.append(
                {
                    "subject": str(subject)[:240],
                    "snippet": str(snippet)[:500],
                    "direction": str(direction),
                }
            )
    calendar_titles: list[str] = []
    for email in emails[:2]:
        tokens = re.findall(r"[a-z0-9]+", email.lower())
        if not tokens:
            continue
        try:
            events = store.search_events('"' + " ".join(tokens) + '"', 8)
        except (
            sqlite3.Error,
            ValueError,
            TypeError,
            LookupError,
            OverflowError,
            OSError,
        ):
            # Calendar context is optional: query failures and malformed stored
            # attendee/timestamp values must not discard the other identity evidence.
            events = []
        for event in events:
            if email in {address.lower() for address in event.attendee_addresses}:
                calendar_titles.append(event.summary)
    signature_candidates = [
        {"platform": str(platform), "profile_url": str(url), "reason": str(reason)}
        for platform, url, reason in store.execute_read(
            "SELECT platform, profile_url, reason FROM social_profile_candidates "
            "WHERE person_id=? AND state='needs_review' ORDER BY confidence DESC LIMIT 6",
            [str(person_id)],
        )
    ]
    known_profiles = [
        {"platform": profile.platform, "profile_url": profile.profile_url}
        for profile in store.social_profiles(PersonId(str(person_id)))
    ]
    return {
        "person_id": str(person_id),
        "name": str(name),
        "aliases": json.loads(str(aliases_json)),
        "emails": emails,
        "organization": str(organization),
        "title": str(title),
        "relationship_tier": int(tier),
        "relationship_score": int(score),
        "recent_email_context": recent_messages,
        "calendar_titles": sorted(set(calendar_titles))[:8],
        "possible_signature_profiles": signature_candidates,
        "already_confirmed_profiles": known_profiles,
    }


def discover_social_profiles(store: ArchiveStore, limit: int = 3) -> dict[str, Any]:
    """Use GPT web search as an identity judge for the next highest-priority people."""
    rows = store.pending_social_discoveries(max(1, limit))
    if not rows:
        return {"searched": 0, "profiles_accepted": 0, "state": "complete"}
    contexts = [_person_discovery_context(store, row) for row in rows]
    prompt = f"""Find confirmed public social profiles for these CRM people.

Search the live public web for LinkedIn, X/Twitter, and Instagram. Judge identity using every
piece of supplied context: full names and aliases, email domains, organization/title, recent
email subjects/snippets, calendar titles, existing confirmed profiles, and signature candidates.
Be conservative. A matching name alone is insufficient. Accept a profile only when at least two
independent details agree, or one first-party page directly links it. Never infer usernames.
Return the canonical profile URL, not a post or search page. Include no result when uncertain.
Confidence must be 0.90 or higher only for a profile you would merge into this person's dossier.

CRM people:
{json.dumps(contexts, ensure_ascii=False, indent=2)}
"""
    result, usage, _events = ask_gpt(
        prompt,
        SocialDiscoveryBatch,
        web=True,
        model=SOL_MODEL,
        reasoning_effort="medium",
    )
    by_person = {person.person_id: person for person in result.people}
    accepted = 0
    searched_at = datetime.now(timezone.utc)
    for row in rows:
        person_id = PersonId(str(row[0]))
        found = by_person.get(
            str(person_id),
            PersonSocialDiscovery(
                person_id=str(person_id), profiles=(), summary="No result returned."
            ),
        )
        accepted_for_person = 0
        for profile in found.profiles:
            canonical = canonical_profile_url(profile.profile_url)
            if (
                canonical is None
                or canonical[0] != profile.platform
                or profile.confidence < 0.9
            ):
                continue
            platform, profile_url, _username = canonical
            save_profile_observation(
                store,
                person_id=person_id,
                platform=platform,
                profile_url=profile_url,
                raw={
                    "url": profile_url,
                    "profile_name": str(row[1]),
                    "evidence_url": profile.evidence_url,
                    "match_reason": profile.match_reason,
                    "gpt_model": SOL_MODEL,
                    "gpt_usage": usage,
                },
                provider="gpt_web_search",
                confidence=profile.confidence,
                match_reason=profile.match_reason,
                discovered_via="gpt_web_search",
                observed_at=searched_at,
            )
            accepted += 1
            accepted_for_person += 1
        store.save_social_discovery_status(
            person_id,
            state="searched",
            profiles_found=accepted_for_person,
            model=SOL_MODEL,
            reason=found.summary[:1000],
            searched_at=searched_at,
        )
    return {
        "searched": len(rows),
        "profiles_accepted": accepted,
        "model": SOL_MODEL,
        "usage": usage,
    }


class BrightDataClient:
    """Call Bright Data through the workspace credential gateway; the key never enters this process."""

    def __init__(self, runner: CommandRunner = subprocess.run) -> None:
        self._runner = runner

    def scrape(self, platform: str, profile_url: str) -> tuple[dict[str, Any], int]:
        dataset_id = DATASET_BY_PLATFORM[platform]
        endpoint = (
            "https://api.brightdata.com/datasets/v3/scrape"
            f"?dataset_id={dataset_id}&format=json&include_errors=true"
        )
        command = [
            "latchkey",
            "curl",
            "-sS",
            "-X",
            "POST",
            "-H",
            "Content-Type: application/json",
            "--data-binary",
            "@-",
            "-w",
            "\n%{http_code}",
            endpoint,
        ]
        try:
            completed = self._runner(
                command,
                input=json.dumps([{"url": profile_url}]),
                text=True,
                capture_output=True,
                check=False,
                timeout=90,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise BrightDataError("Bright Data request did not complete") from error
        if completed.returncode:
            raise BrightDataError(
                completed.stderr[-500:] or f"request exited {completed.returncode}"
            )
        body, separator, status_text = completed.stdout.rpartition("\n")
        if not separator or not status_text.isdigit():
            raise BrightDataError("Bright Data returned an unreadable HTTP response")
        status = int(status_text)
        try:
            decoded = json.loads(body)
        except json.JSONDecodeError:
            decoded = body.strip()
        if status == 400 and "customer is not active" in str(decoded).lower():
            raise BrightDataInactive("Bright Data account is not active")
        if status not in {200, 202}:
            raise BrightDataError(
                f"Bright Data returned HTTP {status}: {str(decoded)[:300]}"
            )
        if status == 202:
            snapshot_id = (
                decoded.get("snapshot_id") if isinstance(decoded, dict) else None
            )
            if isinstance(snapshot_id, str) and re.fullmatch(
                r"[A-Za-z0-9_]+", snapshot_id
            ):
                raise BrightDataPending(snapshot_id)
            raise BrightDataError(
                "Bright Data deferred this profile without a snapshot ID"
            )
        if isinstance(decoded, list):
            if not decoded or not isinstance(decoded[0], dict):
                raise BrightDataError("Bright Data returned no profile record")
            return decoded[0], status
        if isinstance(decoded, dict):
            return decoded, status
        raise BrightDataError("Bright Data returned an unexpected profile response")

    def retrieve_snapshot(self, snapshot_id: str) -> dict[str, Any] | None:
        """Retrieve a delayed one-profile result without submitting a new paid scrape."""
        if not re.fullmatch(r"[A-Za-z0-9_]+", snapshot_id):
            raise BrightDataError("Bright Data returned an invalid snapshot ID")

        def get_json(url: str) -> Any:
            try:
                completed = self._runner(
                    ["latchkey", "curl", "-sS", "-w", "\n%{http_code}", url],
                    text=True,
                    capture_output=True,
                    check=False,
                    timeout=30,
                )
            except (OSError, subprocess.TimeoutExpired) as error:
                raise BrightDataError("Snapshot request did not complete") from error
            if completed.returncode:
                raise BrightDataError(
                    completed.stderr[-500:] or "Snapshot request failed"
                )
            body, separator, status_text = completed.stdout.rpartition("\n")
            if not separator or status_text != "200":
                raise BrightDataError(f"Snapshot request returned HTTP {status_text}")
            try:
                return json.loads(body)
            except json.JSONDecodeError as error:
                raise BrightDataError(
                    "Bright Data returned an unreadable snapshot"
                ) from error

        progress = get_json(
            f"https://api.brightdata.com/datasets/v3/progress/{snapshot_id}"
        )
        if not isinstance(progress, dict):
            raise BrightDataError("Bright Data returned invalid snapshot progress")
        if progress.get("status") == "failed":
            raise BrightDataSnapshotFailed(f"Bright Data snapshot {snapshot_id} failed")
        if progress.get("status") != "ready":
            return None
        result = get_json(
            f"https://api.brightdata.com/datasets/v3/snapshot/{snapshot_id}?format=json"
        )
        if isinstance(result, list) and result and isinstance(result[0], dict):
            return result[0]
        if isinstance(result, dict):
            return result
        raise BrightDataSnapshotFailed("Bright Data snapshot had no profile result")


def _save_brightdata_response(
    store: ArchiveStore,
    *,
    person_id: str,
    platform: str,
    url: str,
    raw: dict[str, Any],
    observed_at: datetime,
    confidence: float,
    reason: str,
    discovered_via: str,
) -> bool:
    """Preserve every response, projecting a profile only when it is usable."""
    if raw.get("error_code") or raw.get("error"):
        store.save_social_source(
            source_id=_source_id("brightdata", person_id, platform, url, observed_at),
            person_id=PersonId(person_id),
            platform=platform,
            source_url=url,
            provider="brightdata",
            fetched_at=observed_at,
            status="error",
            raw=raw,
        )
        store.set_social_queue_state(
            PersonId(person_id),
            "partial",
            str(raw.get("error_code") or raw.get("error")),
        )
        return False
    try:
        save_profile_observation(
            store,
            person_id=PersonId(person_id),
            platform=platform,
            profile_url=url,
            raw=raw,
            provider="brightdata",
            confidence=confidence,
            match_reason=reason,
            discovered_via=discovered_via,
            observed_at=observed_at,
        )
    except (BrightDataError, ValidationError) as error:
        # save_profile_observation writes the unmodified provider response first.
        source_id = _source_id("brightdata", person_id, platform, url, observed_at)
        store.set_social_source_status(source_id, "invalid_profile")
        store.set_social_queue_state(PersonId(person_id), "partial", str(error))
        return False
    store.set_social_queue_state(PersonId(person_id), "partial")
    return True


def run_enrichment_pass(
    store: ArchiveStore,
    client: BrightDataClient,
    limit: int = 10,
    *,
    harvest_known: bool = True,
    workers: int = 1,
) -> EnrichmentRunSummary:
    """Run one bounded, resumable pass in Close/Known/Peripheral priority order."""
    now = datetime.now(timezone.utc)
    queued_now = store.refresh_social_enrichment_queue(now)
    harvest_after = store.social_enrichment_metadata("social_harvest_after")
    if harvest_known and (
        not harvest_after or datetime.fromisoformat(harvest_after) <= now
    ):
        exact_added, signature_added = harvest_known_profiles(store, now)
        store.set_social_enrichment_metadata(
            "social_harvest_after", (now + timedelta(hours=12)).isoformat()
        )
    else:
        exact_added, signature_added = 0, 0
    retry_after = store.social_enrichment_metadata("brightdata_retry_after")
    if retry_after and datetime.fromisoformat(retry_after) > now:
        return EnrichmentRunSummary(
            queued_now=queued_now,
            exact_profiles_added=exact_added,
            signature_candidates_added=signature_added,
            state="waiting_for_brightdata",
            detail="Bright Data account activation is still pending.",
            totals=store.social_enrichment_status(),
        )
    fetched = 0
    errored = 0
    uncertain = 0
    pending_sources = store.execute_read(
        "SELECT src.source_id, src.person_id, src.platform, src.source_url, "
        "p.confidence, p.match_reason, p.discovered_via "
        "FROM social_profile_sources src JOIN social_profiles p "
        "ON p.person_id=src.person_id AND p.platform=src.platform "
        "WHERE src.provider='brightdata' AND src.status='pending_snapshot' "
        "ORDER BY src.fetched_at LIMIT ?",
        [limit],
    )
    for (
        source_id,
        person_id,
        platform,
        url,
        confidence,
        reason,
        discovered_via,
    ) in pending_sources:
        source = store.get_social_source(str(source_id))
        snapshot_id = source["raw"].get("snapshot_id") if source else None
        if not isinstance(snapshot_id, str):
            store.set_social_source_status(str(source_id), "error")
            errored += 1
            continue
        try:
            raw = client.retrieve_snapshot(snapshot_id)
        except BrightDataSnapshotFailed:
            store.set_social_source_status(str(source_id), "error")
            errored += 1
            continue
        except BrightDataError:
            continue
        if raw is None:
            continue
        if _save_brightdata_response(
            store,
            person_id=str(person_id),
            platform=str(platform),
            url=str(url),
            raw=raw,
            observed_at=datetime.now(timezone.utc),
            confidence=float(confidence),
            reason=str(reason),
            discovered_via=str(discovered_via),
        ):
            fetched += 1
        else:
            errored += 1
        store.set_social_source_status(str(source_id), "complete")
    fetches = store.pending_social_fetches(max(0, limit - len(pending_sources)))
    reserved = []
    for (
        person_id,
        platform,
        url,
        _username,
        discovered_via,
        confidence,
        reason,
    ) in fetches:
        observed_at = datetime.now(timezone.utc)
        source_id = _source_id("brightdata", person_id, platform, url, observed_at)
        store.save_social_source(
            source_id=source_id,
            person_id=PersonId(person_id),
            platform=platform,
            source_url=url,
            provider="brightdata",
            fetched_at=observed_at,
            status="requesting",
            raw={"url": url, "request_started_at": observed_at.isoformat()},
        )
        reserved.append(
            (
                source_id,
                person_id,
                platform,
                url,
                discovered_via,
                confidence,
                reason,
                observed_at,
            )
        )
    inactive = False
    with ThreadPoolExecutor(max_workers=max(1, min(workers, 4))) as pool:
        pending = {
            pool.submit(client.scrape, item[2], item[3]): item for item in reserved
        }
        for future in as_completed(pending):
            (
                source_id,
                person_id,
                platform,
                url,
                discovered_via,
                confidence,
                reason,
                observed_at,
            ) = pending[future]
            try:
                raw, _status = future.result()
            except BrightDataInactive:
                inactive = True
                store.set_social_source_status(source_id, "rejected")
                store.set_social_queue_state(
                    PersonId(person_id), "retry", "Bright Data inactive"
                )
                continue
            except BrightDataPending as delayed:
                store.save_social_source(
                    source_id=source_id,
                    person_id=PersonId(person_id),
                    platform=platform,
                    source_url=url,
                    provider="brightdata",
                    fetched_at=observed_at,
                    status="pending_snapshot",
                    raw={"snapshot_id": delayed.snapshot_id},
                )
                store.set_social_queue_state(PersonId(person_id), "partial")
                continue
            except BrightDataError as error:
                # The provider might have accepted a request before the connection failed.
                # Preserve the reservation for reconciliation, avoiding a second paid call.
                store.set_social_source_status(source_id, "uncertain")
                store.set_social_queue_state(PersonId(person_id), "partial", str(error))
                uncertain += 1
                continue
            except Exception as error:
                # A worker exception cannot be allowed to discard other paid responses.
                store.set_social_source_status(source_id, "uncertain")
                store.set_social_queue_state(
                    PersonId(person_id),
                    "partial",
                    f"Request failed: {type(error).__name__}",
                )
                uncertain += 1
                continue
            if _save_brightdata_response(
                store,
                person_id=person_id,
                platform=platform,
                url=url,
                raw=raw,
                observed_at=observed_at,
                confidence=confidence,
                reason=reason,
                discovered_via=discovered_via,
            ):
                fetched += 1
            else:
                errored += 1
    if inactive:
        store.set_social_enrichment_metadata("brightdata_status", "inactive")
        store.set_social_enrichment_metadata(
            "brightdata_retry_after",
            (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat(),
        )
        return EnrichmentRunSummary(
            queued_now=queued_now,
            exact_profiles_added=exact_added,
            signature_candidates_added=signature_added,
            profiles_fetched=fetched,
            profiles_errored=errored,
            requests_uncertain=uncertain,
            state="waiting_for_brightdata",
            detail="The API key works, but Bright Data says the customer account is not active.",
            totals=store.social_enrichment_status(),
        )
    store.set_social_enrichment_metadata("brightdata_status", "active")
    store.set_social_enrichment_metadata("brightdata_retry_after", "")
    return EnrichmentRunSummary(
        queued_now=queued_now,
        exact_profiles_added=exact_added,
        signature_candidates_added=signature_added,
        profiles_fetched=fetched,
        profiles_errored=errored,
        requests_uncertain=uncertain,
        state="running",
        totals=store.social_enrichment_status(),
    )
