import os
from pathlib import Path

from people_archive.data_types import Direction
from people_archive.identity import resolve_and_store_people
from people_archive.primitives import PersonId
from people_archive.store import ArchiveStore
from people_archive.testing import (
    WORK_ACCOUNT,
    make_contact_card,
    make_parsed_email,
    utc,
)
from people_archive.vault_writer import write_vault


def _seed_one_close_person(store: ArchiveStore) -> None:
    with store.transaction():
        for idx in range(8):
            store.upsert_message(make_parsed_email(f"s{idx}", "owner@example.com", ("ada@example.com",), Direction.SENT, utc(2026, 1, idx + 1), f"note {idx}", "body", False, ""))
        store.upsert_contact(make_contact_card("people/1", "Mira Vale", ("ada@example.com",), (), "Example Workshop"))
    resolve_and_store_people(store, [WORK_ACCOUNT])


def test_write_vault_creates_a_page_per_person_with_index_and_rulebook(archive_store: ArchiveStore, tmp_path: Path) -> None:
    _seed_one_close_person(archive_store)
    vault = tmp_path / "vault"

    summary = write_vault(archive_store, vault, max_tier=2)
    page = (vault / "People" / "Mira Vale.md").read_text()

    assert summary.written_count == 1
    assert "name: Mira Vale" in page and "organization: Example Workshop" in page
    assert "- 2026-01-08 · sent · [note 7]" in page
    assert "[[Mira Vale]]" in (vault / "index.md").read_text()
    assert (vault / "CLAUDE.md").exists()


def test_rewrite_preserves_human_notes_and_tags(archive_store: ArchiveStore, tmp_path: Path) -> None:
    _seed_one_close_person(archive_store)
    vault = tmp_path / "vault"
    write_vault(archive_store, vault, max_tier=2)
    page_path = vault / "People" / "Mira Vale.md"
    edited = page_path.read_text().replace("## Notes\n", "## Notes\nLoves difference engines.\n").replace("tags: []", "tags: [engineer, invite]")
    page_path.write_text(edited)
    # New machine content forces a rewrite; the edit was over a minute ago as far as the writer knows.
    with archive_store.transaction():
        archive_store.upsert_message(make_parsed_email("s9", "owner@example.com", ("ada@example.com",), Direction.SENT, utc(2026, 2, 1), "note 9", "body", False, ""))
    resolve_and_store_people(archive_store, [WORK_ACCOUNT])
    os.utime(page_path, (1_700_000_000, 1_700_000_000))

    summary = write_vault(archive_store, vault, max_tier=2)
    rewritten = page_path.read_text()

    assert summary.written_count == 1
    assert "Loves difference engines." in rewritten
    assert "tags: [engineer, invite]" in rewritten
    assert "- 2026-02-01 · sent · [note 9]" in rewritten


def test_hand_edited_machine_region_is_refused_not_overwritten(archive_store: ArchiveStore, tmp_path: Path) -> None:
    _seed_one_close_person(archive_store)
    vault = tmp_path / "vault"
    write_vault(archive_store, vault, max_tier=2)
    page_path = vault / "People" / "Mira Vale.md"
    page_path.write_text(page_path.read_text().replace("Tier 1", "Tier 9"))
    os.utime(page_path, (1_700_000_000, 1_700_000_000))

    summary = write_vault(archive_store, vault, max_tier=2)

    assert summary.refused_count == 1
    assert "Tier 9" in page_path.read_text()


def test_in_place_edits_reach_the_page_marked_as_her_words(archive_store: ArchiveStore, tmp_path: Path) -> None:
    _seed_one_close_person(archive_store)
    person_id = PersonId(archive_store.execute_read("SELECT person_id FROM persons WHERE canonical_name = 'Mira Vale'", [])[0][0])
    archive_store.set_person_edit(person_id, "how_we_met", "Introduced by Babbage (1833 note)")
    archive_store.set_person_edit(person_id, "notes", "Prefers letters to visits.")
    archive_store.set_person_edit(person_id, "interests", "mathematics, poetry")

    write_vault(archive_store, tmp_path / "vault", max_tier=2)
    page = (tmp_path / "vault" / "People" / "Mira Vale.md").read_text()

    assert "> [!example] How we met (your words)\n> - Introduced by Babbage (1833 note)" in page
    assert "> [!quote] Your notes (typed in the CRM)\n> Prefers letters to visits." in page
    assert "#mathematics · #poetry" in page and "interests: [mathematics, poetry]" in page
