"""Validation gate runners.

A `Verifier` runs the validation gate after a dispatch.  Returns
`VerifyResult` describing whether all gates passed; if not, the
diagnosis is captured for carry-forward into the next attempt's prompt.

  ShellVerifier(commands)   — runs a list of shell commands;
                              all must exit 0 for a green verdict.
                              On the first failure, captures the
                              command's stderr/stdout tail as diagnosis.

  CallableVerifier(fn)      — wraps a Python callable.

The contract: a Verifier should be CHEAP enough to run between
dispatches.  The point of running verify between sequential dispatches
is to harvest "this fix happened to also resolve edge_X" wins for
free, instead of paying for a redundant LLM session.

What the Verifier cannot decide is which *edges* a green verdict
resolves — that's the orchestrator's job.  Verifier just answers
"did the validation gate pass right now?".
"""
from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass, field
from typing import Callable, Protocol


@dataclass
class VerifyResult:
    ok: bool
    diagnosis: str = ""
    elapsed_s: float = 0.0
    failing_command: str | None = None
    failing_rc: int | None = None
    transcripts: list = field(default_factory=list)  # list of (cmd, rc, tail)

    def __bool__(self) -> bool:
        return self.ok


class Verifier(Protocol):
    def verify(self, edge_id: int | None = None) -> VerifyResult: ...


@dataclass
class ShellVerifier:
    """Run a list of shell commands.  All must exit 0.  Stops on the
    first failure and reports it as the diagnosis.
    """
    commands: list[str]
    timeout_per_command: int = 600
    use_shell: bool = True
    diagnosis_tail_chars: int = 1500

    def verify(self, edge_id: int | None = None) -> VerifyResult:
        if not self.commands:
            # No gates configured = always green; useful for
            # dispatch-only loops with no validation.
            return VerifyResult(ok=True)

        result = VerifyResult(ok=True)
        t0 = time.monotonic()
        for cmd in self.commands:
            cmd_str = cmd.replace("{eid}", str(edge_id)) if edge_id is not None else cmd
            try:
                proc = subprocess.run(
                    cmd_str,
                    shell=self.use_shell,
                    capture_output=True,
                    text=True,
                    timeout=self.timeout_per_command,
                )
                tail = (proc.stderr or proc.stdout or "")[-self.diagnosis_tail_chars:]
                result.transcripts.append((cmd_str, proc.returncode, tail))
                if proc.returncode != 0:
                    result.ok = False
                    result.failing_command = cmd_str
                    result.failing_rc = proc.returncode
                    result.diagnosis = (
                        f"verify command failed: `{cmd_str}` "
                        f"(exit {proc.returncode})\n--- output tail ---\n{tail}")
                    break
            except subprocess.TimeoutExpired:
                result.ok = False
                result.failing_command = cmd_str
                result.failing_rc = -1
                result.diagnosis = (
                    f"verify command TIMEOUT after {self.timeout_per_command}s: "
                    f"`{cmd_str}`")
                result.transcripts.append((cmd_str, -1, "TIMEOUT"))
                break
            except FileNotFoundError as e:
                result.ok = False
                result.failing_command = cmd_str
                result.failing_rc = -2
                result.diagnosis = f"verify command not found: {e}"
                result.transcripts.append((cmd_str, -2, str(e)))
                break
            except Exception as e:
                result.ok = False
                result.failing_command = cmd_str
                result.failing_rc = -3
                result.diagnosis = (
                    f"verify command raised {type(e).__name__}: {str(e)[:300]}")
                result.transcripts.append((cmd_str, -3, str(e)))
                break

        result.elapsed_s = time.monotonic() - t0
        return result


@dataclass
class CallableVerifier:
    """Wrap a Python callable.  The callable signature is:

        fn(edge_id: int | None) -> bool | (bool, diagnosis_str)
    """
    fn: Callable

    def verify(self, edge_id: int | None = None) -> VerifyResult:
        t0 = time.monotonic()
        try:
            r = self.fn(edge_id)
            elapsed = time.monotonic() - t0
            if isinstance(r, tuple) and len(r) == 2:
                ok, diag = bool(r[0]), str(r[1])
            else:
                ok, diag = bool(r), ("" if r else "verifier returned False")
            return VerifyResult(ok=ok, diagnosis=diag, elapsed_s=elapsed)
        except Exception as e:
            return VerifyResult(
                ok=False,
                diagnosis=f"verifier raised {type(e).__name__}: {str(e)[:300]}",
                elapsed_s=time.monotonic() - t0)


@dataclass
class NullVerifier:
    """Always-green verifier — equivalent to `ShellVerifier([])`.
    Used when the LLM dispatch itself is responsible for committing
    only-when-green (i.e. the gate lives inside the dispatch wrapper)."""

    def verify(self, edge_id: int | None = None) -> VerifyResult:
        return VerifyResult(ok=True)
