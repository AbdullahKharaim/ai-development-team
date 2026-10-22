# AI Development Team (`aidev`)

**A deterministic orchestrator that lets Claude Code and OpenAI Codex CLI build software together —
Claude plans and independently reviews, Codex implements, trusted code enforces the rules, and the
human decides what gets merged.**

You type one command in Claude Desktop:

```
/ai-team Build a CSV quality checker that reports empty cells and duplicate rows as JSON.
```

and a real Claude ↔ Codex workflow runs in an isolated WSL2 environment: plan → implement →
sandboxed tests (including hidden acceptance tests) → review of the exact commit → fixes if needed,
within a fixed budget, with a hash-chained audit journal and a final report. No messages are
copied between the agents by hand.

> **Status — what is verified.** Three real, subscription-authenticated runs succeeded
> (62.4 s, 352.4 s and 256.7 s), the last one started from `/ai-team` with an explicit Codex model
> (`gpt-6.1-sol`, effort `high`). Each was approved in its first review cycle, so the multi-cycle
> fix loop, clarification/re-planning, stall detection and continuation are so far verified only
> with deterministic mock agents (134 automated tests). Details: [docs/REAL-RUNS.md](docs/REAL-RUNS.md).

## How it works

```
 Claude Desktop (Windows)                         WSL2 distribution "agentdev" (user dev, no sudo,
 /ai-team <task>  (explicit only)                 no Windows drives, no Windows interop)
   │ architect: scope, SPEC.md,                   ┌─────────────────────────────────────────────┐
   │ hidden acceptance tests                      │ readiness gate (no inference)               │
   └── skills/ai-team/scripts/aiteam.sh ─wsl.exe─►│ aidev run-live                              │
                                                  │  PLAN     Claude Code, no tools             │
                                                  │  MODEL    policy + CLI catalog → model/effort│
                                                  │  IMPLEMENT/FIX  Codex in its :workspace     │
                                                  │           sandbox; must not commit          │
                                                  │  commit   by the orchestrator → SHA         │
                                                  │  TEST     bubblewrap: no network, no home   │
                                                  │  REVIEW   Claude, reviewed_sha must = SHA   │
                                                  │  ↺ fix while budget allows; stop on         │
                                                  │    approval / stall / budget / block        │
                                                  └─────────────────────────────────────────────┘
 /ai-team status ◄── read-only status ───────────  report.md · result.json · events.jsonl
```

- **Roles.** Claude Code plans and reviews with *all tools disabled* (`--tools ""`); the repository
  files, diff and test output are given in the prompt. Codex CLI implements inside its native
  `:workspace` sandbox (writes only in the task's git worktree, `.git` read-only, no network).
- **Trust.** Facts come from the orchestrator, not the models: it makes every commit, runs the
  owner's test command in a bubblewrap sandbox, and requires the review to name the exact commit
  SHA. Plan and review must leave the worktree untouched.
- **Codex model and reasoning effort.** Chosen by an owner policy (`live/live_config.json`):
  `simple`→medium, `normal`→high, `complex`→xhigh on `gpt-6.1-sol` by default. The planner may only
  *raise* the owner's complexity class. The choice is validated against the installed CLI's own
  model catalog before any call, because the CLI accepts unknown model/effort values silently.
- **Adaptive reviews.** Review/fix cycles continue until Claude approves the exact commit and the
  tests pass — bounded by review-cycle, model-call, clarification and time budgets, with stall
  detection for repeated findings. Final states include `SUCCEEDED`, `BUDGET_EXHAUSTED`, `STALLED`,
  `BLOCKED` and `MODEL_UNSUPPORTED`. More: [docs/ADAPTIVE-COLLABORATION-AND-MODEL-SELECTION.md](docs/ADAPTIVE-COLLABORATION-AND-MODEL-SELECTION.md).
- **`/ai-team` skill.** `disable-model-invocation: true`: Claude can never start team work on its
  own; each typed `/ai-team` authorizes one task. A separate read-only skill answers status
  questions. More: [docs/AI-TEAM-SKILL.md](docs/AI-TEAM-SKILL.md).

## Getting started

Requirements: **Windows 11 + WSL2**, the **Claude Desktop app** (Code tab) with a Claude
subscription, **Git for Windows**, and a **ChatGPT subscription with Codex**. No API keys.

Follow **[docs/INSTALL.md](docs/INSTALL.md)**: one-time setup (create the isolated `agentdev`
distribution, install and log in to both CLIs, run the readiness check, copy the two skills into
Claude Desktop), then daily use with `/ai-team` only.

Without Claude Desktop, the same workflow runs from a shell inside agentdev:

```bash
python3 live/readiness.py                      # non-inference checks → live/readiness-report.json
python3 -m aidev run-live --confirm-live --task TASK.json --repo REPO --runs-dir RUNS \
        --readiness live/readiness-report.json
python3 -m unittest discover -s tests          # 134 tests; mock agents and fake CLIs only
```

## Limits — read before relying on it

- **Python standard-library projects only, in practice.** Codex has no network and cannot install
  packages; tests run without network.
- **Budget per live task** (owner-approved `live_limits`): 2 review cycles, 7 model calls, 15 min
  per call, 60 min total. The protocol ceiling is higher; raising the live limits is an explicit
  config change.
- **Versions are pinned** (Claude Code 2.1.300, Codex CLI 0.162.1); after a CLI update the
  readiness gate fails until the pins are updated and re-checked.
- **The effective Codex model is not verifiable** — `codex exec --json` does not report it; only
  the requested model/effort is recorded as fact.
- **Isolation is a blast-radius reduction, not a hardened sandbox.** Codex's sandbox confines
  writes but not reads (the agent can read anything the `dev` user can, including its own login
  token); WSL2 distributions share one kernel. Changes outside the task worktree are not detected.
- **Windows only** for the skill bridge; the orchestrator itself is plain Linux Python.
- Stall detection compares normalised finding text; rephrased repeats are bounded by the budgets.
- Redaction masks all e-mail addresses in logs and prompts, including synthetic `@example.com` data.

## Repository layout

| path | contents |
|---|---|
| `aidev/` | orchestrator (state machine, budgets, git, sandboxes, CLI backends, model policy, journal) |
| `live/` | readiness check, Codex sandbox probe, live config and limits, `/ai-team` task launcher (`team.py`), trial task |
| `skills/` | the `ai-team` and `ai-team-status` Claude Code skills |
| `tests/`, `mocks/` | 134 unit/integration tests with deterministic mock agents and fake CLIs |
| `examples/` | a reproducible mock demo (`python3 examples/run_example.py`) and its output |
| `docs/` | install guide, real-run record, design notes, isolation checklist, development history |

## Authorship

Designed and built by Abdullah as a supervised AI-assisted project: Claude (Anthropic) acted as
architect and implementation engineer for the orchestrator under the owner's direction, and
commits carry `Co-Authored-By` trailers. Projects produced *by* the orchestrator are written by the
Claude Code + Codex CLI agents it coordinates.

## License

[MIT](LICENSE) © 2026 Abdullah Ali Mohammed Al-Kharaim
