"""CLI: python3 -m aidev run|verify-journal ..."""

from __future__ import annotations

import argparse
import json
import signal
import sys
from pathlib import Path

from .backends import backend_from_config
from .journal import verify_journal
from .orchestrator import Orchestrator, PreflightError


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="aidev")
    sub = ap.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run", help="run one task")
    run.add_argument("--task", required=True, type=Path, help="task JSON file")
    run.add_argument("--repo", required=True, type=Path, help="target git repository root")
    run.add_argument("--runs-dir", required=True, type=Path, help="evidence directory (outside repo)")
    run.add_argument("--agents", required=True, type=Path,
                     help="JSON: {planner|implementer|reviewer: {argv, env?}}")
    vj = sub.add_parser("verify-journal", help="check an events.jsonl hash chain")
    vj.add_argument("path", type=Path)
    a = ap.parse_args(argv)

    if a.cmd == "verify-journal":
        ok, msg = verify_journal(a.path)
        print(("OK: " if ok else "BROKEN: ") + msg)
        return 0 if ok else 1

    try:
        agents = json.loads(a.agents.read_text())
        if not isinstance(agents, dict) or set(agents) != {"planner", "implementer", "reviewer"}:
            raise ValueError("agents file needs exactly planner, implementer, reviewer")
        backends = {k: backend_from_config(k, v) for k, v in agents.items()}
        task = json.loads(a.task.read_text())
        orch = Orchestrator(task=task, repo=a.repo, runs_root=a.runs_dir, **backends)
    except (OSError, ValueError, PreflightError) as e:
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
