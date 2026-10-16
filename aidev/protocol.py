"""Task input and agent response contracts (stdlib-only validation).

Agents receive one JSON request on stdin and must print exactly one JSON object
on stdout. Anything else -- extra keys, a second object, wrong task/stage/cycle --
is rejected. The orchestrator never trusts an agent's self-reported facts
(e.g. `files_changed`); it re-derives them from git.
"""

from __future__ import annotations

import json
import os
import re

PROTOCOL = "aidev.m1/1"
TASK_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
# Absolute ceilings of the protocol. Live runs are further capped by the owner-approved
# limits in live/live_config.json (enforced by `aidev run-live`).
MAX_CYCLES_CEILING = 10
MAX_MODEL_CALLS_CEILING = 60
MAX_CLARIFICATIONS_CEILING = 3
COMPLEXITIES = ("simple", "normal", "complex")


class ProtocolError(ValueError):
    """The input or an agent response violates the contract."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


# ---------------------------------------------------------------- task input

TASK_DEFAULTS = {
    "max_cycles": 2,              # implementation/review cycles (each = one commit + one review)
    "max_model_calls": None,      # None -> 1 + 2*max_cycles + 2*max_clarifications
    "max_clarifications": 1,      # implementer questions answered by a re-plan
    "stall_repeat_limit": 3,      # same finding in this many reviews -> STALLED
    "complexity": "normal",       # owner's classification; the planner may only raise it
    "continue_from": None,        # previous run dir to continue (human extension)
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
    mcl = task["max_clarifications"]
    if type(mcl) is not int or not 0 <= mcl <= MAX_CLARIFICATIONS_CEILING:
        raise ProtocolError("TASK_INVALID", f"max_clarifications must be 0..{MAX_CLARIFICATIONS_CEILING}")
    if task["max_model_calls"] is None:
        task["max_model_calls"] = 1 + 2 * mc + 2 * mcl
    calls = task["max_model_calls"]
    if type(calls) is not int or not 2 <= calls <= MAX_MODEL_CALLS_CEILING:
        raise ProtocolError("TASK_INVALID", f"max_model_calls must be 2..{MAX_MODEL_CALLS_CEILING}")
    srl = task["stall_repeat_limit"]
    if type(srl) is not int or not 2 <= srl <= 10:
        raise ProtocolError("TASK_INVALID", "stall_repeat_limit must be 2..10")
    if task["complexity"] not in COMPLEXITIES:
        raise ProtocolError("TASK_INVALID", f"complexity must be one of {COMPLEXITIES}")
    cf = task["continue_from"]
    if cf is not None and not (isinstance(cf, str) and os.path.isabs(cf)):
        raise ProtocolError("TASK_INVALID", "continue_from must be an absolute run directory path")
    for key in ("stage_timeout_s", "test_timeout_s", "task_timeout_s"):
        v = task[key]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 < v <= 4 * 3600:
            raise ProtocolError("TASK_INVALID", f"{key} must be in (0, 14400]")
    return task


# ---------------------------------------------------------- agent responses

STAGES = ("plan", "replan", "implement", "fix", "review")
VERDICTS = ("approve", "changes_requested", "blocked")
SEVERITIES = ("critical", "high", "medium", "low")
IMPL_STATUSES = ("done", "failed", "needs_clarification")

_COMMON = {"protocol": str, "task_id": str, "stage": str, "cycle": int}
_PLAN = {**_COMMON, "summary": str, "steps": list}
_IMPL = {**_COMMON, "status": str, "summary": str, "files_changed": list}
_FIELDS = {
    "plan": _PLAN,
    "replan": _PLAN,
    "implement": _IMPL,
    "fix": _IMPL,
    "review": {**_COMMON, "reviewed_sha": str, "verdict": str, "summary": str, "findings": list},
}
# Optional fields (accepted, type-checked, never required): older agents stay valid.
_PLAN_OPT = {"complexity": str, "complexity_rationale": str}
_IMPL_OPT = {"questions": list}
_OPTIONAL = {"plan": _PLAN_OPT, "replan": _PLAN_OPT, "implement": _IMPL_OPT, "fix": _IMPL_OPT,
             "review": {}}


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
    fields, optional = _FIELDS[stage], _OPTIONAL[stage]
    extra = set(obj) - set(fields) - set(optional)
    missing = set(fields) - set(obj)
    if missing or extra:
        raise ProtocolError("RESPONSE_INVALID",
                            f"missing={sorted(missing)} unexpected={sorted(extra)}")
    for key, typ in list(fields.items()) + [(k, t) for k, t in optional.items() if k in obj]:
        v = obj[key]
        if typ is int and type(v) is not int or typ is not int and not isinstance(v, typ):
            raise ProtocolError("RESPONSE_INVALID", f"{key} must be {typ.__name__}")
    if obj["protocol"] != PROTOCOL:
        raise ProtocolError("RESPONSE_INVALID", f"protocol must be {PROTOCOL}")
    if obj["task_id"] != task_id or obj["stage"] != stage or obj["cycle"] != cycle:
        raise ProtocolError("RESPONSE_MISMATCH",
                            f"expected task={task_id} stage={stage} cycle={cycle}, got "
                            f"task={obj['task_id']} stage={obj['stage']} cycle={obj['cycle']}")
    if stage in ("plan", "replan"):
        if not obj["steps"] or not all(isinstance(s, str) and s.strip() for s in obj["steps"]):
            raise ProtocolError("RESPONSE_INVALID", "steps must be non-empty strings")
        if "complexity" in obj and obj["complexity"] not in COMPLEXITIES:
            raise ProtocolError("RESPONSE_INVALID", f"complexity must be one of {COMPLEXITIES}")
    elif stage in ("implement", "fix"):
        if obj["status"] not in IMPL_STATUSES:
            raise ProtocolError("RESPONSE_INVALID", f"status must be one of {IMPL_STATUSES}")
        if not all(isinstance(f, str) for f in obj["files_changed"]):
            raise ProtocolError("RESPONSE_INVALID", "files_changed must be strings")
        questions = obj.get("questions", [])
        if not all(isinstance(q, str) and q.strip() for q in questions):
            raise ProtocolError("RESPONSE_INVALID", "questions must be non-empty strings")
        if obj["status"] == "needs_clarification" and not questions:
            raise ProtocolError("RESPONSE_INVALID", "needs_clarification requires questions")
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
