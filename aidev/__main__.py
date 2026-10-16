"""CLI: python3 -m aidev run|run-live|verify-journal ..."""

from __future__ import annotations

import argparse
import json
import signal
import sys
from pathlib import Path

from . import protocol
from .backends import backend_from_config
from .cli_backends import LiveGateError
from .journal import verify_journal
from .live import live_backends, load_gate
from .orchestrator import Orchestrator, PreflightError

# Live runs are capped by the owner-approved "live_limits" of live/live_config.json, as
# captured (and hashed) in the readiness report. These keys must all be present there.
LIVE_LIMIT_KEYS = ("max_cycles", "max_model_calls", "max_clarifications", "stage_timeout_s",
                   "task_timeout_s")


def _live_setup(a, task: dict) -> tuple[dict, dict]:
    if not a.confirm_live:
        raise LiveGateError("run-live needs --confirm-live (real model sessions will run)")
    pinned = json.loads(a.readiness.read_text())["pinned"]
    limits = pinned.get("live_limits")
    if not isinstance(limits, dict) or set(limits) != set(LIVE_LIMIT_KEYS):
        raise LiveGateError("readiness report has no complete owner-approved live_limits")
    effective = protocol.load_task(task)     # applies defaults, e.g. derived max_model_calls
    for key in LIVE_LIMIT_KEYS:
        if effective[key] > limits[key]:
            raise ValueError(f"live task must set {key} <= {limits[key]} (owner-approved live limit)")
    gate = load_gate(a.readiness)
    extra = {"test_sandbox": True, "model_policy": pinned["codex_policy"],
             "model_catalog": gate.codex_catalog}
    return live_backends(gate, pinned), extra


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="aidev")
    sub = ap.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run", help="run one task")
    run.add_argument("--task", required=True, type=Path, help="task JSON file")
    run.add_argument("--repo", required=True, type=Path, help="target git repository root")
    run.add_argument("--runs-dir", required=True, type=Path, help="evidence directory (outside repo)")
    run.add_argument("--agents", required=True, type=Path,
                     help="JSON: {planner|implementer|reviewer: {argv, env?}}")
    live = sub.add_parser("run-live", help="run one task with the REAL Claude Code and Codex CLIs")
    live.add_argument("--task", required=True, type=Path)
    live.add_argument("--repo", required=True, type=Path)
    live.add_argument("--runs-dir", required=True, type=Path)
    live.add_argument("--readiness", required=True, type=Path,
                      help="live/readiness-report.json produced by live/readiness.py")
    live.add_argument("--confirm-live", action="store_true",
                      help="required: acknowledges that real model sessions will run")
    vj = sub.add_parser("verify-journal", help="check an events.jsonl hash chain")
    vj.add_argument("path", type=Path)
    a = ap.parse_args(argv)

    if a.cmd == "verify-journal":
        ok, msg = verify_journal(a.path)
        print(("OK: " if ok else "BROKEN: ") + msg)
        return 0 if ok else 1

    try:
        task = json.loads(a.task.read_text())
        extra: dict = {}
        if a.cmd == "run-live":
            backends, extra = _live_setup(a, task)
        else:
            agents = json.loads(a.agents.read_text())
            if not isinstance(agents, dict) or set(agents) != {"planner", "implementer", "reviewer"}:
                raise ValueError("agents file needs exactly planner, implementer, reviewer")
            backends = {k: backend_from_config(k, v) for k, v in agents.items()}
        orch = Orchestrator(task=task, repo=a.repo, runs_root=a.runs_dir, **backends, **extra)
    except (OSError, ValueError, PreflightError, LiveGateError) as e:
        print(f"aidev: refused before start: {e}", file=sys.stderr)
        return 2
    def on_term(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, on_term)
    result = orch.run()
    print(f"{result['state']}: {result['detail']}")
    print(f"report: {orch.run_dir / 'report.md'}")
    if result["state"] == "INTERRUPTED":
        return 130
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
