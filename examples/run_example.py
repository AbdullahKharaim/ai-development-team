#!/usr/bin/env python3
"""Reproducible mock demo: builds a throwaway repo, runs aidev, copies the evidence.

    cd m1_orchestrator && python3 examples/run_example.py

Everything is created under examples/_work/ (git-ignored, recreated each run);
the evidence is copied to examples/output/. Only the mock agents are used.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EX = ROOT / "examples"
WORK = EX / "_work"
OUT = EX / "output"
GIT_ENV = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_CONFIG_GLOBAL": os.devnull, "HOME": str(WORK)}


def git(*args):
    subprocess.run(["/usr/bin/git", "-c", "user.name=demo", "-c", "user.email=demo@invalid",
                    "-c", "init.defaultBranch=main", *args],
                   cwd=WORK / "repo", env=GIT_ENV, check=True, capture_output=True)


def main() -> int:
    shutil.rmtree(WORK, ignore_errors=True)
    (WORK / "repo").mkdir(parents=True)
    repo = WORK / "repo"
    (repo / "textutil.py").write_text("def shout(text):\n    return text.upper()\n")
    (repo / "test_textutil.py").write_text(
        "import unittest\n\nfrom textutil import shout\n\n\n"
        "class TextUtilTests(unittest.TestCase):\n"
        "    def test_shout(self):\n        self.assertEqual(shout(\"hi\"), \"HI\")\n")
    (repo / ".gitignore").write_text("__pycache__/\n")
    git("init", "-q")
    git("add", "--all")
    git("commit", "-q", "-m", "initial: textutil.shout")

    mock = str(ROOT / "mocks" / "fake_agent.py")
    env = {"AIDEV_MOCK_SCENARIO": str(EX / "scenario.json")}
    agents = {
        "planner": {"argv": ["/usr/bin/python3", "-I", mock, "--role", "claude"], "env": env},
        "implementer": {"argv": ["/usr/bin/python3", "-I", mock, "--role", "codex"], "env": env},
        "reviewer": {"argv": ["/usr/bin/python3", "-I", mock, "--role", "claude"], "env": env},
    }
    (WORK / "agents.json").write_text(json.dumps(agents, indent=2))

    rc = subprocess.run([sys.executable, "-m", "aidev", "run", "--task", str(EX / "task.json"),
                         "--repo", str(repo), "--runs-dir", str(WORK / "runs"),
                         "--agents", str(WORK / "agents.json")], cwd=ROOT).returncode

    run_dir = WORK / "runs" / "demo-slugify"
    shutil.rmtree(OUT, ignore_errors=True)
    OUT.mkdir()
    for name in ("events.jsonl", "result.json", "report.md"):
        shutil.copy2(run_dir / name, OUT / name)
    shutil.copytree(run_dir / "logs", OUT / "logs")
    shutil.copy2(WORK / "agents.json", OUT / "agents.json")
    log = subprocess.run(["/usr/bin/git", "log", "--format=%H %P %an: %s", "--all"],
                         cwd=repo, env=GIT_ENV, capture_output=True, text=True).stdout
    (OUT / "git-log.txt").write_text(log)
    print(f"evidence copied to {OUT}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
