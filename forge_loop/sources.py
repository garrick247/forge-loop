"""Edge sources — pluggable filers that turn external signal into edges.

An `EdgeSource` is anything callable that returns a list of dicts
suitable for `EdgeDB.add_edge(**d)`.  The orchestrator runs all
configured sources at the start of each cycle and files anything new
into the DB before the dispatch step.

Built-ins:

  ShellEdgeSource(cmd)     — runs a shell command; expects JSON on
                             stdout (either a list of dicts, or a
                             single dict).  Useful for ad-hoc bug
                             scrapers — write a Python / bash one-liner
                             that emits JSON, point a ShellEdgeSource
                             at it, done.

  CallableEdgeSource(fn)   — wraps an in-process Python function.
                             Use when your filer wants direct DB or
                             SDK access without spawning a subprocess.

  StaticEdgeSource(items)  — yields a fixed list once.  Mostly useful
                             for tests and CI seed-data.

Each source is invoked with `(db: EdgeDB)` so it can dedupe against
the existing edge list before yielding new candidates.
"""
from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable, Protocol

from .db import EdgeDB


@dataclass
class SourceResult:
    """Outcome of running one source: how many candidates it yielded
    and how many actually made it past the dedup check."""
    name: str
    candidates: int = 0
    filed: int = 0
    error: str | None = None
    elapsed_s: float = 0.0


class EdgeSource(Protocol):
    name: str

    def fetch(self, db: EdgeDB) -> list[dict]: ...


def file_candidates(
    db: EdgeDB,
    candidates: Iterable[dict],
    *,
    dedup_by: tuple[str, ...] = ("title", "target"),
) -> int:
    """File `candidates` into `db`, skipping any that already exist
    according to `dedup_by` (a tuple of edge fields to compare).

    Default dedup: same (title, target) as an existing OPEN edge.
    Resolved edges don't block re-filing — if a bug regresses with the
    same title+target, we want it back in the queue.

    Returns the number of edges actually filed.
    """
    if not dedup_by:
        n = 0
        for cand in candidates:
            db.add_edge(**cand)
            n += 1
        return n

    # Pull existing OPEN edges keyed by the dedup tuple.
    existing = set()
    sql = "SELECT " + ", ".join(dedup_by) + " FROM edges WHERE status = 'open'"
    for row in db.query(sql):
        existing.add(tuple(row))

    n = 0
    for cand in candidates:
        key = tuple(cand.get(field) for field in dedup_by)
        if key in existing:
            continue
        db.add_edge(**cand)
        existing.add(key)
        n += 1
    return n


@dataclass
class ShellEdgeSource:
    """Run a shell command per cycle; parse stdout as JSON.

    Output formats accepted:
      - JSON array of dicts:  [{"title": "...", ...}, ...]
      - Single JSON dict:     {"title": "...", ...}
      - JSONL (one dict/line)

    Each dict must include at least `title`; everything else maps to
    `EdgeDB.add_edge` kwargs (description, target, cluster_key,
    severity, prompt_context, notes, category).  Unknown keys are
    silently dropped to keep the source robust against schema drift.
    """
    name: str
    cmd: str
    timeout: int = 600
    use_shell: bool = True
    dedup_by: tuple[str, ...] = ("title", "target")

    _ALLOWED = ("title", "category", "description", "target", "cluster_key",
                "severity", "prompt_context", "notes")

    def fetch(self, db: EdgeDB) -> list[dict]:
        try:
            proc = subprocess.run(
                self.cmd, shell=self.use_shell, capture_output=True,
                text=True, timeout=self.timeout)
        except subprocess.TimeoutExpired:
            raise RuntimeError(
                f"shell source {self.name!r} TIMEOUT after {self.timeout}s")
        if proc.returncode != 0:
            raise RuntimeError(
                f"shell source {self.name!r} exited {proc.returncode}: "
                f"{(proc.stderr or proc.stdout or '').strip()[:300]}")
        return self._parse_output(proc.stdout)

    def _parse_output(self, text: str) -> list[dict]:
        text = text.strip()
        if not text:
            return []
        candidates: list[dict] = []
        try:
            obj = json.loads(text)
            if isinstance(obj, dict):
                candidates = [obj]
            elif isinstance(obj, list):
                candidates = [d for d in obj if isinstance(d, dict)]
        except json.JSONDecodeError:
            # Try JSONL — one JSON object per line.
            for line in text.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    o = json.loads(line)
                    if isinstance(o, dict):
                        candidates.append(o)
                except json.JSONDecodeError:
                    continue
        # Drop unknown keys so add_edge doesn't reject them.
        return [
            {k: v for k, v in c.items() if k in self._ALLOWED}
            for c in candidates if c.get("title")
        ]


@dataclass
class CallableEdgeSource:
    """Wrap an in-process callable.  Signature: `fn(db) -> list[dict]`."""
    name: str
    fn: Callable
    dedup_by: tuple[str, ...] = ("title", "target")

    def fetch(self, db: EdgeDB) -> list[dict]:
        r = self.fn(db)
        if not r:
            return []
        if isinstance(r, dict):
            return [r]
        return [d for d in r if isinstance(d, dict) and d.get("title")]


@dataclass
class StaticEdgeSource:
    """A canned list of edges; emitted once on the first fetch.

    By default re-fetches return [] — the list has been "consumed".  Pass
    `repeat=True` to emit on every fetch (useful for testing dedup logic).
    """
    name: str
    items: list[dict]
    repeat: bool = False
    dedup_by: tuple[str, ...] = ("title", "target")
    _consumed: bool = field(default=False, init=False)

    def fetch(self, db: EdgeDB) -> list[dict]:
        if self._consumed and not self.repeat:
            return []
        self._consumed = True
        return list(self.items)


def run_sources(
    db: EdgeDB,
    sources: Iterable,
) -> tuple[int, list[SourceResult]]:
    """Run every source; file new edges; return (total_filed, per_source).

    Failures in one source don't abort the others.  Each source's
    failure is captured in its `SourceResult.error` for logging.
    """
    out: list[SourceResult] = []
    total_filed = 0
    for src in sources:
        r = SourceResult(name=getattr(src, "name", type(src).__name__))
        t0 = time.monotonic()
        try:
            cands = src.fetch(db) or []
            r.candidates = len(cands)
            dedup = getattr(src, "dedup_by", ("title", "target"))
            r.filed = file_candidates(db, cands, dedup_by=dedup)
            total_filed += r.filed
        except Exception as e:
            r.error = f"{type(e).__name__}: {str(e)[:300]}"
        r.elapsed_s = time.monotonic() - t0
        out.append(r)
    return total_filed, out
