import json
import subprocess
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from people_archive import cleanup_gpt
from people_archive.assertions import Decision, append_assertion, make_assertion
from people_archive.cleanup import (
    apply_pair,
    main,
    research_batch,
    validate_pair_ids,
    validate_pair_result,
)
from people_archive.cleanup_candidates import candidate_pairs
from people_archive.cleanup_gpt import ask_gpt, run_command_in_process_group
from people_archive.cleanup_ledger import (
    CleanupLedger,
    load_overlay,
    resolve_alias,
    save_aliases,
    valid_citations,
)
from people_archive.cleanup_models import (
    Citation,
    CleanupError,
    Extraction,
    JudgmentBatch,
    PairJudgment,
    PairResult,
    ProviderUnavailable,
)
from people_archive.data_types import Direction
from people_archive.identity import _UnionFind, resolve_and_store_people
from people_archive.primitives import HandleKey
from people_archive.testing import WORK_ACCOUNT, make_parsed_email, utc


@pytest.fixture(autouse=True)
def isolated_codex_binding(monkeypatch):
    monkeypatch.setenv("CRM_CLEANUP_ENABLED", "true")
    # These tests inject subprocess runners; package installation is tested separately.
    monkeypatch.setattr(cleanup_gpt, "private_codex_command", lambda: "/private/crm/codex.js")


def test_citations_require_exact_existing_quote() -> None:
    citation = Citation(source_id="email:1",quote="My name is Jane Doe")
    assert valid_citations([citation],[{"id":"email:1","text":"Hello. My name is Jane Doe."}])
    assert not valid_citations([citation],[{"id":"email:2","text":"My name is Jane Doe"}])
    assert not valid_citations([citation],[{"id":"email:1","text":"My name is John Doe"}])
    assert not valid_citations([],[])


def test_unicode_candidates_and_recovered_names() -> None:
    people = [{"id":"a","name":"José Núñez","aliases":[],"handles":["email:jn@one.test"]},
              {"id":"b","name":"JN","aliases":[],"handles":["email:other@two.test"]}]
    assert candidate_pairs(people,{}) == set()
    assert candidate_pairs(people,{"b":[{"field":"name","value":"José Núñez"}]}) == {("a","b")}


def test_ledger_disabled_by_default_and_reversible(tmp_path: Path) -> None:
    path = tmp_path / "cleanup.sqlite"
    ledger = CleanupLedger(path)
    ledger.decision("SAME",["email:a@test.com"],["email:b@test.com"],"","strong evidence")
    assert load_overlay(path) == []
    ledger.connection.execute("INSERT INTO metadata VALUES ('apply_enabled','true')")
    ledger.connection.commit()
    assert load_overlay(path)[0]["kind"] == "SAME"
    ledger.connection.execute("UPDATE decisions SET active=0")
    ledger.connection.commit()
    assert load_overlay(path) == []
    ledger.close()


def test_aliases_follow_merges_and_remove_split_links(tmp_path: Path) -> None:
    path = tmp_path / "cleanup.sqlite"
    ledger = CleanupLedger(path)
    save_aliases({"a":"old","b":"kept"},{"a":"kept","b":"kept"},path)
    assert resolve_alias("old",path) == "kept"
    save_aliases({"a":"old"},{"a":"old"},path)
    assert resolve_alias("old",path) == "old"
    ledger.close()


def test_merge_vetoes_user_edits_and_transitive_uncertainty(tmp_path: Path) -> None:
    ledger = CleanupLedger(tmp_path/"cleanup.sqlite")
    profiles = {p:{"handles":["email:"+p+"@test.com"]} for p in ("a","b","c")}
    judgment = PairJudgment(decision="SAME",confidence="high",basis="explicit_link",citations=[],reason="link",needs_more=False)
    assert apply_pair(ledger,"a","b",judgment,profiles) == "same"
    ledger.connection.execute("INSERT INTO pairs VALUES (?,?,?,?)",("a","b",judgment.model_dump_json(),"same"))
    assert apply_pair(ledger,"b","c",judgment,profiles) == "cluster_review"
    ledger.connection.execute("INSERT INTO pairs VALUES (?,?,?,?)",("a","c",judgment.model_dump_json(),"review"))
    profiles["c"]["has_user_edits"] = True
    assert apply_pair(ledger,"b","c",judgment,profiles) == "protected_user_edits"
    ledger.close()


def test_inventory_resume_preserves_snapshot(tmp_path: Path) -> None:
    path = tmp_path/"cleanup.sqlite"
    ledger = CleanupLedger(path)
    ledger.inventory([{"id":"a","name":"Original"}])
    ledger.inventory([{"id":"a","name":"Changed"},{"id":"b","name":"New"}])
    assert ledger.profiles()["a"]["name"] == "Original"
    assert "b" not in ledger.profiles()
    ledger.save_call("extract","a",{"input_tokens":12},json.dumps({"type":"turn.completed"}))
    assert ledger.progress()["usage"] == {"input_tokens":12}
    ledger.close()


def test_nicknames_and_sparse_names_enter_review() -> None:
    people = [{"id":"a","name":"Robert Smith","aliases":[],"handles":[]},
              {"id":"b","name":"Bob Smith","aliases":[],"handles":[]},
              {"id":"c","name":"Bob","aliases":[],"handles":[]}]
    assert candidate_pairs(people,{}) == {("a","b"),("a","c"),("b","c")}


def test_resolver_uses_adjacent_overlay_and_honors_user_split(archive_store, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CRM_DATA_DIR",str(tmp_path))
    with archive_store.transaction():
        archive_store.upsert_message(make_parsed_email("a","alice@one.test",("owner@example.com",),Direction.RECEIVED,utc(2026,1,1),"hello","Alice Smith",False,"Alice"))
        archive_store.upsert_message(make_parsed_email("b","asmith@two.test",("owner@example.com",),Direction.RECEIVED,utc(2026,1,1),"hello","Alice Smith",False,"A Smith"))
    before = resolve_and_store_people(archive_store,[WORK_ACCOUNT])
    assert len(before) == 2
    ledger = CleanupLedger(tmp_path/"cleanup.sqlite")
    ledger.decision("SAME",["email:alice@one.test"],["email:asmith@two.test"],"","explicit linkage")
    ledger.decision("NAME",["email:alice@one.test"],[],"Alice Smith","signed")
    ledger.connection.execute("INSERT INTO metadata VALUES ('apply_enabled','true')")
    ledger.connection.commit()
    merged = resolve_and_store_people(archive_store,[WORK_ACCOUNT])
    assert len(merged) == 1
    assert merged[0].canonical_name == "Alice Smith"
    append_assertion(tmp_path/"identity_assertions.jsonl",make_assertion(Decision.DIFFERENT,["email:alice@one.test"],["email:asmith@two.test"],"two people"))
    assert len(resolve_and_store_people(archive_store,[WORK_ACCOUNT])) == 2
    ledger.close()


def fake_gpt_runner(expected_model: str, expected_effort: str, input_tokens: int, output_tokens: int):
    def run(command, **_kwargs):
        assert command[0] == '/private/crm/codex.js'
        assert command[command.index('-m')+1] == expected_model
        assert f'model_reasoning_effort="{expected_effort}"' in command
        assert '--ignore-user-config' in command
        assert 'web_search="disabled"' in command
        Path(command[command.index('-o')+1]).write_text(json.dumps({'facts':[], 'needs_more':False, 'reason':'No evidence'}))
        event = json.dumps({'type':'turn.completed','usage':{'input_tokens':input_tokens,'output_tokens':output_tokens}})
        return subprocess.CompletedProcess(command,0,stdout=event,stderr="")
    return run


def test_gpt_adapter_uses_only_pinned_gpt_and_validates_output() -> None:
    result,usage,events = ask_gpt("Assess supplied evidence",Extraction,runner=fake_gpt_runner("gpt-5.6-sol","medium",12,3))
    assert result.reason == "No evidence"
    assert usage == {"input_tokens":12,"output_tokens":3}
    assert 'turn.completed' in events


def test_gpt_adapter_accepts_explicit_routine_model() -> None:
    result,usage,_ = ask_gpt("Assess supplied evidence",Extraction,model="gpt-5.6-terra",reasoning_effort="low",runner=fake_gpt_runner("gpt-5.6-terra","low",8,2))
    assert result.reason == "No evidence"
    assert usage == {"input_tokens":8,"output_tokens":2}


def test_gpt_adapter_retries_one_temporary_failure() -> None:
    successful_runner = fake_gpt_runner("gpt-5.6-sol", "medium", 7, 2)
    attempts = 0

    def flaky_runner(command, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        return successful_runner(command, **kwargs)

    _, usage, _ = ask_gpt("Assess supplied evidence", Extraction, runner=flaky_runner)
    assert attempts == 2
    assert usage == {"input_tokens": 7, "output_tokens": 2}


def test_gpt_adapter_pauses_after_bounded_failures() -> None:
    attempts = 0

    def unavailable_runner(command, **kwargs):
        nonlocal attempts
        attempts += 1
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    with pytest.raises(ProviderUnavailable, match="paused safely"):
        ask_gpt("Assess supplied evidence", Extraction, runner=unavailable_runner)
    assert attempts == 2


def test_gpt_timeout_terminates_descendants(tmp_path: Path) -> None:
    child_ready = tmp_path / "child-ready"
    child_terminated = tmp_path / "child-terminated"
    child_program = (
        "import os, signal, sys; "
        f"open({str(child_ready)!r}, 'w').close(); "
        "os.kill(int(sys.argv[1]), signal.SIGUSR1); "
        f"signal.signal(signal.SIGTERM, lambda *_: (open({str(child_terminated)!r}, 'w').close(), sys.exit(0))); "
        "signal.pause()"
    )
    parent_program = f"""
import os
import signal
import subprocess
import sys

signal.signal(signal.SIGUSR1, lambda *_: None)
subprocess.Popen([sys.executable, "-c", {child_program!r}, str(os.getpid())])
signal.pause()
signal.pause()
"""
    with pytest.raises(subprocess.TimeoutExpired):
        run_command_in_process_group(
            [sys.executable, "-c", parent_program],
            input="",
            text=True,
            capture_output=True,
            cwd=tmp_path,
            timeout=0.5,
            check=False,
        )
    assert child_ready.exists()
    assert child_terminated.exists()


def test_public_research_declines_unidentified_first_names() -> None:
    result = research_batch([{"id":"a","name":"Alex","organization":"","title":""}],{})
    assert result[0][0] == "a"
    assert result[0][1].facts == []
    assert "Insufficient public identifiers" in result[0][1].reason
    assert result[0][2] == {}


def test_larger_cluster_requires_every_independent_high_confidence_edge(tmp_path: Path) -> None:
    ledger = CleanupLedger(tmp_path/"cleanup.sqlite")
    profiles = {p:{"handles":["email:"+p+"@test.com"]} for p in ("a","b","c")}
    strong = PairJudgment(decision="SAME",confidence="high",basis="explicit_link",citations=[],reason="link",needs_more=False)
    ledger.connection.execute("INSERT INTO pairs VALUES (?,?,?,?)",("a","b",strong.model_dump_json(),"same"))
    weak = strong.model_copy(update={"confidence":"moderate"})
    ledger.connection.execute("INSERT INTO pairs VALUES (?,?,?,?)",("a","c",weak.model_dump_json(),"cluster_review"))
    assert apply_pair(ledger,"b","c",strong,profiles) == "cluster_review"
    ledger.connection.execute("UPDATE pairs SET judgment=? WHERE a='a' AND b='c'",(strong.model_dump_json(),))
    assert apply_pair(ledger,"b","c",strong,profiles) == "same"
    ledger.close()


def test_split_blocks_indirect_cluster_merge() -> None:
    union = _UnionFind()
    a,b,c,d = (HandleKey(value) for value in ("email:a@x.test","email:b@x.test","email:c@x.test","email:d@x.test"))
    union.forbid(frozenset([a]),frozenset([d]))
    union.union(a,b)
    union.union(c,d)
    union.union(b,c)
    assert union.find(a) != union.find(d)
    assert union.find(a) == union.find(b)
    assert union.find(c) == union.find(d)


def test_until_complete_stops_for_finite_empty_roster(archive_store, tmp_path: Path) -> None:
    ledger_path = tmp_path / "cleanup.sqlite"
    progress = tmp_path / "progress.json"
    result = CliRunner().invoke(main,["run","--archive",str(archive_store.path),"--ledger",str(ledger_path),"--progress",str(progress),"--until-complete","--workers","1"])
    assert result.exit_code == 0, result.output
    assert json.loads(progress.read_text())["state"] == "complete"
    ledger = CleanupLedger(ledger_path)
    ledger.inventory([{"id":"new","name":"New arrival"}])
    assert ledger.profiles() == {}
    ledger.close()


def test_pair_batch_rejects_missing_duplicate_and_unrequested_ids() -> None:
    judgment = PairJudgment(decision="UNSURE",confidence="low",basis="insufficient",citations=[],reason="insufficient",needs_more=False)
    pair = PairResult(pair_id="a:b",judgment=judgment)
    validate_pair_ids(JudgmentBatch(pairs=[pair]),{"a:b"})
    for result in [JudgmentBatch(pairs=[]),JudgmentBatch(pairs=[pair,pair]),JudgmentBatch(pairs=[pair.model_copy(update={"pair_id":"c:d"})])]:
        with pytest.raises(CleanupError):
            validate_pair_ids(result,{"a:b"})


def test_pair_batch_cannot_borrow_another_pairs_sources() -> None:
    left = {"id":"a","handles":["email:a@x.test"]}
    right = {"id":"b","handles":["email:b@x.test"]}
    evidence = [{"id":"source-a","owner_id":"a","text":"Alice works at Example"},
                {"id":"source-b","owner_id":"b","text":"The conversation has specific matching details"}]
    left_only = PairJudgment(decision="SAME",confidence="high",basis="corroborated_context",citations=[Citation(source_id="source-a",quote="Alice works at Example")],reason="matching details",needs_more=False)
    assert validate_pair_result(left_only,left,right,evidence).decision == "UNSURE"
    unrelated = left_only.model_copy(update={"citations":[Citation(source_id="source-c",quote="Alice works at Example")]})
    assert validate_pair_result(unrelated,left,right,evidence).decision == "UNSURE"
    supported = left_only.model_copy(update={"citations":[*left_only.citations,Citation(source_id="source-b",quote="specific matching details")]})
    assert validate_pair_result(supported,left,right,evidence).decision == "SAME"
    awaiting = supported.model_copy(update={"needs_more":True})
    assert validate_pair_result(awaiting,left,right,evidence).decision == "UNSURE"
