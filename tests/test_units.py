"""Unit tests: process runner, real-client guard, protocol validation, journal."""

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from aidev import protocol
from aidev.backends import (ClaudeCliBackend, CodexCliBackend, CommandBackend,
                            RealBackendDisabled, backend_from_config, REAL_BACKENDS_ENABLED)
from aidev.journal import Journal, verify_journal
from aidev.orchestrator import FORBIDDEN_EXECUTABLE, Orchestrator
from aidev.proc import ForbiddenExecutable, check_not_forbidden, run_bounded

from helpers import WORK, OrchestratorTestCase, pid_alive, task

ENV = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}


class Proc(unittest.TestCase):
    def setUp(self):
        WORK.mkdir(exist_ok=True)
        self.tmp = Path(tempfile.mkdtemp(prefix="unit-", dir=WORK))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_stdin_stdout_roundtrip(self):
        r = run_bounded(["cat"], cwd=self.tmp, env=ENV, stdin=b"hello" * 50000, timeout_s=10)
        self.assertTrue(r.ok)
        self.assertEqual(r.stdout, b"hello" * 50000)

    def test_timeout_kills_whole_group(self):
        pidfile = self.tmp / "bg.pid"
        script = f"sleep 300 & echo $! > {pidfile}; sleep 300"
        r = run_bounded(["/bin/sh", "-c", script], cwd=self.tmp, env=ENV, timeout_s=1)
        self.assertTrue(r.timed_out)
        self.assertIsNone(r.returncode)
        self.assertFalse(r.cleanup_failed)
        self.assertFalse(pid_alive(int(pidfile.read_text())))

    def test_background_child_killed_after_leader_exits(self):
        pidfile = self.tmp / "bg.pid"
        script = f"sleep 300 </dev/null >/dev/null 2>&1 & echo $! > {pidfile}; exit 0"
        r = run_bounded(["/bin/sh", "-c", script], cwd=self.tmp, env=ENV, timeout_s=10)
        self.assertEqual(r.returncode, 0)
        self.assertTrue(r.group_members_killed)
        self.assertFalse(pid_alive(int(pidfile.read_text())))

    def test_output_limit(self):
        r = run_bounded(["/bin/sh", "-c", "yes"], cwd=self.tmp, env=ENV, timeout_s=10,
                        out_limit=10000)
        self.assertTrue(r.output_limit_exceeded)
        self.assertFalse(r.ok)

    def test_killed_members_still_exiting_are_waited_for(self):
        # Reproduces the observed race: SIGKILLed members stay visible for a few scans.
        from unittest import mock
        from aidev import proc
        seen = {"n": 0}
        real = proc._group_has_live_members

        def slow_exit(pgid):
            seen["n"] += 1
            return True if seen["n"] <= 4 else real(pgid)

        with mock.patch.object(proc, "_group_has_live_members", side_effect=slow_exit):
            r = run_bounded(["/bin/sh", "-c", "sleep 300 & sleep 300"], cwd=self.tmp, env=ENV,
                            timeout_s=0.3)
        self.assertTrue(r.timed_out)
        self.assertFalse(r.cleanup_failed)
        self.assertGreater(seen["n"], 4)

    def test_genuine_survivor_still_reported(self):
        from unittest import mock
        from aidev import proc
        with mock.patch.object(proc, "_group_has_live_members", return_value=True), \
                mock.patch.object(proc, "GROUP_EXIT_GRACE_S", 0.3):
            r = run_bounded(["/bin/sh", "-c", "exit 0"], cwd=self.tmp, env=ENV, timeout_s=5)
        self.assertTrue(r.cleanup_failed)
        self.assertFalse(r.ok)

    def test_nonzero_exit(self):
        r = run_bounded(["/bin/sh", "-c", "exit 7"], cwd=self.tmp, env=ENV, timeout_s=10)
        self.assertEqual(r.returncode, 7)
        self.assertFalse(r.ok)


class RealClientGuard(unittest.TestCase):
    """No test here ever executes a real client: the guard raises before spawn."""

    def setUp(self):
        WORK.mkdir(exist_ok=True)
        self.tmp = Path(tempfile.mkdtemp(prefix="guard-", dir=WORK))
        self.marker = self.tmp / "EXECUTED"
        for name in ("claude", "codex"):
            p = self.tmp / name
            p.write_text(f"#!/bin/sh\ntouch {self.marker}\n")
            p.chmod(0o755)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_names_refused_wherever_they_live(self):
        for name in ("claude", "codex"):
            with self.assertRaises(ForbiddenExecutable):
                run_bounded([str(self.tmp / name)], cwd=self.tmp, env=ENV, timeout_s=5)
        self.assertFalse(self.marker.exists())

    def test_symlink_to_forbidden_name_refused(self):
        link = self.tmp / "innocent"
        link.symlink_to(self.tmp / "codex")
        with self.assertRaises(ForbiddenExecutable):
            run_bounded([str(link)], cwd=self.tmp, env=ENV, timeout_s=5)
        self.assertFalse(self.marker.exists())

    def test_interpreter_running_forbidden_script_refused(self):
        with self.assertRaises(ForbiddenExecutable):
            run_bounded(["/bin/sh", str(self.tmp / "claude")], cwd=self.tmp, env=ENV, timeout_s=5)
        self.assertFalse(self.marker.exists())

    def test_installed_locations_refused_by_path_alone(self):
        # Pure path check; nothing is opened or executed.
        for p in (Path.home() / ".local/bin/claude", Path.home() / ".local/bin/codex",
                  Path.home() / ".codex/packages/standalone/current/bin/anything"):
            with self.assertRaises(ForbiddenExecutable):
                check_not_forbidden(p)

    def test_bare_names_and_relative_paths_refused(self):
        for argv0 in ("claude", "codex", "./claude", "bin/tool"):
            with self.assertRaises(ForbiddenExecutable):
                run_bounded([argv0], cwd=self.tmp, env=ENV, timeout_s=5)

    def test_real_backends_disabled(self):
        self.assertFalse(REAL_BACKENDS_ENABLED)
        for b in (ClaudeCliBackend(), CodexCliBackend()):
            with self.assertRaises(RealBackendDisabled):
                b.invoke({}, cwd=self.tmp)
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox",
                         CodexCliBackend().planned_argv("implement", self.tmp))

    def test_backend_config_rejects_path_lookup_and_env_override(self):
        with self.assertRaises(ValueError):
            backend_from_config("planner", {"argv": ["claude", "-p"]})
        with self.assertRaises(ValueError):
            backend_from_config("planner", {"argv": ["/usr/bin/python3"], "env": {"PATH": "/x"}})


class GuardInsideWorkflow(OrchestratorTestCase):
    def test_orchestrator_refuses_real_client_backend(self):
        fake = self.tmp / "claude"
        marker = self.tmp / "EXECUTED"
        fake.write_text(f"#!/bin/sh\ntouch {marker}\n")
        fake.chmod(0o755)
        b = self.backends({})
        b["planner"] = CommandBackend("would-be-real", [str(fake), "-p"])
        orch = Orchestrator(task=task(), repo=self.repo, runs_root=self.runs, **b)
        result = orch.run()
        self.assertFinalConsistent(orch, result, FORBIDDEN_EXECUTABLE)
        self.assertFalse(marker.exists())


class Protocol(unittest.TestCase):
    def _resp(self, **over):
        r = {"protocol": protocol.PROTOCOL, "task_id": "t", "stage": "review", "cycle": 1,
             "reviewed_sha": "a" * 40, "verdict": "approve", "summary": "ok", "findings": []}
        r.update(over)
        return r

    def test_valid_review(self):
        protocol.validate_response(self._resp(), stage="review", task_id="t", cycle=1)

    def test_cycle_bool_is_not_int(self):
        with self.assertRaises(protocol.ProtocolError):
            protocol.validate_response(self._resp(cycle=True), stage="review", task_id="t", cycle=1)

    def test_bad_sha_and_finding_shapes(self):
        for over in ({"reviewed_sha": "abc"},
                     {"findings": [{"id": "1", "severity": "urgent", "description": "x"}]},
                     {"findings": ["text"]}):
            with self.assertRaises(protocol.ProtocolError):
                protocol.validate_response(self._resp(**over), stage="review", task_id="t", cycle=1)

    def test_single_json_document(self):
        self.assertEqual(protocol.parse_single_json(b' {"a": 1}\n'), {"a": 1})
        with self.assertRaises(protocol.ProtocolError) as cm:
            protocol.parse_single_json(b'{"a": 1}\n{"a": 2}\n')
        self.assertEqual(cm.exception.code, "RESPONSE_CONFLICTING")
        for bad in (b"", b"[1]", b"\xff", b"text"):
            with self.assertRaises(protocol.ProtocolError):
                protocol.parse_single_json(bad)

    def test_task_validation(self):
        protocol.load_task(task())
        for over in ({"task_id": "Bad ID"}, {"max_cycles": 11}, {"max_cycles": 0}, {"complexity": "huge"}, {"max_model_calls": 1}, {"max_clarifications": 4}, {"continue_from": "relative/dir"},
                     {"test_command": "pytest"}, {"task_timeout_s": 0}, {"auto_merge": True},
                     {"acceptance_criteria": [""]}):
            with self.assertRaises(protocol.ProtocolError, msg=str(over)):
                protocol.load_task(task(**over))


class JournalChain(unittest.TestCase):
    def test_chain_detects_deletion_and_refuses_overwrite(self):
        WORK.mkdir(exist_ok=True)
        tmp = Path(tempfile.mkdtemp(prefix="journal-", dir=WORK))
        try:
            path = tmp / "events.jsonl"
            j = Journal(path, "t")
            for i in range(3):
                j.append("e", i=i)
            j.close()
            self.assertTrue(verify_journal(path)[0])
            with self.assertRaises(FileExistsError):
                Journal(path, "t")
            lines = path.read_text().splitlines(True)
            path.write_text(lines[0] + lines[2])
            ok, msg = verify_journal(path)
            self.assertFalse(ok)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
