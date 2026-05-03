"""Per-cycle orchestrator.

A `Cycle` runs:

  1. pre-cycle hooks   — refresh signal sources, pull data, etc.
  2. edge sources      — file any new edges into the DB.
  3. dispatch loop     — pick the next dispatchable edge (cluster-rotated,
                         severity-sorted, cooldown-honored), build a
                         prompt, dispatch to the LLM, run the verifier
                         between dispatches, prune resolved edges.
  4. post-cycle hooks  — re-verify resolved-pending-verify edges, etc.

A single `Cycle.run_once()` call returns a `CycleResult` summary.
`Cycle.run_forever(interval_s)` keeps invoking until interrupted.

All side effects flow through the injected `EdgeDB`, `Dispatcher`,
and `Verifier`.  Nothing is hardcoded — even the prompt builder is
pluggable.  The hardcoded openptxas / forge / claude pieces all live
outside this module, in user config.
"""
from __future__ import annotations

import signal
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from .db import Edge, EdgeDB, cluster_rotation
from .dispatch import Dispatcher, DispatchResult
from .prompts import default_prompt_builder
from .sources import SourceResult, run_sources
from .verify import NullVerifier, VerifyResult, Verifier


@dataclass
class DispatchOutcome:
    edge_id: int
    rc: int
    elapsed_s: float
    transcript_path: str | None = None
    error: str | None = None
    verify_passed_after: bool | None = None


@dataclass
class CycleResult:
    cycle_id: int
    started_at: str
    finished_at: str = ""
    pre_cycle_rcs: list = field(default_factory=list)
    sources: list = field(default_factory=list)  # list[SourceResult]
    edges_filed: int = 0
    dispatched: list = field(default_factory=list)  # list[DispatchOutcome]
    post_cycle_rcs: list = field(default_factory=list)
    edges_resolved: int = 0
    note: str | None = None

    @property
    def n_dispatched(self) -> int:
        return len(self.dispatched)

    @property
    def n_dispatched_ok(self) -> int:
        return sum(1 for d in self.dispatched if d.rc == 0 and not d.error)


def _run_shell(cmd: str, timeout: int) -> tuple[int, str]:
    """Run a shell command, return (rc, tail).  Tail is last 500 chars
    of stdout/stderr concatenated.  -1 rc on TimeoutExpired."""
    try:
        proc = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        tail = (proc.stderr or proc.stdout or "")[-500:]
        return (proc.returncode, tail)
    except subprocess.TimeoutExpired:
        return (-1, f"TIMEOUT after {timeout}s")
    except Exception as e:
        return (-2, f"{type(e).__name__}: {str(e)[:200]}")


@dataclass
class Cycle:
    """One end-to-end cycle of the forge-loop.

    Required:
      db          — an EdgeDB.
      dispatcher  — a Dispatcher (LLM dispatch).

    Optional:
      verifier    — Verifier (default: NullVerifier — no validation gate).
      sources     — list of EdgeSources to run at cycle start.
      pre_cycle   — list of shell commands to run before sources.
      post_cycle  — list of shell commands to run after dispatch.
      prompt_builder — `(edge, similar_fixes, **kwargs) -> str`.  Default
                       uses `default_prompt_builder`.
      prompt_kwargs  — extra kwargs forwarded to prompt_builder.
                       Useful for repo paths / hard constraints / hints.
      cooldown_hours          — per-edge dispatch cooldown.
      max_dispatches_per_cycle — cap LLM sessions per cycle (None = no cap).
      verify_between_dispatches — if True, run verify after each dispatch
                                  and prune resolved edges from the queue.
                                  Default True.
      pre_cycle_timeout / post_cycle_timeout — per-command timeouts.
      log                     — callable(str) for per-cycle logging.
                                Default: print().
    """
    db: EdgeDB
    dispatcher: Dispatcher
    verifier: Verifier = field(default_factory=NullVerifier)
    sources: list = field(default_factory=list)
    pre_cycle: list = field(default_factory=list)
    post_cycle: list = field(default_factory=list)
    prompt_builder: Callable | None = None
    prompt_kwargs: dict = field(default_factory=dict)
    cooldown_hours: float = 24.0
    max_dispatches_per_cycle: int | None = 4
    verify_between_dispatches: bool = True
    pre_cycle_timeout: int = 600
    post_cycle_timeout: int = 600
    log: Callable[[str], None] | None = None

    def __post_init__(self):
        if self.prompt_builder is None:
            self.prompt_builder = default_prompt_builder
        if self.log is None:
            self.log = lambda msg: print(msg, flush=True)

    # ---- helpers ----

    def _emit(self, msg: str) -> None:
        self.log(msg)

    def _build_prompt(self, edge: Edge) -> str:
        # Pull a few similar fixes to surface as pattern hints.
        similar = []
        if edge.cluster_key:
            similar.extend(self.db.list_fixes(
                cluster_key=edge.cluster_key, limit=5))
        if edge.target:
            for f in self.db.list_fixes(target=edge.target, limit=5):
                if f not in similar:
                    similar.append(f)
        # Drop the SQLite columns we don't surface in the prompt:
        # convert (fix_id, fixed_at, edge_id, cluster_key, commit_sha,
        #          summary, target, verified) -> (fixed_at, commit_sha,
        #          summary, target).
        slim = [(row[1], row[4], row[5], row[6]) for row in similar]
        return self.prompt_builder(
            edge,
            similar_fixes=slim,
            **self.prompt_kwargs,
        )

    def _next_dispatchable(self) -> list[Edge]:
        """Read the dispatchable queue from the DB, applying the cooldown."""
        return self.db.dispatchable_edges(cooldown_hours=self.cooldown_hours)

    def _pick_queue(self, edges: list[Edge]) -> list[int]:
        """Cluster-rotated, severity-preserving dispatch order."""
        return cluster_rotation(edges)

    def _check_still_dispatchable(self, edge_ids: list[int]) -> set[int]:
        """Return the subset of `edge_ids` whose status is still in
        ('open', 'investigating') — i.e. not resolved by a side-effect
        of an earlier dispatch in this cycle."""
        if not edge_ids:
            return set()
        placeholders = ",".join("?" for _ in edge_ids)
        rows = self.db.query(
            f"SELECT edge_id FROM edges WHERE edge_id IN ({placeholders}) "
            f"AND status IN ('open', 'investigating')", tuple(edge_ids))
        return {r[0] for r in rows}

    # ---- single cycle ----

    def run_once(self) -> CycleResult:
        cycle_id = self.db.open_cycle()
        ts_start = time.strftime("%Y-%m-%dT%H:%M:%S")
        result = CycleResult(cycle_id=cycle_id, started_at=ts_start)
        self._emit(f"[forge-loop] cycle #{cycle_id} @ {ts_start}")

        # --- 1. pre-cycle hooks ---
        for cmd in self.pre_cycle:
            rc, tail = _run_shell(cmd, self.pre_cycle_timeout)
            result.pre_cycle_rcs.append((cmd, rc))
            marker = "ok" if rc == 0 else f"FAIL({rc})"
            self._emit(f"  pre-cycle: {marker}  {cmd[:80]}")
            if rc != 0:
                self._emit(f"    tail: {tail.strip()[:200]}")

        # --- 2. edge sources ---
        if self.sources:
            filed, source_results = run_sources(self.db, self.sources)
            result.edges_filed = filed
            result.sources = source_results
            for sr in source_results:
                if sr.error:
                    self._emit(f"  source[{sr.name}] ERROR: {sr.error}")
                else:
                    self._emit(f"  source[{sr.name}]: filed {sr.filed}/"
                               f"{sr.candidates} candidates "
                               f"({sr.elapsed_s:.1f}s)")

        # --- 3. dispatch loop ---
        eligible = self._next_dispatchable()
        if not eligible:
            self._emit("  no dispatchable edges this cycle")
        else:
            self._emit(f"  {len(eligible)} dispatchable edge(s); "
                       f"cooldown={self.cooldown_hours}h, "
                       f"cap={self.max_dispatches_per_cycle}")

        queue = self._pick_queue(eligible)
        edges_by_id = {e.edge_id: e for e in eligible}
        n_dispatched = 0
        cap = self.max_dispatches_per_cycle
        while queue:
            if cap is not None and n_dispatched >= cap:
                self._emit(f"  cap {cap} reached; "
                           f"{len(queue)} edge(s) deferred to next cycle")
                break

            # Verify between dispatches.  Re-query edges that are still
            # open — earlier fixes may have resolved them as a side effect.
            if n_dispatched > 0 and self.verify_between_dispatches:
                vr = self.verifier.verify(edge_id=None)
                if vr.ok:
                    self._emit(f"    verify: GREEN ({vr.elapsed_s:.1f}s)")
                else:
                    cmd = vr.failing_command or '?'
                    self._emit(f"    verify: RED ({vr.elapsed_s:.1f}s) "
                               f"-- {cmd[:60]}")

            still_open = self._check_still_dispatchable(queue)
            if not still_open:
                self._emit(f"    queue drained after {n_dispatched} "
                           f"dispatch(es)")
                break
            queue = [e for e in queue if e in still_open]
            if not queue:
                break

            eid = queue.pop(0)
            edge = edges_by_id.get(eid) or self.db.get_edge(eid)
            if edge is None:
                self._emit(f"    edge_{eid} vanished from DB; skipping")
                continue
            prompt = self._build_prompt(edge)
            self.db.stamp_dispatched(eid)
            try:
                dr = self.dispatcher.dispatch(prompt, eid)
            except Exception as e:
                dr = DispatchResult(
                    edge_id=eid, rc=-3,
                    error=f"dispatcher raised {type(e).__name__}: {e}",
                    elapsed_s=0.0)
            n_dispatched += 1
            marker = "OK" if dr.ok else f"FAIL(rc={dr.rc})"
            tail = (dr.error or dr.transcript or "").strip().splitlines()
            last = tail[-1][:120] if tail else ""
            self._emit(f"    dispatch edge_{eid}: {marker}  "
                       f"({dr.elapsed_s:.0f}s)  {last}")

            # If verifier passes after this dispatch, mark the edge
            # resolved-pending-verify (the agent's commit may or may
            # not have already done so — be idempotent).
            verify_passed = None
            post_vr: VerifyResult | None = None
            if self.verify_between_dispatches:
                post_vr = self.verifier.verify(edge_id=eid)
                verify_passed = post_vr.ok
                # Only flip status if the agent didn't already.
                cur_status = (self.db.get_edge(eid).status if
                              self.db.get_edge(eid) else None)
                if post_vr.ok and cur_status in ("open", "investigating"):
                    self.db.record_resolution(
                        edge_id=eid,
                        commit_sha=None,
                        summary=f"verify-green after dispatch (cycle {cycle_id})",
                        notes=f"verifier ran after dispatch; rc={dr.rc}")
                    result.edges_resolved += 1
                    self._emit(f"      verify GREEN — edge_{eid} marked "
                               f"resolved-pending-verify")
                elif not post_vr.ok:
                    diag = post_vr.diagnosis or "(no diagnosis)"
                    self.db.record_diagnosis(eid, diag)

            result.dispatched.append(DispatchOutcome(
                edge_id=eid, rc=dr.rc, elapsed_s=dr.elapsed_s,
                transcript_path=dr.transcript_path, error=dr.error,
                verify_passed_after=verify_passed))

        # --- 4. post-cycle hooks ---
        for cmd in self.post_cycle:
            rc, tail = _run_shell(cmd, self.post_cycle_timeout)
            result.post_cycle_rcs.append((cmd, rc))
            marker = "ok" if rc == 0 else f"FAIL({rc})"
            self._emit(f"  post-cycle: {marker}  {cmd[:80]}")
            if rc != 0:
                self._emit(f"    tail: {tail.strip()[:200]}")

        ts_end = time.strftime("%Y-%m-%dT%H:%M:%S")
        result.finished_at = ts_end
        self.db.close_cycle(
            cycle_id,
            edges_filed=result.edges_filed,
            edges_dispatched=result.n_dispatched,
            edges_resolved=result.edges_resolved,
            pre_cycle_rc=(0 if all(rc == 0 for _, rc in result.pre_cycle_rcs)
                          else 1),
            post_cycle_rc=(0 if all(rc == 0 for _, rc in result.post_cycle_rcs)
                           else 1),
        )
        self._emit(f"  cycle #{cycle_id} done @ {ts_end}: "
                   f"filed={result.edges_filed} "
                   f"dispatched={result.n_dispatched} "
                   f"resolved={result.edges_resolved}")
        return result

    # ---- forever ----

    def run_forever(
        self,
        interval_s: int,
        *,
        max_cycles: int | None = None,
    ) -> list[CycleResult]:
        """Run cycles in a loop, sleeping `interval_s` between iterations.

        Honors SIGINT (Ctrl-C) by finishing the current cycle and exiting
        cleanly.  `max_cycles` caps the run for testing or scheduled
        bounded loops.
        """
        results: list[CycleResult] = []
        stop = {"flag": False}

        def _handler(signum, frame):
            stop["flag"] = True
            self._emit("[forge-loop] SIGINT received; exiting after current cycle")

        prev = signal.getsignal(signal.SIGINT)
        try:
            signal.signal(signal.SIGINT, _handler)
        except (ValueError, OSError):
            # signal.signal only works in main thread
            pass

        try:
            while True:
                results.append(self.run_once())
                if stop["flag"]:
                    break
                if max_cycles is not None and len(results) >= max_cycles:
                    self._emit(f"[forge-loop] reached max_cycles={max_cycles}; "
                               f"stopping")
                    break
                self._emit(f"[forge-loop] sleeping {interval_s}s...")
                # Sleep in small chunks so SIGINT lands quickly.
                slept = 0
                while slept < interval_s and not stop["flag"]:
                    time.sleep(min(5, interval_s - slept))
                    slept += 5
                if stop["flag"]:
                    break
        finally:
            try:
                signal.signal(signal.SIGINT, prev)
            except (ValueError, OSError):
                pass
        return results
