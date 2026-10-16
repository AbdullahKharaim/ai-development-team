"""Adaptive collaboration and model-selection policy, with deterministic mock agents."""

import json
import unittest

from aidev import model_policy as mp
from aidev.journal import verify_journal
from aidev.orchestrator import Orchestrator, PreflightError

from helpers import CALC_GOOD, CALC_V0, GOOD_IMPL, TEST_V1, OrchestratorTestCase, git, task

POLICY = {"model": "gpt-6.1-sol", "effort_by_complexity": {"simple": "medium", "normal": "high",
                                                            "complex": "xhigh"},
          "allowed_models": ["gpt-6.1-sol"], "allowed_efforts": ["medium", "high", "xhigh"]}
CATALOG = {"gpt-6.1-sol": ["low", "medium", "high", "xhigh", "max", "ultra"]}


def calc(body):  # a distinct, still-wrong multiply for each revision
    return CALC_V0 + f"\n\ndef multiply(a, b):\n    return {body}\n"


def impl(body):
    return {"write_files": {"calc.py": calc(body), "test_calc.py": TEST_V1}}


def changes(fid, text, severity="high"):
    return {"override": {"verdict": "changes_requested", "summary": text,
                         "findings": [{"id": fid, "severity": severity, "description": text}]}}


class AdaptiveCycles(OrchestratorTestCase):
    def test_single_review_approval_needs_no_extra_cycle(self):
        result, orch = self.run_task({"implement": GOOD_IMPL})
        self.assertFinalConsistent(orch, result, "SUCCEEDED")
        self.assertEqual((len(result["cycles"]), result["model_calls"]), (1, 3))
        self.assertEqual([c["stage"] for c in self.mock_calls(orch)], ["plan", "implement", "review"])

    def test_four_genuine_review_fix_cycles_until_approval(self):
        scenario = {"implement": impl("a + b"), "review:1": changes("R1", "multiply adds"),
                    "fix:2": impl("a - b"), "review:2": changes("R2", "multiply subtracts"),
                    "fix:3": impl("a ** b"), "review:3": changes("R3", "multiply exponentiates"),
                    "fix:4": GOOD_IMPL}
        result, orch = self.run_task(scenario, max_cycles=4)
        self.assertFinalConsistent(orch, result, "SUCCEEDED")
        self.assertEqual(len(result["cycles"]), 4)
        self.assertEqual(result["model_calls"], 1 + 4 * 2)
        stages = [c["stage"] for c in self.mock_calls(orch)]
        self.assertEqual(stages, ["plan", "implement", "review"] + ["fix", "review"] * 3)
        # SHA binding after every revision, and a linear commit chain on the task branch.
        prev = self.base_sha
        for c in result["cycles"]:
            self.assertEqual(c["review"]["reviewed_sha"], c["commit"])
            self.assertEqual(git(self.repo, "rev-parse", f"{c['commit']}^").strip(), prev)
            prev = c["commit"]
        self.assertEqual(git(self.repo, "rev-parse", "aidev/t-multiply").strip(), prev)
        self.assertEqual(git(self.repo, "rev-parse", "main").strip(), self.base_sha)
        reviewed = [c["commit_sha"] for c in self.mock_calls(orch) if c["stage"] == "review"]
        self.assertEqual(reviewed, [c["commit"] for c in result["cycles"]])
        fixes = [c for c in self.mock_calls(orch) if c["stage"] == "fix"]
        self.assertTrue(all(c["n_findings"] == 1 for c in fixes))
        self.assertTrue(verify_journal(orch.run_dir / "events.jsonl")[0])

    def test_stale_review_in_a_later_cycle_is_rejected(self):
        scenario = {"implement": impl("a + b"), "review:1": changes("R1", "adds"),
                    "fix:2": impl("a - b"), "review:2": changes("R2", "subtracts"),
                    "fix:3": GOOD_IMPL, "review:3": {"reviewed_sha": "commit_parent"}}
        result, orch = self.run_task(scenario, max_cycles=4)
        self.assertFinalConsistent(orch, result, "STALE_REVIEW")
        self.assertEqual(len(result["cycles"]), 2)

    def test_identical_findings_twice_in_a_row_stall(self):
        same = changes("R1", "multiply() adds instead of multiplying")
        result, orch = self.run_task({"implement": impl("a + b"), "fix": impl("a - b"), "review": same},
                                     max_cycles=6)
        self.assertFinalConsistent(orch, result, "STALLED")
        self.assertEqual(len(result["cycles"]), 2)        # stopped long before the budget of 6
        self.assertEqual(result["detail_data"]["repeated_findings"],
                         ["high:multiply adds instead of multiplying"])

    def test_finding_recurring_across_reviews_stalls(self):
        a = {"id": "A", "severity": "high", "description": "Missing input validation"}
        b = {"id": "B", "severity": "low", "description": "Docstring typo"}
        c = {"id": "C", "severity": "medium", "description": "Edge case for zero"}
        rv = lambda *fs: {"override": {"verdict": "changes_requested", "summary": "x", "findings": list(fs)}}
        scenario = {"implement": impl("a + b"), "review:1": rv(a), "fix:2": impl("a - b"),
                    "review:2": rv(a, b), "fix:3": impl("a ** b"), "review:3": rv(dict(a, id="A2"), c)}
        result, orch = self.run_task(scenario, max_cycles=6, stall_repeat_limit=3)
        self.assertFinalConsistent(orch, result, "STALLED")
        self.assertEqual(len(result["cycles"]), 3)
        self.assertEqual(result["detail_data"]["repeated_findings"], ["high:missing input validation"])

    def test_model_call_budget_stops_before_an_unreviewable_commit(self):
        scenario = {"implement": impl("a + b"), "review": changes("R1", "adds")}
        result, orch = self.run_task(scenario, max_cycles=5, max_model_calls=4)
        self.assertFinalConsistent(orch, result, "BUDGET_EXHAUSTED")
        self.assertEqual(result["detail_data"]["budget"], "model_calls")
        self.assertEqual((result["model_calls"], len(result["cycles"])), (3, 1))
        self.assertNotIn("fix", [c["stage"] for c in self.mock_calls(orch)])

    def test_default_model_call_budget_is_derived(self):
        result, _ = self.run_task({"implement": GOOD_IMPL})
        self.assertEqual(result["budget"]["max_model_calls"], 1 + 2 * 2 + 2 * 1)


class Clarification(OrchestratorTestCase):
    ASK = {"override": {"status": "needs_clarification", "summary": "ambiguous",
                        "questions": ["Should multiply accept floats?"]}}

    def test_clarification_replan_then_implementation(self):
        result, orch = self.run_task({"implement:1:1": self.ASK, "implement:1:2": GOOD_IMPL})
        self.assertFinalConsistent(orch, result, "SUCCEEDED")
        calls = self.mock_calls(orch)
        self.assertEqual([c["stage"] for c in calls], ["plan", "implement", "replan", "implement", "review"])
        self.assertEqual(calls[2]["questions"], ["Should multiply accept floats?"])
        self.assertTrue(calls[3]["plan_summary"].startswith("Mock replan"))
        self.assertEqual((result["clarifications"], result["model_calls"], len(result["cycles"])), (1, 5, 1))

    def test_clarification_limit_blocks(self):
        result, orch = self.run_task({"implement": self.ASK}, max_clarifications=1)
        self.assertFinalConsistent(orch, result, "BLOCKED")
        self.assertEqual(result["detail_data"]["by"], "implementer")
        self.assertEqual([c["stage"] for c in self.mock_calls(orch)],
                         ["plan", "implement", "replan", "implement"])

    def test_no_clarifications_allowed(self):
        result, orch = self.run_task({"implement": self.ASK}, max_clarifications=0)
        self.assertFinalConsistent(orch, result, "BLOCKED")
        self.assertNotIn("replan", [c["stage"] for c in self.mock_calls(orch)])

    def test_clarification_without_questions_is_invalid(self):
        bad = {"override": {"status": "needs_clarification", "summary": "?"}}
        result, orch = self.run_task({"implement": bad})
        self.assertFinalConsistent(orch, result, "INVALID_RESPONSE")

    def test_replan_must_be_read_only(self):
        scenario = {"implement:1:1": self.ASK, "replan": {"write_files": {"x.txt": "no"}}}
        result, orch = self.run_task(scenario)
        self.assertFinalConsistent(orch, result, "UNAUTHORIZED_MODIFICATION")


class Continuation(OrchestratorTestCase):
    def test_human_extension_continues_from_last_reviewed_commit(self):
        first, orch1 = self.run_task({"implement": impl("a + b"), "review": changes("R1", "adds")},
                                     max_cycles=1)
        self.assertEqual(first["state"], "BUDGET_EXHAUSTED")
        result, orch2 = self.run_task({"fix": GOOD_IMPL}, task_id="t-multiply-ext",
                                      continue_from=str(orch1.run_dir), max_cycles=2)
        self.assertFinalConsistent(orch2, result, "SUCCEEDED")
        self.assertEqual(result["continued_from"], "t-multiply")
        calls = self.mock_calls(orch2)
        self.assertEqual([c["stage"] for c in calls], ["fix", "review"])      # no new plan
        self.assertEqual(calls[0]["n_findings"], 1)                           # carried findings
        new = result["cycles"][0]["commit"]
        self.assertEqual(git(self.repo, "rev-parse", f"{new}^").strip(), first["final_commit"])
        self.assertEqual(git(self.repo, "rev-parse", "main").strip(), self.base_sha)
        self.assertEqual(git(self.repo, "rev-parse", "aidev/t-multiply").strip(), first["final_commit"])

    def test_continuation_refused_for_success_or_tampered_evidence(self):
        ok, orch1 = self.run_task({"implement": GOOD_IMPL})
        result, _ = self.run_task({}, task_id="t-ext-a", continue_from=str(orch1.run_dir))
        self.assertEqual(result["state"], "FAILED_PREFLIGHT")
        self.assertIn("not continuable", result["detail"])

        _, orch2 = self.run_task({"implement": impl("a + b"), "review": changes("R1", "adds")},
                                 task_id="t-b", max_cycles=1)
        journal = orch2.run_dir / "events.jsonl"
        journal.write_text(journal.read_text().replace("preflight_ok", "preflight_OK"))
        result, _ = self.run_task({}, task_id="t-ext-b", continue_from=str(orch2.run_dir))
        self.assertEqual(result["state"], "FAILED_PREFLIGHT")
        self.assertIn("journal", result["detail"])

    def test_continuation_refused_when_previous_branch_moved(self):
        _, orch1 = self.run_task({"implement": impl("a + b"), "review": changes("R1", "adds")},
                                 max_cycles=1)
        git(self.repo, "update-ref", "refs/heads/aidev/t-multiply", self.base_sha)  # someone moved it
        result, _ = self.run_task({}, task_id="t-ext", continue_from=str(orch1.run_dir))
        self.assertEqual(result["state"], "FAILED_PREFLIGHT")
        self.assertIn("final reviewed commit", result["detail"])


class ModelPolicy(OrchestratorTestCase):
    def run_policy(self, scenario, catalog=CATALOG, **over):
        orch = Orchestrator(task=task(**over), repo=self.repo, runs_root=self.runs,
                            **self.backends(scenario), model_policy=POLICY, model_catalog=catalog)
        return orch.run(), orch

    def test_selection_reaches_implementer_and_is_journalled(self):
        result, orch = self.run_policy({"implement": GOOD_IMPL})
        self.assertFinalConsistent(orch, result, "SUCCEEDED")
        sel = self.mock_calls(orch)[1]["model_selection"]
        self.assertEqual((sel["model"], sel["effort"], sel["complexity"]), ("gpt-6.1-sol", "high", "normal"))
        self.assertEqual(result["cycles"][0]["model_selection"]["effort"], "high")

    def test_planner_raises_complexity_to_xhigh(self):
        plan = {"override": {"complexity": "complex", "complexity_rationale": "large refactor"}}
        result, orch = self.run_policy({"plan": plan, "implement": GOOD_IMPL})
        self.assertEqual(self.mock_calls(orch)[1]["model_selection"]["effort"], "xhigh")

    def test_planner_raise_to_unsupported_effort_stops_before_implementation(self):
        plan = {"override": {"complexity": "complex", "complexity_rationale": "hard"}}
        result, orch = self.run_policy({"plan": plan}, catalog={"gpt-6.1-sol": ["medium", "high"]})
        self.assertFinalConsistent(orch, result, "MODEL_UNSUPPORTED")
        self.assertEqual([c["stage"] for c in self.mock_calls(orch)], ["plan"])

    def test_unsupported_selection_refused_before_any_call(self):
        for catalog in ({"gpt-6.1-sol": ["low", "medium"]}, {"gpt-other": ["high"]}):
            with self.assertRaises(PreflightError):
                Orchestrator(task=task(), repo=self.repo, runs_root=self.runs, **self.backends({}),
                             model_policy=POLICY, model_catalog=catalog)
        self.assertFalse(self.runs.exists())


class PolicyUnit(unittest.TestCase):
    def test_select_rules(self):
        s = mp.select(POLICY, "normal")
        self.assertEqual((s.model, s.effort, s.complexity_source), ("gpt-6.1-sol", "high", "task"))
        s = mp.select(POLICY, "normal", "complex", "why")
        self.assertEqual((s.effort, s.complexity_source), ("xhigh", "planner-raised"))
        s = mp.select(POLICY, "complex", "simple")
        self.assertEqual((s.effort, s.complexity), ("xhigh", "complex"))     # never downgraded
        self.assertEqual(mp.select(POLICY, "simple").effort, "medium")

    def test_validate_and_load(self):
        mp.validate(mp.select(POLICY, "complex"), POLICY, CATALOG)
        with self.assertRaises(mp.ModelPolicyError):
            mp.validate(mp.select(POLICY, "complex"), POLICY, {"gpt-6.1-sol": ["high"]})
        bad = [dict(POLICY, model="gpt-x"), dict(POLICY, allowed_efforts=["high"]),
               dict(POLICY, effort_by_complexity={"simple": "xhigh", "normal": "high", "complex": "xhigh"}),
               dict(POLICY, effort_by_complexity={"simple": "medium", "normal": "turbo", "complex": "xhigh"}),
               {k: v for k, v in POLICY.items() if k != "allowed_models"}]
        for p in bad:
            with self.assertRaises(mp.ModelPolicyError, msg=json.dumps(p)):
                mp.load_policy(p)


if __name__ == "__main__":
    unittest.main()
