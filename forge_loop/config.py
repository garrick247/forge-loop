"""Build a `Cycle` from a JSON config.

JSON-only on purpose — keeps the package dependency-free.  YAML or
TOML support is trivial to add downstream by parsing the config in
your own loader and calling `Cycle(...)` directly.

Schema (all fields optional except `db_path`):

  {
    "db_path": "./forge-loop.sqlite",

    "dispatch": {
      "kind": "shell",                 // "shell" | "claude"
      "template": "claude --print",    // for "shell"
      "claude_bin": "claude",          // for "claude"
      "extra_args": "--print",         // for "claude"
      "timeout": 1500,
      "stdin_prompt": true,
      "prompt_dir": "/tmp/forge-loop-prompts"
    },

    "verifier": {
      "kind": "shell",                 // "shell" | "null"
      "commands": [
        "pytest -x tests/",
        "./scripts/regression_gate.sh {eid}"
      ],
      "timeout_per_command": 600
    },

    "sources": [
      {"kind": "shell", "name": "scan-bugs", "cmd": "./scan_bugs.sh",
       "timeout": 300}
    ],

    "pre_cycle":  ["./refresh_signals.sh"],
    "post_cycle": ["./reverify_resolved.sh"],

    "cooldown_hours": 24,
    "max_dispatches_per_cycle": 4,
    "verify_between_dispatches": true,

    "prompt_kwargs": {
      "project_intro": "You are fixing openptxas, a CUDA PTX→SASS compiler.",
      "repo_paths": ["C:/Users/kraken/openptxas"],
      "likely_files": ["sass/isel.py", "sass/regalloc.py"],
      "hard_constraints": ["Do not skip pytest.", "..."],
      "validation_commands": [
        "workbench probe-commit --resolves {eid} --push"
      ],
      "done_criterion": "probe-commit exits 0 and the push lands."
    }
  }
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .cycle import Cycle
from .db import EdgeDB
from .dispatch import (CallableDispatcher, ClaudeCliDispatcher, Dispatcher,
                       ShellDispatcher)
from .sources import (CallableEdgeSource, ShellEdgeSource, StaticEdgeSource)
from .verify import CallableVerifier, NullVerifier, ShellVerifier, Verifier


def load_config(path: str | Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def build_dispatcher(cfg: dict | None) -> Dispatcher:
    if not cfg:
        return ClaudeCliDispatcher()
    kind = cfg.get("kind", "shell")
    if kind == "claude":
        return ClaudeCliDispatcher(
            claude_bin=cfg.get("claude_bin", "claude"),
            timeout=int(cfg.get("timeout", 1500)),
            prompt_dir=cfg.get("prompt_dir"),
            extra_args=cfg.get("extra_args", "--print"),
        )
    if kind == "shell":
        return ShellDispatcher(
            template=cfg["template"],
            timeout=int(cfg.get("timeout", 1500)),
            stdin_prompt=bool(cfg.get("stdin_prompt", True)),
            prompt_dir=cfg.get("prompt_dir"),
            extra_env=dict(cfg.get("extra_env", {})),
            use_shell=bool(cfg.get("use_shell", True)),
        )
    raise ValueError(f"unknown dispatch.kind: {kind!r}")


def build_verifier(cfg: dict | None) -> Verifier:
    if not cfg:
        return NullVerifier()
    kind = cfg.get("kind", "shell")
    if kind == "null":
        return NullVerifier()
    if kind == "shell":
        return ShellVerifier(
            commands=list(cfg.get("commands", [])),
            timeout_per_command=int(cfg.get("timeout_per_command", 600)),
            use_shell=bool(cfg.get("use_shell", True)),
            diagnosis_tail_chars=int(cfg.get("diagnosis_tail_chars", 1500)),
        )
    raise ValueError(f"unknown verifier.kind: {kind!r}")


def build_sources(cfg: list | None) -> list:
    out = []
    for s in (cfg or []):
        kind = s.get("kind", "shell")
        if kind == "shell":
            out.append(ShellEdgeSource(
                name=s["name"],
                cmd=s["cmd"],
                timeout=int(s.get("timeout", 600)),
                use_shell=bool(s.get("use_shell", True)),
                dedup_by=tuple(s.get("dedup_by", ("title", "target"))),
            ))
        elif kind == "static":
            out.append(StaticEdgeSource(
                name=s["name"],
                items=list(s.get("items", [])),
                repeat=bool(s.get("repeat", False)),
            ))
        else:
            raise ValueError(f"unknown source.kind: {kind!r}")
    return out


def build_cycle(config: dict | str | Path) -> Cycle:
    """Build a fully-wired Cycle from a config dict (or path to JSON file)."""
    cfg = load_config(config) if isinstance(config, (str, Path)) else dict(config)

    if "db_path" not in cfg:
        raise ValueError("config requires a 'db_path' field")

    db = EdgeDB(cfg["db_path"])
    dispatcher = build_dispatcher(cfg.get("dispatch"))
    verifier = build_verifier(cfg.get("verifier"))
    sources = build_sources(cfg.get("sources"))

    return Cycle(
        db=db,
        dispatcher=dispatcher,
        verifier=verifier,
        sources=sources,
        pre_cycle=list(cfg.get("pre_cycle", [])),
        post_cycle=list(cfg.get("post_cycle", [])),
        prompt_kwargs=dict(cfg.get("prompt_kwargs", {})),
        cooldown_hours=float(cfg.get("cooldown_hours", 24.0)),
        max_dispatches_per_cycle=cfg.get("max_dispatches_per_cycle", 4),
        verify_between_dispatches=bool(cfg.get("verify_between_dispatches", True)),
        pre_cycle_timeout=int(cfg.get("pre_cycle_timeout", 600)),
        post_cycle_timeout=int(cfg.get("post_cycle_timeout", 600)),
    )
