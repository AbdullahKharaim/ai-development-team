# M1 report — deterministic orchestrator with mock agents

Date: 2026-10-14 · Environment: Ubuntu 26.04.1 under WSL2, Python 3.14.4, git 2.53.0,
run as `abdul` via `wsl.exe -d Ubuntu -u abdul`. Stdlib only; nothing installed.

## Result in one line

The PLAN → IMPLEMENT → TEST → REVIEW → FIX loop runs end-to-end against deterministic mock
agents, with git-SHA review binding, read-only-stage detection, bounded cycles/time, process-group
cleanup and a hash-chained journal. **No real model was invoked; real Claude → Codex → Claude
collaboration is still undemonstrated.**

## 1. Files created (all inside `m1_orchestrator/`)

```
aidev/{__init__,__main__,orchestrator,proc,gitops,protocol,journal,backends,report}.py
mocks/fake_agent.py
tests/{helpers,test_units,test_workflow}.py
examples/{task.json,scenario.json,run_example.py}  examples/output/  (captured demo evidence)
docs/AGENTDEV-ISOLATION-CHECKLIST.md
README.md  M1-REPORT.md  .gitignore
```

An independent git repository was initialised in `m1_orchestrator/` only. No file outside it was
created or modified (checked with `find -newer`; `live_cli/SHA256SUMS` still verifies).

## 2. Architectural decisions

- **One process, no broker/server/database.** A sequential state machine in `orchestrator.py`;
  agents are short-lived subprocesses speaking one-JSON-in / one-JSON-out.
- **Git is the artifact store.** The orchestrator — never the agent — creates each commit;
  the reviewer's `reviewed_sha` must equal that commit. This replaces the earlier proposal
  storage/SHA-256 artifact layer.
- **Test command comes from the human's task file**, not from a model, and is executed by the
  orchestrator.
- **Read-only stages are verified, not trusted:** `(HEAD, git status --ignored)` snapshot before/after.
- **Process handling reuses the live_cli/runtime.py approach** (rewritten, not imported):
  new session, non-blocking pumped I/O with limits, `waitid(WNOWAIT)` so the PGID cannot be reused,
  then `killpg(SIGKILL)` after *every* stage.
- **Real backends exist only as disabled stubs** (`ClaudeCliBackend`, `CodexCliBackend`) with the
  planned argv. Independently, `run_bounded` refuses any executable named `claude`/`codex` or located
  under `~/.local/bin`, `~/.claude`, `~/.codex`, and resolves names only against `/usr/bin:/bin`.
- **Strict failure taxonomy.** 18 final states; only `SUCCEEDED` exits 0. Approval with failing tests
  is its own non-success state.

## 3. Tests executed and results

| Run | Result |
|---|---|
| Full suite, final code (`python3 -m unittest discover -s tests`) | **58 tests, OK** (≈21 s) |
| Mutation check: 5 safeguards disabled one at a time in throwaway copies (SHA binding, read-only check, group kill, test-pass gate, HEAD check) | every mutation made the suite fail (3, 5, 7, 2, 1 failures) |
| Demo (`examples/run_example.py`) | `SUCCEEDED` after 2 cycles; journal `OK: 27 events, chain intact` |
| After suite + demo | no leftover `sleep`/mock processes; `.test-work/` empty |

Coverage of the required scenarios: success path; changes requested then fixed; max cycles (2 and 1);
reviewer block; invalid JSON / bad enum / wrong task / extra field / findings missing; stage timeout;
overall timeout; test-command timeout; orphan killed after normal exit and after timeout; SIGTERM of
the orchestrator (→ `INTERRUPTED`, exit 130, orphan killed, journal intact); implementer failure /
no changes / crash; failing tests never reported as success; tests that write files; stale review
(base commit, unknown commit, previous-cycle commit); reviewer edits / deletes / creates an ignored
file; planner writes; implementer self-commits; modification reported even with invalid output;
duplicate completion message; reused task_id; existing task branch; dirty repo; invalid task; runs dir
inside repo; real-client guard (names, symlink, interpreter + script, installed paths, bare names,
disabled backends); CLI exit codes; journal tamper detection; source repo fingerprint unchanged.

## 4. Git / worktree behaviour

- Preflight requires the repo root, a named (non-detached) branch, a clean tree, and that
  `aidev/<task_id>` does not exist.
- `git worktree add -b aidev/<task_id> <runs>/<task_id>/worktree <base_sha>` — the only change to the
  source repository is this new branch and its worktree metadata in `.git/`. Base branch and working
  tree are verified unchanged (fingerprint test; `base_branch_unchanged` in every result).
- Commits use a fixed identity (`aidev-orchestrator`), no global/system git config, hooks disabled,
  no signing. Each commit's parent is checked against the previous cycle's commit.
- Never: merge, push, remote configuration, worktree removal. The report prints the human's merge
  and cleanup commands.

## 5. Failures found and corrected

- **Ignored-file blind spot (design review, before first run):** plain `git status` hides files
  matching `.gitignore`, so a reviewer could create one unseen. Fixed by snapshotting with
  `--ignored` around read-only stages; covered by `test_reviewer_creates_ignored_file`.
- **Test-generated files vs. review check:** a strict "clean tree" rule would misfire on
  `__pycache__`. Resolved by allowing ignored files after tests, rejecting non-ignored ones
  (`TESTS_MODIFIED_WORKTREE`) and running children with `PYTHONDONTWRITEBYTECODE=1`.
- **Interrupted runs left no final record:** added SIGTERM→KeyboardInterrupt translation and an
  `INTERRUPTED` final state; covered by `test_sigterm_interrupts_cleanly`.
- The full suite passed on its first execution; the mutation check above was added to make sure the
  tests are actually capable of failing. That check's deliberately broken "no group kill" copy left six
  `sleep` processes, which were identified by PID and killed.

## 6. Current limitations

- **Detection, not prevention:** agents run as the same Linux user; only changes inside the worktree
  are detected. Real-agent confinement depends on the agentdev distro and native CLI sandboxes (M2).
- Content changes to an already-ignored file are invisible to git status.
- A descendant that calls `setsid()` escapes group cleanup (no cgroups).
- Journal chain is tamper-evident against casual edits only.
- No resume of an interrupted task: rerun with a new `task_id` (the old evidence is preserved).
- Mock agents prove orchestration logic only; they say nothing about real model output quality,
  CLI output formats, latency or quota use. The real backends' argv is unverified.
- Diff sent to the reviewer is capped at 256 KiB (flagged as truncated).

## 7. Readiness for the next stage

Ready for M2 **orchestration-wise**: replacing a `CommandBackend` with real CLI backends only requires
translating the request into each CLI's prompt/flags and its output into the existing response
contract. Not ready **environment-wise**: the agentdev distribution has not been created, logins have
not been done there, and native sandbox behaviour has not been checked.

## 8. One recommended next action

The human prepares and verifies the `agentdev` distribution with
`docs/AGENTDEV-ISOLATION-CHECKLIST.md` (sections 2–6), then explicitly authorises M2: implement the two
real backends and run one controlled real task end-to-end in that distro.
