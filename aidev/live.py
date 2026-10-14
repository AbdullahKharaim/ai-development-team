"""Turn a passed readiness report into live backends (no process is started here)."""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

from .cli_backends import ClaudeCodeBackend, CodexExecBackend, LiveGate, LiveGateError


def load_gate(report_path: Path) -> LiveGate:
    raw = Path(report_path).read_bytes()
    rep = json.loads(raw)
    if rep.get("passed") is not True:
        raise LiveGateError("readiness report did not pass")
    age = time.time() - rep.get("generated_epoch", 0)
    if not 0 <= age <= rep["pinned"]["readiness_max_age_s"]:
        raise LiveGateError(f"readiness report is stale ({int(age)} s old); rerun live/readiness.py")
    ex = rep["executables"]
    for name in ("claude", "codex"):
        if os.path.realpath(ex[name]) != ex[f"{name}_real"]:
            raise LiveGateError(f"{name} changed since readiness (auto-update?); rerun readiness")
    if not ex["claude_real"].endswith("/" + rep["pinned"]["claude_version"]):
        raise LiveGateError("claude executable is not the pinned version")
    if f"/{rep['pinned']['codex_version']}-" not in ex["codex_real"]:
        raise LiveGateError("codex executable is not the pinned version")
    return LiveGate(claude=ex["claude"], codex=ex["codex"],
                    report_sha256=hashlib.sha256(raw).hexdigest())


def live_backends(gate: LiveGate, pinned: dict) -> dict:
    claude = ClaudeCodeBackend([gate.claude], gate, model=pinned.get("claude_model"))
    codex = CodexExecBackend([gate.codex], gate, model=pinned.get("codex_model"))
    return {"planner": claude, "implementer": codex, "reviewer": claude}
