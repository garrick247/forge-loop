"""LLM dispatch — pluggable.  Default: pipe a prompt to a CLI on stdin.

A `Dispatcher` is anything callable that takes a prompt + edge_id and
returns a `DispatchResult`.  Concrete implementations:

  ShellDispatcher        — runs a shell command template (bash / cmd.exe);
                           the prompt is written to a temp file and either
                           passed via stdin or substituted into the template
                           via `{prompt}` / `{prompt_file}`.

  ClaudeCliDispatcher    — convenience wrapper around `claude --print`.
                           Equivalent to:
                             ShellDispatcher("claude --print", stdin_prompt=True)

  CallableDispatcher     — wraps an arbitrary Python callable.  Useful for
                           tests, for direct SDK calls (anthropic-python /
                           openai), or for in-process dispatching.

All dispatchers honor a per-call timeout.  A timeout returns a result
with rc=-1 and `error="TIMEOUT after Ns"`.  Callers should treat any
non-zero rc as a dispatch failure but still trust the verify gate as
the source of truth — a "successful" agent transcript that didn't
land a passing fix is still a failure.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol


@dataclass
class DispatchResult:
    edge_id: int
    rc: int
    transcript: str = ""
    error: str | None = None
    elapsed_s: float = 0.0
    prompt_path: str | None = None
    transcript_path: str | None = None

    @property
    def ok(self) -> bool:
        return self.rc == 0 and self.error is None


class Dispatcher(Protocol):
    def dispatch(self, prompt: str, edge_id: int) -> DispatchResult: ...


@dataclass
class ShellDispatcher:
    """Run a shell command per dispatch.  The command can consume the
    prompt three ways (mutually exclusive):

      stdin_prompt=True   — prompt is piped to the command's stdin
                            (default; mirrors `claude --print < prompt`).
      "{prompt_file}" in template — substitute the path of a temp file
                                    containing the prompt.
      "{prompt}" in template      — substitute the prompt string in-line
                                    (only safe for very short prompts;
                                    you usually want `{prompt_file}`).

    `{eid}` in the template is replaced with the edge_id.
    """
    template: str
    timeout: int = 1500
    stdin_prompt: bool = True
    prompt_dir: str | None = None  # where to stash prompt+transcript files
    extra_env: dict = field(default_factory=dict)
    use_shell: bool = True  # run via shell=True so cmd templates work

    def dispatch(self, prompt: str, edge_id: int) -> DispatchResult:
        prompt_dir = Path(self.prompt_dir or tempfile.gettempdir())
        prompt_dir.mkdir(parents=True, exist_ok=True)
        ts = int(time.time())
        prompt_path = prompt_dir / f"forge_loop_edge_{edge_id}_{ts}.md"
        transcript_path = prompt_path.with_suffix(prompt_path.suffix + ".transcript")
        prompt_path.write_text(prompt, encoding="utf-8")

        cmd = self.template
        if "{eid}" in cmd:
            cmd = cmd.replace("{eid}", str(edge_id))
        else:
            # Append edge_id only if no placeholder was supplied AND the
            # template doesn't expect the prompt body (a stdin-prompt
            # template with no {eid} probably doesn't want extra args).
            if not self.stdin_prompt and "{prompt" not in cmd:
                cmd = f"{cmd} {edge_id}"
        if "{prompt_file}" in cmd:
            cmd = cmd.replace("{prompt_file}", str(prompt_path))
        if "{prompt}" in cmd:
            # Inline substitution; quote to survive shell parsing.
            import shlex as _sh
            cmd = cmd.replace("{prompt}", _sh.quote(prompt))

        env = os.environ.copy()
        env.update(self.extra_env)

        stdin_data = prompt if self.stdin_prompt else None

        t0 = time.monotonic()
        try:
            proc = subprocess.run(
                cmd,
                shell=self.use_shell,
                input=stdin_data,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                env=env,
            )
            elapsed = time.monotonic() - t0
            transcript = (proc.stdout or "") + (
                f"\n--- stderr ---\n{proc.stderr}" if proc.stderr else "")
            transcript_path.write_text(transcript, encoding="utf-8")
            return DispatchResult(
                edge_id=edge_id,
                rc=proc.returncode,
                transcript=transcript,
                elapsed_s=elapsed,
                prompt_path=str(prompt_path),
                transcript_path=str(transcript_path),
            )
        except subprocess.TimeoutExpired as e:
            elapsed = time.monotonic() - t0
            partial = ""
            if e.stdout:
                partial = (e.stdout if isinstance(e.stdout, str)
                           else e.stdout.decode("utf-8", "replace"))
            transcript_path.write_text(
                partial + f"\n--- TIMEOUT after {self.timeout}s ---\n",
                encoding="utf-8")
            return DispatchResult(
                edge_id=edge_id,
                rc=-1,
                transcript=partial,
                error=f"TIMEOUT after {self.timeout}s",
                elapsed_s=elapsed,
                prompt_path=str(prompt_path),
                transcript_path=str(transcript_path),
            )
        except FileNotFoundError as e:
            return DispatchResult(
                edge_id=edge_id, rc=-2,
                error=f"command not found: {e}",
                elapsed_s=time.monotonic() - t0,
                prompt_path=str(prompt_path))
        except Exception as e:
            return DispatchResult(
                edge_id=edge_id, rc=-3,
                error=f"{type(e).__name__}: {str(e)[:200]}",
                elapsed_s=time.monotonic() - t0,
                prompt_path=str(prompt_path))


def ClaudeCliDispatcher(
    claude_bin: str = "claude",
    timeout: int = 1500,
    prompt_dir: str | None = None,
    extra_args: str = "--print",
) -> ShellDispatcher:
    """Convenience constructor for the most common dispatcher: pipe the
    prompt to `claude --print` and capture the transcript.

    `extra_args` lets you tack on `--dangerously-skip-permissions`,
    `--model`, etc.  Default `--print` runs Claude non-interactively.
    """
    return ShellDispatcher(
        template=f"{claude_bin} {extra_args}".strip(),
        timeout=timeout,
        stdin_prompt=True,
        prompt_dir=prompt_dir,
    )


@dataclass
class CallableDispatcher:
    """Wrap a Python callable so it satisfies the Dispatcher protocol.

    The callable signature is `fn(prompt: str, edge_id: int) -> str | tuple`:
    return a string (transcript, rc=0 implied) or `(rc, transcript)`.
    Raising counts as rc=-3 with the exception captured as `error`.
    """
    fn: Callable

    def dispatch(self, prompt: str, edge_id: int) -> DispatchResult:
        t0 = time.monotonic()
        try:
            r = self.fn(prompt, edge_id)
            elapsed = time.monotonic() - t0
            if isinstance(r, tuple) and len(r) == 2:
                rc, transcript = int(r[0]), str(r[1])
            else:
                rc, transcript = 0, str(r)
            return DispatchResult(
                edge_id=edge_id, rc=rc, transcript=transcript,
                elapsed_s=elapsed)
        except Exception as e:
            return DispatchResult(
                edge_id=edge_id, rc=-3,
                error=f"{type(e).__name__}: {str(e)[:200]}",
                elapsed_s=time.monotonic() - t0)
