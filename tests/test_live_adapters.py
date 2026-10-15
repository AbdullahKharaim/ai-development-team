"""Live-adapter tests with FAKE Claude/Codex CLIs. No real CLI is executed here.

The fakes reproduce the real output formats (Claude `--output-format json`,
Codex `--json` events + `-o` final message) so the adapters, the orchestrator
integration and the live trial's trusted test command are exercised end to end.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from aidev import cli_backends as cb
from aidev.live import load_gate
from aidev.orchestrator import Orchestrator
from aidev.redact import redact

from helpers import M1_ROOT, WORK, pid_alive

FAKES = M1_ROOT / "tests" / "fakes"
TRIAL = M1_ROOT / "live" / "trial"
PY = "/usr/bin/python3"
GATE = cb.LiveGate("test", "test", "test")


class LiveAdapterCase(unittest.TestCase):
    def setUp(self):
        WORK.mkdir(exist_ok=True)
        self.tmp = Path(tempfile.mkdtemp(prefix="live-", dir=WORK))
        self.home = self.tmp / "fakehome"   # the backends' "real HOME" for the fakes
        self.home.mkdir()
        self.repo = self.tmp / "repo"
        subprocess.run([PY, str(TRIAL / "create_trial_repo.py"), "--dest", str(self.repo)],
                       check=True, capture_output=True)
        self.task = json.loads((TRIAL / "task.json").read_text())
        self.task.update(stage_timeout_s=60, task_timeout_s=300,
                         test_command=["python3", str(TRIAL / "run_tests.py")])

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def backends(self, scenario: dict, claude_cmd=None, codex_cmd=None):
        (self.home / "scenario.json").write_text(json.dumps(scenario))
        claude = cb.ClaudeCodeBackend(claude_cmd or [PY, "-I", str(FAKES / "fake_claude_cli.py")],
                                      GATE, real_home=str(self.home))
        codex = cb.CodexExecBackend(codex_cmd or [PY, "-I", str(FAKES / "fake_codex_cli.py")],
                                    GATE, real_home=str(self.home))
        return {"planner": claude, "implementer": codex, "reviewer": claude}

    def run_task(self, scenario, **task_over):
        task = {**self.task, **task_over}
        orch = Orchestrator(task=task, repo=self.repo, runs_root=self.tmp / "runs",
                            **self.backends(scenario), test_sandbox=True)
        return orch.run(), orch

    def calls(self):
        p = self.home / "calls.jsonl"
        return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []

    @staticmethod
    def events(orch, name=None):
        evs = [json.loads(l) for l in (orch.run_dir / "events.jsonl").read_text().splitlines()]
        return [e for e in evs if name is None or e["event"] == name]


class ArgvAndGate(unittest.TestCase):
    def test_claude_argv_is_tool_free_and_restricted(self):
        b = cb.ClaudeCodeBackend(["/x/claude"], GATE)
        for stage in ("plan", "review"):
            a = b.build_argv(stage)
            self.assertEqual(a[a.index("--tools") + 1], "")
            for flag in ("-p", "--safe-mode", "--strict-mcp-config", "--no-session-persistence",
                         "--disable-slash-commands"):
                self.assertIn(flag, a)
            self.assertEqual(a[a.index("--permission-mode") + 1], "dontAsk")
            self.assertEqual(a[a.index("--permission-prompts") + 1], "none")
            self.assertFalse(cb.CLAUDE_FORBIDDEN_FLAGS & set(a))
            schema = json.loads(a[a.index("--json-schema") + 1])
            self.assertEqual("reviewed_sha" in schema["properties"], stage == "review")

    def test_codex_argv_uses_verified_workspace_profile(self):
        a = cb.CodexExecBackend(["/x/codex"], GATE).build_argv(Path("/w"), Path("/s.json"), Path("/o.json"))
        self.assertEqual(a[1], "exec")
        for flag in ("--strict-config", "--ignore-user-config", "--ignore-rules", "--ephemeral", "--json"):
            self.assertIn(flag, a)
        self.assertIn('default_permissions=":workspace"', a)
        self.assertIn('approval_policy="never"', a)
        self.assertEqual(a[a.index("-C") + 1], "/w")
        self.assertEqual(a[-1], "-")
        self.assertFalse(cb.CODEX_FORBIDDEN_FLAGS & set(a))
        self.assertNotIn("--sandbox", a)  # the profile above is what was probed

    def test_backends_require_gate(self):
        with self.assertRaises(cb.LiveGateError):
            cb.ClaudeCodeBackend(["/x/claude"], None)
        with self.assertRaises(cb.LiveGateError):
            cb.CodexExecBackend(["/x/codex"], None)


class EndToEndWithFakes(LiveAdapterCase):
    def test_success_single_cycle(self):
        result, orch = self.run_task({})
        self.assertEqual(result["state"], "SUCCEEDED", result["detail"])
        sha = result["final_commit"]
        self.assertEqual(result["cycles"][0]["review"]["reviewed_sha"], sha)
        tests_log = (orch.run_dir / "logs" / "tests-c1.stdout").read_text()
        self.assertIn("== acceptance PASSED", tests_log)
        calls = self.calls()
        self.assertEqual([(c["tool"], c["stage"]) for c in calls],
                         [("claude", "plan"), ("codex", "implement"), ("claude", "review")])
        plan, impl, review = calls
        self.assertIn("----- FILE: durations.py -----", plan["prompt"])      # context supplied
        self.assertIn(f"COMMIT_SHA: {sha}", review["prompt"])
        self.assertIn("def parse_duration", review["prompt"])                 # diff + files at commit
        self.assertIn("passed=True", review["prompt"])
        self.assertEqual(impl["cwd"], str(orch.worktree))
        for c in (plan, review):                                              # Claude: empty scratch dir
            self.assertNotEqual(c["cwd"], str(orch.worktree))
            self.assertEqual(os.listdir(c["cwd"]), [])
        metas = self.events(orch, "backend_meta")
        self.assertEqual([m["data"]["backend"] for m in metas], ["claude-code", "codex-exec", "claude-code"])
        self.assertEqual(metas[1]["data"]["usage"]["output_tokens"], 300)
        self.assertGreater(metas[0]["data"]["input_tokens"], 0)

    def test_changes_requested_then_fixed(self):
        finding = {"id": "R1", "severity": "high", "description": "accepts repeated/out-of-order units"}
        result, orch = self.run_task({"implement": {"impl": "buggy"},
                                      "review:1": {"verdict": "changes_requested", "findings": [finding]},
                                      "fix": {"impl": "good"}})
        self.assertEqual(result["state"], "SUCCEEDED", result["detail"])
        c1, c2 = result["cycles"]
        self.assertFalse(c1["tests"]["passed"])
        self.assertTrue(c2["tests"]["passed"])
        fix_call = [c for c in self.calls() if c["stage"] == "fix"][0]
        self.assertIn("accepts repeated/out-of-order units", fix_call["prompt"])
        self.assertIn(c1["commit"], fix_call["prompt"])

    def test_wrong_sha_is_stale_review(self):
        result, _ = self.run_task({"review": {"wrong_sha": True}})
        self.assertEqual(result["state"], "STALE_REVIEW")

    def test_claude_error_result(self):
        result, orch = self.run_task({"plan": {"is_error": True}})
        self.assertEqual(result["state"], "INVALID_RESPONSE")
        self.assertIn("is_error=True", self.events(orch, "backend_meta")[0]["data"]["adapter_error"])

    def test_claude_nonzero_exit(self):
        result, _ = self.run_task({"plan": {"is_error": True, "exit_code": 1}})
        self.assertEqual(result["state"], "AGENT_FAILED")

    def test_claude_non_json(self):
        result, _ = self.run_task({"plan": {"raw": "I could not comply"}})
        self.assertEqual(result["state"], "INVALID_RESPONSE")

    def test_claude_tool_use_detected_via_scratch_dir(self):
        result, orch = self.run_task({"plan": {"write_scratch": True}})
        self.assertEqual(result["state"], "INVALID_RESPONSE")
        meta = self.events(orch, "backend_meta")[0]["data"]
        self.assertEqual(meta["scratch_modified"], ["notes.txt"])

    def test_permission_denials_recorded(self):
        result, orch = self.run_task({"plan": {"permission_denials": 2}})
        self.assertEqual(result["state"], "SUCCEEDED")
        meta = self.events(orch, "backend_meta")[0]["data"]
        self.assertEqual(meta["permission_denials"], 2)
        self.assertIn("warning", meta)

    def test_codex_without_final_message(self):
        result, orch = self.run_task({"implement": {"no_last_message": True}})
        self.assertEqual(result["state"], "INVALID_RESPONSE")
        self.assertIn("-o file missing", self.events(orch, "backend_meta")[1]["data"]["adapter_error"])

    def test_codex_reports_failure(self):
        result, _ = self.run_task({"implement": {"impl": "none", "status": "failed"}})
        self.assertEqual(result["state"], "FAILED_IMPLEMENTATION")

    def test_codex_self_commit_detected(self):
        result, _ = self.run_task({"implement": {"git_commit": True}})
        self.assertEqual(result["state"], "UNAUTHORIZED_MODIFICATION")

    def test_credentials_redacted_in_logs(self):
        result, orch = self.run_task({"plan": {"leak": True}, "implement": {"leak": True}})
        self.assertEqual(result["state"], "SUCCEEDED")
        logs = b"".join(p.read_bytes() for p in (orch.run_dir / "logs").iterdir())
        for secret in (b"sk-ant-oat01-ABCDEFGHIJKLMNOPQRSTUV", b"person@example.com",
                       b"abcdefghijklmnopqrstuvwxyz0123", b"eyJhbGciOiJIUzI1NiJ9"):
            self.assertNotIn(secret, logs)
        self.assertIn(b"[REDACTED]", logs)

    def test_timeout_kills_cli_and_descendants(self):
        result, orch = self.run_task({"plan": {"sleep_s": 60, "spawn_orphan": True}}, stage_timeout_s=2)
        self.assertEqual(result["state"], "STAGE_TIMEOUT")
        pid = int((self.home / "orphan-plan-1.pid").read_text())
        self.assertFalse(pid_alive(pid))

    def test_source_repo_untouched(self):
        head = subprocess.run(["git", "-C", str(self.repo), "rev-parse", "main"], capture_output=True,
                              text=True).stdout
        status_before = subprocess.run(["git", "-C", str(self.repo), "status", "--porcelain"],
                                       capture_output=True, text=True).stdout
        result, _ = self.run_task({})
        self.assertEqual(result["state"], "SUCCEEDED")
        self.assertEqual(subprocess.run(["git", "-C", str(self.repo), "rev-parse", "main"],
                                        capture_output=True, text=True).stdout, head)
        self.assertEqual(subprocess.run(["git", "-C", str(self.repo), "status", "--porcelain"],
                                        capture_output=True, text=True).stdout, status_before)


class M1GuardStillBlocksRealClients(LiveAdapterCase):
    """Until the owner approves live/PROPOSED-GUARD-CHANGE.patch, executables named
    claude/codex are refused before spawn even through the live backends."""

    def test_live_backends_with_client_named_executables_are_refused(self):
        marker = self.tmp / "EXECUTED"
        for name in ("claude", "codex"):
            p = self.tmp / name
            p.write_text(f"#!/bin/sh\ntouch {marker}\n")
            p.chmod(0o755)
        b = self.backends({}, claude_cmd=[str(self.tmp / "claude")], codex_cmd=[str(self.tmp / "codex")])
        orch = Orchestrator(task=self.task, repo=self.repo, runs_root=self.tmp / "runs", **b,
                            test_sandbox=True)
        result = orch.run()
        self.assertEqual(result["state"], "FORBIDDEN_EXECUTABLE")
        self.assertFalse(marker.exists())


class Gate(unittest.TestCase):
    def setUp(self):
        WORK.mkdir(exist_ok=True)
        self.tmp = Path(tempfile.mkdtemp(prefix="gate-", dir=WORK))
        vdir = self.tmp / "versions"
        vdir.mkdir()
        (vdir / "2.1.300").write_text("x")
        rel = self.tmp / "rel" / "0.162.1-x86_64" / "bin"
        rel.mkdir(parents=True)
        (rel / "codex").write_text("x")
        (self.tmp / "claude-link").symlink_to(vdir / "2.1.300")
        (self.tmp / "codex-link").symlink_to(rel / "codex")
        self.base = {"passed": True, "generated_epoch": int(time.time()),
                     "pinned": {"claude_version": "2.1.300", "codex_version": "0.162.1",
                                "readiness_max_age_s": 3600},
                     "executables": {"claude": str(self.tmp / "claude-link"),
                                     "codex": str(self.tmp / "codex-link"),
                                     "claude_real": str(vdir / "2.1.300"),
                                     "codex_real": str(rel / "codex")}}

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _load(self, rep):
        p = self.tmp / "report.json"
        p.write_text(json.dumps(rep))
        return load_gate(p)

    def test_good_report(self):
        g = self._load(self.base)
        self.assertEqual(g.claude, str(self.tmp / "claude-link"))

    def test_failed_stale_or_changed_reports_refused(self):
        bad = [dict(self.base, passed=False), dict(self.base, generated_epoch=0)]
        changed = json.loads(json.dumps(self.base))
        changed["executables"]["codex_real"] = "/elsewhere/codex"
        bad.append(changed)
        for rep in bad:
            with self.assertRaises(cb.LiveGateError):
                self._load(rep)


class RunLiveCommand(unittest.TestCase):
    def test_refuses_without_confirmation_and_with_excess_limits(self):
        WORK.mkdir(exist_ok=True)
        tmp = Path(tempfile.mkdtemp(prefix="cli-", dir=WORK))
        try:
            task = json.loads((TRIAL / "task.json").read_text())
            (tmp / "t.json").write_text(json.dumps(task))
            (tmp / "t2.json").write_text(json.dumps({**task, "stage_timeout_s": 1800}))
            base = [sys.executable, "-m", "aidev", "run-live", "--repo", str(tmp), "--runs-dir",
                    str(tmp / "runs"), "--readiness", str(tmp / "missing.json")]
            r = subprocess.run(base + ["--task", str(tmp / "t.json")], cwd=M1_ROOT,
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 2)
            self.assertIn("--confirm-live", r.stderr)
            r = subprocess.run(base + ["--task", str(tmp / "t2.json"), "--confirm-live"], cwd=M1_ROOT,
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 2)
            self.assertIn("stage_timeout_s <= 900", r.stderr)
            self.assertFalse((tmp / "runs").exists())
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class Redaction(unittest.TestCase):
    def test_patterns(self):
        raw = (b'{"access_token": "abc123def456ghi"} sk-proj-AAAAAAAAAAAAAAAAAAAAAAAA '
               b"Bearer zzzzzzzzzzzzzzzz a.b@c.io keep-this")
        out = redact(raw)
        for s in (b"abc123def456ghi", b"sk-proj-AAAA", b"zzzzzzzzzzzzzzzz", b"a.b@c.io"):
            self.assertNotIn(s, out)
        self.assertIn(b"keep-this", out)


if __name__ == "__main__":
    unittest.main()
