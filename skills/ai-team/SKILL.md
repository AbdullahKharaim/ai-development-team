---
name: ai-team
description: Start an AI Development Team task (Claude plans and reviews, Codex implements, in the isolated agentdev WSL environment), or show the status of team tasks. Runs only when the user types /ai-team.
disable-model-invocation: true
argument-hint: "<task in natural language> | status [task-id] | list"
allowed-tools: Bash(bash ${CLAUDE_SKILL_DIR}/scripts/aiteam.sh *)
---

# /ai-team — AI Development Team entry point

The user explicitly invoked `/ai-team` with: **$ARGUMENTS**

This invocation authorizes **one** new team task (or a read-only status query). It does not
authorize anything later in the conversation; a further task needs a new `/ai-team`.

Run the helper only like this (Git Bash on Windows):

```bash
bash "${CLAUDE_SKILL_DIR}/scripts/aiteam.sh" <check|prepare|start|status|list> [arg]
```

It forwards to `live/team.py` in the WSL2 distro `agentdev` (user `dev`), which wraps the existing
orchestrator (`python3 -m aidev run-live --confirm-live`). Never call the orchestrator, Codex or
Claude CLIs another way, never pass `--mock-agents`, never edit `live/live_config.json`, never read
credential files, never merge or push.

## A. Status requests

If the arguments are empty, `status`, `status <task-id>` or `list`: run `aiteam.sh status [id]` or
`aiteam.sh list` and answer in Arabic. This is read-only; start nothing. Interpret `phase`:

- `RUNNING` — the workflow is still working (say which step: `current_step`). Not finished.
- `FINISHED` — finished; the result is `state` (`SUCCEEDED` is the only success).
- `DIED` — the process ended without a final result (crash or kill); show `log_tail`, no success.

## B. New task

1. **Check the environment:** `aiteam.sh check`. Stop and explain if it errors.
2. **Engineer the task** (you are the consulting architect). Decide independently; ask the user only
   if the request is genuinely ambiguous. Respect what the team can actually do:
   - Codex works in a sandbox: writes only in the task worktree, **no network, no package
     installation**. Tests run in a sandbox without network or home access. Available toolchain:
     **Python 3 standard library** (incl. `sqlite3`, `http.server`, `json`, `argparse`, `unittest`),
     git. No browser, image generation, external services or paid APIs.
   - Budget (owner-approved `live_limits` from `check`): 2 review cycles, 7 model calls,
     15 min per call, 60 min total. Scope a **complete, testable MVP** that fits; tell the user
     what was deferred. Large requests become a focused first version.
   - Pick `complexity`: `simple`, `normal` (default) or `complex`. This selects Codex's reasoning
     effort through the owner policy (`gpt-6.1-sol`: medium/high/xhigh). Do not choose `simple` to save effort on
     work that needs care.
3. **Prepare:** choose a project name (`a-z0-9-`, descriptive) and run `aiteam.sh prepare <name>`.
   It returns a Linux `staging` path. Write the files below with the Write tool to the Windows path
   `\\wsl.localhost\agentdev` + staging path (forward slashes become backslashes):
   - `task.json`: `{"objective": "...", "acceptance_criteria": ["...", ...], "complexity": "normal"}`
     (optional: `max_cycles`, `max_model_calls`, `max_clarifications` — never above `live_limits`).
   - `repo/SPEC.md`: the binding specification — purpose, layout, every public function/CLI
     command with exact names, inputs, outputs, errors and exit codes, data formats, and these
     rules: Python ≥ 3.10 stdlib only; unit tests under `tests/` (with `tests/__init__.py`), run by
     `python3 -m unittest discover -s tests -t .`; **tests must only write to temporary
     directories**; a README with usage and limitations. Optional skeleton files may go in `repo/`.
   - `acceptance/acceptance_test_<topic>.py`: **hidden trusted acceptance tests** (unittest) that
     import the project from the current directory and check the SPEC's observable behaviour.
     Deterministic, synthetic data only, temporary directories only, CLI via
     `subprocess.run([sys.executable, "-m", ...])`. Test only what SPEC.md states exactly. These
     tests stay outside the repository; Codex can read but not change them.
4. **Summarise in Arabic** (3-6 lines): project name, scope, architecture, what is deferred, complexity.
5. **Start:** `aiteam.sh start <staging>` (use a Bash timeout of 300000 ms). It runs readiness (no
   inference, ~5 s) and launches the real workflow **detached** inside agentdev; it returns
   `task_id`. If it fails, report the error; no model has run.
6. **Report the launch in Arabic:** the `task_id`, that Claude (plan, review) and Codex (implement)
   now collaborate in the background independently of this conversation, the budget, and how to
   follow it: `/ai-team status <task_id>` (or just ask about the team task's status).
   Say clearly: this conversation going idle does **not** mean the task is finished — only
   `phase: FINISHED` does.
7. **Optionally wait:** if a Monitor/background-wait tool is available, you may wait until
   `aiteam.sh status <task_id>` no longer shows `"phase": "RUNNING"` (poll every 60 s, at most
   65 min). Do not loop with repeated short checks in conversation turns.
8. **Final report** (when `FINISHED`): read `report.md` from the run directory
   (`\\wsl.localhost\agentdev` + `run_dir` + `\report.md`) and report in Arabic: final `state`,
   review cycles, model calls, Codex model/effort **requested** (the effective Codex model is not
   reported by the CLI — say so), tests and acceptance result, review findings, final commit,
   project path, and that nothing was merged or pushed. Do not call anything a success unless
   `state` is `SUCCEEDED`.
