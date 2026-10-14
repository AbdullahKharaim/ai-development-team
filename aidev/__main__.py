"""CLI: python3 -m aidev run|run-live|verify-journal ..."""

from __future__ import annotations

import argparse
import json
import signal
import sys
from pathlib import Path

from .backends import backend_from_config
from .cli_backends import LiveGateError
from .journal import verify_journal
from .live import live_backends, load_gate
from .orchestrator import Orchestrator, PreflightError

# Hard limits for live runs, independent of what the task file asks for.
LIVE_LIMITS = {"max_cycles": 2, "stage_timeout_s": 900, "task_timeout_s": 3600}


def _live_setup(a, task: dict) -> dict:
    if not a.confirm_live:
        raise LiveGateError("run-live needs --confirm-live (real model sessions will run)")
    for key, limit in LIVE_LIMITS.items():
        if task.get(key, limit + 1) > limit:
            raise ValueError(f"live task must set {key} <= {limit}")
    gate = load_gate(a.readiness)
    pinned = json.loads(a.readiness.read_text())["pinned"]
    return live_backends(gate, pinned)


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
        if a.cmd == "run-live":
            backends = _live_setup(a, task)
        else:
            agents = json.loads(a.agents.read_text())
            if not isinstance(agents, dict) or set(agents) != {"planner", "implementer", "reviewer"}:
                raise ValueError("agents file needs exactly planner, implementer, reviewer")
            backends = {k: backend_from_config(k, v) for k, v in agents.items()}
        orch = Orchestrator(task=task, repo=a.repo, runs_root=a.runs_dir, **backends)
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
