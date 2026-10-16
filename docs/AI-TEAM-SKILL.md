# `/ai-team` — Claude Code skill entry point

Lets the owner start an AI Development Team task from a normal **Claude Desktop → Code** session
by typing `/ai-team <task in natural language>`, without a terminal, JSON or manual Codex launch.

## How it works

```
Claude Desktop (Windows, Code tab)
  /ai-team <task>   ── explicit only (disable-model-invocation: true)
     │  the session acts as consulting architect: scopes an MVP that fits the live limits,
     │  writes SPEC.md + hidden acceptance tests into a staging dir (\\wsl.localhost\agentdev\...)
     ▼
  skills/ai-team/scripts/aiteam.sh  (Git Bash; validated arguments only)
     ▼  wsl.exe -d agentdev -u dev
  live/team.py start  ── readiness (no inference) ──► python3 -m aidev run-live --confirm-live
                         launched DETACHED (own session), so it outlives the Desktop turn
     ▼
  orchestrator: Claude PLAN → Codex IMPLEMENT/FIX (gpt-6.1-sol, policy effort) → sandboxed tests
                → Claude REVIEW of the exact commit … within live_limits
  status: team.py status (read-only) ◄── /ai-team status <id>  or the ai-team-status skill
```

Nothing in this path bypasses the orchestrator: live limits, readiness gate, model policy and
catalog check, Codex sandbox, test sandbox, commit/SHA binding and redaction all apply unchanged.
`aiteam.sh` cannot pass mock agents, cannot change limits and only accepts a validated project
name, staging path or task id.

## Skills

| skill | invocation | what it can do |
|---|---|---|
| `ai-team` | **only** when the user types `/ai-team …` (`disable-model-invocation: true`; Claude cannot load it on its own, its description is not even in the model's context) | start one task per invocation; `status`/`list` |
| `ai-team-status` | the user's question about their team task, or `/ai-team-status` | read-only status via `status.sh`, which can only run `team.py status/list` |

Installed as **personal skills** of the Windows Claude Code used by the desktop app:
`C:\Users\<user>\.claude\skills\ai-team\` and `...\ai-team-status\`. Personal skills are available
in every local Claude Code project on that machine. They are **not** available in Cowork
(which does not read `~/.claude/skills`) or in claude.ai chat.

Install / update from this repository (PowerShell on Windows):

```powershell
Copy-Item -Recurse -Force \\wsl.localhost\agentdev\home\dev\ai-development-team\skills\ai-team "$env:USERPROFILE\.claude\skills\"
Copy-Item -Recurse -Force \\wsl.localhost\agentdev\home\dev\ai-development-team\skills\ai-team-status "$env:USERPROFILE\.claude\skills\"
```

## Usage

```
/ai-team Build a command-line expense tracker with categories, monthly summaries and CSV export.
/ai-team status
/ai-team status <task-id>
/ai-team list
```

Status phases: `RUNNING` (still working; `current_step` shows plan/implement/tests/review and
cycle), `FINISHED` (final `state`; only `SUCCEEDED` is success), `DIED` (process ended without a
result). The Desktop conversation going idle says nothing about the task: a task is finished only
when its status is `FINISHED`.

Files: project `~/aidev-projects/<name>/` (task branch `aidev/<task-id>`, never merged or pushed),
hidden tests `~/aidev-projects/<name>-acceptance/`, evidence `~/aidev-projects/runs/<task-id>/`
(`events.jsonl`, `result.json`, `report.md`), launcher record and log `~/aidev-projects/tasks/`.

## Capabilities the skill plans for (verified)

- Codex: writes only in the task worktree, no network, no package installation; Python 3.14 stdlib,
  git. Tests: bubblewrap sandbox without network or home access.
- Claude (plan/review): no tools; repository files, diff and test output are given in the prompt.
- Not available to the agents: browser, image generation, external services, paid APIs.
- Budget per task (owner-approved `live_limits`): 2 review cycles, 7 model calls, 15 min per call,
  60 min total. Larger requests are scoped down to a complete MVP.

## Verification (no real model calls)

- `tests/test_team.py` (10 tests, mock agents): full flow to `SUCCEEDED`; a running task reports
  `RUNNING` with its current step, never a result; a SIGKILLed workflow reports `DIED`; SIGTERM gives
  `FINISHED`/`INTERRUPTED`; status/list are read-only (byte-identical files before/after);
  budget exhaustion reported with findings; refusals (bad name, existing project, missing SPEC or
  acceptance tests, unsupported fields, foreign directories); live limits still enforced by run-live.
- Windows bridge, run from Claude Desktop's Git Bash with the installed skill paths: `check`;
  `status`/`list` with no tasks; injection attempts rejected; `prepare` + files written over
  `\\wsl.localhost` + incomplete `start` refused with nothing launched; the same staging launched
  with mock agents ran detached after `wsl.exe` returned and was followed from Windows from
  `RUNNING` to `FINISHED`/`SUCCEEDED`.
- Detached processes inside agentdev kept running for the full probe duration after the launching
  `wsl.exe` call returned.
- After installation the session's skill list showed `ai-team-status` but not `ai-team`, i.e.
  `ai-team` is hidden from model invocation.

Known limitation: agents run in their own process session (needed for the orchestrator's group
cleanup). If the orchestrator itself is killed (`DIED`), an agent that was running keeps going
until it ends by itself; `status` then lists it under `orphan_processes` (reported, not killed).

Not verified here: a real (non-mock) `/ai-team` run — it needs the owner's explicit invocation and
consumes subscription usage.
