"""Deterministic PLAN -> IMPLEMENT -> TEST -> REVIEW (-> FIX ...) orchestrator.

The orchestrator owns sequencing, git, tests, limits and the journal. Agents
only ever see a request on stdin and a git worktree as cwd; their claims are
checked against git before anything is recorded as fact.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

from . import protocol
from .backends import CommandBackend
from .gitops import Git, GitError
from .journal import Journal
from .proc import SAFE_PATH, ForbiddenExecutable, run_bounded
from .report import write_report

DIFF_LIMIT = 256 * 1024
FILE_LIMIT = 64 * 1024
FILES_TOTAL_LIMIT = 256 * 1024
TEST_TAIL = 4096

# Final states. Only SUCCEEDED maps to exit code 0.
SUCCEEDED = "SUCCEEDED"
FAILED_PREFLIGHT = "FAILED_PREFLIGHT"
AGENT_FAILED = "AGENT_FAILED"
INVALID_RESPONSE = "INVALID_RESPONSE"
CONFLICTING_COMPLETION = "CONFLICTING_COMPLETION"
FAILED_IMPLEMENTATION = "FAILED_IMPLEMENTATION"
UNAUTHORIZED_MODIFICATION = "UNAUTHORIZED_MODIFICATION"
TESTS_MODIFIED_WORKTREE = "TESTS_MODIFIED_WORKTREE"
STALE_REVIEW = "STALE_REVIEW"
STAGE_TIMEOUT = "STAGE_TIMEOUT"
TASK_TIMEOUT = "TASK_TIMEOUT"
MAX_CYCLES_REACHED = "MAX_CYCLES_REACHED"
ESCALATED_BY_REVIEWER = "ESCALATED_BY_REVIEWER"
APPROVED_WITH_FAILING_TESTS = "APPROVED_WITH_FAILING_TESTS"
CLEANUP_FAILED = "CLEANUP_FAILED"
FORBIDDEN_EXECUTABLE = "FORBIDDEN_EXECUTABLE"
INTERRUPTED = "INTERRUPTED"
INTERNAL_ERROR = "INTERNAL_ERROR"


class PreflightError(RuntimeError):
    """Raised before a run directory/journal exists; nothing was written."""


class Stop(Exception):
    def __init__(self, state: str, detail: str, **data):
        super().__init__(f"{state}: {detail}")
        self.state, self.detail, self.data = state, detail, data


def _sha256(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


class Orchestrator:
    def __init__(self, *, task: dict, repo: Path, runs_root: Path,
                 planner: CommandBackend, implementer: CommandBackend,
                 reviewer: CommandBackend):
        try:
            self.task = protocol.load_task(task)
        except protocol.ProtocolError as e:
            raise PreflightError(str(e)) from None
        self.repo = Path(os.path.realpath(repo))
        self.runs_root = Path(os.path.realpath(runs_root))
        self.backends = {"planner": planner, "implementer": implementer, "reviewer": reviewer}
        self.task_id = self.task["task_id"]
        self.branch = f"aidev/{self.task_id}"
        self.run_dir = self.runs_root / self.task_id
        if self.run_dir.exists():
            raise PreflightError(f"run directory already exists (task_id reused?): {self.run_dir}")
        if self.runs_root == self.repo or self.repo in self.runs_root.parents:
            raise PreflightError("runs directory must be outside the target repository")
        self.completed: set[tuple[str, int]] = set()
        self.cycles: list[dict] = []
        self.plan: dict | None = None

    # ----------------------------------------------------------------- public
    def run(self) -> dict:
        self.run_dir.mkdir(parents=True)
        self.logs = self.run_dir / "logs"
        self.home = self.run_dir / "home"  # HOME for every child: no real auth stores
        self.worktree = self.run_dir / "worktree"
        self.logs.mkdir()
        self.home.mkdir()
        self.git = Git(self.home)
        self.journal = Journal(self.run_dir / "events.jsonl", self.task_id)
        self.t0 = time.monotonic()
        self.deadline = self.t0 + self.task["task_timeout_s"]
        self.base_sha = self.base_branch = None
        self.journal.append("task_received", task=self.task, repo=str(self.repo),
                            backends={k: getattr(b, "command", None) or b.argv
                                      for k, b in self.backends.items()})
        state, detail, data = SUCCEEDED, "", {}
        try:
            state, detail = self._run()
        except Stop as s:
            state, detail, data = s.state, s.detail, s.data
        except ForbiddenExecutable as e:
            state, detail = FORBIDDEN_EXECUTABLE, str(e)
        except GitError as e:
            state, detail = INTERNAL_ERROR, f"git: {e}"
        except KeyboardInterrupt:  # Ctrl-C, or SIGTERM translated by the CLI
            state, detail = INTERRUPTED, "run interrupted; child process group was killed"
        except Exception as e:  # never report success on an unexpected error
            state, detail = INTERNAL_ERROR, f"{type(e).__name__}: {e}"
        result = self._finish(state, detail, data)
        return result

    # --------------------------------------------------------------- workflow
    def _run(self) -> tuple[str, str]:
        self._preflight()
        self.git.add_worktree(self.repo, self.worktree, self.branch, self.base_sha)
        self.journal.append("worktree_created", path=str(self.worktree),
                            branch=self.branch, base_sha=self.base_sha)

        # PLAN (read-only)
        self.plan = self._agent("plan", 0, "planner",
                                self._request("plan", 0, files=self._files()), read_only=True)

        prev_sha = self.base_sha
        findings: list[dict] = []
        max_cycles = self.task["max_cycles"]
        for cycle in range(1, max_cycles + 1):
            stage = "implement" if cycle == 1 else "fix"
            req = self._request(stage, cycle, plan=self.plan, previous_sha=prev_sha,
                                findings=findings)
            impl = self._agent(stage, cycle, "implementer", req, read_only=False,
                               expect_head=prev_sha)
            if impl["status"] == "failed":
                raise Stop(FAILED_IMPLEMENTATION, f"{stage} cycle {cycle} reported failure: "
                           f"{impl['summary']}")
            if self.git.is_clean(self.worktree):
                raise Stop(FAILED_IMPLEMENTATION, f"{stage} cycle {cycle} produced no changes")
            sha = self._commit(stage, cycle, prev_sha, impl)

            tests = self._run_tests(cycle, sha)

            review_req = self._request("review", cycle, plan=self.plan, base_sha=self.base_sha,
                                       commit_sha=sha, **self._diff_payload(sha), tests=tests,
                                       files=self._files())
            review = self._agent("review", cycle, "reviewer", review_req, read_only=True)
            if review["reviewed_sha"] != sha:
                raise Stop(STALE_REVIEW, f"review cycle {cycle} is bound to "
                           f"{review['reviewed_sha']}, implementation commit is {sha}",
                           expected=sha, got=review["reviewed_sha"])
            self.cycles.append({"cycle": cycle, "stage": stage, "commit": sha,
                                "implementation": impl, "tests": tests, "review": review})
            self.journal.append("review_bound", cycle=cycle, commit=sha,
                                verdict=review["verdict"])

            verdict = review["verdict"]
            if verdict == "approve":
                if tests["passed"]:
                    return SUCCEEDED, f"approved at {sha} after {cycle} cycle(s); tests passed"
                return (APPROVED_WITH_FAILING_TESTS,
                        f"reviewer approved {sha} but tests did not pass; human decision needed")
            if verdict == "blocked":
                return ESCALATED_BY_REVIEWER, f"reviewer blocked at {sha}: {review['summary']}"
            findings = review["findings"]
            prev_sha = sha
        return (MAX_CYCLES_REACHED,
                f"changes still requested after {max_cycles} cycle(s); last commit {prev_sha}")

    def _preflight(self) -> None:
        try:
            top = Path(os.path.realpath(self.git.toplevel(self.repo)))
        except GitError as e:
            raise Stop(FAILED_PREFLIGHT, f"not a git repository: {e}") from None
        if top != self.repo:
            raise Stop(FAILED_PREFLIGHT, f"repo must be the repository root ({top})")
        try:
            self.base_branch = self.git.current_branch(self.repo)
        except GitError:
            raise Stop(FAILED_PREFLIGHT, "repository HEAD is detached") from None
        self.base_sha = self.git.head(self.repo)
        if not self.git.is_clean(self.repo):
            raise Stop(FAILED_PREFLIGHT, "repository has uncommitted changes")
        if self.git.branch_exists(self.repo, self.branch):
            raise Stop(FAILED_PREFLIGHT, f"branch {self.branch} already exists")
        self.journal.append("preflight_ok", base_branch=self.base_branch,
                            base_sha=self.base_sha)

    # ----------------------------------------------------------------- stages
    def _request(self, stage: str, cycle: int, **extra) -> dict:
        t = self.task
        return {"protocol": protocol.PROTOCOL, "task_id": self.task_id, "stage": stage,
                "cycle": cycle, "objective": t["objective"],
                "acceptance_criteria": t["acceptance_criteria"], **extra}

    def _remaining(self) -> float:
        return self.deadline - time.monotonic()

    def _budget(self, limit: float) -> tuple[float, bool]:
        remaining = self._remaining()
        if remaining <= 0:
            raise Stop(TASK_TIMEOUT, "overall task time limit reached")
        return (min(limit, remaining), remaining < limit)

    def _agent(self, stage: str, cycle: int, role: str, request: dict, *,
               read_only: bool, expect_head: str | None = None) -> dict:
        key = (stage, cycle)
        if key in self.completed:  # defensive: the loop never schedules a stage twice
            raise Stop(CONFLICTING_COMPLETION, f"{stage} cycle {cycle} already completed")
        timeout, task_bound = self._budget(self.task["stage_timeout_s"])
        backend = self.backends[role]
        before = self.git.snapshot(self.worktree)
        self.journal.append("stage_started", stage=stage, cycle=cycle, role=role,
                            backend=backend.name, timeout_s=round(timeout, 3),
                            request_sha256=_sha256(json.dumps(request, sort_keys=True).encode()))
        inv = backend.invoke(request, cwd=self.worktree, home=self.home, timeout_s=timeout,
                             log_prefix=self.logs / f"{stage}-c{cycle}")
        r = inv.result
        self.journal.append("stage_process_exit", stage=stage, cycle=cycle,
                            returncode=r.returncode, timed_out=r.timed_out,
                            duration_s=r.duration_s, output_limit_exceeded=r.output_limit_exceeded,
                            group_members_killed=r.group_members_killed,
                            cleanup_failed=r.cleanup_failed, stdout_bytes=len(r.stdout),
                            stdout_sha256=_sha256(r.stdout), stdout_log=inv.stdout_path.name,
                            stderr_log=inv.stderr_path.name)
        if inv.meta:
            self.journal.append("backend_meta", stage=stage, cycle=cycle, **inv.meta)
        if r.cleanup_failed:
            raise Stop(CLEANUP_FAILED, f"{stage} cycle {cycle}: child process group not cleaned up")
        # Workspace checks come before output checks: a modification is the more
        # serious finding even if the agent also crashed or printed garbage.
        head, status = self.git.snapshot(self.worktree)
        try:
            branch = self.git.current_branch(self.worktree)
        except GitError:
            branch = "(detached)"
        if branch != self.branch:
            raise Stop(UNAUTHORIZED_MODIFICATION, f"{stage} cycle {cycle} switched branch to {branch}")
        if read_only and (head, status) != before:
            raise Stop(UNAUTHORIZED_MODIFICATION,
                       f"read-only {stage} cycle {cycle} changed the worktree",
                       head_before=before[0], head_after=head, status_after=status)
        if expect_head is not None and head != expect_head:
            raise Stop(UNAUTHORIZED_MODIFICATION,
                       f"{stage} cycle {cycle} moved HEAD (agents must not commit)",
                       head_before=expect_head, head_after=head)
        if r.timed_out:
            raise Stop(TASK_TIMEOUT if task_bound else STAGE_TIMEOUT,
                       f"{stage} cycle {cycle} exceeded {timeout:.1f}s")
        if r.output_limit_exceeded:
            raise Stop(INVALID_RESPONSE, f"{stage} cycle {cycle} exceeded output limit")
        if r.returncode != 0:
            raise Stop(AGENT_FAILED, f"{stage} cycle {cycle} agent exited {r.returncode}")
        try:
            resp = protocol.validate_response(protocol.parse_single_json(r.stdout),
                                              stage=stage, task_id=self.task_id, cycle=cycle)
        except protocol.ProtocolError as e:
            state = CONFLICTING_COMPLETION if e.code == "RESPONSE_CONFLICTING" else INVALID_RESPONSE
            raise Stop(state, f"{stage} cycle {cycle}: {e}") from None
        self.completed.add(key)
        self.journal.append("stage_completed", stage=stage, cycle=cycle, response=resp)
        return resp

    def _commit(self, stage: str, cycle: int, prev_sha: str, impl: dict) -> str:
        sha = self.git.commit_all(self.worktree, f"aidev({self.task_id}): {stage} cycle {cycle}\n\n"
                                  f"{impl['summary']}")
        parent = self.git.parent(self.worktree, sha)
        if parent != prev_sha:
            raise Stop(INTERNAL_ERROR, f"commit parent {parent} != expected {prev_sha}")
        if not self.git.is_clean(self.worktree):
            raise Stop(INTERNAL_ERROR, "worktree not clean after commit")
        files = self.git.diff_names(self.worktree, prev_sha, sha)
        claimed = sorted(impl["files_changed"])
        self.journal.append("commit_created", stage=stage, cycle=cycle, sha=sha, parent=parent,
                            files=files, claimed_files=claimed,
                            claim_matches_git=claimed == sorted(files))
        return sha

    def _run_tests(self, cycle: int, sha: str) -> dict:
        timeout, task_bound = self._budget(self.task["test_timeout_s"])
        env = {"PATH": SAFE_PATH, "HOME": str(self.home), "LANG": "C.UTF-8",
               "PYTHONDONTWRITEBYTECODE": "1"}
        self.journal.append("tests_started", cycle=cycle, commit=sha,
                            command=self.task["test_command"], timeout_s=round(timeout, 3))
        r = run_bounded(self.task["test_command"], cwd=self.worktree, env=env, timeout_s=timeout)
        (self.logs / f"tests-c{cycle}.stdout").write_bytes(r.stdout)
        (self.logs / f"tests-c{cycle}.stderr").write_bytes(r.stderr)
        passed = r.ok
        tail = (r.stdout + b"\n" + r.stderr)[-TEST_TAIL:].decode(errors="replace")
        result = {"commit": sha, "passed": passed, "returncode": r.returncode,
                  "timed_out": r.timed_out, "duration_s": r.duration_s, "output_tail": tail}
        self.journal.append("tests_finished", cycle=cycle, commit=sha, passed=passed,
                            returncode=r.returncode, timed_out=r.timed_out,
                            duration_s=r.duration_s, cleanup_failed=r.cleanup_failed)
        if r.cleanup_failed:
            raise Stop(CLEANUP_FAILED, f"test process group for cycle {cycle} not cleaned up")
        if r.timed_out and task_bound:
            raise Stop(TASK_TIMEOUT, f"overall time limit reached during tests (cycle {cycle})")
        if self.git.head(self.worktree) != sha or not self.git.is_clean(self.worktree):
            raise Stop(TESTS_MODIFIED_WORKTREE,
                       f"test command changed tracked/untracked files at cycle {cycle} "
                       "(ignored files are tolerated)")
        return result

    def _files(self) -> dict[str, str]:
        """Tracked text files of the (clean) worktree, for tool-free planning/review."""
        files, total = {}, 0
        for path in self.git.run(self.worktree, "ls-files", "-z").split("\0"):
            if not path:
                continue
            try:
                text = (self.worktree / path).read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                files[path] = "(binary or unreadable file omitted)"
                continue
            if len(text) > FILE_LIMIT or total + len(text) > FILES_TOTAL_LIMIT:
                files[path] = f"(omitted: {len(text)} characters exceeds the context limit)"
                continue
            files[path] = text
            total += len(text)
        return files

    def _diff_payload(self, sha: str) -> dict:
        diff = self.git.diff(self.worktree, self.base_sha, sha)
        raw = diff.encode()
        truncated = len(raw) > DIFF_LIMIT
        if truncated:
            diff = raw[:DIFF_LIMIT].decode(errors="ignore")
        return {"diff": diff, "diff_truncated": truncated, "diff_sha256": _sha256(raw)}

    # ----------------------------------------------------------------- finish
    def _finish(self, state: str, detail: str, data: dict) -> dict:
        final_sha = self.cycles[-1]["commit"] if self.cycles else None
        diffstat = ""
        try:
            if final_sha and self.base_sha:
                diffstat = self.git.diff_stat(self.worktree, self.base_sha, final_sha)
            base_after = self.git.head(self.repo) if self.base_sha else None
        except Exception:
            base_after = None
        result = {
            "task_id": self.task_id,
            "state": state,
            "success": state == SUCCEEDED,
            "detail": detail,
            "detail_data": data,
            "repo": str(self.repo),
            "base_branch": self.base_branch,
            "base_sha": self.base_sha,
            "base_branch_head_after": base_after,
            "base_branch_unchanged": base_after == self.base_sha if self.base_sha else None,
            "task_branch": self.branch,
            "worktree": str(self.worktree),
            "final_commit": final_sha,
            "plan": self.plan,
            "cycles": self.cycles,
            "diffstat": diffstat,
            "duration_s": round(time.monotonic() - self.t0, 3),
            "merged": False,
            "pushed": False,
        }
        self.journal.append("task_finished", state=state, success=result["success"],
                            detail=detail, final_commit=final_sha,
                            base_branch_unchanged=result["base_branch_unchanged"])
        self.journal.close()
        (self.run_dir / "result.json").write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
        write_report(self.run_dir / "report.md", result)
        return result
