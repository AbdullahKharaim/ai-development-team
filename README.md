# AI Development Team (`aidev`)

A small, deterministic Python orchestrator that makes the official **Claude Code** and
**OpenAI Codex CLI** clients collaborate on a software task without a human relaying messages:
**Claude plans and reviews, Codex implements, trusted code enforces the workflow, the human merges.**

> **Verified status:** two real automated runs have completed with real, subscription-authenticated
> CLIs: a small `parse_duration` task (62.4 s) and the `authlog-threat-analyzer` MVP (352.4 s),
> each with one review cycle. The **adaptive multi-cycle loop, clarification/re-planning and the
> explicit Codex model selection** described below are implemented and covered by mock-agent tests,
> but have **not yet been exercised in a real run**.

## Workflow

```
 human: task.json ──► aidev run-live --confirm-live            (aidev run = mock/command agents)
   preflight: repo root, clean tree, base SHA, branch absent, owner-approved live limits, readiness
   git worktree add -b aidev/<task_id>   (outside the repo)
   PLAN      Claude (no tools; repo files given in the prompt) ──► plan + complexity recommendation
   MODEL     trusted code: owner policy + CLI catalog ──► Codex model / reasoning effort (or stop)
 ┌─ IMPLEMENT/FIX  Codex in its :workspace sandbox; must not commit
 │     └─ may answer "needs_clarification" ──► Claude RE-PLAN ──► Codex continues (bounded)
 │   orchestrator commits ──► SHA_n (parent must be SHA_{n-1})
 │   TEST      trusted test command inside bubblewrap (no network, no home, read-only worktree)
 │   REVIEW    Claude (no tools) gets diff + test result; reviewed_sha must equal SHA_n
 │   approve + tests pass ─► SUCCEEDED          approve + tests fail ─► APPROVED_WITH_FAILING_TESTS
 │   blocked ─► BLOCKED       same findings keep returning ─► STALLED
 └─ changes_requested, budget left ─► next FIX with the findings
                              budget used up ─► BUDGET_EXHAUSTED (human may continue: continue_from)
   report.md + result.json + hash-chained events.jsonl   — nothing merged, nothing pushed
```

Details: [docs/ADAPTIVE-COLLABORATION-AND-MODEL-SELECTION.md](docs/ADAPTIVE-COLLABORATION-AND-MODEL-SELECTION.md)
and [live/LIVE-RUN.md](live/LIVE-RUN.md).

## Usage

Linux (here: the isolated WSL2 distro `agentdev`), Python ≥ 3.10, git, bubblewrap. Standard library only.

```bash
python3 -m unittest discover -s tests          # full suite (mock agents and fake CLIs only)
python3 live/readiness.py                      # non-inference checks; writes live/readiness-report.json
python3 -m aidev run-live --confirm-live --task T.json --repo REPO --runs-dir RUNS \
        --readiness live/readiness-report.json
python3 -m aidev verify-journal RUNS/<task_id>/events.jsonl
```

Exit codes: `0` SUCCEEDED · `1` any other final state · `2` refused before start · `130` interrupted.

### Task file

| field | meaning |
|---|---|
| `task_id` | `[a-z0-9][a-z0-9-]{0,62}`; branch `aidev/<task_id>` and run directory |
| `objective`, `acceptance_criteria` | passed to every agent |
| `test_command` | argv run by the orchestrator after every commit (trusted, from the human) |
| `complexity` | `simple` / `normal` (default) / `complex` — owner's class; the planner may only raise it |
| `max_cycles` | implementation/review cycles allowed (default 2; protocol ceiling 10) |
| `max_model_calls` | all model calls (default `1 + 2·max_cycles + 2·max_clarifications`; ceiling 60) |
| `max_clarifications` | implementer question → re-plan rounds (default 1; ceiling 3) |
| `stall_repeat_limit` | same finding in this many reviews → `STALLED` (default 3) |
| `continue_from` | absolute path of a stopped run to continue (human extension) |
| `stage_timeout_s`, `test_timeout_s`, `task_timeout_s` | per-call and overall time limits |

`run-live` additionally refuses any task whose limits exceed the owner-approved `live_limits` in
[live/live_config.json](live/live_config.json) (currently 2 cycles, 7 calls, 1 clarification,
900 s per call, 3600 s per task — the limits approved for the earlier live runs).

## Layout

| path | role |
|---|---|
| `aidev/orchestrator.py` | adaptive state machine, budgets, stall detection, continuation, git/test checks |
| `aidev/model_policy.py` | Codex model / reasoning-effort policy and catalog validation |
| `aidev/cli_backends.py` | live Claude Code and Codex CLI adapters (argv, prompts, output parsing, audit metadata) |
| `aidev/live.py` | readiness report → `LiveGate` (versions, executables, Codex model catalog) |
| `aidev/proc.py` | bounded subprocesses, process-group cleanup, real-client allowlist |
| `aidev/test_sandbox.py` | bubblewrap sandbox for the trusted test command |
| `aidev/protocol.py`, `journal.py`, `gitops.py`, `redact.py`, `report.py` | contracts, journal, git, redaction, report |
| `live/` | readiness check, sandbox probe, live config, trial task |
| `mocks/`, `tests/` | deterministic mock agents, fake CLIs, unit and integration tests |

## Final states

`SUCCEEDED`, `APPROVED_WITH_FAILING_TESTS`, `BUDGET_EXHAUSTED`, `STALLED`, `BLOCKED`,
`MODEL_UNSUPPORTED`, `FAILED_PREFLIGHT`, `FAILED_IMPLEMENTATION`, `AGENT_FAILED`,
`INVALID_RESPONSE`, `CONFLICTING_COMPLETION`, `STALE_REVIEW`, `UNAUTHORIZED_MODIFICATION`,
`TESTS_MODIFIED_WORKTREE`, `STAGE_TIMEOUT`, `TASK_TIMEOUT`, `CLEANUP_FAILED`,
`FORBIDDEN_EXECUTABLE`, `INTERRUPTED`, `INTERNAL_ERROR`.
Only `SUCCEEDED` is success. (`MAX_CYCLES_REACHED` and `ESCALATED_BY_REVIEWER` from earlier
versions are now `BUDGET_EXHAUSTED` and `BLOCKED`.)

## What is enforced, and what is not

Enforced by orchestrator code (and tested):

- Review is bound to the exact commit after **every** revision; commits form a linear chain.
- Plan, re-plan and review must leave the worktree unchanged; implementers may not commit or switch branch.
- Review-cycle, model-call, clarification and time budgets; per-call timeouts; process-group cleanup.
- Codex never runs on the CLI's implicit default model/effort: model and effort are explicit and
  validated against the owner policy and the installed CLI's catalog before the call.
- Trusted tests run in bubblewrap; live logs, test output and prompts are redacted.
- No merge, push or remote; a reused `task_id` is refused; continuation requires intact evidence.

Not provided:

- Detection, not prevention, outside the worktree: agents run as the `dev` user; Codex's sandbox
  confines writes but **not reads**. Isolation from the owner's files comes from the `agentdev` distro.
- The **effective Codex model is not verifiable**: `codex exec --json` (0.162.1) reports no model or
  effort. Only the requested values are recorded as fact. Claude's effective model *is* reported.
- Stall detection compares normalised finding text; rephrased repeats are bounded only by budgets.
- The journal hash chain detects casual edits, not a deliberate rewrite.
