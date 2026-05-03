"""End-to-end tests for forge-loop.

No real LLM is exercised — we plug in CallableDispatcher / CallableVerifier
fakes so each test runs in milliseconds and is fully deterministic.

Test surface (>= 10 cases):

  - EdgeDB roundtrip (add, get, list, severity ordering, status filter)
  - Cooldown filter on dispatchable_edges
  - Cluster rotation: round-robin across cluster_key
  - Resolution flow: open -> resolved-pending-verify -> resolved
  - Reopen
  - ShellEdgeSource JSON / JSONL parsing + dedup
  - ShellVerifier: green / red / first-failure-stops
  - Cycle: edges filed -> dispatched -> resolved by verifier
  - Cycle: failed verify carries diagnosis forward
  - Cycle: max_dispatches_per_cycle cap
  - Cycle: pre/post hooks rc captured
  - CLI smoke: init-config + edges add/list/show
  - Default prompt builder structure (header + sections)
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from forge_loop import (
    CallableDispatcher,
    CallableVerifier,
    Cycle,
    DispatchResult,
    Edge,
    EdgeDB,
    NullVerifier,
    ShellEdgeSource,
    ShellVerifier,
    StaticEdgeSource,
    cluster_rotation,
    default_prompt_builder,
    file_candidates,
    run_sources,
)
from forge_loop import cli as cli_mod


# ---- EdgeDB ----

def test_edgedb_add_get_list(tmp_path):
    db = EdgeDB(tmp_path)
    eid1 = db.add_edge(title="bug-A", target="foo.py:10", severity="high")
    eid2 = db.add_edge(title="bug-B", target="bar.py:20", severity="low")
    db.add_edge(title="bug-C", target="baz.py:30", severity="blocker",
                cluster_key="cluster-1")
    assert eid1 == 1 and eid2 == 2

    e = db.get_edge(eid1)
    assert isinstance(e, Edge)
    assert e.title == "bug-A" and e.target == "foo.py:10"
    assert e.severity == "high"
    assert e.status == "open"

    edges = db.list_edges()
    assert [e.title for e in edges] == ["bug-C", "bug-A", "bug-B"]
    assert db.edge_count() == 3
    assert db.edge_count(status="open") == 3
    assert db.edge_count(status="resolved") == 0


def test_edgedb_dispatchable_cooldown(tmp_path):
    db = EdgeDB(tmp_path)
    eid = db.add_edge(title="t", severity="medium")
    # Fresh edge: dispatchable.
    assert any(e.edge_id == eid for e in db.dispatchable_edges())
    db.stamp_dispatched(eid)
    # 24h cooldown: not dispatchable until tomorrow.
    assert not any(e.edge_id == eid
                   for e in db.dispatchable_edges(cooldown_hours=24.0))
    # 0h cooldown: dispatchable immediately again.
    assert any(e.edge_id == eid
               for e in db.dispatchable_edges(cooldown_hours=0.0))
    # attempts incremented.
    assert db.get_edge(eid).attempts == 1


def test_edgedb_resolution_flow(tmp_path):
    db = EdgeDB(tmp_path)
    eid = db.add_edge(title="t")
    fid = db.record_resolution(edge_id=eid, commit_sha="abc1234567",
                               summary="fix the thing")
    e = db.get_edge(eid)
    assert e.status == "resolved-pending-verify"
    fixes = db.list_fixes()
    assert len(fixes) == 1
    assert fixes[0][0] == fid
    assert fixes[0][7] == 0  # not yet verified
    db.mark_resolution_verified(eid, verifying_run="cycle_42")
    e = db.get_edge(eid)
    assert e.status == "resolved"
    assert db.list_fixes()[0][7] == 1  # verified flag flipped


def test_edgedb_reopen(tmp_path):
    db = EdgeDB(tmp_path)
    eid = db.add_edge(title="t")
    db.record_resolution(edge_id=eid, summary="thought we fixed it")
    db.mark_resolution_verified(eid)
    assert db.get_edge(eid).status == "resolved"
    db.reopen_edge(eid, "regression hit again")
    assert db.get_edge(eid).status == "open"
    notes = db.get_edge(eid).notes or ""
    assert "reopened" in notes


def test_edgedb_diagnosis_carryforward(tmp_path):
    db = EdgeDB(tmp_path)
    eid = db.add_edge(title="t")
    db.record_diagnosis(eid, "test_x failed: assertion on line 17")
    e = db.get_edge(eid)
    assert "assertion on line 17" in e.last_diagnosis


# ---- cluster rotation ----

def test_cluster_rotation_round_robin():
    edges = [
        Edge(edge_id=1, discovered_at="", category="", title="a", cluster_key="X"),
        Edge(edge_id=2, discovered_at="", category="", title="b", cluster_key="X"),
        Edge(edge_id=3, discovered_at="", category="", title="c", cluster_key="Y"),
        Edge(edge_id=4, discovered_at="", category="", title="d", cluster_key="Y"),
        Edge(edge_id=5, discovered_at="", category="", title="e", cluster_key="Z"),
    ]
    order = cluster_rotation(edges)
    # Round-robin: X(1), Y(3), Z(5), X(2), Y(4)
    assert order == [1, 3, 5, 2, 4]


def test_cluster_rotation_solo_keys_distinct():
    edges = [
        Edge(edge_id=1, discovered_at="", category="", title="a"),
        Edge(edge_id=2, discovered_at="", category="", title="b", cluster_key="X"),
        Edge(edge_id=3, discovered_at="", category="", title="c"),
    ]
    order = cluster_rotation(edges)
    # All three are in distinct clusters; order preserved.
    assert order == [1, 2, 3]


# ---- sources ----

def test_static_source_dedup(tmp_path):
    db = EdgeDB(tmp_path)
    src = StaticEdgeSource(name="s", items=[
        {"title": "T1", "target": "a"},
        {"title": "T2", "target": "b"},
        {"title": "T1", "target": "a"},  # duplicate
    ])
    n_total, results = run_sources(db, [src])
    assert n_total == 2
    assert results[0].candidates == 3
    assert results[0].filed == 2
    # Re-running should not file dupes (dedup against existing OPEN).
    src2 = StaticEdgeSource(name="s2", items=[
        {"title": "T1", "target": "a"},
        {"title": "T3", "target": "c"},
    ])
    n2, _ = run_sources(db, [src2])
    assert n2 == 1
    assert db.edge_count() == 3


def test_shell_source_parses_json_array(tmp_path):
    db = EdgeDB(tmp_path)
    py = sys.executable.replace("\\", "/")
    cmd = (f'{py} -c "import json; print(json.dumps('
           f'[{{\\"title\\":\\"sh-1\\",\\"target\\":\\"x\\"}},'
           f'{{\\"title\\":\\"sh-2\\",\\"target\\":\\"y\\"}}]))"')
    src = ShellEdgeSource(name="sh", cmd=cmd, timeout=30)
    n, [r] = run_sources(db, [src])
    assert r.error is None, f"source error: {r.error}"
    assert n == 2
    assert db.edge_count() == 2


def test_shell_source_unknown_keys_dropped(tmp_path):
    db = EdgeDB(tmp_path)
    py = sys.executable.replace("\\", "/")
    cmd = (f'{py} -c "import json; print(json.dumps('
           f'[{{\\"title\\":\\"sh-1\\",\\"weirdkey\\":42,\\"target\\":\\"x\\"}}]))"')
    src = ShellEdgeSource(name="sh", cmd=cmd, timeout=30)
    n, [r] = run_sources(db, [src])
    assert r.error is None
    assert n == 1


# ---- verifier ----

def test_shell_verifier_all_pass():
    py = sys.executable.replace("\\", "/")
    v = ShellVerifier(commands=[
        f'{py} -c "import sys; sys.exit(0)"',
        f'{py} -c "print(\'ok\')"',
    ], timeout_per_command=15)
    r = v.verify()
    assert r.ok
    assert r.failing_command is None
    assert len(r.transcripts) == 2


def test_shell_verifier_first_failure_stops():
    py = sys.executable.replace("\\", "/")
    v = ShellVerifier(commands=[
        f'{py} -c "import sys; print(\'first\'); sys.exit(0)"',
        f'{py} -c "import sys; print(\'fail-here\'); sys.exit(2)"',
        f'{py} -c "import sys; sys.exit(0)"',
    ], timeout_per_command=15)
    r = v.verify()
    assert not r.ok
    assert r.failing_rc == 2
    assert "fail-here" in r.diagnosis
    # Should have stopped after second command — third is never run.
    assert len(r.transcripts) == 2


def test_null_verifier_always_green():
    assert NullVerifier().verify().ok


def test_callable_verifier_wraps_function():
    v = CallableVerifier(fn=lambda eid: (True, "all good"))
    r = v.verify(edge_id=42)
    assert r.ok and r.diagnosis == "all good"
    v_bad = CallableVerifier(fn=lambda eid: (False, f"edge_{eid} failed"))
    r2 = v_bad.verify(edge_id=42)
    assert not r2.ok
    assert "edge_42 failed" in r2.diagnosis


def test_callable_verifier_handles_exceptions():
    v = CallableVerifier(fn=lambda eid: (_ for _ in ()).throw(RuntimeError("boom")))
    r = v.verify()
    assert not r.ok
    assert "RuntimeError" in r.diagnosis and "boom" in r.diagnosis


# ---- dispatcher ----

def test_callable_dispatcher_returns_transcript():
    seen = []
    d = CallableDispatcher(fn=lambda prompt, eid: (
        seen.append((eid, len(prompt))) or "all done"))
    r = d.dispatch("hello", 7)
    assert r.ok
    assert r.transcript == "all done"
    assert seen == [(7, 5)]


def test_callable_dispatcher_handles_exceptions():
    d = CallableDispatcher(fn=lambda *a: (_ for _ in ()).throw(ValueError("nope")))
    r = d.dispatch("p", 1)
    assert r.rc == -3
    assert r.error and "ValueError" in r.error


# ---- cycle ----

def test_cycle_dispatches_and_records_resolution(tmp_path):
    db = EdgeDB(tmp_path)
    eid = db.add_edge(title="t1", severity="high")
    # Verifier "passes" => orchestrator should mark the edge resolved-pending-verify.
    cycle = Cycle(
        db=db,
        dispatcher=CallableDispatcher(fn=lambda p, e: "ok"),
        verifier=CallableVerifier(fn=lambda eid: True),
        cooldown_hours=0.0,
        max_dispatches_per_cycle=4,
    )
    r = cycle.run_once()
    assert r.n_dispatched == 1
    assert r.dispatched[0].verify_passed_after is True
    e = db.get_edge(eid)
    assert e.status == "resolved-pending-verify"
    assert e.attempts == 1


def test_cycle_failed_verify_carries_diagnosis(tmp_path):
    db = EdgeDB(tmp_path)
    eid = db.add_edge(title="t-stuck", severity="medium")
    cycle = Cycle(
        db=db,
        dispatcher=CallableDispatcher(fn=lambda p, e: "ok"),
        verifier=CallableVerifier(
            fn=lambda eid: (False, f"edge_{eid}: still failing")),
        cooldown_hours=0.0,
    )
    cycle.run_once()
    e = db.get_edge(eid)
    assert e.status == "open"  # still open since verify said no
    assert e.last_diagnosis is not None
    assert "still failing" in e.last_diagnosis
    # Second dispatch (cooldown=0) should see the diagnosis in the prompt.
    captured = {}
    cycle.dispatcher = CallableDispatcher(
        fn=lambda p, eid: (captured.setdefault("p", p), "ok")[1])
    cycle.run_once()
    assert "still failing" in captured["p"]
    assert "Last attempt" in captured["p"]


def test_cycle_max_dispatches_cap(tmp_path):
    db = EdgeDB(tmp_path)
    for i in range(6):
        db.add_edge(title=f"e{i}", cluster_key=f"c{i % 3}", severity="medium")
    seen = []
    cycle = Cycle(
        db=db,
        dispatcher=CallableDispatcher(fn=lambda p, eid: seen.append(eid) or "ok"),
        verifier=NullVerifier(),
        cooldown_hours=0.0,
        max_dispatches_per_cycle=3,
    )
    r = cycle.run_once()
    assert r.n_dispatched == 3
    assert len(seen) == 3
    # Cluster rotation: should have hit 3 distinct clusters in the first 3 picks.
    edge_objs = [db.get_edge(eid) for eid in seen]
    assert len({e.cluster_key for e in edge_objs}) == 3


def test_cycle_pre_post_hooks_captured(tmp_path):
    db = EdgeDB(tmp_path)
    db.add_edge(title="t")
    py = sys.executable.replace("\\", "/")
    cycle = Cycle(
        db=db,
        dispatcher=CallableDispatcher(fn=lambda p, e: "ok"),
        verifier=NullVerifier(),
        pre_cycle=[f'{py} -c "import sys; sys.exit(0)"'],
        post_cycle=[f'{py} -c "import sys; sys.exit(7)"'],
        cooldown_hours=0.0,
    )
    r = cycle.run_once()
    assert r.pre_cycle_rcs[0][1] == 0
    assert r.post_cycle_rcs[0][1] == 7


def test_cycle_sources_file_then_dispatch(tmp_path):
    db = EdgeDB(tmp_path)
    src = StaticEdgeSource(name="seed", items=[
        {"title": "from-source-1", "severity": "blocker"},
        {"title": "from-source-2", "severity": "low"},
    ])
    captured = []
    cycle = Cycle(
        db=db,
        dispatcher=CallableDispatcher(
            fn=lambda p, eid: captured.append(eid) or "ok"),
        verifier=NullVerifier(),
        sources=[src],
        cooldown_hours=0.0,
        max_dispatches_per_cycle=10,
    )
    r = cycle.run_once()
    assert r.edges_filed == 2
    assert r.n_dispatched == 2
    # Blocker should be dispatched first (severity ordering).
    first_edge = db.get_edge(captured[0])
    assert first_edge.severity == "blocker"


def test_cycle_run_forever_max_cycles(tmp_path):
    db = EdgeDB(tmp_path)
    db.add_edge(title="t")
    cycle = Cycle(
        db=db,
        dispatcher=CallableDispatcher(fn=lambda p, e: "ok"),
        verifier=NullVerifier(),
        cooldown_hours=0.0,
    )
    rs = cycle.run_forever(interval_s=0, max_cycles=3)
    assert len(rs) == 3


# ---- prompt builder ----

def test_default_prompt_builder_includes_sections():
    e = Edge(edge_id=42, discovered_at="2026-05-02", category="bug",
             title="thing is broken", description="when X, then Y",
             target="foo.py:101", cluster_key="abc",
             severity="high", attempts=2,
             last_diagnosis="test failed: ASSERT (line 5)",
             prompt_context={"knob": "MIO_THROTTLE = 4"})
    p = default_prompt_builder(
        e,
        validation_commands=["pytest -x", "./gate.sh {eid}"],
        repo_paths=["/repo/myproj"],
        likely_files=["src/codegen.py"],
        hard_constraints=["No mocks in tests."],
    )
    assert "edge_42" in p
    assert "thing is broken" in p
    assert "foo.py:101" in p
    assert "abc" in p  # cluster_key
    assert "Last attempt's diagnosis" in p  # only when attempts > 0
    assert "ASSERT (line 5)" in p
    assert "MIO_THROTTLE" in p  # evidence
    assert "pytest -x" in p
    assert "/repo/myproj" in p
    assert "src/codegen.py" in p
    assert "No mocks in tests." in p


def test_default_prompt_omits_diagnosis_on_first_attempt():
    e = Edge(edge_id=1, discovered_at="t", category="bug", title="new bug",
             attempts=0, last_diagnosis="leftover from previous run")
    p = default_prompt_builder(e)
    # On first attempt, last_diagnosis should NOT be surfaced even if set.
    assert "leftover from previous run" not in p
    assert "Last attempt" not in p


# ---- CLI smoke ----

def test_cli_init_config_and_edges_addlist(tmp_path, capsys):
    cfg_path = tmp_path / "fl.json"
    rc = cli_mod.main(["init-config", str(cfg_path)])
    assert rc == 0
    assert cfg_path.exists()
    cfg = json.loads(cfg_path.read_text())
    assert "db_path" in cfg

    db_path = tmp_path / "fl.sqlite"
    rc = cli_mod.main([
        "edges", "add",
        "--db", str(db_path),
        "--title", "via cli",
        "--target", "x.py:1",
        "--severity", "high",
        "--cluster", "c1",
    ])
    assert rc == 0
    rc = cli_mod.main(["edges", "list", "--db", str(db_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "via cli" in out
    assert "high" in out


def test_cli_edges_resolve_and_show(tmp_path, capsys):
    db_path = tmp_path / "fl.sqlite"
    cli_mod.main([
        "edges", "add", "--db", str(db_path),
        "--title", "to-resolve", "--target", "y.py:9",
    ])
    rc = cli_mod.main([
        "edges", "resolve", "--db", str(db_path), "1",
        "--commit-sha", "deadbeef00", "--summary", "fixed!",
        "--verify",
    ])
    assert rc == 0
    capsys.readouterr()
    rc = cli_mod.main(["edges", "show", "--db", str(db_path), "1"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "resolved" in out
