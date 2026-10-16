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

import re

from . import model_policy as model_policy_mod
from . import protocol
from .backends import CommandBackend
from .gitops import Git, GitError
from .journal import Journal, verify_journal
from . import test_sandbox as test_sandbox_mod
from .cli_backends import ClaudeCodeBackend, CodexExecBackend
from .proc import SAFE_PATH, ForbiddenExecutable, run_bounded
from .redact import redact
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
BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"    # review cycles or model calls used up
STALLED = "STALLED"                      # the same findings keep coming back
BLOCKED = "BLOCKED"                      # reviewer blocked, or clarification limit exceeded
MODEL_UNSUPPORTED = "MODEL_UNSUPPORTED"  # selection not allowed by policy / CLI catalog
APPROVED_WITH_FAILING_TESTS = "APPROVED_WITH_FAILING_TESTS"
# A human may continue a run that stopped in one of these states (task "continue_from").
CONTINUABLE = (BUDGET_EXHAUSTED, STALLED, TASK_TIMEOUT)
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
                 reviewer: CommandBackend, test_sandbox: bool = False,
                 model_policy: dict | None = None,
                 model_catalog: dict[str, list[str]] | None = None):
        try:
            self.task = protocol.load_task(task)
        except protocol.ProtocolError as e:
            raise PreflightError(str(e)) from None
        # A live Codex call must never run on the CLI's implicit default model/effort.
        if isinstance(implementer, CodexExecBackend) and (model_policy is None or model_catalog is None):
            raise PreflightError("live Codex backend requires an explicit model policy and CLI catalog")
        self.model_policy = model_policy
        self.model_catalog = model_catalog
        if model_policy is not None:
            try:
                model_policy_mod.load_policy(model_policy)
                # Fail before any model call if the owner's own classification is not runnable.
                first = model_policy_mod.select(model_policy, self.task["complexity"])
                if model_catalog is not None:
                    model_policy_mod.validate(first, model_policy, model_catalog)
            except model_policy_mod.ModelPolicyError as e:
                raise PreflightError(f"model policy: {e}") from None
        # Live agents can rewrite the repo's tests, so live runs must sandbox the test command.
        if not test_sandbox and any(isinstance(b, (ClaudeCodeBackend, CodexExecBackend))
                                    for b in (planner, implementer, reviewer)):
            raise PreflightError("live backends require test_sandbox=True")
        self.test_sandbox = test_sandbox
        if test_sandbox:
            try:
                test_sandbox_mod.check_bwrap()
                test_sandbox_mod.readonly_dirs(self.task["test_command"])
            except test_sandbox_mod.SandboxUnavailable as e:
                raise PreflightError(f"test sandbox: {e}") from None
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
        self.completed: set[tuple[str, int, int]] = set()
        self.cycles: list[dict] = []
        self.plan: dict | None = None
        self.calls = 0                  # model calls started (all roles)
        self.clarifications = 0
        self.selection: model_policy_mod.ModelSelection | None = None
        self.prev: dict | None = None   # previous result when continuing a run

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
        """Adaptive loop: implement/fix -> commit -> tests -> review, until approval.

        Agents decide whether another revision is needed (review verdict) or whether
        the plan must be clarified (implementer status). This code decides whether
        that is still allowed: review-cycle, model-call, clarification and time
        budgets, stall detection, git integrity.
        """
        self._preflight()
        start_sha = self.prev["final_commit"] if self.prev else self.base_sha
        self.git.add_worktree(self.repo, self.worktree, self.branch, start_sha)
        self.journal.append("worktree_created", path=str(self.worktree),
                            branch=self.branch, base_sha=self.base_sha, start_sha=start_sha)

        history: list[frozenset] = []   # finding fingerprints of every review so far
        if self.prev:
            self.plan = self.prev["plan"]
            findings = self.prev["cycles"][-1]["review"]["findings"]
            history = [self._fingerprints(c["review"]["findings"]) for c in self.prev["cycles"]]
            stage = "fix"
        else:
            self._need_calls(3, "plan + implement + review")
            self.plan = self._agent("plan", 0, "planner",
                                    self._request("plan", 0, files=self._files()), read_only=True)
            findings, stage = [], "implement"
        self.selection = self._select_model("plan")

        prev_sha = start_sha
        max_cycles = self.task["max_cycles"]
        cycle = 0
        while True:
            if cycle >= max_cycles:
                raise Stop(BUDGET_EXHAUSTED, f"changes still requested after {cycle} review cycle(s) "
                           f"(budget {max_cycles}); last commit {prev_sha}", budget="review_cycles")
            cycle += 1
            impl = self._implement(stage, cycle, prev_sha, findings)
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
                                "implementation": impl, "tests": tests, "review": review,
                                "model_selection": self.selection.to_dict() if self.selection else None})
            self.journal.append("review_bound", cycle=cycle, commit=sha,
                                verdict=review["verdict"])

            verdict = review["verdict"]
            if verdict == "approve":
                if tests["passed"]:
                    return SUCCEEDED, f"approved at {sha} after {cycle} cycle(s); tests passed"
                return (APPROVED_WITH_FAILING_TESTS,
                        f"reviewer approved {sha} but tests did not pass; human decision needed")
            if verdict == "blocked":
                raise Stop(BLOCKED, f"reviewer blocked at {sha}: {review['summary']}", by="reviewer")
            fps = self._fingerprints(review["findings"])
            repeated = self._stalled(history, fps)
            history.append(fps)
            if repeated:
                raise Stop(STALLED, f"review cycle {cycle} repeats earlier findings without progress; "
                           f"last commit {sha}", repeated_findings=sorted(repeated))
            findings = review["findings"]
            prev_sha = sha
            stage = "fix"

    def _implement(self, stage: str, cycle: int, prev_sha: str, findings: list[dict]) -> dict:
        """One implement/fix call, plus at most `max_clarifications` clarification rounds
        (implementer asks -> planner re-plans -> implementer continues in the same worktree)."""
        attempt = 0
        while True:
            attempt += 1
            self._need_calls(2, f"{stage} + review in cycle {cycle}")
            req = self._request(stage, cycle, plan=self.plan, previous_sha=prev_sha, findings=findings,
                                model_selection=self.selection.to_dict() if self.selection else None)
            impl = self._agent(stage, cycle, "implementer", req, read_only=False,
                               expect_head=prev_sha, attempt=attempt)
            if impl["status"] != "needs_clarification":
                return impl
            self.clarifications += 1
            if self.clarifications > self.task["max_clarifications"]:
                raise Stop(BLOCKED, f"implementer still needs clarification after "
                           f"{self.task['max_clarifications']} re-plan(s)", by="implementer",
                           questions=impl["questions"])
            self._need_calls(3, f"replan + {stage} + review in cycle {cycle}")
            replan_req = self._request("replan", cycle, previous_plan=self.plan,
                                       questions=impl["questions"], implementer_summary=impl["summary"],
                                       files=self._files())
            self.plan = self._agent("replan", cycle, "planner", replan_req, read_only=True,
                                    attempt=self.clarifications)
            self.selection = self._select_model("replan")

    # ------------------------------------------------------- budgets & policy
    def _need_calls(self, n: int, what: str) -> None:
        limit = self.task["max_model_calls"]
        if self.calls + n > limit:
            raise Stop(BUDGET_EXHAUSTED, f"model-call budget: {what} needs {n} more call(s), "
                       f"{limit - self.calls} of {limit} left", budget="model_calls")

    @staticmethod
    def _fingerprints(findings: list[dict]) -> frozenset:
        """Severity + normalised description. Rephrased findings are not caught here;
        the review-cycle and model-call budgets bound that case."""
        return frozenset(f"{f['severity']}:" + re.sub(r"[^a-z0-9]+", " ", f["description"].lower()).strip()
                         for f in findings)

    def _stalled(self, history: list[frozenset], fps: frozenset) -> set:
        if not fps:
            return set()
        if history and fps == history[-1]:
            return set(fps)                         # identical findings twice in a row
        limit = self.task["stall_repeat_limit"]
        return {fp for fp in fps if sum(fp in h for h in history) + 1 >= limit}

    def _select_model(self, trigger: str) -> model_policy_mod.ModelSelection | None:
        if self.model_policy is None:
            return None
        plan = self.plan or {}
        try:
            sel = model_policy_mod.select(self.model_policy, self.task["complexity"],
                                          plan.get("complexity"), plan.get("complexity_rationale", ""))
            if self.model_catalog is not None:
                model_policy_mod.validate(sel, self.model_policy, self.model_catalog)
        except model_policy_mod.ModelPolicyError as e:
            raise Stop(MODEL_UNSUPPORTED, f"model selection after {trigger}: {e}") from None
        self.journal.append("model_selected", trigger=trigger, **sel.to_dict(),
                            catalog_checked=self.model_catalog is not None)
        return sel

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
        if self.task["continue_from"]:
            self._load_continuation(Path(self.task["continue_from"]))
        self.journal.append("preflight_ok", base_branch=self.base_branch,
                            base_sha=self.base_sha)

    def _load_continuation(self, prev_dir: Path) -> None:
        """Human extension: continue a stopped run from its last reviewed commit.

        The owner starts a NEW task (new task_id, its own budget) with
        `continue_from`; the previous run's evidence must be intact and consistent."""
        def bad(msg):
            raise Stop(FAILED_PREFLIGHT, f"continue_from: {msg}")
        try:
            prev = json.loads((prev_dir / "result.json").read_text())
        except (OSError, ValueError) as e:
            bad(f"cannot read result.json: {e}")
        ok, msg = verify_journal(prev_dir / "events.jsonl")
        if not ok:
            bad(f"previous journal fails verification: {msg}")
        if prev.get("repo") != str(self.repo):
            bad("previous run used a different repository")
        if prev.get("state") not in CONTINUABLE:
            bad(f"state {prev.get('state')} is not continuable (allowed: {', '.join(CONTINUABLE)})")
        if not prev.get("cycles") or prev["cycles"][-1]["review"]["verdict"] != "changes_requested":
            bad("previous run has no reviewed commit with outstanding findings")
        if prev.get("base_sha") != self.base_sha:
            bad("base branch moved since the previous run")
        try:
            branch_head = self.git.run(self.repo, "rev-parse", "--verify", prev["task_branch"]).strip()
        except GitError:
            bad(f"previous branch {prev.get('task_branch')} not found")
        if branch_head != prev["final_commit"]:
            bad("previous task branch no longer points at its final reviewed commit")
        self.prev = prev
        self.journal.append("continuation_ok", previous_task=prev["task_id"],
                            previous_state=prev["state"], start_sha=prev["final_commit"],
                            previous_cycles=len(prev["cycles"]))

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
               read_only: bool, expect_head: str | None = None, attempt: int = 1) -> dict:
        key = (stage, cycle, attempt)
        if key in self.completed:  # defensive: the loop never schedules a stage twice
            raise Stop(CONFLICTING_COMPLETION, f"{stage} cycle {cycle} already completed")
        timeout, task_bound = self._budget(self.task["stage_timeout_s"])
        if self.calls >= self.task["max_model_calls"]:   # last line of defence; _need_calls plans ahead
            raise Stop(BUDGET_EXHAUSTED, "model-call budget exhausted", budget="model_calls")
        self.calls += 1
        backend = self.backends[role]
        before = self.git.snapshot(self.worktree)
        self.journal.append("stage_started", stage=stage, cycle=cycle, attempt=attempt, role=role,
                            backend=backend.name, timeout_s=round(timeout, 3), model_call=self.calls,
                            request_sha256=_sha256(json.dumps(request, sort_keys=True).encode()))
        suffix = f"-a{attempt}" if attempt > 1 else ""
        inv = backend.invoke(request, cwd=self.worktree, home=self.home, timeout_s=timeout,
                             log_prefix=self.logs / f"{stage}-c{cycle}{suffix}")
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
        argv = self.task["test_command"]
        if self.test_sandbox:
            argv = test_sandbox_mod.wrap(argv, self.worktree)
        self.journal.append("tests_started", cycle=cycle, commit=sha,
                            command=self.task["test_command"], sandboxed=self.test_sandbox,
                            timeout_s=round(timeout, 3))
        r = run_bounded(argv, cwd=self.worktree, env=env, timeout_s=timeout)
        # Test output may come from agent-written code: redact before it is stored,
        # reported, or handed to the reviewer.
        stdout, stderr = redact(r.stdout), redact(r.stderr)
        (self.logs / f"tests-c{cycle}.stdout").write_bytes(stdout)
        (self.logs / f"tests-c{cycle}.stderr").write_bytes(stderr)
        passed = r.ok
        tail = (stdout + b"\n" + stderr)[-TEST_TAIL:].decode(errors="replace")
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
            "model_calls": self.calls,
            "budget": {k: self.task[k] for k in ("max_cycles", "max_model_calls", "max_clarifications",
                                                  "stall_repeat_limit", "stage_timeout_s",
                                                  "task_timeout_s")},
            "clarifications": self.clarifications,
            "model_selection": self.selection.to_dict() if self.selection else None,
            "continued_from": self.prev["task_id"] if self.prev else None,
            "diffstat": diffstat,
            "duration_s": round(time.monotonic() - self.t0, 3),
            "merged": False,
            "pushed": False,
        }
        self.journal.append("task_finished", state=state, success=result["success"],
                            detail=detail, final_commit=final_sha, model_calls=self.calls,
                            review_cycles=len(self.cycles), clarifications=self.clarifications,
                            base_branch_unchanged=result["base_branch_unchanged"])
        self.journal.close()
        (self.run_dir / "result.json").write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
        write_report(self.run_dir / "report.md", result)
        return result
