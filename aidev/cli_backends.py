"""Live backends: Claude Code (plan, review) and Codex CLI (implement, fix).

Each backend has the same `invoke()` contract as `CommandBackend`: it receives
the orchestrator's request, runs the CLI once, and returns an `Invocation` whose
`result.stdout` is the normalised one-object JSON response that the M1
orchestrator already validates. Everything that is a fact (commit, diff, test
result) still comes from the orchestrator, not from the model.

Execution goes through `proc.run_bounded`, so the M1 real-client guard applies
unchanged: with the real executables these backends are refused
(FORBIDDEN_EXECUTABLE) until the owner approves the guard change proposed in
live/PROPOSED-GUARD-CHANGE.patch. With fake CLIs (tests) they run normally.

Flags were checked against `--help` of Claude Code 2.1.300 and Codex CLI 0.162.1
and parse-validated without inference (empty stdin); see live/readiness.sh.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from . import protocol
from .backends import Invocation
from .proc import ProcResult, run_bounded
from .redact import redact

FILES_NOTE = "File contents are provided below; you have no tools and must not ask for any."

PLAN_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["summary", "steps"],
    "properties": {"summary": {"type": "string"},
                   "steps": {"type": "array", "minItems": 1, "items": {"type": "string"}}},
}
REVIEW_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["reviewed_sha", "verdict", "summary", "findings"],
    "properties": {
        "reviewed_sha": {"type": "string", "pattern": "^[0-9a-f]{40}$"},
        "verdict": {"type": "string", "enum": list(protocol.VERDICTS)},
        "summary": {"type": "string"},
        "findings": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["id", "severity", "description"],
            "properties": {"id": {"type": "string"},
                           "severity": {"type": "string", "enum": list(protocol.SEVERITIES)},
                           "description": {"type": "string"}}}},
    },
}
IMPL_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["status", "summary", "files_changed"],
    "properties": {"status": {"type": "string", "enum": ["done", "failed"]},
                   "summary": {"type": "string"},
                   "files_changed": {"type": "array", "items": {"type": "string"}}},
}

# Flags that must never appear in a live argv (checked by tests and at build time).
CLAUDE_FORBIDDEN_FLAGS = {"--dangerously-skip-permissions", "--allow-dangerously-skip-permissions",
                          "--bare", "bypassPermissions", "--add-dir", "--plugin-dir", "--plugin-url",
                          "--mcp-config", "--chrome", "--remote-control", "--bg", "--cloud"}
CODEX_FORBIDDEN_FLAGS = {"--dangerously-bypass-approvals-and-sandbox", "--approve-for-me",
                         "--dangerously-bypass-hook-trust", "danger-full-access", "--add-dir",
                         "--worktree", "--oss", "--cyber-access-program"}


class LiveGateError(RuntimeError):
    pass


@dataclass(frozen=True)
class LiveGate:
    """Proof that readiness passed for these exact executables (see readiness.py).

    A programming-level safeguard against constructing live backends by
    accident; it is not a security boundary.
    """
    claude: str
    codex: str
    report_sha256: str


def _adapter_error(msg: str) -> bytes:
    # Deliberately fails protocol validation ("unexpected field") so the
    # orchestrator records INVALID_RESPONSE; the message lands in backend meta.
    return json.dumps({"adapter_error": msg}).encode()


def _with_stdout(r: ProcResult, stdout: bytes) -> ProcResult:
    return ProcResult(argv=r.argv, returncode=r.returncode, stdout=stdout, stderr=r.stderr,
                      timed_out=r.timed_out, output_limit_exceeded=r.output_limit_exceeded,
                      duration_s=r.duration_s, group_members_killed=r.group_members_killed,
                      cleanup_failed=r.cleanup_failed)


def _header(req: dict) -> dict:
    return {k: req[k] for k in ("protocol", "task_id", "stage", "cycle")}


def _files_block(files: dict | None) -> str:
    if not files:
        return "(no files provided)"
    parts = []
    for path, text in files.items():
        parts.append(f"----- FILE: {path} -----\n{text}\n----- END FILE: {path} -----")
    return "\n".join(parts)


def _criteria(req: dict) -> str:
    return "\n".join(f"- {c}" for c in req["acceptance_criteria"])


def _live_env(home: str, extra: dict[str, str]) -> dict[str, str]:
    # Real HOME is required for the CLIs' own subscription login. No API keys are
    # passed: the orchestrator's environment is not inherited.
    return {"PATH": "/usr/bin:/bin", "HOME": home, "LANG": "C.UTF-8", "TERM": "dumb",
            "USER": os.environ.get("USER", "dev"), **extra}


@dataclass
class ClaudeCodeBackend:
    """Plan and review with Claude Code in print mode with every tool disabled."""

    command: list[str]           # e.g. ["/home/dev/.local/bin/claude"] or a fake for tests
    gate: LiveGate | None
    real_home: str = field(default_factory=lambda: str(Path.home()))
    model: str | None = None
    name: str = "claude-code"

    def __post_init__(self):
        if self.gate is None:
            raise LiveGateError("ClaudeCodeBackend requires a passed readiness gate")
        if not os.path.isabs(self.command[0]):
            raise ValueError("command[0] must be absolute")

    def build_argv(self, stage: str) -> list[str]:
        schema = PLAN_SCHEMA if stage == "plan" else REVIEW_SCHEMA
        a = list(self.command) + [
            "-p", "--output-format", "json", "--json-schema", json.dumps(schema),
            "--tools", "",                 # no built-in tools at all
            "--safe-mode",                 # no CLAUDE.md, hooks, plugins, MCP, skills
            "--strict-mcp-config",         # ... and no MCP servers from any config
            "--no-session-persistence",
            "--permission-mode", "dontAsk", "--permission-prompts", "none",
            "--disable-slash-commands",
        ]
        if self.model:
            a += ["--model", self.model]
        bad = CLAUDE_FORBIDDEN_FLAGS.intersection(a)
        if bad:
            raise ValueError(f"forbidden Claude flags: {sorted(bad)}")
        return a

    def prompt(self, req: dict) -> str:
        if req["stage"] == "plan":
            return (
                "You are the planning engineer in an automated two-agent workflow. Another agent "
                "(Codex) will implement your plan in the repository; trusted code will run the tests.\n"
                f"{FILES_NOTE}\n\nOBJECTIVE:\n{req['objective']}\n\nACCEPTANCE CRITERIA:\n"
                f"{_criteria(req)}\n\nREPOSITORY FILES (base commit):\n{_files_block(req.get('files'))}\n\n"
                "Produce a concise, concrete implementation plan: which files to change and how, "
                "edge cases, and which unit tests to add. Do not write the full implementation. "
                "Respond only with the structured output.")
        tests = req["tests"]
        return (
            "You are the independent reviewer in an automated two-agent workflow. Codex implemented "
            "the change below; trusted code committed it and ran the tests.\n"
            f"{FILES_NOTE}\n\nCOMMIT_SHA: {req['commit_sha']}\n"
            "Your reviewed_sha MUST be exactly this COMMIT_SHA.\n\n"
            f"OBJECTIVE:\n{req['objective']}\n\nACCEPTANCE CRITERIA:\n{_criteria(req)}\n\n"
            f"PLAN THAT WAS GIVEN TO THE IMPLEMENTER:\n{json.dumps(req.get('plan'), indent=1)}\n\n"
            f"TRUSTED TEST RESULT: passed={tests['passed']} returncode={tests['returncode']} "
            f"timed_out={tests['timed_out']}\nTEST OUTPUT (tail):\n{tests['output_tail']}\n\n"
            f"DIFF base..COMMIT_SHA (truncated={req.get('diff_truncated')}):\n{req['diff']}\n\n"
            f"FILES AT COMMIT_SHA:\n{_files_block(req.get('files'))}\n\n"
            "Verdict rules: 'approve' only if the change meets every acceptance criterion and the "
            "trusted tests passed; 'changes_requested' with concrete findings if fixable defects "
            "remain; 'blocked' if a human decision is required. Respond only with the structured output.")

    def invoke(self, request: dict, *, cwd: Path, home: Path, timeout_s: float,
               log_prefix: Path) -> Invocation:
        # Claude gets an empty scratch directory, not the worktree: it needs no
        # filesystem access, and any file appearing there reveals tool use.
        scratch = home.parent / f"claude-cwd-{request['stage']}-c{request['cycle']}"
        scratch.mkdir()
        env = _live_env(self.real_home, {"DISABLE_AUTOUPDATER": "1",
                                         "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"})
        r = run_bounded(self.build_argv(request["stage"]), cwd=scratch, env=env,
                        stdin=self.prompt(request).encode(), timeout_s=timeout_s)
        out, err = log_prefix.with_suffix(".stdout"), log_prefix.with_suffix(".stderr")
        out.write_bytes(redact(r.stdout))
        err.write_bytes(redact(r.stderr))
        meta: dict = {"backend": self.name}
        leftovers = sorted(p.name for p in scratch.iterdir())
        if leftovers:
            meta["scratch_modified"] = leftovers
        normalized = self._normalize(r, request, meta)
        return Invocation(_with_stdout(r, normalized), out, err, meta)

    def _normalize(self, r: ProcResult, req: dict, meta: dict) -> bytes:
        if r.returncode != 0 or r.timed_out:
            return r.stdout[:0]
        if "scratch_modified" in meta:
            meta["adapter_error"] = "Claude created files although all tools were disabled"
            return _adapter_error(meta["adapter_error"])
        try:
            obj = json.loads(r.stdout)
        except (json.JSONDecodeError, UnicodeDecodeError):
            meta["adapter_error"] = "Claude stdout is not a JSON result object"
            return _adapter_error(meta["adapter_error"])
        if not isinstance(obj, dict):
            meta["adapter_error"] = "Claude result is not an object"
            return _adapter_error(meta["adapter_error"])
        usage = obj.get("usage") or {}
        meta.update(num_turns=obj.get("num_turns"), duration_ms=obj.get("duration_ms"),
                    total_cost_usd=obj.get("total_cost_usd"),
                    input_tokens=usage.get("input_tokens"), output_tokens=usage.get("output_tokens"),
                    permission_denials=len(obj.get("permission_denials") or []))
        if (obj.get("type") != "result" or obj.get("subtype") != "success"
                or obj.get("is_error") is not False
                or not isinstance(obj.get("structured_output"), dict)):
            meta["adapter_error"] = (f"Claude result not usable: type={obj.get('type')} "
                                     f"subtype={obj.get('subtype')} is_error={obj.get('is_error')}")
            return _adapter_error(meta["adapter_error"])
        if meta["permission_denials"]:
            meta["warning"] = "Claude attempted a tool call that was denied"
        return json.dumps({**_header(req), **obj["structured_output"]}).encode()


@dataclass
class CodexExecBackend:
    """Implement and fix with `codex exec` inside Codex's built-in :workspace sandbox."""

    command: list[str]
    gate: LiveGate | None
    real_home: str = field(default_factory=lambda: str(Path.home()))
    model: str | None = None
    name: str = "codex-exec"

    def __post_init__(self):
        if self.gate is None:
            raise LiveGateError("CodexExecBackend requires a passed readiness gate")
        if not os.path.isabs(self.command[0]):
            raise ValueError("command[0] must be absolute")

    def build_argv(self, worktree: Path, schema_path: Path, last_path: Path) -> list[str]:
        a = list(self.command) + [
            "exec", "--strict-config", "--ignore-user-config", "--ignore-rules", "--ephemeral",
            # The exact built-in profile verified by live/sandbox_probe.sh: writes only in
            # the worktree and /tmp, .git read-only, no network.
            "-c", 'default_permissions=":workspace"',
            "-c", 'approval_policy="never"',          # escalation requests fail instead of hanging
            "-c", "check_for_update_on_startup=false",
            "--json", "--color", "never",
            "--output-schema", str(schema_path), "-o", str(last_path),
            "-C", str(worktree), "-",
        ]
        if self.model:
            a[2:2] = ["-m", self.model]
        bad = CODEX_FORBIDDEN_FLAGS.intersection(a)
        if bad:
            raise ValueError(f"forbidden Codex flags: {sorted(bad)}")
        return a

    def prompt(self, req: dict) -> str:
        lines = [
            "You are the implementation engineer in an automated two-agent workflow.",
            "Edit files in the current directory (a disposable git worktree) to complete the task.",
            "Rules: do NOT run git commit, git checkout, git reset or any other command that changes "
            "git state; the orchestrator commits your changes. Do not touch files outside this "
            "directory. No network access is available. You may run the existing tests with "
            "`python3 -m unittest`. Use only the Python standard library.",
            "", f"OBJECTIVE:\n{req['objective']}", "", f"ACCEPTANCE CRITERIA:\n{_criteria(req)}", "",
            f"PLAN (from the planning engineer):\n{json.dumps(req.get('plan'), indent=1)}",
        ]
        if req["stage"] == "fix":
            lines += ["", "REVIEW FINDINGS TO FIX (from the independent reviewer):",
                      json.dumps(req.get("findings"), indent=1),
                      f"Your previous attempt is commit {req.get('previous_sha')}; fix the findings."]
        lines += ["", "When finished, reply with the structured output: status 'done' or 'failed', "
                  "a short summary, and the list of files you changed."]
        return "\n".join(lines)

    def invoke(self, request: dict, *, cwd: Path, home: Path, timeout_s: float,
               log_prefix: Path) -> Invocation:
        schema_path = log_prefix.with_suffix(".schema.json")
        last_path = log_prefix.with_suffix(".last-message.json")
        schema_path.write_text(json.dumps(IMPL_SCHEMA))
        env = _live_env(self.real_home, {})
        r = run_bounded(self.build_argv(cwd, schema_path, last_path), cwd=cwd, env=env,
                        stdin=self.prompt(request).encode(), timeout_s=timeout_s,
                        out_limit=8 << 20)
        out, err = log_prefix.with_suffix(".stdout"), log_prefix.with_suffix(".stderr")
        out.write_bytes(redact(r.stdout))
        err.write_bytes(redact(r.stderr))
        meta = {"backend": self.name, **self._events(r.stdout)}
        normalized = self._normalize(r, request, last_path, meta)
        if last_path.exists():
            last_path.write_bytes(redact(last_path.read_bytes()))
        return Invocation(_with_stdout(r, normalized), out, err, meta)

    @staticmethod
    def _events(stdout: bytes) -> dict:
        counts: dict[str, int] = {}
        usage: dict = {}
        errors = 0
        for line in stdout.splitlines():
            try:
                ev = json.loads(line)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if not isinstance(ev, dict):
                continue
            t = str(ev.get("type"))
            if t in ("item.completed", "item.started"):
                t = f"{t}:{(ev.get('item') or {}).get('type')}"
            counts[t] = counts.get(t, 0) + 1
            if isinstance(ev.get("usage"), dict):
                for k, v in ev["usage"].items():
                    if isinstance(v, int):
                        usage[k] = usage.get(k, 0) + v
            if t in ("error", "turn.failed"):
                errors += 1
        return {"event_counts": counts, "usage": usage, "error_events": errors}

    @staticmethod
    def _normalize(r: ProcResult, req: dict, last_path: Path, meta: dict) -> bytes:
        if r.returncode != 0 or r.timed_out:
            return r.stdout[:0]
        try:
            obj = json.loads(last_path.read_text())
        except FileNotFoundError:
            meta["adapter_error"] = "Codex wrote no final message (-o file missing)"
            return _adapter_error(meta["adapter_error"])
        except (json.JSONDecodeError, UnicodeDecodeError):
            meta["adapter_error"] = "Codex final message is not JSON"
            return _adapter_error(meta["adapter_error"])
        if not isinstance(obj, dict):
            meta["adapter_error"] = "Codex final message is not an object"
            return _adapter_error(meta["adapter_error"])
        return json.dumps({**_header(req), **obj}).encode()


def resolve_cli(name: str, home: str | None = None) -> str:
    """Absolute path of the user-local CLI launcher (not resolved through PATH)."""
    p = Path(home or Path.home()) / ".local" / "bin" / name
    if not p.exists():
        raise FileNotFoundError(f"{name} not found at {p}")
    return str(p)
