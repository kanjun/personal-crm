import hashlib
import json
import os
import re
import time
import urllib.parse
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final

import yaml
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from loguru import logger
from pydantic import Field

from people_archive.primitives import PersonId
from people_archive.store import DOSSIER_FIELDS, ArchiveStore, parse_tag_list

# Writer rules (design doc, "Writer rules"): rewrite only the fenced machine regions, keep
# everything else byte for byte, write atomically, never delete, skip a file the user touched
# moments ago, and refuse to touch a file whose machine regions were edited by hand.
_FACTS_START: Final[str] = "<!-- crm:facts -->"
_FACTS_END: Final[str] = "<!-- /crm:facts -->"
_SYNTHESIS_START: Final[str] = "<!-- crm:synthesis -->"
_SYNTHESIS_END: Final[str] = "<!-- /crm:synthesis -->"
_TIMELINE_START: Final[str] = "<!-- crm:timeline -->"
_TIMELINE_END: Final[str] = "<!-- /crm:timeline -->"
_STAMP_PATTERN: Final[re.Pattern[str]] = re.compile(r"<!-- crm:written ([0-9a-f]{64}) -->\n?$")
_FRONTMATTER_PATTERN: Final[re.Pattern[str]] = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)
_RECENTLY_EDITED_SECONDS: Final[float] = 60.0
_TIMELINE_ROWS: Final[int] = 120
# Human-owned frontmatter keys survive every rewrite; everything else is machine-written.
_HUMAN_FRONTMATTER_KEYS: Final[tuple[str, ...]] = ("tags", "how_we_met_locked")
_UNSAFE_FILENAME_PATTERN: Final[re.Pattern[str]] = re.compile(r'[\\/:*?"<>|#^\[\]]+')

_HUMAN_SKELETON: Final[str] = """
## Notes

"""
# The skeleton pages carried before the model-written region existed; an untouched copy of it
# is treated as empty so its headings do not linger beside the written sections.
_LEGACY_SKELETON: Final[str] = """
## How we met

## What they care about

## Notes

"""


class VaultWriteSummary(FrozenModel):
    """What one vault write did."""

    written_count: int = Field(description="Files created or updated")
    unchanged_count: int = Field(description="Files whose machine regions were already current")
    skipped_recent_count: int = Field(description="Files left alone because the user edited them just now")
    refused_count: int = Field(description="Files left alone because their machine regions were hand-edited")


@pure
def _safe_filename(name: str) -> str:
    cleaned = _UNSAFE_FILENAME_PATTERN.sub(" ", name).strip().strip(".")
    return re.sub(r"\s+", " ", cleaned) or "Unnamed"


@pure
def _machine_digest(facts: str, timeline: str) -> str:
    return hashlib.sha256((facts + "\n" + timeline).encode()).hexdigest()


@pure
def _extract_region(text: str, start: str, end: str) -> str | None:
    start_idx = text.find(start)
    end_idx = text.find(end)
    if start_idx < 0 or end_idx < 0 or end_idx < start_idx:
        return None
    return text[start_idx + len(start):end_idx]


@pure
def _human_frontmatter(existing_text: str) -> dict[str, Any]:
    match = _FRONTMATTER_PATTERN.match(existing_text)
    if match is None:
        return {}
    try:
        parsed = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError:
        return {}
    return {key: parsed[key] for key in _HUMAN_FRONTMATTER_KEYS if isinstance(parsed, dict) and key in parsed}


@pure
def _render_frontmatter(machine: dict[str, Any], human: dict[str, Any]) -> str:
    merged = dict(machine)
    merged["tags"] = human.get("tags", [])
    for key, value in human.items():
        merged[key] = value
    return "---\n" + yaml.safe_dump(merged, sort_keys=False, allow_unicode=True, default_flow_style=None).rstrip() + "\n---\n"


@pure
def _render_page(frontmatter: str, facts: str, synthesis: str, human_body: str, timeline: str) -> str:
    body = (
        f"{_FACTS_START}\n{facts}\n{_FACTS_END}\n"
        f"{_SYNTHESIS_START}\n{synthesis}\n{_SYNTHESIS_END}\n"
        f"{human_body}"
        f"{_TIMELINE_START}\n{timeline}\n{_TIMELINE_END}\n"
    )
    return frontmatter + body + f"<!-- crm:written {_machine_digest(facts + synthesis, timeline)} -->\n"


@pure
def _human_body_from(existing_text: str) -> str:
    """Everything between the machine regions up top and the timeline region: the user's territory."""
    last_machine_end = _SYNTHESIS_END if _SYNTHESIS_END in existing_text else _FACTS_END
    after = existing_text.split(last_machine_end, 1)
    if len(after) < 2:
        return _HUMAN_SKELETON
    between = after[1].split(_TIMELINE_START, 1)[0]
    if not between.strip() or between.strip() == _LEGACY_SKELETON.strip():
        return _HUMAN_SKELETON
    return between.lstrip("\n")


def _write_atomically(path: Path, content: str) -> None:
    temporary = path.with_name(path.name + ".crm-tmp")
    temporary.write_text(content)
    os.replace(temporary, path)


def _gmail_url(account: str, thread_id: str) -> str:
    return f"https://mail.google.com/mail/u/0/?authuser={urllib.parse.quote(account, safe='')}#all/{thread_id}"


def _timeline_markdown(store: ArchiveStore, emails: Sequence[str]) -> str:
    if not emails:
        return "## Timeline\n\n_No interactions archived yet._"
    placeholders = ",".join("?" for _ in emails)
    rows = store.execute_read(
        "SELECT DISTINCT m.ts, m.direction, m.subject, m.account, m.thread_id, m.is_automated "
        "FROM message_participants p JOIN messages m ON m.account = p.account AND m.message_id = p.message_id "
        f"WHERE p.address IN ({placeholders}) ORDER BY m.ts DESC LIMIT ?",
        [*emails, _TIMELINE_ROWS],
    )
    like_clauses = " OR ".join("attendees_json LIKE ?" for _ in emails)
    events = store.execute_read(
        f"SELECT start_ts, summary FROM calendar_events WHERE {like_clauses} ORDER BY start_ts DESC LIMIT ?",
        [*[f'%"{e}"%' for e in emails], _TIMELINE_ROWS],
    )
    items: list[tuple[int, str]] = []
    for ts, direction, subject, account, thread_id, is_automated in rows:
        marker = "bulk" if is_automated else ("sent" if direction == "SENT" else "received")
        items.append((int(ts), f"- {datetime.fromtimestamp(int(ts), tz=timezone.utc):%Y-%m-%d} · {marker} · [{subject or '(no subject)'}]({_gmail_url(str(account), str(thread_id))})"))
    for start_ts, summary in events:
        items.append((int(start_ts), f"- {datetime.fromtimestamp(int(start_ts), tz=timezone.utc):%Y-%m-%d} · meeting · {summary or '(untitled event)'}"))
    items.sort(key=lambda pair: pair[0], reverse=True)
    lines = [line for _, line in items[:_TIMELINE_ROWS]]
    return "## Timeline\n\n" + ("\n".join(lines) if lines else "_No interactions archived yet._")


def _synthesis_markdown(store: ArchiveStore, person_id: str) -> tuple[str, dict[str, Any]]:
    """The model-written sections with the user's in-place edits laid over them, as Obsidian callouts
    (each kind its own color), plus the frontmatter keys they add."""
    row = store.get_synthesis(PersonId(person_id))
    edits = store.person_edits(PersonId(person_id))
    if row is None and not edits:
        return "_No written dossier yet._", {}
    text: dict[str, str] = {field: "" for field in DOSSIER_FIELDS}
    if row is not None:
        text.update(
            one_liner=row.one_liner, key_facts=row.key_facts, how_we_met=row.how_we_met, what_they_care_about=row.what_they_care_about,
            where_things_stand=row.where_things_stand, watch_out=row.watch_out,
            interests=", ".join(_tags(row.interests_json)), background=", ".join(_tags(row.background_json)),
        )
    text.update(edits)
    interests = list(parse_tag_list(text["interests"]))
    background = list(parse_tag_list(text["background"]))
    parts = [f"> [!abstract] {text['one_liner']}" if text["one_liner"] else "", ""]
    if background or interests:
        parts += ["**" + " · ".join(background) + "**  " if background else "", " · ".join(f"#{_tag_slug(t)}" for t in interests), ""]
    parts += _callout("info", _edited_title("Key facts", "key_facts", edits), text["key_facts"])
    parts += _callout("example", _edited_title("How we met", "how_we_met", edits), text["how_we_met"])
    parts += _callout("tip", _edited_title("Cares about", "what_they_care_about", edits), text["what_they_care_about"])
    parts += _callout("note", _edited_title("State", "where_things_stand", edits), text["where_things_stand"])
    if text["watch_out"].strip():
        parts += _callout("warning", _edited_title("Watch out", "watch_out", edits), text["watch_out"])
    if row is not None and row.next_steps.strip():
        parts += _callout("todo", "Next", row.next_steps)
    if text["notes"].strip():
        parts += ["> [!quote] Your notes (typed in the CRM)", *[f"> {line}" for line in text["notes"].splitlines()], ""]
    if row is not None:
        parts.append(f"_Written {row.synthesized_at:%Y-%m-%d} from {row.excerpt_count} dated excerpts; (date kind) points at the evidence._")
    return "\n".join(parts), {"interests": interests, "background": background}


@pure
def _edited_title(title: str, field: str, edits: dict[str, str]) -> str:
    return f"{title} (your words)" if field in edits else title


@pure
def _callout(kind: str, title: str, bullets: str) -> list[str]:
    """An Obsidian callout block whose body is the section's bullets."""
    lines = [line.strip() for line in bullets.splitlines() if line.strip()]
    if not lines:
        return []
    body = [f"> {line if line.startswith('- ') else '- ' + line}" for line in lines]
    return [f"> [!{kind}] {title}", *body, ""]


@pure
def _tag_slug(tag: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", tag.lower()).strip("-")


@pure
def _tags(value: str) -> list[str]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return []
    return [str(item) for item in parsed] if isinstance(parsed, list) else []


def _facts_markdown(row: tuple[Any, ...]) -> str:
    (_pid, _name, aliases_json, emails_json, phones_json, organization, title, tier, score,
     sent, received, automated, meetings, first_ts, last_ts) = row
    emails = json.loads(emails_json)
    phones = json.loads(phones_json)
    aliases = json.loads(aliases_json)
    first = datetime.fromtimestamp(int(first_ts), tz=timezone.utc).strftime("%Y-%m-%d") if first_ts else "—"
    last = datetime.fromtimestamp(int(last_ts), tz=timezone.utc).strftime("%Y-%m-%d") if last_ts else "—"
    parts = [
        f"**{organization or '—'}**{(' · ' + title) if title else ''}",
        f"Tier {tier} · score {score}",
        f"Emails: {', '.join(f'`{e}`' for e in emails) or '—'}",
        f"Phones: {', '.join(f'`{p}`' for p in phones) or '—'}",
        f"Exchanged: {sent} sent · {received} received · {automated} automated · {meetings} meeting series",
        f"First · last contact: {first} · {last}",
    ]
    if aliases:
        parts.append(f"Also seen as: {', '.join(aliases)}")
    return "\n".join(f"{line}  " for line in parts)


def write_vault(store: ArchiveStore, vault_root: Path, max_tier: int) -> VaultWriteSummary:
    """Write one page per human, non-colleague person up to the given tier, plus the index and rulebook."""
    people_dir = vault_root / "People"
    people_dir.mkdir(parents=True, exist_ok=True)
    rows = store.execute_read(
        "SELECT person_id, canonical_name, aliases_json, emails_json, phones_json, organization, title, tier, score, "
        "sent_count, received_human_count, received_automated_count, meeting_count, first_contact_ts, last_contact_ts "
        "FROM persons WHERE is_human = 1 AND is_colleague = 0 AND tier <= ? ORDER BY score DESC",
        [max_tier],
    )
    written = unchanged = skipped_recent = refused = 0
    used_names: dict[str, str] = {}
    index_lines_by_tier: dict[int, list[str]] = {1: [], 2: [], 3: []}
    now = time.time()
    for row in rows:
        person_id, name, _aliases, emails_json, _phones, organization, _title, tier, score, *_rest = row
        emails = json.loads(emails_json)
        base_name = _safe_filename(str(name))
        file_stem = base_name
        if used_names.get(base_name, str(person_id)) != str(person_id):
            file_stem = _safe_filename(f"{base_name} ({organization or (emails[0] if emails else str(person_id))})")
        used_names.setdefault(base_name, str(person_id))
        path = people_dir / f"{file_stem}.md"
        index_lines_by_tier[int(tier)].append(f"- [[{file_stem}]] — {organization or ''}".rstrip(" —"))

        facts = _facts_markdown(row)
        synthesis, synthesis_front = _synthesis_markdown(store, str(person_id))
        timeline = _timeline_markdown(store, emails)
        machine_frontmatter = {
            "crm_id": str(person_id), "name": str(name), "emails": emails, "phones": json.loads(row[4]),
            "organization": str(organization), "title": str(row[6]), "tier": int(tier), "score": int(score),
            "first_contact": datetime.fromtimestamp(int(row[13]), tz=timezone.utc).strftime("%Y-%m-%d") if row[13] else None,
            "last_contact": datetime.fromtimestamp(int(row[14]), tz=timezone.utc).strftime("%Y-%m-%d") if row[14] else None,
            "interactions": {"sent": int(row[9]), "received": int(row[10]), "automated": int(row[11]), "meetings": int(row[12])},
            **synthesis_front,
        }
        if path.exists():
            existing = path.read_text()
            stamp = _STAMP_PATTERN.search(existing)
            old_facts = _extract_region(existing, _FACTS_START, _FACTS_END)
            old_synthesis = _extract_region(existing, _SYNTHESIS_START, _SYNTHESIS_END) or ""
            old_timeline = _extract_region(existing, _TIMELINE_START, _TIMELINE_END)
            if stamp is not None and old_facts is not None and old_timeline is not None:
                if _machine_digest(old_facts.strip("\n") + old_synthesis.strip("\n"), old_timeline.strip("\n")) != stamp.group(1):
                    logger.warning("Refusing to rewrite {}: its machine regions were edited by hand", path.name)
                    refused += 1
                    continue
                if _machine_digest(facts + synthesis, timeline) == stamp.group(1):
                    unchanged += 1
                    continue
            # Only a page that actually needs new machine content can collide with an open editor.
            if now - path.stat().st_mtime < _RECENTLY_EDITED_SECONDS:
                skipped_recent += 1
                continue
            human_body = _human_body_from(existing)
            human_front = _human_frontmatter(existing)
        else:
            human_body = _HUMAN_SKELETON
            human_front = {}
        page = _render_page(_render_frontmatter(machine_frontmatter, human_front), facts, synthesis, human_body, timeline)
        _write_atomically(path, page)
        written += 1

    _write_atomically(vault_root / "index.md", _render_index(index_lines_by_tier))
    rulebook = vault_root / "CLAUDE.md"
    if not rulebook.exists():
        _write_atomically(rulebook, _RULEBOOK)
    with (vault_root / "log.md").open("a") as handle:
        handle.write(f"- {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC · wrote {written}, unchanged {unchanged}, skipped (recently edited) {skipped_recent}, refused (hand-edited machine region) {refused}\n")
    logger.info("Vault write: {} written, {} unchanged, {} skipped, {} refused", written, unchanged, skipped_recent, refused)
    return VaultWriteSummary(written_count=written, unchanged_count=unchanged, skipped_recent_count=skipped_recent, refused_count=refused)


@pure
def _render_index(lines_by_tier: dict[int, list[str]]) -> str:
    sections = ["# People\n", f"_Generated {datetime.now(timezone.utc):%Y-%m-%d}. Tier 1 = close, 2 = known, 3 = peripheral._\n"]
    for tier, label in ((1, "Close"), (2, "Known"), (3, "Peripheral")):
        if lines_by_tier[tier]:
            sections.append(f"\n## {label} ({len(lines_by_tier[tier])})\n\n" + "\n".join(lines_by_tier[tier]) + "\n")
    return "".join(sections)


_RULEBOOK: Final[str] = """# How this folder works

One Markdown page per person under `People/`, written by the CRM pipeline from the
archive of the user's email, contacts, and calendar (and, later, texts, meetings, and notes).

## What the pipeline owns and what you own

- Between `<!-- crm:facts -->` and `<!-- /crm:facts -->`: machine-written. Rewritten on every run.
- Between `<!-- crm:synthesis -->` and `<!-- /crm:synthesis -->`: written by a model from dated evidence, with citations. Rewritten when new evidence arrives.
- Between `<!-- crm:timeline -->` and `<!-- /crm:timeline -->`: machine-written. Rewritten on every run.
- Everything else in the body (`## Notes`, anything you add): yours. Never touched. If you want to correct "how we met", write the truth under Notes and it wins next time.
- In the frontmatter, `tags:` (and any other key you add) is yours; the rest is machine-written.
- The `<!-- crm:written ... -->` line at the end is a checksum of the machine regions. If you edit inside a machine region, the pipeline notices and refuses to overwrite that page (it logs the refusal to `log.md`); delete the checksum line to let it take over again.

## Rules the pipeline follows

- It never deletes a page. A person merged into another becomes a one-line stub pointing at the survivor.
- It never renames a page on its own.
- It skips any page modified in the last minute, so it will not fight an editor.
- Writes are atomic (temp file, then rename).

## Adding a note about someone

Write it under `## Notes` on their page, or tag them in your Logseq journal; both reach the CRM.
"""
