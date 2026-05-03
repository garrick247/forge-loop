"""forge-loop — autonomous LLM-driven differential-compiler-improvement loop.

Public API:

  EdgeDB                       — SQLite-backed edge / fix / cycle tracker.
  Edge                         — dataclass view of an edge row.
  cluster_rotation             — round-robin queue across cluster_key.

  Dispatcher                   — Protocol; pluggable LLM dispatch.
  ShellDispatcher              — runs a shell template per dispatch.
  ClaudeCliDispatcher          — convenience: pipe to `claude --print`.
  CallableDispatcher           — wrap an in-process Python callable.
  DispatchResult.

  Verifier                     — Protocol; pluggable validation gate.
  ShellVerifier                — runs a list of shell commands; all must exit 0.
  CallableVerifier / NullVerifier.
  VerifyResult.

  EdgeSource                   — Protocol; pluggable edge-filing source.
  ShellEdgeSource / CallableEdgeSource / StaticEdgeSource.
  run_sources / file_candidates.

  default_prompt_builder       — generic prompt template.
  Cycle                        — per-cycle orchestrator (run_once / run_forever).
  CycleResult, DispatchOutcome.

  build_cycle, load_config     — JSON-config -> Cycle wiring.
"""
from .db import (
    Edge,
    EdgeDB,
    cluster_rotation,
    VALID_SEVERITIES,
    VALID_STATUSES,
)
from .dispatch import (
    CallableDispatcher,
    ClaudeCliDispatcher,
    DispatchResult,
    Dispatcher,
    ShellDispatcher,
)
from .verify import (
    CallableVerifier,
    NullVerifier,
    ShellVerifier,
    VerifyResult,
    Verifier,
)
from .sources import (
    CallableEdgeSource,
    EdgeSource,
    ShellEdgeSource,
    SourceResult,
    StaticEdgeSource,
    file_candidates,
    run_sources,
)
from .prompts import default_prompt_builder
from .cycle import Cycle, CycleResult, DispatchOutcome
from .config import build_cycle, load_config


__version__ = "0.1.0"


__all__ = [
    "Edge",
    "EdgeDB",
    "cluster_rotation",
    "VALID_SEVERITIES",
    "VALID_STATUSES",
    "CallableDispatcher",
    "ClaudeCliDispatcher",
    "DispatchResult",
    "Dispatcher",
    "ShellDispatcher",
    "CallableVerifier",
    "NullVerifier",
    "ShellVerifier",
    "VerifyResult",
    "Verifier",
    "CallableEdgeSource",
    "EdgeSource",
    "ShellEdgeSource",
    "SourceResult",
    "StaticEdgeSource",
    "file_candidates",
    "run_sources",
    "default_prompt_builder",
    "Cycle",
    "CycleResult",
    "DispatchOutcome",
    "build_cycle",
    "load_config",
    "__version__",
]
