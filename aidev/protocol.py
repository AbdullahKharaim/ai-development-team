"""Task input and agent response contracts (stdlib-only validation).

Agents receive one JSON request on stdin and must print exactly one JSON object
on stdout. Anything else -- extra keys, a second object, wrong task/stage/cycle --
is rejected. The orchestrator never trusts an agent's self-reported facts
(e.g. `files_changed`); it re-derives them from git.
"""

from __future__ import annotations

import json
import re

PROTOCOL = "aidev.m1/1"
TASK_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
MAX_CYCLES_CEILING = 2


class ProtocolError(ValueError):
    """The input or an agent response violates the contract."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


# ---------------------------------------------------------------- task input

TASK_DEFAULTS = {
    "max_cycles": 2,
    "stage_timeout_s": 900,
    "test_timeout_s": 600,
    "task_timeout_s": 3600,
}
TASK_REQUIRED = {"task_id": str, "objective": str, "acceptance_criteria": list, "test_command": list}


def load_task(raw: dict) -> dict:
    if not isinstance(raw, dict):
        raise ProtocolError("TASK_INVALID", "task must be a JSON object")
    allowed = set(TASK_REQUIRED) | set(TASK_DEFAULTS)
    extra = set(raw) - allowed
    if extra:
        raise ProtocolError("TASK_INVALID", f"unknown fields: {sorted(extra)}")
    for key, typ in TASK_REQUIRED.items():
        if not isinstance(raw.get(key), typ):
            raise ProtocolError("TASK_INVALID", f"{key} must be {typ.__name__}")
    task = {**TASK_DEFAULTS, **raw}
    if not TASK_ID_RE.match(task["task_id"]):
        raise ProtocolError("TASK_INVALID", "task_id must match [a-z0-9][a-z0-9-]{0,62}")
    if not task["objective"].strip():
        raise ProtocolError("TASK_INVALID", "objective is empty")
    if not all(isinstance(c, str) and c.strip() for c in task["acceptance_criteria"]):
        raise ProtocolError("TASK_INVALID", "acceptance_criteria must be non-empty strings")
    tc = task["test_command"]
    if not tc or not all(isinstance(a, str) for a in tc):
        raise ProtocolError("TASK_INVALID", "test_command must be a non-empty argv list")
    mc = task["max_cycles"]
    if type(mc) is not int or not 1 <= mc <= MAX_CYCLES_CEILING:
        raise ProtocolError("TASK_INVALID", f"max_cycles must be 1..{MAX_CYCLES_CEILING}")
    for key in ("stage_timeout_s", "test_timeout_s", "task_timeout_s"):
        v = task[key]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 < v <= 4 * 3600:
            raise ProtocolError("TASK_INVALID", f"{key} must be in (0, 14400]")
    return task


# ---------------------------------------------------------- agent responses

STAGES = ("plan", "implement", "fix", "review")
VERDICTS = ("approve", "changes_requested", "blocked")
SEVERITIES = ("critical", "high", "medium", "low")

_COMMON = {"protocol": str, "task_id": str, "stage": str, "cycle": int}
_FIELDS = {
    "plan": {**_COMMON, "summary": str, "steps": list},
    "implement": {**_COMMON, "status": str, "summary": str, "files_changed": list},
    "fix": {**_COMMON, "status": str, "summary": str, "files_changed": list},
    "review": {**_COMMON, "reviewed_sha": str, "verdict": str, "summary": str, "findings": list},
}


def parse_single_json(stdout: bytes) -> dict:
    """Exactly one JSON object, optionally surrounded by whitespace."""
    try:
        text = stdout.decode("utf-8")
    except UnicodeDecodeError:
        raise ProtocolError("RESPONSE_INVALID", "stdout is not UTF-8") from None
    text = text.strip()
    if not text:
        raise ProtocolError("RESPONSE_INVALID", "empty stdout")
    dec = json.JSONDecoder()
    try:
        obj, end = dec.raw_decode(text)
    except json.JSONDecodeError as e:
        raise ProtocolError("RESPONSE_INVALID", f"not JSON: {e.msg}") from None
    if text[end:].strip():
        # A second completion message (or trailing junk) makes the result ambiguous.
        raise ProtocolError("RESPONSE_CONFLICTING", "more than one JSON document on stdout")
    if not isinstance(obj, dict):
        raise ProtocolError("RESPONSE_INVALID", "top-level JSON must be an object")
    return obj


def validate_response(obj: dict, *, stage: str, task_id: str, cycle: int) -> dict:
    fields = _FIELDS[stage]
    extra = set(obj) - set(fields)
    missing = set(fields) - set(obj)
    if missing or extra:
        raise ProtocolError("RESPONSE_INVALID",
                            f"missing={sorted(missing)} unexpected={sorted(extra)}")
    for key, typ in fields.items():
        v = obj[key]
        if typ is int and type(v) is not int or typ is not int and not isinstance(v, typ):
            raise ProtocolError("RESPONSE_INVALID", f"{key} must be {typ.__name__}")
    if obj["protocol"] != PROTOCOL:
        raise ProtocolError("RESPONSE_INVALID", f"protocol must be {PROTOCOL}")
    if obj["task_id"] != task_id or obj["stage"] != stage or obj["cycle"] != cycle:
        raise ProtocolError("RESPONSE_MISMATCH",
                            f"expected task={task_id} stage={stage} cycle={cycle}, got "
                            f"task={obj['task_id']} stage={obj['stage']} cycle={obj['cycle']}")
    if stage == "plan":
        if not obj["steps"] or not all(isinstance(s, str) and s.strip() for s in obj["steps"]):
            raise ProtocolError("RESPONSE_INVALID", "steps must be non-empty strings")
    elif stage in ("implement", "fix"):
        if obj["status"] not in ("done", "failed"):
            raise ProtocolError("RESPONSE_INVALID", "status must be done|failed")
        if not all(isinstance(f, str) for f in obj["files_changed"]):
            raise ProtocolError("RESPONSE_INVALID", "files_changed must be strings")
    elif stage == "review":
        if not SHA_RE.match(obj["reviewed_sha"]):
            raise ProtocolError("RESPONSE_INVALID", "reviewed_sha must be a 40-hex commit id")
        if obj["verdict"] not in VERDICTS:
            raise ProtocolError("RESPONSE_INVALID", f"verdict must be one of {VERDICTS}")
        for f in obj["findings"]:
            if (not isinstance(f, dict) or set(f) != {"id", "severity", "description"}
                    or not all(isinstance(f[k], str) for k in f)
                    or f["severity"] not in SEVERITIES):
                raise ProtocolError("RESPONSE_INVALID",
                                    "finding must be {id, severity, description} with valid severity")
        if obj["verdict"] == "changes_requested" and not obj["findings"]:
            raise ProtocolError("RESPONSE_INVALID", "changes_requested requires findings")
    return obj
