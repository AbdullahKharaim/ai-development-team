# M2C — live Claude → Codex → Claude integration (prepared, not yet run)

Environment: WSL2 distro `agentdev`, user `dev` (no sudo, no Windows mounts/interop),
Claude Code 2.1.300 (Claude Pro login), Codex CLI 0.162.1 (ChatGPT login). This working
copy was cloned from M1 commit `cc896bd`; the original in the Ubuntu distro is untouched.

**No real model session has been run.** Real collaboration is not yet demonstrated.

## What runs where

| Stage | Executable | Restrictions (all flags verified on the installed versions) |
|---|---|---|
| PLAN, REVIEW | Claude Code `-p` | `--tools ""` (no tools), `--safe-mode`, `--strict-mcp-config`, `--permission-mode dontAsk --permission-prompts none`, `--no-session-persistence`, `--disable-slash-commands`, structured output via `--json-schema`; cwd = an **empty scratch dir**; repo files, diff and test results are supplied in the prompt by the orchestrator |
| IMPLEMENT, FIX | `codex exec` | built-in permission profile `:workspace` (probed: writes only in the task worktree and /tmp, `.git` read-only so no commits, network blocked), `approval_policy="never"`, `--strict-config --ignore-user-config --ignore-rules --ephemeral`, `--output-schema` + `-o` |
| TEST | orchestrator, inside **bubblewrap** (`aidev/test_sandbox.py`, mandatory for live backends) | `/usr/bin/bwrap --unshare-all`: no network; tmpfs over /home (no home data, no ~/.claude or ~/.codex); worktree and the test runner/acceptance dir bound read-only; /usr read-only; private /tmp; cleared env; killed with its PID namespace on timeout. Runs `live/trial/run_tests.py`: repo unit tests + hidden acceptance tests |

Unchanged from M1: state machine, orchestrator-made commits, `reviewed_sha` binding, read-only
snapshots, ≤ 2 cycles, timeouts, process-group cleanup, hash-chained journal, no merge/push.
New: per-call `backend_meta` journal events (tokens, turns, cost field, permission denials,
Codex event counts) and redaction of tokens/e-mails in every live log file, in test output (logs,
result.json, the tail handed to the reviewer) and in every prompt sent to either CLI.

## Verified without inference

- `live/readiness.py` → **55/55 checks passed**: isolation (no root/sudo, no drvfs, no Windows
  PATH, no API-key env vars, distro = agentdev), native user-local ELF executables, pinned versions,
  required flags present in `--help`, the adapters' **exact argv** parse (both CLIs exit with
  "no prompt" on empty stdin, before any model call), subscription login mode (Claude `claude.ai`
  Pro; Codex "Logged in using ChatGPT"), the Codex sandbox probe (`live/sandbox_probe.sh`), and the test sandbox (home canary and both CLI
  config dirs absent, network denied, worktree read-only).
- Tests (see below): M1's 58 plus 22 live-adapter tests driving the real adapters against fake CLIs that
  reproduce both CLIs' output formats (end-to-end success, fix cycle, stale SHA, tool use detected
  via the scratch dir, error/non-JSON results, missing Codex final message, self-commit, redaction,
  timeout + descendant cleanup, gate refusal, run-live refusals, source repo untouched).

## Blocker: one approval needed

The M1 guard in `aidev/proc.py` refuses any executable named `claude`/`codex` or located under
`~/.local/bin`, `~/.claude`, `~/.codex`. It still applies to the live backends, so a live run today
stops at `FORBIDDEN_EXECUTABLE` before spawning anything (covered by a test).

Changing that guard was refused by this session's safety classifier as a security weakening, so it
was **not** applied. The minimal change is in `live/PROPOSED-GUARD-CHANGE.patch` (applies cleanly):
`run_bounded(..., allowed_client=PATH)` exempts exactly one absolute launcher path, which the live
backends take from a `LiveGate` built only from a passed, fresh (< 6 h), version-matching readiness
report. All other guard checks stay. The owner reviews and applies it:

```bash
cd ~/ai-development-team && git apply live/PROPOSED-GUARD-CHANGE.patch && python3 -m unittest discover -s tests
```

## First live run (after the patch is approved and applied)

```bash
cd ~/ai-development-team
python3 live/trial/create_trial_repo.py                       # ~/aidev-trial/repo (synthetic)
python3 live/readiness.py                                     # must print PASSED
python3 -m aidev run-live --confirm-live --task live/trial/task.json \
  --repo ~/aidev-trial/repo --runs-dir ~/aidev-trial/runs --readiness live/readiness-report.json
```

Scope: one task (`parse_duration`), ≤ 2 cycles, 900 s per model call, 3600 s overall (enforced by
`run-live` regardless of the task file), no privileges, no network for Codex tools, no merge/push.
Evidence: `~/aidev-trial/runs/live-parse-duration-001/` (report.md, result.json, events.jsonl, logs/).
Expected subscription usage: 2–5 model calls.

## Residual risks (accepted, disclosed)

- Codex's sandbox does **not** confine reads: Codex can read anything `dev` can, including both CLIs'
  login files. Mitigation: only synthetic, owner-authored input; Codex tools have no network.
- Claude's tool-free mode is flag-configured; effectiveness is observed per call (scratch dir must
  stay empty, `permission_denials` recorded, worktree snapshot unchanged), not proven in advance.
- The CLI processes themselves talk to their providers; prompts include the trial repo's files.
- Hidden acceptance tests are readable by Codex (not modifiable).
- The M1 limits remain: detection not prevention outside the worktree; setsid escapes; journal
  chain is tamper-evident only.

## Update: adaptive collaboration and explicit Codex model selection

Since branch `upgrade/adaptive-collab-model-selection`, live runs pass an explicit Codex model and
reasoning effort (`gpt-6.1-sol` / `high` by default) validated against the owner policy and the CLI
catalog, and the review/fix loop is adaptive within the owner-approved `live_limits` of
`live/live_config.json`. See [../docs/ADAPTIVE-COLLABORATION-AND-MODEL-SELECTION.md](../ADAPTIVE-COLLABORATION-AND-MODEL-SELECTION.md).
These features have not yet been exercised in a real run.
