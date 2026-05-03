"""forge-loop command-line entry point.

Subcommands:

  run           — run a single cycle from a config file.
  run-forever   — run cycles in a loop until interrupted.
  edges add     — file a single edge by hand (useful for seeding).
  edges list    — list edges, filtered by status / cluster.
  edges show    — show one edge in detail.
  edges resolve — record a manual resolution (for one-shot manual fixes).
  edges reopen  — bump status back to 'open'.
  fixes list    — list recent fixes.
  cycles list   — list recent cycle summaries.
  init-config   — write a starter config file you can edit.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import build_cycle, load_config
from .db import EdgeDB, VALID_SEVERITIES, VALID_STATUSES
from . import __version__


_STARTER_CONFIG = {
    "db_path": "./forge-loop.sqlite",
    "dispatch": {
        "kind": "claude",
        "claude_bin": "claude",
        "extra_args": "--print",
        "timeout": 1500,
        "prompt_dir": "./forge-loop-prompts"
    },
    "verifier": {
        "kind": "shell",
        "commands": [
            "pytest -x"
        ],
        "timeout_per_command": 600
    },
    "sources": [],
    "pre_cycle": [],
    "post_cycle": [],
    "cooldown_hours": 24,
    "max_dispatches_per_cycle": 4,
    "verify_between_dispatches": True,
    "prompt_kwargs": {
        "project_intro": "You are an autonomous fix agent.  Read the bug, edit the code, validate, commit.",
        "repo_paths": [],
        "likely_files": [],
        "hard_constraints": [
            "Do not skip tests.",
            "Do not amend or rebase prior commits.",
            "Keep the change minimal."
        ],
        "validation_commands": [
            "pytest -x"
        ],
        "done_criterion": "Validation gate exits 0; the fix is committed."
    }
}


def _cmd_run(args) -> int:
    cycle = build_cycle(args.config)
    cycle.run_once()
    return 0


def _cmd_run_forever(args) -> int:
    cycle = build_cycle(args.config)
    cycle.run_forever(interval_s=args.interval, max_cycles=args.max_cycles)
    return 0


def _cmd_init_config(args) -> int:
    p = Path(args.path)
    if p.exists() and not args.force:
        print(f"refusing to overwrite existing {p} (use --force)",
              file=sys.stderr)
        return 2
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(_STARTER_CONFIG, indent=2) + "\n",
                 encoding="utf-8")
    print(f"wrote starter config to {p}")
    return 0


# ---- edges ----

def _open_db(args) -> EdgeDB:
    if args.db:
        return EdgeDB(args.db)
    if args.config:
        cfg = load_config(args.config)
        return EdgeDB(cfg["db_path"])
    print("provide --db or --config", file=sys.stderr)
    sys.exit(2)


def _cmd_edges_add(args) -> int:
    db = _open_db(args)
    ctx = json.loads(args.context) if args.context else None
    eid = db.add_edge(
        title=args.title,
        category=args.category,
        description=args.description,
        target=args.target,
        cluster_key=args.cluster,
        severity=args.severity,
        prompt_context=ctx,
        notes=args.notes,
    )
    print(f"filed edge_{eid}")
    return 0


def _cmd_edges_list(args) -> int:
    db = _open_db(args)
    edges = db.list_edges(
        status=args.status,
        category=args.category,
        cluster_key=args.cluster,
        limit=args.limit,
    )
    if not edges:
        print("(no edges)")
        return 0
    for e in edges:
        cluster = e.cluster_key or "-"
        target = e.target or "-"
        print(f"edge_{e.edge_id:<5} [{e.severity:<7}] [{e.status:<22}] "
              f"{cluster:<24} {target:<32} {e.title}")
    return 0


def _cmd_edges_show(args) -> int:
    db = _open_db(args)
    e = db.get_edge(args.edge_id)
    if e is None:
        print(f"edge_{args.edge_id} not found", file=sys.stderr)
        return 2
    print(f"edge_id:           {e.edge_id}")
    print(f"discovered_at:     {e.discovered_at}")
    print(f"category:          {e.category}")
    print(f"title:             {e.title}")
    print(f"target:            {e.target}")
    print(f"cluster_key:       {e.cluster_key}")
    print(f"severity:          {e.severity}")
    print(f"status:            {e.status}")
    print(f"attempts:          {e.attempts}")
    print(f"last_dispatched:   {e.last_dispatched_at}")
    if e.description:
        print(f"description:\n  {e.description}")
    if e.last_diagnosis:
        print(f"last_diagnosis:\n  {e.last_diagnosis}")
    if e.prompt_context:
        print(f"prompt_context:\n  {json.dumps(e.prompt_context, indent=2)}")
    if e.notes:
        print(f"notes:\n  {e.notes}")
    return 0


def _cmd_edges_resolve(args) -> int:
    db = _open_db(args)
    fix_id = db.record_resolution(
        edge_id=args.edge_id,
        commit_sha=args.commit_sha,
        summary=args.summary,
        notes=args.notes,
    )
    print(f"recorded fix_{fix_id} for edge_{args.edge_id} (status -> resolved-pending-verify)")
    if args.verify:
        db.mark_resolution_verified(args.edge_id, verifying_run="cli")
        print(f"promoted edge_{args.edge_id} -> resolved")
    return 0


def _cmd_edges_reopen(args) -> int:
    db = _open_db(args)
    db.reopen_edge(args.edge_id, args.reason or "reopened via CLI")
    print(f"reopened edge_{args.edge_id}")
    return 0


def _cmd_fixes_list(args) -> int:
    db = _open_db(args)
    rows = db.list_fixes(cluster_key=args.cluster, target=args.target,
                         limit=args.limit)
    if not rows:
        print("(no fixes)")
        return 0
    for (fid, fixed_at, eid, cluster, sha, summary, target, verified) in rows:
        v = "✓" if verified else " "
        sha_s = (sha or "-")[:12]
        print(f"fix_{fid:<5} [{v}] {fixed_at} edge_{eid or '-':<5} "
              f"{(cluster or '-'):<22} {sha_s:<12} {target or '-':<24} "
              f"{summary or ''}")
    return 0


def _cmd_cycles_list(args) -> int:
    db = _open_db(args)
    rows = db.list_cycles(limit=args.limit)
    if not rows:
        print("(no cycles)")
        return 0
    for (cid, started, finished, filed, dispatched, resolved,
         pre_rc, post_rc, _notes) in rows:
        finished = finished or "(in-progress)"
        print(f"cycle_{cid:<5} {started} -> {finished}  "
              f"filed={filed}  dispatched={dispatched}  resolved={resolved}  "
              f"pre={pre_rc}  post={post_rc}")
    return 0


# ---- argparse wiring ----

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="forge-loop",
        description="Autonomous LLM-driven fix loop with edge tracking.")
    p.add_argument("--version", action="version",
                   version=f"forge-loop {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    # run
    sp = sub.add_parser("run", help="run a single cycle from a config file")
    sp.add_argument("config", help="path to a forge-loop JSON config")
    sp.set_defaults(func=_cmd_run)

    # run-forever
    sp = sub.add_parser("run-forever",
                        help="run cycles in a loop until interrupted")
    sp.add_argument("config", help="path to a forge-loop JSON config")
    sp.add_argument("--interval", type=int, default=3600,
                    help="seconds between cycles (default: 3600)")
    sp.add_argument("--max-cycles", type=int, default=None,
                    help="stop after N cycles (default: infinite)")
    sp.set_defaults(func=_cmd_run_forever)

    # init-config
    sp = sub.add_parser("init-config",
                        help="write a starter forge-loop.json")
    sp.add_argument("path", nargs="?", default="forge-loop.json")
    sp.add_argument("--force", action="store_true",
                    help="overwrite existing file")
    sp.set_defaults(func=_cmd_init_config)

    # edges
    edges = sub.add_parser("edges", help="manage edges")
    esub = edges.add_subparsers(dest="edges_cmd", required=True)

    common_db_args = lambda parser: (
        parser.add_argument("--db", help="path to the forge-loop sqlite DB "
                                          "(or directory)"),
        parser.add_argument("--config", help="path to config (read db_path "
                                              "from it)"),
    )

    sp = esub.add_parser("add", help="file a new edge")
    common_db_args(sp)
    sp.add_argument("--title", required=True)
    sp.add_argument("--category", default="bug")
    sp.add_argument("--description", default=None)
    sp.add_argument("--target", default=None)
    sp.add_argument("--cluster", default=None,
                    help="cluster_key — used for round-robin rotation")
    sp.add_argument("--severity", default="medium",
                    choices=VALID_SEVERITIES)
    sp.add_argument("--context", default=None,
                    help="JSON string of prompt-context evidence")
    sp.add_argument("--notes", default=None)
    sp.set_defaults(func=_cmd_edges_add)

    sp = esub.add_parser("list", help="list edges")
    common_db_args(sp)
    sp.add_argument("--status", default=None, choices=(*VALID_STATUSES, None))
    sp.add_argument("--category", default=None)
    sp.add_argument("--cluster", default=None)
    sp.add_argument("--limit", type=int, default=None)
    sp.set_defaults(func=_cmd_edges_list)

    sp = esub.add_parser("show", help="show one edge in detail")
    common_db_args(sp)
    sp.add_argument("edge_id", type=int)
    sp.set_defaults(func=_cmd_edges_show)

    sp = esub.add_parser("resolve", help="record a manual resolution")
    common_db_args(sp)
    sp.add_argument("edge_id", type=int)
    sp.add_argument("--commit-sha", default=None)
    sp.add_argument("--summary", default=None)
    sp.add_argument("--notes", default=None)
    sp.add_argument("--verify", action="store_true",
                    help="also promote to 'resolved' (skip pending-verify)")
    sp.set_defaults(func=_cmd_edges_resolve)

    sp = esub.add_parser("reopen", help="reopen a resolved edge")
    common_db_args(sp)
    sp.add_argument("edge_id", type=int)
    sp.add_argument("--reason", default=None)
    sp.set_defaults(func=_cmd_edges_reopen)

    # fixes
    fixes = sub.add_parser("fixes", help="manage fixes")
    fsub = fixes.add_subparsers(dest="fixes_cmd", required=True)
    sp = fsub.add_parser("list", help="list recent fixes")
    common_db_args(sp)
    sp.add_argument("--cluster", default=None)
    sp.add_argument("--target", default=None)
    sp.add_argument("--limit", type=int, default=50)
    sp.set_defaults(func=_cmd_fixes_list)

    # cycles
    cycles = sub.add_parser("cycles", help="manage cycles")
    csub = cycles.add_subparsers(dest="cycles_cmd", required=True)
    sp = csub.add_parser("list", help="list recent cycles")
    common_db_args(sp)
    sp.add_argument("--limit", type=int, default=20)
    sp.set_defaults(func=_cmd_cycles_list)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
