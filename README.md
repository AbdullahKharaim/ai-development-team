# aidev — M1 orchestrator (mock agents only)

A small, deterministic Python orchestrator for a supervised two-agent development
workflow: **Claude plans and reviews, Codex implements, trusted code enforces the
workflow, the human merges.**

> **Status (M1):** the full workflow runs end-to-end against *deterministic mock
> agents*. No real Claude or Codex session has been run through this
> orchestrator yet. Real backends are stubbed and hard-disabled.

## Workflow

```
 human: task.json ──► aidev run
                         │
   preflight: repo root, clean tree, base branch/SHA, task branch absent
                         │
   git worktree add -b aidev/<task_id>  (outside the repo, in runs/<task_id>/worktree)
                         │
   PLAN       planner  (read-only; worktree snapshot must be identical afterwards)
                         │
   ┌── IMPLEMENT / FIX   implementer writes files; must NOT commit
   │        │
   │   orchestrator: git add -A && commit  ──► SHA_n  (parent must be SHA_{n-1})
   │        │
   │   TEST  orchestrator runs task.test_command (from the human, not from a model)
   │        │
   │   REVIEW  reviewer (read-only) gets diff base..SHA_n + test result
   │        │    reviewed_sha must equal SHA_n, worktree snapshot must be unchanged
   │        │
   │   approve + tests pass ─► SUCCEEDED
   │   approve + tests fail ─► APPROVED_WITH_FAILING_TESTS (human decides)
   │   blocked              ─► ESCALATED_BY_REVIEWER
   └── changes_requested and cycle < max_cycles (≤ 2) ─► next cycle with findings
                         │
   report.md + result.json + events.jsonl   — nothing merged, nothing pushed
```

The orchestrator owns sequencing, git, tests, limits and the journal. Agents
see a JSON request on stdin and the worktree as cwd; they print one JSON
response. Agent claims (e.g. `files_changed`) are recorded but git is the source
of truth.

## Usage

Requires Linux (WSL2 Ubuntu here), Python ≥ 3.10, git. Standard library only.

```bash
cd m1_orchestrator
python3 -m unittest discover -s tests          # full suite
python3 examples/run_example.py                # mock demo → examples/output/
python3 -m aidev run --task T.json --repo /path/to/repo --runs-dir /path/to/runs --agents agents.json
python3 -m aidev verify-journal runs/<task_id>/events.jsonl
```

Exit codes: `0` SUCCEEDED · `1` any other final state · `2` refused before start · `130` interrupted.

`agents.json` maps `planner`, `implementer`, `reviewer` to `{argv, env?}`;
`argv[0]` must be an absolute path. `task.json` fields:

| field | meaning |
|---|---|
| `task_id` | `[a-z0-9][a-z0-9-]{0,62}`; also the branch name `aidev/<task_id>` and run directory |
| `objective`, `acceptance_criteria` | passed to every agent |
| `test_command` | argv run by the orchestrator in the worktree after every commit |
| `max_cycles` | 1–2 implementation/review cycles (default 2; hard ceiling 2) |
| `stage_timeout_s`, `test_timeout_s`, `task_timeout_s` | per-process and overall limits |

## Layout

| path | role |
|---|---|
| `aidev/orchestrator.py` | state machine, stage checks, final states |
| `aidev/proc.py` | bounded subprocess runner: process group, deadline, output limits, group kill, real-client guard |
| `aidev/gitops.py` | git wrapper with fixed identity, no user/system config, hooks disabled |
| `aidev/protocol.py` | task and response validation (strict, stdlib) |
| `aidev/journal.py` | hash-chained JSONL journal + verifier |
| `aidev/backends.py` | `CommandBackend` (used), `ClaudeCliBackend`/`CodexCliBackend` (M2 shape, disabled) |
| `aidev/report.py` | Markdown report from `result.json` data |
| `mocks/fake_agent.py` | deterministic mock Claude (`--role claude`) and Codex (`--role codex`) |
| `tests/` | 58 unit + integration tests on disposable repos under `.test-work/` |
| `examples/` | demo task, mock scenario, runner, and captured output |
| `docs/AGENTDEV-ISOLATION-CHECKLIST.md` | how to prepare the isolated WSL distro for M2 |
| `M1-REPORT.md` | what was built, tested, and what remains |

## Final states

`SUCCEEDED`, `MAX_CYCLES_REACHED`, `ESCALATED_BY_REVIEWER`, `APPROVED_WITH_FAILING_TESTS`,
`FAILED_PREFLIGHT`, `FAILED_IMPLEMENTATION`, `AGENT_FAILED`, `INVALID_RESPONSE`,
`CONFLICTING_COMPLETION`, `STALE_REVIEW`, `UNAUTHORIZED_MODIFICATION`,
`TESTS_MODIFIED_WORKTREE`, `STAGE_TIMEOUT`, `TASK_TIMEOUT`, `CLEANUP_FAILED`,
`FORBIDDEN_EXECUTABLE`, `INTERRUPTED`, `INTERNAL_ERROR`.
Only `SUCCEEDED` is success; every state is written to the journal, `result.json` and `report.md`.

## What is enforced, and what is not

Enforced by orchestrator code (and tested):

- Review is bound to the exact commit: `reviewed_sha` must equal the commit just created.
- Read-only stages (plan, review) are checked by comparing `(HEAD, git status incl. ignored)`
  before/after; any difference stops the run. Implementers may not move HEAD or switch branch.
- At most 2 cycles; per-process and overall deadlines; the child's whole process group is
  SIGKILLed after every stage, including after a normal exit.
- Exactly one JSON object per response, with exact fields; task/stage/cycle must match.
- No merge, push, remote, or deletion of evidence; a reused `task_id` is refused.
- Children run with `PATH=/usr/bin:/bin` and `HOME=<run>/home`; the executables `claude`/`codex`
  and anything under `~/.local/bin`, `~/.claude`, `~/.codex` are refused before spawn.

**Not** provided (M1 is not a sandbox):

- **Detection, not prevention.** An agent runs with the orchestrator's Linux identity and can
  read or write anything that user can. The checks above detect changes *inside the worktree*
  after the fact; git makes them reversible. Writes elsewhere on the filesystem are not seen.
- Git gives rollback and integrity of the reviewed artifact, **not confidentiality**.
- Content changes inside an already-ignored file are invisible to `git status`.
- A descendant that calls `setsid()` escapes process-group cleanup (needs cgroups).
- The journal hash chain detects casual edits, not a deliberate rewrite of the whole file.
- Filesystem isolation for real agents is the job of the separate `agentdev` WSL distro
  plus each CLI's native sandbox — see the checklist. It is not implemented or verified here.
