# forge-loop

**Autonomous LLM-driven differential-compiler-improvement loop.**

Configure a *dispatcher* (typically `claude --print`), a *verifier*
(any list of shell commands that must exit 0), and zero or more
*edge sources* (anything that can produce JSON describing a bug).
forge-loop schedules per-cycle work: file new edges, build a focused
prompt for one, hand it to the LLM, run the verifier, commit-or-revert
based on the gate, persist the outcome, rotate to the next cluster,
repeat.

A single SQLite file tracks every edge through `open` →
`investigating` → `resolved-pending-verify` → `resolved`, carries
verify-failure diagnoses forward across attempts, applies per-edge
cooldowns, and records every fix as historical pattern hints for
future prompts.

There's no public framework that does this.  LangChain / LlamaIndex
focus on retrieval and chain composition; agent SDKs (Claude Agent
SDK, OpenAI Assistants) build single-task agents.  forge-loop is the
*loop around* those agents — the part that decides what to fix next,
how to ground the prompt, what counts as success, and what to retry.
It's the extracted, generalized form of the autonomous-fix harness
that drove a 38-phase openptxas-vs-ptxas differential-compiler session.

## Install

From source:

```bash
git clone https://github.com/garrick99/forge-loop
cd forge-loop
pip install -e .
```

Or run the package directly — `forge_loop` is a single dependency-free
Python package:

```bash
python -m forge_loop.cli --help
```

A PyPI release will land once the API stabilizes (target: 0.2.0).

## Quick start

1. **Initialize a config**:

   ```bash
   forge-loop init-config ./forge-loop.json
   ```

   This writes a starter JSON config you can edit.

2. **File an edge by hand** to seed the queue:

   ```bash
   forge-loop edges add \
       --db ./forge-loop.sqlite \
       --title "test_user_login fails on Postgres 16" \
       --target "tests/test_auth.py::test_user_login" \
       --severity high \
       --cluster auth-tests
   ```

3. **Edit `forge-loop.json`** to point at your real validator + dispatcher:

   ```json
   {
     "db_path": "./forge-loop.sqlite",
     "dispatch": {"kind": "claude", "claude_bin": "claude", "timeout": 1500},
     "verifier": {
       "kind": "shell",
       "commands": ["pytest -x tests/", "ruff check ."],
       "timeout_per_command": 600
     },
     "cooldown_hours": 24,
     "max_dispatches_per_cycle": 4,
     "prompt_kwargs": {
       "project_intro": "You are fixing the auth service.",
       "repo_paths": ["./"],
       "validation_commands": ["pytest -x tests/", "ruff check ."]
     }
   }
   ```

4. **Run a single cycle** (dispatches one edge, verifies, records):

   ```bash
   forge-loop run ./forge-loop.json
   ```

5. **Run forever** (cron-style — one cycle every 30 minutes):

   ```bash
   forge-loop run-forever ./forge-loop.json --interval 1800
   ```

   On Windows this is what Task Scheduler should invoke.  On Linux,
   either cron-fire `forge-loop run` once an hour, or run
   `forge-loop run-forever` under systemd.

## Concepts

### Edge

A unit of work.  Created either by hand (`edges add`) or by an
*edge source* (a configured scanner).  An edge has:

- `title`, `description`, `target` (free-form: file path, kernel name,
  test id, anything that identifies *what* is broken)
- `cluster_key` — used for round-robin rotation; if you have many
  bugs of the same shape, group them with the same cluster_key so
  the dispatcher rotates *across* shapes rather than grinding one
- `severity` — `low | medium | high | blocker`; ordering hint
- `prompt_context` — JSON passed through to the prompt as evidence;
  use this to ground the LLM in tables, knob values, error logs,
  anything you don't want it to invent

### Edge state machine

```
open ──► investigating ──► resolved-pending-verify ──► resolved
                                  │
                                  ▼
                              wontfix
```

`open` and `investigating` are dispatchable.  `resolved-pending-verify`
sits until promoted by re-running the verifier (typically post-cycle).
`resolved` is terminal until you `edges reopen`.

### Cycle

One pass of: pre-cycle hooks → run all sources to file new edges →
build a cluster-rotated, severity-sorted, cooldown-honored dispatch
queue → for each (up to cap): build prompt, dispatch to LLM, run
verifier, commit-or-fail, prune resolved edges from the queue → run
post-cycle hooks → write a `cycles` row.

### Dispatcher

A `Dispatcher` is anything callable that takes a prompt and an
edge_id and returns a `DispatchResult`.  Built-in:

- `ShellDispatcher(template, ...)` — any shell command; prompt via
  stdin or via `{prompt_file}` substitution
- `ClaudeCliDispatcher()` — `claude --print` convenience constructor
- `CallableDispatcher(fn)` — for tests or in-process SDK calls

Pluggable via Python.  The JSON config supports `kind: shell` and
`kind: claude` directly.

### Verifier

A `Verifier` runs the validation gate and returns `VerifyResult`
with a pass/fail and (on failure) a diagnosis string.  The diagnosis
is automatically carried forward into the next attempt's prompt
(under "## Last attempt's diagnosis"), so the LLM doesn't repeat
mistakes.

Built-in:

- `ShellVerifier(commands)` — list of shell commands; all must exit 0;
  first-failing command's stderr/stdout tail becomes the diagnosis
- `CallableVerifier(fn)` — for in-process Python validators
- `NullVerifier()` — always green; use when the dispatch wrapper is
  itself responsible for committing only-when-green

## CI integration

forge-loop isn't usually part of CI itself — it's the meta-loop that
*generates* fixes which CI then runs against.  But two integration
points are useful:

**Cron-fire from CI**: schedule a CI job (GitHub Actions cron, GitLab
schedule) that runs `forge-loop run ./forge-loop.json` hourly.  The
cycle commits + pushes its own fixes; CI's regular pipeline then
verifies them on every push.

**Block-on-edges**: in your normal CI, fail the build if any edge is
`open` and `severity ≥ high`:

```bash
test -z "$(forge-loop edges list --db ./forge-loop.sqlite \
              --status open --severity high)" || exit 1
```

## Example: the openptxas autofix loop

The repo ships with `examples/openptxas-config.json` — the real config
shape used to drive an hourly autonomous-fix loop on a CUDA PTX→SASS
compiler.  Pre-cycle: pull a fresh remote bug-DB snapshot via SSH.
Sources: a single shell scanner that reads the snapshot and emits
JSON for new gpu_incorrect probe clusters.  Verifier: run pytest +
the regression-probe gate.  Dispatcher: `claude --print` with a
25-minute timeout.  Post-cycle: re-verify any pending-verify edges
against the latest local HEAD.

That config is intentionally generic — drop your own paths in and
the same loop runs against any compiler / repo with a bug-database
of probes.

## End-to-end test (no real LLM needed)

```bash
cd forge-loop
pip install -e ".[dev]"
pytest -x
```

The included test suite uses `CallableDispatcher` and
`CallableVerifier` fakes to exercise every code path in milliseconds —
no Claude session, no shell commands, no GPU.

## Limitations (v0.1)

- **Single-process.**  `Cycle.run_once()` runs one dispatch at a time
  in serial mode.  Parallel dispatch across hosts is v0.2.
- **JSON config only.**  YAML / TOML add dependencies; out for v0.1.
- **No web dashboard.**  `cycles list` and `edges list` are the
  introspection surface; pair with the markdown-digest pattern shown
  in `examples/`.
- **Bring your own LLM.**  forge-loop assumes the operator has
  configured `claude` (or whatever dispatcher CLI) on their PATH.
  No credentials handling here.

## Origin

Extracted from a 38-phase autonomous-fix session on `openptxas`, an
in-development CUDA PTX→SASS compiler.  The original session ran
`forge-workbench`'s `probe-watch` against a SQLite probe database
populated by GPU correctness sweeps; this is the generalized,
dependency-free package.

`gpudiff` is the companion tool from the same session — it's the
differential cubin tester whose verdicts feed forge-loop's edge
sources in the openptxas case.

## License

MIT.
