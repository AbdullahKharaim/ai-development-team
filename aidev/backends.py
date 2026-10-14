"""Agent backends.

M1 ships exactly one usable backend, `CommandBackend`, which runs a configured
argv (in M1: the mock agents), writes the request JSON to stdin and reads one
JSON response from stdout.

`ClaudeCliBackend` / `CodexCliBackend` document how the real clients will be
wired in M2. They are deliberately disabled: `invoke` raises before building a
process, and `proc.run_bounded` independently refuses the real executables.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from .proc import SAFE_PATH, ProcResult, run_bounded

REAL_BACKENDS_ENABLED = False  # M1: hard-coded; there is no flag or env override.


class RealBackendDisabled(RuntimeError):
    pass


@dataclass
class Invocation:
    result: ProcResult
    stdout_path: Path
    stderr_path: Path
    meta: dict | None = None  # live backends: usage, turns, adapter errors (journalled)


@dataclass
class CommandBackend:
    name: str
    argv: list[str]
    env: dict[str, str] = field(default_factory=dict)

    def __post_init__(self):
        if not self.argv or not os.path.isabs(self.argv[0]):
            raise ValueError(f"{self.name}: argv[0] must be an absolute path (no PATH lookup)")

    def invoke(self, request: dict, *, cwd: Path, home: Path, timeout_s: float,
               log_prefix: Path) -> Invocation:
        env = {"PATH": SAFE_PATH, "HOME": str(home), "LANG": "C.UTF-8",
               "PYTHONDONTWRITEBYTECODE": "1", **self.env}
        payload = json.dumps(request, sort_keys=True).encode()
        result = run_bounded(self.argv, cwd=cwd, env=env, stdin=payload, timeout_s=timeout_s)
        out = log_prefix.with_suffix(".stdout")
        err = log_prefix.with_suffix(".stderr")
        out.write_bytes(result.stdout)
        err.write_bytes(result.stderr)
        return Invocation(result, out, err)


class _RealCliBackend:
    """Shape of the M2 integration. Not callable in M1."""

    name = "real"

    def planned_argv(self, stage: str, cwd: Path) -> list[str]:
        raise NotImplementedError

    def invoke(self, *a, **kw):
        raise RealBackendDisabled(f"{self.name}: real model invocation is disabled in M1")


class ClaudeCliBackend(_RealCliBackend):
    name = "claude-cli"

    def planned_argv(self, stage: str, cwd: Path) -> list[str]:
        # Flags confirmed present in `claude --help` for 2.1.286; behaviour to be
        # verified in the agentdev distribution before M2 relies on it.
        read_only = "Read,Grep,Glob,Bash(git diff:*),Bash(git log:*),Bash(git show:*)"
        return ["claude", "-p", "--output-format", "json", "--json-schema", "<schema>",
                "--allowedTools", read_only, "--strict-mcp-config",
                "--setting-sources", "project"]


class CodexCliBackend(_RealCliBackend):
    name = "codex-cli"

    def planned_argv(self, stage: str, cwd: Path) -> list[str]:
        # Never --dangerously-bypass-approvals-and-sandbox or --approve-for-me.
        return ["codex", "exec", "--sandbox", "workspace-write", "--cd", str(cwd),
                "--ephemeral", "--json", "--output-schema", "<schema>",
                "--output-last-message", "<file>", "-"]


def backend_from_config(name: str, cfg: dict) -> CommandBackend:
    if not isinstance(cfg, dict) or set(cfg) - {"argv", "env"}:
        raise ValueError(f"{name}: config must be {{argv, env?}}")
    argv, env = cfg.get("argv"), cfg.get("env", {})
    if not isinstance(argv, list) or not all(isinstance(a, str) for a in argv):
        raise ValueError(f"{name}: argv must be a list of strings")
    if not isinstance(env, dict) or not all(isinstance(k, str) and isinstance(v, str)
                                            for k, v in env.items()):
        raise ValueError(f"{name}: env must map strings to strings")
    if set(env) & {"PATH", "HOME"}:
        raise ValueError(f"{name}: PATH and HOME are set by the orchestrator")
    return CommandBackend(name=name, argv=argv, env=env)
