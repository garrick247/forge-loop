# Changelog

## 0.1.0 — initial release

First public version.  Extracted and generalized from the openptxas
autonomous-fix loop in `forge-workbench` (`workbench.probe.db`,
`probe.wrappers.dispatch_via_claude.sh`, `_harvest/autofix_cycle.sh`,
`_harvest/verify_open_edges.py`, and the `_cmd_probe_watch` orchestrator
in `workbench.cli`).

### Features

- **`EdgeDB`** — SQLite-backed tracker for *edges* (work items),
  *fixes* (resolution history), and *cycles* (orchestrator runs).
  WAL mode, dependency-free, schema-versioned-by-additive-ALTER.
  Validated `status` and `severity` enums.
- **Edge state machine**: `open` → `investigating` →
  `resolved-pending-verify` → `resolved`, plus `wontfix`.
  Reopen supported.  Diagnosis carry-forward (`last_diagnosis`)
  for multi-attempt retries.
- **Cluster-aware dispatch rotation** (`cluster_rotation`): round-robin
  across `cluster_key` so a single noisy bug class doesn't monopolize
  the queue.  Solo edges (no cluster) each get their own bucket.
- **Per-edge cooldown** (`last_dispatched_at`): re-pickable after
  N hours; default 24h.
- **`Dispatcher` interface** — pluggable LLM dispatch.  Built-ins:
  `ShellDispatcher` (any CLI template, prompt via stdin or file),
  `ClaudeCliDispatcher` (convenience wrapper), `CallableDispatcher`
  (in-process Python).  Each call returns a `DispatchResult` with
  rc, transcript, prompt path, transcript path.
- **`Verifier` interface** — pluggable validation gate.  Built-ins:
  `ShellVerifier` (list of commands; all must exit 0; first-failure
  diagnosis capture), `CallableVerifier`, `NullVerifier`.
- **`EdgeSource` interface** — pluggable filers.  `ShellEdgeSource`
  parses JSON/JSONL output from any external scanner; `CallableEdgeSource`
  for in-process; `StaticEdgeSource` for tests / seed data.  Built-in
  dedup-by-(title, target) against open edges so you can re-run sources
  every cycle without duplication.
- **`Cycle` orchestrator** — pre-cycle hooks → sources → cluster-rotated
  dispatch (severity-sorted, cooldown-honored, cap-truncated) →
  verify-between-dispatches → post-cycle hooks.  `run_once()` and
  `run_forever(interval_s)` (SIGINT-safe).
- **Default prompt builder** — structured markdown prompt with bug
  summary, evidence section (for ground-truth pass-through), last-attempt
  diagnosis carry-forward, similar-fix pattern hints from history,
  validation commands, hard constraints.  Fully replaceable.
- **JSON-config loader** (`build_cycle`): wires up the dispatcher,
  verifier, sources, hooks, and prompt kwargs from a single JSON file.
  Stdlib only — no PyYAML, no TOML library required.
- **CLI** (`forge-loop`): `run`, `run-forever`, `init-config`,
  `edges add/list/show/resolve/reopen`, `fixes list`, `cycles list`.
- **Example config**: `examples/openptxas-config.json` — the real shape
  of the openptxas autofix-cycle config we run hourly.
- **Tests**: 27 unit tests covering the DB roundtrip, cooldown logic,
  cluster rotation, resolution flow, sources/dedup, verifier
  pass/fail/timeout, cycle orchestration (dispatch + verify, diagnosis
  carry-forward, max-dispatches cap, pre/post hook capture, sources →
  dispatch end-to-end), prompt builder structure, and CLI smoke tests.

### Out of scope (deferred to v0.2+)

- **Multi-machine dispatch**: parallel dispatch across hosts (e.g. BD +
  GD) is a v0.2 target.  Today, `Cycle` runs one process at a time.
- **Web dashboard**: there's no HTTP service — the markdown digest
  pattern from the source autofix-cycle script is the path forward.
- **YAML / TOML config**: JSON only for v0.1 to keep the package
  dependency-free.
- **Auth / credential management**: assume the operator has any required
  CLI tools (`claude`, `gh`, etc.) configured outside of forge-loop.
- **Per-cycle digest emission**: source script writes a markdown
  digest after each cycle; not yet ported to forge-loop's CLI (callers
  can stitch one from `cycles list` + `edges list` for now).

### Provenance

The 38-phase openptxas-vs-ptxas autonomous-fix session that motivated
this extraction lives in the companion `forge-workbench` repo (private).
This repo is the generalized, dependency-free distillation suitable
for any LLM-driven fix-and-verify loop.
