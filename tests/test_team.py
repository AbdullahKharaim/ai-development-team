"""Tests for live/team.py (the /ai-team skill entry point) with MOCK agents only.

The live start path (readiness + run-live) is deliberately never executed here:
every start uses --mock-agents, so no real model can be launched by this suite.
"""

import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from helpers import CALC_GOOD, CALC_V0, M1_ROOT, MOCK, WORK

TEAM = M1_ROOT / "live" / "team.py"
TEST_PKG = {"tests/__init__.py": "",
            "tests/test_calc.py": "import unittest\nfrom calc import add, multiply\n\n\n"
                                  "class T(unittest.TestCase):\n    def test_ops(self):\n"
                                  "        self.assertEqual((add(2, 3), multiply(3, 4)), (5, 12))\n"}
ACCEPTANCE = ("import unittest\nfrom calc import multiply\n\n\nclass A(unittest.TestCase):\n"
              "    def test_multiply(self):\n        self.assertEqual(multiply(6, 7), 42)\n")


class TeamCase(unittest.TestCase):
    def setUp(self):
        WORK.mkdir(exist_ok=True)
        self.tmp = Path(tempfile.mkdtemp(prefix="team-", dir=WORK))
        self.env = {**os.environ, "AIDEV_PROJECTS": str(self.tmp / "projects")}

    def tearDown(self):
        for rec in (self.tmp / "projects" / "tasks").glob("*.json") if (self.tmp / "projects").exists() else []:
            try:
                pid = json.loads(rec.read_text()).get("pid")
                if pid:
                    os.killpg(pid, signal.SIGKILL)
            except (ValueError, ProcessLookupError, PermissionError, KeyError):
                pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def team(self, *args):
        r = subprocess.run([sys.executable, str(TEAM), *args], cwd=M1_ROOT, env=self.env,
                           capture_output=True, text=True, timeout=60)
        return r.returncode, json.loads(r.stdout) if r.stdout.strip().startswith(("{", "[")) else r.stdout

    def stage(self, slug="calc-demo", task=None, acceptance=ACCEPTANCE):
        rc, out = self.team("prepare", slug)
        self.assertEqual(rc, 0, out)
        st = Path(out["staging"])
        (st / "task.json").write_text(json.dumps(task or {
            "objective": "Add multiply(a, b) to calc.py", "acceptance_criteria": ["multiply(6, 7) == 42"],
            "complexity": "simple"}))
        (st / "repo" / "SPEC.md").write_text("# calc\nmultiply(a, b) returns a*b.\n")
        (st / "repo" / "calc.py").write_text(CALC_V0)
        if acceptance is not None:
            (st / "acceptance" / "acceptance_test_calc.py").write_text(acceptance)
        return st

    def agents(self, scenario):
        sp = self.tmp / "scenario.json"
        sp.write_text(json.dumps(scenario))
        env = {"AIDEV_MOCK_SCENARIO": str(sp)}
        role = lambda r: {"argv": ["/usr/bin/python3", "-I", str(MOCK), "--role", r], "env": env}
        path = self.tmp / "agents.json"
        path.write_text(json.dumps({"planner": role("claude"), "implementer": role("codex"),
                                    "reviewer": role("claude")}))
        return str(path)

    def wait(self, task_id, timeout=90):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            rc, st = self.team("status", task_id)
            if st["phase"] != "RUNNING":
                return st
            time.sleep(0.3)
        self.fail("task did not finish")


GOOD = {"implement": {"write_files": {"calc.py": CALC_GOOD, **TEST_PKG}}}


class StartAndStatus(TeamCase):
    def test_natural_flow_succeeds_and_status_reports_final_result(self):
        st = self.stage()
        rc, rec = self.team("start", str(st), "--mock-agents", self.agents(GOOD))
        self.assertEqual(rc, 0, rec)
        self.assertEqual(rec["mode"], "mock")
        self.assertFalse(st.exists())                         # staging consumed
        final = self.wait(rec["task_id"])
        self.assertEqual((final["phase"], final["state"], final["success"]), ("FINISHED", "SUCCEEDED", True))
        self.assertEqual(final["review_cycles"], 1)
        self.assertTrue(final["base_branch_unchanged"])
        self.assertIn("== acceptance PASSED",
                      (Path(final["run_dir"]) / "logs" / "tests-c1.stdout").read_text())
        # Hidden acceptance tests live outside the project repository.
        files = subprocess.run(["git", "-C", final["project"], "ls-files"], capture_output=True,
                               text=True).stdout.split()
        self.assertNotIn("acceptance_test_calc.py", " ".join(files))
        self.assertIn("SPEC.md", files)
        rc, latest = self.team("status")                     # no id -> latest task
        self.assertEqual(latest["task_id"], rec["task_id"])

    def test_running_task_is_not_reported_as_finished(self):
        slow = {"plan": {"sleep_s": 4}, **GOOD}
        rc, rec = self.team("start", str(self.stage()), "--mock-agents", self.agents(slow))
        self.assertEqual(rc, 0, rec)
        time.sleep(1.0)
        rc, st = self.team("status", rec["task_id"])
        self.assertEqual(st["phase"], "RUNNING")
        self.assertNotIn("state", st)
        self.assertEqual(st["current_step"]["stage"], "plan")
        self.assertTrue(st["process_alive"])
        self.assertEqual(self.wait(rec["task_id"])["state"], "SUCCEEDED")

    def test_killed_workflow_is_reported_as_died_not_finished(self):
        rc, rec = self.team("start", str(self.stage()), "--mock-agents",
                            self.agents({"plan": {"sleep_s": 30}, **GOOD}))
        time.sleep(1.0)
        os.killpg(rec["pid"], signal.SIGKILL)
        time.sleep(0.5)
        rc, st = self.team("status", rec["task_id"])
        self.assertEqual((st["phase"], st["process_alive"]), ("DIED", False))
        self.assertNotIn("state", st)
        # The unsupervised mock planner (own session) is reported, not hidden; then cleaned up here.
        orphans = st["orphan_processes"]
        self.assertTrue(any("fake_agent.py" in o["cmd"] for o in orphans), orphans)
        for o in orphans:
            try:
                os.killpg(o["pid"], signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass

    def test_interrupted_workflow_finishes_with_interrupted_state(self):
        rc, rec = self.team("start", str(self.stage()), "--mock-agents",
                            self.agents({"plan": {"sleep_s": 30}, **GOOD}))
        time.sleep(1.0)
        os.kill(rec["pid"], signal.SIGTERM)
        st = self.wait(rec["task_id"])
        self.assertEqual((st["phase"], st["state"], st["success"]), ("FINISHED", "INTERRUPTED", False))

    def test_status_is_read_only(self):
        rc, rec = self.team("start", str(self.stage()), "--mock-agents", self.agents(GOOD))
        self.wait(rec["task_id"])
        root = self.tmp / "projects"
        digest = lambda: hashlib.sha256(b"".join(
            str(p).encode() + p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file())).hexdigest()
        before = digest()
        for _ in range(3):
            self.team("status", rec["task_id"])
            self.team("list")
        self.assertEqual(digest(), before)

    def test_failed_review_budget_is_reported(self):
        changes = {"override": {"verdict": "changes_requested", "summary": "wrong",
                                "findings": [{"id": "F1", "severity": "high", "description": "wrong"}]}}
        rc, rec = self.team("start", str(self.stage(task={"objective": "x", "acceptance_criteria": ["y"],
                                                          "max_cycles": 1})),
                            "--mock-agents", self.agents({**GOOD, "review": changes}))
        st = self.wait(rec["task_id"])
        self.assertEqual((st["state"], st["success"]), ("BUDGET_EXHAUSTED", False))
        self.assertEqual(st["review_findings"][0]["id"], "F1")


class Refusals(TeamCase):
    def test_bad_slug_and_existing_project(self):
        self.assertEqual(self.team("prepare", "Bad Name")[0], 1)
        (self.tmp / "projects" / "taken").mkdir(parents=True)
        rc, out = self.team("prepare", "taken")
        self.assertEqual(rc, 1)
        self.assertIn("already exists", out["error"])

    def test_missing_acceptance_tests_or_spec_refused(self):
        st = self.stage(acceptance=None)
        rc, out = self.team("start", str(st), "--mock-agents", self.agents(GOOD))
        self.assertEqual(rc, 1)
        self.assertIn("acceptance", out["error"])
        st2 = self.stage(slug="calc-two")
        (st2 / "repo" / "SPEC.md").unlink()
        self.assertIn("SPEC.md", self.team("start", str(st2), "--mock-agents", self.agents(GOOD))[1]["error"])

    def test_unsupported_task_fields_and_foreign_dirs_refused(self):
        st = self.stage(task={"objective": "x", "acceptance_criteria": ["y"], "auto_merge": True})
        self.assertIn("unsupported", self.team("start", str(st), "--mock-agents", self.agents(GOOD))[1]["error"])
        other = self.tmp / "elsewhere"
        other.mkdir()
        self.assertIn("staging", self.team("start", str(other), "--mock-agents", self.agents(GOOD))[1]["error"])

    def test_live_limits_still_enforced_through_run_live(self):
        # The skill path uses run-live; its limit check runs before readiness/gate use.
        task = {"task_id": "x-1", "objective": "x", "acceptance_criteria": ["y"], "max_cycles": 9,
                "test_command": ["python3", "/nonexistent"]}
        (self.tmp / "t.json").write_text(json.dumps(task))
        cfg = json.loads((M1_ROOT / "live" / "live_config.json").read_text())
        (self.tmp / "r.json").write_text(json.dumps({"passed": False, "pinned": cfg}))
        r = subprocess.run([sys.executable, "-m", "aidev", "run-live", "--confirm-live", "--task",
                            str(self.tmp / "t.json"), "--repo", str(self.tmp), "--runs-dir",
                            str(self.tmp / "runs"), "--readiness", str(self.tmp / "r.json")],
                           cwd=M1_ROOT, capture_output=True, text=True)
        self.assertEqual(r.returncode, 2)
        self.assertIn("max_cycles <= 2", r.stderr)


if __name__ == "__main__":
    unittest.main()
