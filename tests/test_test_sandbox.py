"""Regression tests for the live-run test sandbox (aidev/test_sandbox.py).

They run real bubblewrap (/usr/bin/bwrap) on synthetic files only. The CLIs'
credential stores are only checked for *absence* inside the sandbox; nothing
reads them.
"""

import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from aidev import test_sandbox as ts
from aidev.orchestrator import Orchestrator, PreflightError
from aidev.proc import run_bounded

from helpers import M1_ROOT, WORK, OrchestratorTestCase, task
from test_live_adapters import LiveAdapterCase

ENV = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}
SECRET = "canary-home-secret-7f3a"


class Boundary(unittest.TestCase):
    def setUp(self):
        WORK.mkdir(exist_ok=True)
        self.tmp = Path(tempfile.mkdtemp(prefix="sbx-", dir=WORK))
        self.wt = self.tmp / "worktree"
        self.wt.mkdir()
        (self.wt / "mod.py").write_text("VALUE = 42\n")
        self.fixtures = self.tmp / "fixtures"
        self.fixtures.mkdir()
        (self.fixtures / "runner.py").write_text(
            "import sys\nsys.path.insert(0, '.')\nexec(open(sys.argv[1]).read())\n")
        # Unrelated home data outside the worktree and outside the fixtures dir.
        cache = Path.home() / ".cache"
        cache.mkdir(exist_ok=True)
        self.canary = Path(tempfile.mkstemp(prefix="aidev-canary-", dir=cache)[1])
        self.canary.write_text(SECRET)

    def tearDown(self):
        self.canary.unlink(missing_ok=True)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def sandboxed(self, code: str, timeout=30):
        script = self.fixtures / "probe_code.py"
        script.write_text(code)
        argv = ts.wrap(["python3", str(self.fixtures / "runner.py"), str(script)], self.wt)
        return run_bounded(argv, cwd=self.wt, env=ENV, timeout_s=timeout)

    def out(self, code, **kw):
        r = self.sandboxed(code, **kw)
        return r, r.stdout.decode() + r.stderr.decode()

    def test_worktree_and_fixtures_readable_stdlib_works(self):
        r, out = self.out("import mod, json, sqlite3, unittest; print('VALUE', mod.VALUE)")
        self.assertEqual(r.returncode, 0, out)
        self.assertIn("VALUE 42", out)

    def test_home_data_hidden(self):
        r, out = self.out(f"""
import os
for p in [{str(self.canary)!r}, {str(Path.home() / '.codex')!r}, {str(Path.home() / '.claude')!r},
          {str(Path.home() / '.local/bin')!r}, {str(M1_ROOT / 'aidev')!r}]:
    print(p, os.path.exists(p))
print('HOME', os.environ.get('HOME'), sorted(os.listdir('/home')))
""")
        self.assertEqual(r.returncode, 0, out)
        self.assertNotIn(" True", out)          # none of those paths exists inside
        self.assertNotIn(SECRET, out)
        # /home only contains the path components leading to the bound worktree/fixtures.
        self.assertIn("HOME /tmp", out)

    def test_network_blocked(self):
        r, out = self.out("""
import socket
for host, port in (("1.1.1.1", 443), ("127.0.0.1", 22)):
    try:
        socket.create_connection((host, port), timeout=3); print("OPEN", host)
    except OSError as e:
        print("DENIED", host, type(e).__name__)
try:
    socket.getaddrinfo("example.com", 443); print("DNS OPEN")
except OSError as e:
    print("DNS DENIED")
""")
        self.assertNotIn("OPEN", out.replace("DENIED", ""))
        self.assertEqual(out.count("DENIED"), 3, out)

    def test_worktree_and_fixtures_read_only_tmp_private(self):
        r, out = self.out(f"""
for p in ("written.txt", {str(self.fixtures / 'x.txt')!r}, "/usr/aidev-x", "/tmp/ok.txt"):
    try:
        open(p, "w").write("x"); print("WROTE", p)
    except OSError as e:
        print("DENIED", p, type(e).__name__)
""")
        self.assertIn("WROTE /tmp/ok.txt", out)
        self.assertEqual(out.count("DENIED"), 3, out)
        self.assertFalse((self.wt / "written.txt").exists())
        self.assertFalse(Path("/tmp/ok.txt").exists())

    def test_environment_cleared(self):
        os.environ["AIDEV_SHOULD_NOT_LEAK"] = "1"
        try:
            r, out = self.out("import os; print(sorted(os.environ))")
        finally:
            del os.environ["AIDEV_SHOULD_NOT_LEAK"]
        self.assertNotIn("AIDEV_SHOULD_NOT_LEAK", out)
        self.assertNotIn("WSL_DISTRO_NAME", out)

    def test_timeout_kills_sandbox_and_descendants(self):
        marker = f"aidev-sbx-sleep-{os.getpid()}-{time.monotonic_ns()}"
        r = self.sandboxed(f"""
import subprocess
subprocess.Popen(["/bin/sh", "-c", "exec -a {marker} sleep 300"])
import time; time.sleep(300)
""", timeout=2)
        self.assertTrue(r.timed_out)
        time.sleep(0.5)
        ps = subprocess.run(["ps", "-eo", "args"], capture_output=True, text=True).stdout
        self.assertNotIn(marker, ps)

    def test_paths_exposing_home_refused(self):
        for bad in (str(Path.home()), "/home", str(Path.home() / ".codex" / "x"),
                    str(Path.home() / ".claude")):
            if not os.path.exists(bad):
                continue
            with self.assertRaises(ts.SandboxUnavailable, msg=bad):
                ts.readonly_dirs(["python3", bad])

    def test_bwrap_is_system_binary(self):
        self.assertEqual(ts.check_bwrap(), "/usr/bin/bwrap")


class LiveRequiresSandbox(OrchestratorTestCase):
    def test_live_backends_without_test_sandbox_refused(self):
        from aidev import cli_backends as cb
        gate = cb.LiveGate("t", "t", "t")
        b = {"planner": cb.ClaudeCodeBackend(["/x/claude"], gate),
             "implementer": cb.CodexExecBackend(["/x/codex"], gate),
             "reviewer": cb.ClaudeCodeBackend(["/x/claude"], gate)}
        with self.assertRaises(PreflightError):
            Orchestrator(task=task(), repo=self.repo, runs_root=self.runs, **b)

    def test_mock_workflow_still_works_inside_sandbox(self):
        from helpers import GOOD_IMPL
        orch = Orchestrator(task=task(), repo=self.repo, runs_root=self.runs,
                            **self.backends({"implement": GOOD_IMPL}), test_sandbox=True)
        result = orch.run()
        self.assertEqual(result["state"], "SUCCEEDED", result["detail"])
        started = [json.loads(l) for l in (orch.run_dir / "events.jsonl").read_text().splitlines()
                   if '"tests_started"' in l]
        self.assertTrue(started[0]["data"]["sandboxed"])


class HostileAgentTest(LiveAdapterCase):
    def test_agent_written_test_cannot_reach_home_or_network_and_is_redacted(self):
        cache = Path.home() / ".cache"
        cache.mkdir(exist_ok=True)
        canary = Path(tempfile.mkstemp(prefix="aidev-canary-", dir=cache)[1])
        canary.write_text(SECRET)
        try:
            result, orch = self.run_task({"implement": {"hostile_test": str(canary)}})
        finally:
            canary.unlink(missing_ok=True)
        self.assertEqual(result["state"], "SUCCEEDED", result["detail"])
        log = (orch.run_dir / "logs" / "tests-c1.stdout").read_text()
        self.assertIn("== acceptance PASSED", log)          # synthetic acceptance test still works
        self.assertIn(f"DENIED {canary}", log)
        self.assertIn("DENIED /home/dev/.claude", log)
        self.assertIn("NET DENIED", log)
        self.assertNotIn(SECRET, log)
        token = "sk-ant-oat01-HOSTILETOKEN1234567890"
        everything = "".join(p.read_text(errors="replace") for p in (orch.run_dir / "logs").iterdir())
        everything += (orch.run_dir / "result.json").read_text()
        everything += (orch.run_dir / "events.jsonl").read_text()
        everything += (orch.run_dir / "report.md").read_text()
        review_prompt = [c for c in self.calls() if c["stage"] == "review"][0]["prompt"]
        self.assertNotIn(token, everything)
        self.assertNotIn(token, review_prompt)               # nothing leaked to Claude
        self.assertIn("[REDACTED]", log)


if __name__ == "__main__":
    unittest.main()
