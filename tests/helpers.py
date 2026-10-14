"""Synthetic fixtures: disposable git repos under m1_orchestrator/.test-work only."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from aidev.backends import CommandBackend
from aidev.orchestrator import Orchestrator

M1_ROOT = Path(__file__).resolve().parents[1]
MOCK = M1_ROOT / "mocks" / "fake_agent.py"
WORK = M1_ROOT / ".test-work"
PYTHON = "/usr/bin/python3"

CALC_V0 = "def add(a, b):\n    return a + b\n"
TEST_V0 = ("import unittest\nfrom calc import add\n\n\n"
           "class T(unittest.TestCase):\n"
           "    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n")
CALC_GOOD = CALC_V0 + "\n\ndef multiply(a, b):\n    return a * b\n"
CALC_BUGGY = CALC_V0 + "\n\ndef multiply(a, b):\n    return a + b\n"
TEST_V1 = ("import unittest\nfrom calc import add, multiply\n\n\n"
           "class T(unittest.TestCase):\n"
           "    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n\n"
           "    def test_multiply(self):\n        self.assertEqual(multiply(3, 4), 12)\n")

GOOD_IMPL = {"write_files": {"calc.py": CALC_GOOD, "test_calc.py": TEST_V1}}
BUGGY_IMPL = {"write_files": {"calc.py": CALC_BUGGY, "test_calc.py": TEST_V1}}
FINDING = {"id": "F1", "severity": "high", "description": "multiply() adds instead of multiplying"}
CHANGES = {"override": {"verdict": "changes_requested", "findings": [FINDING],
                        "summary": "multiply is wrong"}}

GIT_ENV = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_CONFIG_GLOBAL": os.devnull}


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["/usr/bin/git", "-c", "user.name=fixture", "-c",
                           "user.email=fixture@invalid", "-c", "init.defaultBranch=main", *args],
                          cwd=cwd, env={**GIT_ENV, "HOME": str(cwd)}, check=True,
                          capture_output=True, text=True).stdout


def task(**over) -> dict:
    t = {"task_id": "t-multiply", "objective": "Add multiply(a, b) to calc.py with a unit test",
         "acceptance_criteria": ["multiply(3, 4) == 12", "existing tests still pass"],
         "test_command": ["python3", "-m", "unittest", "-q"],
         "stage_timeout_s": 30, "test_timeout_s": 30, "task_timeout_s": 120}
    t.update(over)
    return t


def repo_fingerprint(repo: Path) -> dict:
    """Everything a run must not change in the source repo (aidev/* refs excluded)."""
    files = {}
    for p in sorted(repo.rglob("*")):
        if ".git" in p.relative_to(repo).parts or not p.is_file():
            continue
        files[str(p.relative_to(repo))] = hashlib.sha256(p.read_bytes()).hexdigest()
    branches = [b for b in git(repo, "for-each-ref", "--format=%(refname) %(objectname)",
                               "refs/heads").splitlines() if " refs/heads/aidev/" not in f" {b}"]
    return {"files": files, "branches": branches, "status": git(repo, "status", "--porcelain"),
            "head": git(repo, "rev-parse", "HEAD").strip()}


class OrchestratorTestCase(unittest.TestCase):
    def setUp(self):
        WORK.mkdir(exist_ok=True)
        self.tmp = Path(tempfile.mkdtemp(prefix="case-", dir=WORK))
        self.repo = self.tmp / "repo"
        self.runs = self.tmp / "runs"
        self.repo.mkdir()
        git(self.repo, "init", "-q")
        (self.repo / "calc.py").write_text(CALC_V0)
        (self.repo / "test_calc.py").write_text(TEST_V0)
        (self.repo / ".gitignore").write_text("*.log\n__pycache__/\n")
        git(self.repo, "add", "--all")
        git(self.repo, "commit", "-q", "-m", "initial")
        self.base_sha = git(self.repo, "rev-parse", "HEAD").strip()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def backends(self, scenario: dict) -> dict:
        sp = self.tmp / f"scenario-{len(list(self.tmp.glob('scenario-*')))}.json"
        sp.write_text(json.dumps(scenario))
        env = {"AIDEV_MOCK_SCENARIO": str(sp)}
        claude = CommandBackend("mock-claude", [PYTHON, "-I", str(MOCK), "--role", "claude"], env)
        codex = CommandBackend("mock-codex", [PYTHON, "-I", str(MOCK), "--role", "codex"], env)
        return {"planner": claude, "implementer": codex, "reviewer": claude}

    def run_task(self, scenario: dict, **task_over) -> tuple[dict, Orchestrator]:
        orch = Orchestrator(task=task(**task_over), repo=self.repo, runs_root=self.runs,
                            **self.backends(scenario))
        return orch.run(), orch

    @staticmethod
    def events(orch: Orchestrator) -> list[dict]:
        return [json.loads(l) for l in (orch.run_dir / "events.jsonl").read_text().splitlines()]

    @staticmethod
    def mock_calls(orch: Orchestrator) -> list[dict]:
        p = orch.run_dir / "home" / "mock-calls.jsonl"
        return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []

    def assertFinalConsistent(self, orch: Orchestrator, result: dict, state: str):
        """result.json, journal and report all tell the same story; no false success."""
        self.assertEqual(result["state"], state, result["detail"])
        self.assertEqual(result["success"], state == "SUCCEEDED")
        on_disk = json.loads((orch.run_dir / "result.json").read_text())
        self.assertEqual(on_disk["state"], state)
        last = self.events(orch)[-1]
        self.assertEqual(last["event"], "task_finished")
        self.assertEqual(last["data"]["state"], state)
        self.assertEqual(last["data"]["success"], state == "SUCCEEDED")
        report = (orch.run_dir / "report.md").read_text()
        self.assertIn(f"`{state}`", report)
        self.assertIn("NOT SUCCESSFUL" if state != "SUCCEEDED" else "**Outcome: SUCCEEDED**", report)
        self.assertFalse(result["merged"])
        self.assertFalse(result["pushed"])


def pid_alive(pid: int) -> bool:
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[-1].split()[0]
    except OSError:
        return False
    return state != "Z"
