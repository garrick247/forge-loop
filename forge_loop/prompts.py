"""Default prompt builder.

Generates a structured markdown prompt for an edge that an LLM agent
can consume.  Designed to be replaced — `Cycle(prompt_builder=...)`
takes any callable `(edge: Edge, fixes: list, ctx: dict) -> str`.

The default prompt has six sections:

  1. Header               — edge_id, attempt count.
  2. Bug summary          — title, severity, status, target, cluster.
  3. Description / notes  — free-text from the edge row.
  4. Last diagnosis       — verify-failure carry-forward (only on retries).
  5. Similar past fixes   — pattern hints from the fix history.
  6. Validation           — the literal commands that must exit-0.
  7. Hard constraints     — instructed bounds on agent behavior.
  8. Done criterion.

The prompt mirrors `_cmd_probe_autofix`'s structure from forge-workbench
but is compiler-agnostic: the validation commands are passed in, not
hardcoded; the repository / file hints come from the config, not from
hard-coded openptxas paths.
"""
from __future__ import annotations

from typing import Iterable

from .db import Edge


def default_prompt_builder(
    edge: Edge,
    *,
    similar_fixes: Iterable[tuple] = (),
    validation_commands: Iterable[str] = (),
    repo_paths: Iterable[str] = (),
    likely_files: Iterable[str] = (),
    hard_constraints: Iterable[str] = (),
    done_criterion: str | None = None,
    project_intro: str | None = None,
) -> str:
    """Build a markdown prompt for `edge`.  All iterables are optional —
    pass what makes sense for your project.

    `similar_fixes`: iterable of (fixed_at, commit_sha, summary, target)
                     tuples.  Surfaces pattern hints to the agent.
    """
    L: list[str] = []
    L.append(f"# Autonomous fix request: edge_{edge.edge_id}")
    L.append("")
    if project_intro:
        L.append(project_intro.strip())
        L.append("")

    L.append("## Bug summary")
    L.append(f"- **Title**: {edge.title}")
    L.append(f"- **Category**: `{edge.category}` | severity: `{edge.severity}` "
             f"| status: `{edge.status}` | attempt: `{edge.attempts + 1}`")
    if edge.cluster_key:
        L.append(f"- **Cluster**: `{edge.cluster_key}`")
    if edge.target:
        L.append(f"- **Target**: `{edge.target}`")
    L.append("")

    if edge.description:
        L.append("## Description")
        L.append(edge.description.strip())
        L.append("")

    if edge.notes:
        L.append("## Notes")
        L.append(edge.notes.strip())
        L.append("")

    if edge.prompt_context:
        # Anything the edge filer wanted the agent to ground in:
        # denvdis-style ground truth, knob-table extracts, evidence URIs.
        L.append("## Evidence (ground truth — cite this, do not invent)")
        for k, v in edge.prompt_context.items():
            if isinstance(v, str) and "\n" in v:
                L.append(f"### {k}")
                L.append("```")
                L.append(v.strip())
                L.append("```")
            else:
                L.append(f"- **{k}**: `{v}`")
        L.append("")

    if edge.attempts > 0 and edge.last_diagnosis:
        L.append("## Last attempt's diagnosis")
        L.append("Your prior attempt did NOT pass the validation gate.  "
                 "The gate reported:")
        L.append("")
        L.append("```")
        L.append(edge.last_diagnosis.strip())
        L.append("```")
        L.append("")
        L.append("Take this into account.  Do not repeat the failed "
                 "approach without addressing the specific failure above.")
        L.append("")

    fixes_list = list(similar_fixes)
    if fixes_list:
        L.append("## Recent similar fixes (pattern hints)")
        for tup in fixes_list[:10]:
            # Tolerate (fixed_at, commit, summary) or (fixed_at, commit,
            # summary, target) shapes.
            ts = tup[0] if len(tup) > 0 else "?"
            sha = (tup[1] or "?")[:12] if len(tup) > 1 else "?"
            summary = tup[2] if len(tup) > 2 else "(no summary)"
            target = tup[3] if len(tup) > 3 else None
            tgt = f" target=`{target}`" if target else ""
            L.append(f"- `{ts}` `{sha}`{tgt}: {summary}")
        L.append("")

    repo_list = list(repo_paths)
    if repo_list:
        L.append("## Repository")
        for p in repo_list:
            L.append(f"- `{p}`")
        L.append("")

    files_list = list(likely_files)
    if files_list:
        L.append("## Likely files to investigate")
        for f in files_list:
            L.append(f"- `{f}`")
        L.append("")

    L.append("## Your task")
    L.append("1. Investigate.  Read the bug summary, evidence, and any "
             "relevant source files above.")
    L.append("2. Edit.  Make the minimum surgical change that addresses "
             "the bug.  Refactors are out of scope.")
    L.append("3. Validate.  Run every command listed below from a shell.  "
             "All MUST exit 0 before you commit.")
    L.append("")

    val_list = list(validation_commands)
    if val_list:
        L.append("## Validation gate")
        L.append("Run these commands; ALL must exit 0:")
        L.append("")
        L.append("```")
        for cmd in val_list:
            L.append(cmd)
        L.append("```")
        L.append("")

    constraints = list(hard_constraints)
    if constraints:
        L.append("## Hard constraints")
        for c in constraints:
            L.append(f"- {c}")
        L.append("")
    else:
        # Sensible defaults.
        L.append("## Hard constraints")
        L.append("- Do NOT modify the validation commands or any test "
                 "they depend on.")
        L.append("- Do NOT amend or rebase prior commits.")
        L.append("- Do NOT skip / disable hooks (no `--no-verify`, "
                 "no `--no-gpg-sign`).")
        L.append("- Keep the change minimal.  Surgical fixes age better "
                 "than refactors.")
        L.append("")

    L.append("## Done criterion")
    if done_criterion:
        L.append(done_criterion.strip())
    else:
        L.append("Every command in the validation gate exits 0, your fix "
                 "is committed, and the commit message describes the bug "
                 "and the fix.")
    L.append("")

    return "\n".join(L) + "\n"
