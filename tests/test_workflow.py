"""End-to-end orchestrator behaviour against mock agents and disposable repos."""

import json
import signal
import subprocess
import sys
import time
import unittest

from aidev.journal import verify_journal
from aidev.orchestrator import Orchestrator, PreflightError

from helpers import (BUGGY_IMPL, CHANGES, GOOD_IMPL, M1_ROOT, OrchestratorTestCase, git,
                     pid_alive, repo_fingerprint, task)


class HappyPath(OrchestratorTestCase):
    def test_plan_implement_test_review_succeeds(self):
        before = repo_fingerprint(self.repo)
        result, orch = self.run_task({"implement": GOOD_IMPL})
        self.assertFinalConsistent(orch, result, "SUCCEEDED")

        self.assertEqual([c["stage"] for c in self.mock_calls(orch)], ["plan", "implement", "review"])
        (cycle,) = result["cycles"]
        sha = cycle["commit"]
        self.assertEqual(cycle["review"]["reviewed_sha"], sha)
        self.assertTrue(cycle["tests"]["passed"])
        self.assertEqual(result["final_commit"], sha)
        # The reviewer was sent exactly the commit that was created.
        self.assertEqual(self.mock_calls(orch)[2]["commit_sha"], sha)

        # Git: task branch holds the commit, parent is the base, base branch untouched.
        self.assertEqual(git(self.repo, "rev-parse", "aidev/t-multiply").strip(), sha)
        self.assertEqual(git(self.repo, "rev-parse", f"{sha}^").strip(), self.base_sha)
        self.assertTrue(result["base_branch_unchanged"])
        self.assertEqual(repo_fingerprint(self.repo), before)
        self.assertEqual(sorted(git(self.repo, "diff", "--name-only", self.base_sha, sha).split()),
                         ["calc.py", "test_calc.py"])

        ok, msg = verify_journal(orch.run_dir / "events.jsonl")
        self.assertTrue(ok, msg)
        names = [e["event"] for e in self.events(orch)]
        self.assertEqual(names[:3], ["task_received", "preflight_ok", "worktree_created"])
        self.assertLess(names.index("commit_created"), names.index("tests_started"))
        self.assertLess(names.index("tests_finished"), names.index("review_bound"))
        commit_ev = next(e for e in self.events(orch) if e["event"] == "commit_created")
        self.assertTrue(commit_ev["data"]["claim_matches_git"])


class RevisionCycles(OrchestratorTestCase):
    def test_changes_requested_then_fixed_in_second_cycle(self):
        result, orch = self.run_task({"implement": BUGGY_IMPL, "review:1": CHANGES,
                                      "fix:2": GOOD_IMPL})
        self.assertFinalConsistent(orch, result, "SUCCEEDED")
        c1, c2 = result["cycles"]
        self.assertFalse(c1["tests"]["passed"])
        self.assertEqual(c1["review"]["verdict"], "changes_requested")
        self.assertTrue(c2["tests"]["passed"])
        self.assertEqual(c2["review"]["reviewed_sha"], c2["commit"])
        self.assertEqual(git(self.repo, "rev-parse", f"{c2['commit']}^").strip(), c1["commit"])
        calls = self.mock_calls(orch)
        self.assertEqual([c["stage"] for c in calls], ["plan", "implement", "review", "fix", "review"])
        self.assertEqual(calls[3]["n_findings"], 1)  # findings were handed to the fixer

    def test_review_cycle_budget_exhausted_stops_without_success(self):
        fix = {"write_files": {"calc.py": CALC_ALT}}
        other = {"override": {"verdict": "changes_requested", "summary": "still wrong",
                              "findings": [{"id": "F2", "severity": "high",
                                            "description": "multiply() now subtracts"}]}}
        result, orch = self.run_task({"implement": BUGGY_IMPL, "review:1": CHANGES, "review:2": other,
                                      "fix": fix})
        self.assertFinalConsistent(orch, result, "BUDGET_EXHAUSTED")
        self.assertEqual(result["detail_data"]["budget"], "review_cycles")
        self.assertEqual(len(result["cycles"]), 2)
        self.assertEqual(len([c for c in self.mock_calls(orch) if c["stage"] in ("implement", "fix")]), 2)

    def test_max_cycles_one_means_no_fix_attempt(self):
        result, orch = self.run_task({"implement": BUGGY_IMPL, "review": CHANGES}, max_cycles=1)
        self.assertFinalConsistent(orch, result, "BUDGET_EXHAUSTED")
        self.assertNotIn("fix", [c["stage"] for c in self.mock_calls(orch)])

    def test_reviewer_block_escalates(self):
        blocked = {"override": {"verdict": "blocked", "summary": "needs a human decision"}}
        result, orch = self.run_task({"implement": GOOD_IMPL, "review": blocked})
        self.assertFinalConsistent(orch, result, "BLOCKED")
        self.assertEqual(result["detail_data"]["by"], "reviewer")


CALC_ALT = "def add(a, b):\n    return a + b\n\n\ndef multiply(a, b):\n    return a - b\n"


class InvalidResponses(OrchestratorTestCase):
    def test_non_json_plan(self):
        result, orch = self.run_task({"plan": {"raw_stdout": "Sure! Here is my plan..."}})
        self.assertFinalConsistent(orch, result, "INVALID_RESPONSE")
        self.assertEqual(result["cycles"], [])

    def test_bad_verdict_enum(self):
        result, orch = self.run_task({"implement": GOOD_IMPL,
                                      "review": {"override": {"verdict": "looks fine"}}})
        self.assertFinalConsistent(orch, result, "INVALID_RESPONSE")

    def test_response_for_wrong_task(self):
        result, orch = self.run_task({"plan": {"override": {"task_id": "other-task"}}})
        self.assertFinalConsistent(orch, result, "INVALID_RESPONSE")
        self.assertIn("expected task=t-multiply", result["detail"])

    def test_unexpected_extra_field(self):
        result, orch = self.run_task({"implement": {**GOOD_IMPL, "override": {"merge": True}}})
        self.assertFinalConsistent(orch, result, "INVALID_RESPONSE")

    def test_changes_requested_without_findings(self):
        result, orch = self.run_task({"implement": GOOD_IMPL,
                                      "review": {"override": {"verdict": "changes_requested"}}})
        self.assertFinalConsistent(orch, result, "INVALID_RESPONSE")


class Timeouts(OrchestratorTestCase):
    def test_stage_timeout(self):
        result, orch = self.run_task({"implement": {"sleep_s": 60}}, stage_timeout_s=1)
        self.assertFinalConsistent(orch, result, "STAGE_TIMEOUT")
        self.assertLess(result["duration_s"], 20)

    def test_overall_task_timeout(self):
        result, orch = self.run_task({"plan": {"sleep_s": 60}}, task_timeout_s=2, stage_timeout_s=30)
        self.assertFinalConsistent(orch, result, "TASK_TIMEOUT")
        self.assertLess(result["duration_s"], 20)

    def test_test_command_timeout_counts_as_failed_tests(self):
        slow = {"write_files": {**GOOD_IMPL["write_files"],
                                "test_slow.py": "import time, unittest\n\n\nclass S(unittest.TestCase):\n"
                                                "    def test_slow(self):\n        time.sleep(60)\n"}}
        result, orch = self.run_task({"implement": slow}, test_timeout_s=2)
        self.assertFinalConsistent(orch, result, "APPROVED_WITH_FAILING_TESTS")
        self.assertTrue(result["cycles"][0]["tests"]["timed_out"])


class ProcessCleanup(OrchestratorTestCase):
    def _orphan_pid(self, orch, stage, cycle):
        return int((orch.run_dir / "home" / f"orphan-{stage}-{cycle}.pid").read_text())

    def test_orphan_left_by_successful_agent_is_killed(self):
        result, orch = self.run_task({"implement": {**GOOD_IMPL, "spawn_orphan": True}})
        self.assertFinalConsistent(orch, result, "SUCCEEDED")
        self.assertFalse(pid_alive(self._orphan_pid(orch, "implement", 1)))
        exit_ev = next(e for e in self.events(orch) if e["event"] == "stage_process_exit"
                       and e["data"]["stage"] == "implement")
        self.assertTrue(exit_ev["data"]["group_members_killed"])

    def test_orphan_of_timed_out_agent_is_killed(self):
        result, orch = self.run_task({"plan": {"spawn_orphan": True, "sleep_s": 60}},
                                     stage_timeout_s=1)
        self.assertFinalConsistent(orch, result, "STAGE_TIMEOUT")
        self.assertFalse(pid_alive(self._orphan_pid(orch, "plan", 0)))


class FailedImplementationAndTests(OrchestratorTestCase):
    def test_implementer_reports_failure(self):
        result, orch = self.run_task({"implement": {"override": {"status": "failed"}}})
        self.assertFinalConsistent(orch, result, "FAILED_IMPLEMENTATION")

    def test_implementer_makes_no_changes(self):
        result, orch = self.run_task({})
        self.assertFinalConsistent(orch, result, "FAILED_IMPLEMENTATION")
        self.assertIn("no changes", result["detail"])

    def test_implementer_crashes(self):
        result, orch = self.run_task({"implement": {**GOOD_IMPL, "exit_code": 1}})
        self.assertFinalConsistent(orch, result, "AGENT_FAILED")
        self.assertIsNone(result["final_commit"])

    def test_failing_tests_are_never_reported_as_success(self):
        result, orch = self.run_task({"implement": BUGGY_IMPL})  # reviewer approves anyway
        self.assertFinalConsistent(orch, result, "APPROVED_WITH_FAILING_TESTS")
        self.assertFalse(result["cycles"][0]["tests"]["passed"])

    def test_tests_that_create_files_are_detected(self):
        writer = {"write_files": {**GOOD_IMPL["write_files"],
                                  "test_writer.py": "import unittest\n\n\nclass W(unittest.TestCase):\n"
                                                    "    def test_w(self):\n"
                                                    "        open('artifact.txt', 'w').write('x')\n"}}
        result, orch = self.run_task({"implement": writer})
        self.assertFinalConsistent(orch, result, "TESTS_MODIFIED_WORKTREE")


class ReviewBinding(OrchestratorTestCase):
    def test_review_of_base_commit_is_rejected(self):
        result, orch = self.run_task({"implement": GOOD_IMPL, "review": {"reviewed_sha": "parent"}})
        self.assertFinalConsistent(orch, result, "STALE_REVIEW")
        self.assertEqual(result["detail_data"]["got"], self.base_sha)

    def test_review_of_unknown_commit_is_rejected(self):
        result, orch = self.run_task({"implement": GOOD_IMPL, "review": {"reviewed_sha": "zeros"}})
        self.assertFinalConsistent(orch, result, "STALE_REVIEW")

    def test_second_cycle_review_bound_to_first_commit_is_rejected(self):
        # Reviewer re-approves the cycle-1 commit instead of the fix.
        scenario = {"implement": BUGGY_IMPL, "review:1": CHANGES, "fix:2": GOOD_IMPL,
                    "review:2": {"reviewed_sha": "commit_parent"}}
        result, orch = self.run_task(scenario)
        self.assertFinalConsistent(orch, result, "STALE_REVIEW")
        self.assertEqual(len(result["cycles"]), 1)  # cycle 2 never recorded as reviewed
        first = next(e for e in self.events(orch) if e["event"] == "commit_created")
        self.assertEqual(result["detail_data"]["got"], first["data"]["sha"])


class UnauthorizedModification(OrchestratorTestCase):
    def test_reviewer_edits_tracked_file(self):
        result, orch = self.run_task({"implement": GOOD_IMPL,
                                      "review": {"write_files": {"calc.py": "# sabotaged\n"}}})
        self.assertFinalConsistent(orch, result, "UNAUTHORIZED_MODIFICATION")

    def test_reviewer_creates_ignored_file(self):
        result, orch = self.run_task({"implement": GOOD_IMPL,
                                      "review": {"write_files": {"notes.log": "hidden\n"}}})
        self.assertFinalConsistent(orch, result, "UNAUTHORIZED_MODIFICATION")

    def test_reviewer_deletes_file(self):
        result, orch = self.run_task({"implement": GOOD_IMPL,
                                      "review": {"delete_files": ["test_calc.py"]}})
        self.assertFinalConsistent(orch, result, "UNAUTHORIZED_MODIFICATION")

    def test_planner_writes_file(self):
        result, orch = self.run_task({"plan": {"write_files": {"plan.md": "x"}}})
        self.assertFinalConsistent(orch, result, "UNAUTHORIZED_MODIFICATION")

    def test_implementer_commits_itself(self):
        result, orch = self.run_task({"implement": {**GOOD_IMPL, "git_commit": True}})
        self.assertFinalConsistent(orch, result, "UNAUTHORIZED_MODIFICATION")
        self.assertIn("moved HEAD", result["detail"])

    def test_modification_reported_even_if_response_invalid(self):
        result, orch = self.run_task({"implement": GOOD_IMPL,
                                      "review": {"write_files": {"x.py": "1"}, "raw_stdout": "??"}})
        self.assertFinalConsistent(orch, result, "UNAUTHORIZED_MODIFICATION")


class DuplicateAndConflicting(OrchestratorTestCase):
    def test_two_completion_messages_rejected(self):
        result, orch = self.run_task({"implement": GOOD_IMPL, "review": {"duplicate": True}})
        self.assertFinalConsistent(orch, result, "CONFLICTING_COMPLETION")

    def test_reused_task_id_refused_without_touching_evidence(self):
        result, orch = self.run_task({"implement": GOOD_IMPL})
        journal = (orch.run_dir / "events.jsonl").read_bytes()
        with self.assertRaises(PreflightError):
            Orchestrator(task=task(), repo=self.repo, runs_root=self.runs,
                         **self.backends({"implement": GOOD_IMPL}))
        self.assertEqual((orch.run_dir / "events.jsonl").read_bytes(), journal)

    def test_existing_task_branch_refused(self):
        git(self.repo, "branch", "aidev/t-multiply")
        result, orch = self.run_task({"implement": GOOD_IMPL})
        self.assertFinalConsistent(orch, result, "FAILED_PREFLIGHT")
        self.assertEqual(self.mock_calls(orch), [])


class Preflight(OrchestratorTestCase):
    def test_dirty_repo_refused_and_left_alone(self):
        (self.repo / "calc.py").write_text("# local edit\n")
        result, orch = self.run_task({"implement": GOOD_IMPL})
        self.assertFinalConsistent(orch, result, "FAILED_PREFLIGHT")
        self.assertEqual((self.repo / "calc.py").read_text(), "# local edit\n")
        self.assertEqual(self.mock_calls(orch), [])

    def test_invalid_task_refused_before_any_write(self):
        with self.assertRaises(PreflightError):
            Orchestrator(task=task(max_cycles=11), repo=self.repo, runs_root=self.runs,
                         **self.backends({}))
        self.assertFalse(self.runs.exists())

    def test_runs_dir_inside_repo_refused(self):
        with self.assertRaises(PreflightError):
            Orchestrator(task=task(), repo=self.repo, runs_root=self.repo / "runs",
                         **self.backends({}))


class CommandLine(OrchestratorTestCase):
    def _cli(self, scenario, task_over=None):
        sp = self.tmp / "cli-scenario.json"
        sp.write_text(json.dumps(scenario))
        agents = {r: {"argv": ["/usr/bin/python3", "-I", str(M1_ROOT / "mocks" / "fake_agent.py"),
                               "--role", "codex" if r == "implementer" else "claude"],
                      "env": {"AIDEV_MOCK_SCENARIO": str(sp)}}
                  for r in ("planner", "implementer", "reviewer")}
        (self.tmp / "agents.json").write_text(json.dumps(agents))
        (self.tmp / "task.json").write_text(json.dumps(task(**(task_over or {}))))
        return subprocess.run([sys.executable, "-m", "aidev", "run", "--task", str(self.tmp / "task.json"),
                               "--repo", str(self.repo), "--runs-dir", str(self.runs),
                               "--agents", str(self.tmp / "agents.json")],
                              cwd=M1_ROOT, capture_output=True, text=True, timeout=120)

    def test_exit_codes_and_journal_verification(self):
        ok = self._cli({"implement": GOOD_IMPL})
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.assertTrue(ok.stdout.startswith("SUCCEEDED"))
        journal = self.runs / "t-multiply" / "events.jsonl"
        v = subprocess.run([sys.executable, "-m", "aidev", "verify-journal", str(journal)],
                           cwd=M1_ROOT, capture_output=True, text=True)
        self.assertEqual(v.returncode, 0, v.stdout)

        # Tampering with any line breaks the chain.
        lines = journal.read_text().splitlines(True)
        lines[2] = lines[2].replace("worktree_created", "worktree_createX")
        journal.write_text("".join(lines))
        v = subprocess.run([sys.executable, "-m", "aidev", "verify-journal", str(journal)],
                           cwd=M1_ROOT, capture_output=True, text=True)
        self.assertEqual(v.returncode, 1)
        self.assertIn("BROKEN", v.stdout)

    def test_sigterm_interrupts_cleanly(self):
        sp = self.tmp / "cli-scenario.json"
        sp.write_text(json.dumps({"plan": {"spawn_orphan": True, "sleep_s": 60}}))
        agents = {r: {"argv": ["/usr/bin/python3", "-I", str(M1_ROOT / "mocks" / "fake_agent.py"),
                               "--role", "codex" if r == "implementer" else "claude"],
                      "env": {"AIDEV_MOCK_SCENARIO": str(sp)}}
                  for r in ("planner", "implementer", "reviewer")}
        (self.tmp / "agents.json").write_text(json.dumps(agents))
        (self.tmp / "task.json").write_text(json.dumps(task()))
        p = subprocess.Popen([sys.executable, "-m", "aidev", "run", "--task", str(self.tmp / "task.json"),
                              "--repo", str(self.repo), "--runs-dir", str(self.runs),
                              "--agents", str(self.tmp / "agents.json")],
                             cwd=M1_ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        pidfile = self.runs / "t-multiply" / "home" / "orphan-plan-0.pid"
        deadline = time.monotonic() + 20
        while not pidfile.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue(pidfile.exists(), "mock planner never started")
        p.send_signal(signal.SIGTERM)
        out, _ = p.communicate(timeout=30)
        self.assertEqual(p.returncode, 130, out)
        result = json.loads((self.runs / "t-multiply" / "result.json").read_text())
        self.assertEqual(result["state"], "INTERRUPTED")
        self.assertFalse(result["success"])
        self.assertFalse(pid_alive(int(pidfile.read_text())))
        self.assertTrue(verify_journal(self.runs / "t-multiply" / "events.jsonl")[0])

    def test_failure_exit_code(self):
        r = self._cli({"implement": BUGGY_IMPL, "review": CHANGES, "fix": {"write_files": {"calc.py": CALC_ALT}}})
        self.assertEqual(r.returncode, 1)
        self.assertTrue(r.stdout.startswith("STALLED"), r.stdout)

    def test_bad_agents_file_refused(self):
        sp = self.tmp / "agents.json"
        sp.write_text(json.dumps({"planner": {"argv": ["claude", "-p"]}}))
        (self.tmp / "task.json").write_text(json.dumps(task()))
        r = subprocess.run([sys.executable, "-m", "aidev", "run", "--task", str(self.tmp / "task.json"),
                            "--repo", str(self.repo), "--runs-dir", str(self.runs), "--agents", str(sp)],
                           cwd=M1_ROOT, capture_output=True, text=True)
        self.assertEqual(r.returncode, 2)
        self.assertFalse(self.runs.exists())


if __name__ == "__main__":
    unittest.main()
