"""SQLite-backed edge tracker for forge-loop.

An *edge* is a unit of work — typically a bug, regression, or
follow-up the LLM is being asked to fix.  Edges flow through a small
state machine:

    open ──► investigating ──► resolved-pending-verify ──► resolved
                  │                       │
                  ▼                       ▼
               wontfix                stuck-open  (pending -> open after timeout)

The DB is a single sqlite file in WAL mode.  No external dependencies.
A *fix* is a historical record of a successful resolution: commit
sha, summary, the edge it resolved.  The tracker carries forward
diagnoses and dispatch counts so a per-cycle orchestrator can pick
the next edge to work on with cluster rotation, severity ordering,
and per-edge cooldowns.

This module is the equivalent of forge-workbench's `probe.db.ProbeDB`,
generalized away from openptxas.  The probe-specific parts (PTX/cubin
content addressing, target_op / template_id columns, GPU correctness
oracles) are gone; what remains is the generic edge / fix / cycle
tracking that any LLM-driven loop needs.
"""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable


_SCHEMA = """
CREATE TABLE IF NOT EXISTS edges (
    edge_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    discovered_at      TEXT NOT NULL,
    category           TEXT NOT NULL,
    title              TEXT NOT NULL,
    description        TEXT,
    target             TEXT,
    cluster_key        TEXT,
    severity           TEXT DEFAULT 'medium',
    status             TEXT DEFAULT 'open',
    attempts           INTEGER DEFAULT 0,
    last_dispatched_at TEXT,
    last_diagnosis     TEXT,
    prompt_context     TEXT,
    notes              TEXT
);

CREATE INDEX IF NOT EXISTS ix_edges_status   ON edges(status);
CREATE INDEX IF NOT EXISTS ix_edges_cluster  ON edges(cluster_key);
CREATE INDEX IF NOT EXISTS ix_edges_severity ON edges(severity);

CREATE TABLE IF NOT EXISTS fixes (
    fix_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    fixed_at        TEXT NOT NULL,
    edge_id         INTEGER,
    cluster_key     TEXT,
    commit_sha      TEXT,
    summary         TEXT,
    target          TEXT,
    verified        INTEGER DEFAULT 0,
    verifying_run   TEXT,
    notes           TEXT,
    FOREIGN KEY (edge_id) REFERENCES edges(edge_id)
);

CREATE INDEX IF NOT EXISTS ix_fixes_cluster ON fixes(cluster_key);
CREATE INDEX IF NOT EXISTS ix_fixes_target  ON fixes(target);

CREATE TABLE IF NOT EXISTS cycles (
    cycle_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at          TEXT NOT NULL,
    finished_at         TEXT,
    edges_filed         INTEGER DEFAULT 0,
    edges_dispatched    INTEGER DEFAULT 0,
    edges_resolved      INTEGER DEFAULT 0,
    pre_cycle_rc        INTEGER,
    post_cycle_rc       INTEGER,
    notes               TEXT
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


VALID_STATUSES = (
    "open",
    "investigating",
    "resolved-pending-verify",
    "resolved",
    "wontfix",
)
VALID_SEVERITIES = ("low", "medium", "high", "blocker")


@dataclass
class Edge:
    """In-memory view of an edge row."""
    edge_id: int
    discovered_at: str
    category: str
    title: str
    description: str | None = None
    target: str | None = None
    cluster_key: str | None = None
    severity: str = "medium"
    status: str = "open"
    attempts: int = 0
    last_dispatched_at: str | None = None
    last_diagnosis: str | None = None
    prompt_context: dict | None = None
    notes: str | None = None

    @classmethod
    def from_row(cls, row: tuple) -> "Edge":
        (eid, discovered_at, category, title, description, target,
         cluster_key, severity, status, attempts, last_dispatched_at,
         last_diagnosis, prompt_context_json, notes) = row
        try:
            ctx = json.loads(prompt_context_json) if prompt_context_json else None
        except (json.JSONDecodeError, TypeError):
            ctx = None
        return cls(
            edge_id=eid, discovered_at=discovered_at, category=category,
            title=title, description=description, target=target,
            cluster_key=cluster_key, severity=severity or "medium",
            status=status or "open", attempts=attempts or 0,
            last_dispatched_at=last_dispatched_at, last_diagnosis=last_diagnosis,
            prompt_context=ctx, notes=notes)


_EDGE_COLS = (
    "edge_id, discovered_at, category, title, description, target, "
    "cluster_key, severity, status, attempts, last_dispatched_at, "
    "last_diagnosis, prompt_context, notes"
)


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


class EdgeDB:
    """SQLite-backed edge / fix / cycle tracker."""

    def __init__(self, path: str | Path):
        path = Path(path)
        if path.is_dir():
            path = path / "forge_loop.sqlite"
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

    # ---- edge writes ----

    def add_edge(
        self,
        *,
        title: str,
        category: str = "bug",
        description: str | None = None,
        target: str | None = None,
        cluster_key: str | None = None,
        severity: str = "medium",
        prompt_context: dict | None = None,
        notes: str | None = None,
    ) -> int:
        """File a new edge.  Returns edge_id."""
        if severity not in VALID_SEVERITIES:
            raise ValueError(f"severity must be one of {VALID_SEVERITIES}")
        ctx = json.dumps(prompt_context) if prompt_context else None
        cur = self.conn.execute(
            "INSERT INTO edges (discovered_at, category, title, description, "
            "target, cluster_key, severity, prompt_context, notes) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (_now_iso(), category, title, description, target,
             cluster_key, severity, ctx, notes))
        self.conn.commit()
        return cur.lastrowid

    def update_edge(self, edge_id: int, **fields: Any) -> None:
        """Update arbitrary edge columns.  Validates status / severity."""
        if not fields:
            return
        if "status" in fields and fields["status"] not in VALID_STATUSES:
            raise ValueError(f"status must be one of {VALID_STATUSES}")
        if "severity" in fields and fields["severity"] not in VALID_SEVERITIES:
            raise ValueError(f"severity must be one of {VALID_SEVERITIES}")
        if "prompt_context" in fields and not isinstance(
                fields["prompt_context"], (str, type(None))):
            fields["prompt_context"] = json.dumps(fields["prompt_context"])
        sets = ", ".join(f"{k} = ?" for k in fields)
        params = list(fields.values()) + [edge_id]
        self.conn.execute(f"UPDATE edges SET {sets} WHERE edge_id = ?", params)
        self.conn.commit()

    def append_notes(self, edge_id: int, text: str) -> None:
        """Append a timestamped line to the notes column."""
        self.conn.execute(
            "UPDATE edges SET notes = COALESCE(notes, '') || ? WHERE edge_id = ?",
            (f"\n[{_now_iso()}] {text}", edge_id))
        self.conn.commit()

    def stamp_dispatched(self, edge_id: int) -> None:
        """Mark an edge as just-dispatched (bumps attempts, sets timestamp)."""
        self.conn.execute(
            "UPDATE edges SET last_dispatched_at = ?, attempts = attempts + 1 "
            "WHERE edge_id = ?", (_now_iso(), edge_id))
        self.conn.commit()

    def record_diagnosis(self, edge_id: int, diagnosis: str) -> None:
        """Carry a verify-failure diagnosis forward to the next attempt."""
        self.conn.execute(
            "UPDATE edges SET last_diagnosis = ? WHERE edge_id = ?",
            (diagnosis, edge_id))
        self.conn.commit()

    # ---- edge reads ----

    def get_edge(self, edge_id: int) -> Edge | None:
        cur = self.conn.execute(
            f"SELECT {_EDGE_COLS} FROM edges WHERE edge_id = ?", (edge_id,))
        row = cur.fetchone()
        return Edge.from_row(row) if row else None

    def list_edges(
        self,
        *,
        status: str | None = None,
        category: str | None = None,
        cluster_key: str | None = None,
        limit: int | None = None,
    ) -> list[Edge]:
        sql = f"SELECT {_EDGE_COLS} FROM edges WHERE 1=1"
        params: list = []
        if status:
            sql += " AND status = ?"
            params.append(status)
        if category:
            sql += " AND category = ?"
            params.append(category)
        if cluster_key:
            sql += " AND cluster_key = ?"
            params.append(cluster_key)
        sql += (" ORDER BY CASE severity "
                "  WHEN 'blocker' THEN 0 WHEN 'high' THEN 1 "
                "  WHEN 'medium' THEN 2 ELSE 3 END, edge_id")
        if limit:
            sql += f" LIMIT {int(limit)}"
        return [Edge.from_row(r) for r in self.conn.execute(sql, params)]

    def dispatchable_edges(
        self,
        *,
        cooldown_hours: float = 24.0,
        statuses: tuple[str, ...] = ("open", "investigating"),
    ) -> list[Edge]:
        """Return edges that are eligible for dispatch right now.

        Eligible = status in `statuses` AND
            (last_dispatched_at IS NULL OR < now - cooldown_hours).
        Sorted by severity (blocker first), then edge_id.
        """
        if not statuses:
            return []
        placeholders = ",".join("?" for _ in statuses)
        cutoff = f"-{float(cooldown_hours)} hours"
        sql = (
            f"SELECT {_EDGE_COLS} FROM edges "
            f"WHERE status IN ({placeholders}) "
            f"  AND (last_dispatched_at IS NULL "
            f"       OR last_dispatched_at < datetime('now', ?)) "
            f"ORDER BY CASE severity "
            f"  WHEN 'blocker' THEN 0 WHEN 'high' THEN 1 "
            f"  WHEN 'medium' THEN 2 ELSE 3 END, edge_id"
        )
        params = list(statuses) + [cutoff]
        return [Edge.from_row(r) for r in self.conn.execute(sql, params)]

    def edge_count(self, status: str | None = None) -> int:
        if status:
            return self.conn.execute(
                "SELECT COUNT(*) FROM edges WHERE status = ?", (status,)
            ).fetchone()[0]
        return self.conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]

    # ---- fix history ----

    def record_resolution(
        self,
        *,
        edge_id: int,
        commit_sha: str | None = None,
        summary: str | None = None,
        cluster_key: str | None = None,
        target: str | None = None,
        notes: str | None = None,
    ) -> int:
        """Mark `edge_id` as resolved-pending-verify and write a fix row.

        Returns fix_id.  Verification (status -> 'resolved') is the
        responsibility of `mark_resolution_verified`.
        """
        # Pull edge metadata to enrich the fix row when caller didn't pass it.
        e = self.get_edge(edge_id)
        if e is None:
            raise ValueError(f"edge_{edge_id} not found")
        cluster_key = cluster_key or e.cluster_key
        target = target or e.target
        ts = _now_iso()
        cur = self.conn.execute(
            "INSERT INTO fixes (fixed_at, edge_id, cluster_key, commit_sha, "
            "summary, target, notes) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (ts, edge_id, cluster_key, commit_sha, summary, target, notes))
        self.update_edge(edge_id, status="resolved-pending-verify")
        self.append_notes(
            edge_id,
            f"resolution recorded @ {(commit_sha or '?')[:8]}: {summary or ''}")
        return cur.lastrowid

    def mark_resolution_verified(
        self,
        edge_id: int,
        verifying_run: str | None = None,
    ) -> None:
        """Promote an edge from resolved-pending-verify to resolved."""
        self.update_edge(edge_id, status="resolved")
        self.append_notes(
            edge_id,
            f"verified resolved (run={verifying_run or '-'})")
        self.conn.execute(
            "UPDATE fixes SET verified = 1, verifying_run = ? "
            "WHERE fix_id = (SELECT fix_id FROM fixes WHERE edge_id = ? "
            "                ORDER BY fixed_at DESC LIMIT 1)",
            (verifying_run, edge_id))
        self.conn.commit()

    def reopen_edge(self, edge_id: int, reason: str) -> None:
        """Reopen a resolved-pending-verify (or resolved) edge."""
        self.update_edge(edge_id, status="open")
        self.append_notes(edge_id, f"reopened: {reason}")

    def list_fixes(
        self,
        *,
        cluster_key: str | None = None,
        target: str | None = None,
        limit: int = 50,
    ) -> list[tuple]:
        sql = ("SELECT fix_id, fixed_at, edge_id, cluster_key, commit_sha, "
               "summary, target, verified FROM fixes WHERE 1=1")
        params: list = []
        if cluster_key:
            sql += " AND cluster_key = ?"
            params.append(cluster_key)
        if target:
            sql += " AND target = ?"
            params.append(target)
        sql += f" ORDER BY fixed_at DESC LIMIT {int(limit)}"
        return list(self.conn.execute(sql, params))

    def search_fixes(self, query: str, limit: int = 20) -> list[tuple]:
        """Substring search over summary / target / cluster_key."""
        q = f"%{query}%"
        return list(self.conn.execute(
            "SELECT fix_id, fixed_at, edge_id, cluster_key, commit_sha, "
            "summary, target, verified FROM fixes "
            "WHERE summary LIKE ? OR target LIKE ? OR cluster_key LIKE ? "
            f"ORDER BY fixed_at DESC LIMIT {int(limit)}",
            (q, q, q)))

    # ---- cycle rows ----

    def open_cycle(self) -> int:
        cur = self.conn.execute(
            "INSERT INTO cycles (started_at) VALUES (?)", (_now_iso(),))
        self.conn.commit()
        return cur.lastrowid

    def close_cycle(
        self,
        cycle_id: int,
        *,
        edges_filed: int = 0,
        edges_dispatched: int = 0,
        edges_resolved: int = 0,
        pre_cycle_rc: int | None = None,
        post_cycle_rc: int | None = None,
        notes: str | None = None,
    ) -> None:
        self.conn.execute(
            "UPDATE cycles SET finished_at = ?, edges_filed = ?, "
            "edges_dispatched = ?, edges_resolved = ?, pre_cycle_rc = ?, "
            "post_cycle_rc = ?, notes = ? WHERE cycle_id = ?",
            (_now_iso(), edges_filed, edges_dispatched, edges_resolved,
             pre_cycle_rc, post_cycle_rc, notes, cycle_id))
        self.conn.commit()

    def list_cycles(self, limit: int = 20) -> list[tuple]:
        return list(self.conn.execute(
            "SELECT cycle_id, started_at, finished_at, edges_filed, "
            "edges_dispatched, edges_resolved, pre_cycle_rc, post_cycle_rc, "
            "notes FROM cycles ORDER BY cycle_id DESC LIMIT ?", (limit,)))

    # ---- meta ----

    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute(
            "SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value))
        self.conn.commit()

    # ---- raw ----

    def query(self, sql: str, params: tuple = ()) -> list[tuple]:
        return list(self.conn.execute(sql, params))

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "EdgeDB":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def cluster_rotation(edges: Iterable[Edge]) -> list[int]:
    """Round-robin across `cluster_key` so consecutive picks come from
    different clusters.  Solo edges (no cluster_key) each get their own
    cluster.  Within a cluster, severity order from the input is
    preserved.

    Returns a list of edge_ids in dispatch order.
    """
    from collections import OrderedDict, deque
    buckets: "OrderedDict[str, list[int]]" = OrderedDict()
    for e in edges:
        key = e.cluster_key or f"_solo_{e.edge_id}"
        buckets.setdefault(key, []).append(e.edge_id)
    keys = deque(buckets.keys())
    out: list[int] = []
    while keys:
        key = keys.popleft()
        bucket = buckets[key]
        if not bucket:
            continue
        out.append(bucket.pop(0))
        if bucket:
            keys.append(key)
    return out
