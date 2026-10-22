# Installation and daily use

This guide sets up AI Development Team on **Windows 11 with WSL2** and the **Claude Desktop app
(Code tab)**. Part A is a one-time setup that needs a terminal. After that, Part B — starting and
following tasks — happens entirely inside Claude Desktop with `/ai-team`.

## What you need

| Requirement | Why |
|---|---|
| Windows 11 with WSL2 | the agents run in a separate Linux distribution, isolated from your Windows files |
| Claude Desktop app with Claude Code (Code tab) and a Claude subscription | runs the `/ai-team` skill on Windows and Claude Code (plan/review) inside WSL |
| Git for Windows (Git Bash) | Claude Code on Windows runs the skill's helper script in Git Bash |
| ChatGPT subscription with Codex access | runs Codex CLI (implementation) inside WSL |

No API keys are used; both CLIs run on your subscriptions. macOS and Linux desktops are not
supported by the skill bridge (it calls `wsl.exe`).

The tool uses fixed names that the skill relies on: distribution **`agentdev`**, Linux user
**`dev`**, repository at **`/home/dev/ai-development-team`**. Keep them exactly.

## Part A — one-time setup

### A1. Create the isolated `agentdev` distribution (PowerShell)

```powershell
wsl --list --online
wsl --install Ubuntu-26.04 --name agentdev --no-launch
```

Any current Ubuntu release offered by `wsl --list --online` works if it provides Python ≥ 3.10,
git and bubblewrap. Do **not** `wsl --export` an existing distribution — that would copy your
existing home directory and logins into the agent environment.

### A2. Create the user and lock down the distribution (PowerShell, as root inside agentdev)

```powershell
wsl -d agentdev -u root -- useradd --create-home --shell /bin/bash --user-group dev
wsl -d agentdev -u root -- passwd -l dev
wsl -d agentdev -u root -- bash -c "printf '[boot]\nsystemd=true\n\n[automount]\nenabled=false\nmountFsTab=false\n\n[interop]\nenabled=false\nappendWindowsPath=false\n\n[user]\ndefault=dev\n' > /etc/wsl.conf"
wsl -d agentdev -u root -- bash -c "command -v bwrap || (apt-get update && apt-get install -y bubblewrap)"
wsl --terminate agentdev
```

This gives the agents an ordinary account without sudo, no access to your Windows drives
(`automount` off) and no way to start Windows programs (`interop` off). Background and
verification steps: [AGENTDEV-ISOLATION-CHECKLIST.md](AGENTDEV-ISOLATION-CHECKLIST.md).

### A3. Install the tool and the two CLIs (inside agentdev, as `dev`)

Open a shell with `wsl -d agentdev`, then:

```bash
git clone https://github.com/AbdullahKharaim/ai-development-team.git ~/ai-development-team
curl -fsSL https://claude.ai/install.sh -o /tmp/claude-install.sh && bash /tmp/claude-install.sh
curl -fsSL https://chatgpt.com/codex/install.sh -o /tmp/codex-install.sh && CODEX_NON_INTERACTIVE=1 sh /tmp/codex-install.sh
exec bash -l
claude --version && codex --version
```

(You can read the downloaded installer scripts before running them.)

### A4. Log in with your subscriptions (inside agentdev)

```bash
claude auth login --claudeai
codex login --device-auth
claude auth status
codex login status
```

Both logins print a link to open in your Windows browser. Never paste login codes or tokens
into a chat.

### A5. Match the configuration to your installation (inside agentdev)

```bash
cd ~/ai-development-team
python3 live/readiness.py
```

Readiness runs **no model inference**: it checks isolation, executables, flags, logins, the Codex
sandbox, the test sandbox and the Codex model catalog, and writes `live/readiness-report.json`.
Fix any `FAIL` line before continuing. The usual ones on a new machine:

- `claude_version_pinned` / `codex_version_pinned`: the installers fetched newer versions than
  the ones this release was verified with (Claude Code 2.1.300, Codex CLI 0.162.1). Set
  `claude_version` and `codex_version` in `live/live_config.json` to what `claude --version` and
  `codex --version` print, then rerun readiness — the flag checks will tell you if a new version
  changed anything the tool relies on.
- `codex_catalog_has_gpt-6.1-sol`: your Codex account does not list that model. Run
  `codex debug models`, pick a model your plan offers, and set `model` and `allowed_models` in
  `codex_policy` (and `allowed_efforts`/`effort_by_complexity` to levels that model supports).

Optional: `python3 -m unittest discover -s tests` runs the full test suite (mock agents and
fake CLIs only — no real model calls).

### A6. Install the skills into Claude Desktop (PowerShell)

```powershell
Copy-Item -Recurse \\wsl.localhost\agentdev\home\dev\ai-development-team\skills\ai-team "$env:USERPROFILE\.claude\skills\"
Copy-Item -Recurse \\wsl.localhost\agentdev\home\dev\ai-development-team\skills\ai-team-status "$env:USERPROFILE\.claude\skills\"
```

These are personal Claude Code skills: available in every Claude Desktop **Code** session on
this PC (not in Cowork or claude.ai chat). To update them later, repeat with `-Force`.

### A7. Check it works

In Claude Desktop, open a Code session and type `/ai-team status`. The expected answer is that
no team tasks exist yet. Also type `/` and check that `ai-team` appears in the menu.

## Part B — daily use (no terminal)

### Start a task

```
/ai-team Build a command-line expense tracker with categories, monthly summaries and CSV export.
```

The Claude Desktop session acts as the architect: it scopes a version that fits the budget,
writes the specification and hidden acceptance tests, shows you a short summary, then starts the
real workflow and gives you a **task id**. `/ai-team` must be typed by you — Claude never starts
team work on its own, and each `/ai-team` authorizes exactly one task.

### Follow it

```
/ai-team status
/ai-team status <task-id>
/ai-team list
```

or just ask "what is the status of my team task?" (read-only).

| Status | Meaning |
|---|---|
| `RUNNING` | still working (shows the current step: plan, implement, tests, review) |
| `FINISHED` | done — the result is in `state`; only `SUCCEEDED` is success |
| `DIED` | the workflow process stopped without a result (it lists any agent process still running) |

The Desktop conversation going quiet does **not** mean the task is finished — the workflow runs in
the background inside agentdev. Only `FINISHED` means finished.

### Results

- Project: `\\wsl.localhost\agentdev\home\dev\aidev-projects\<name>\`, with the work on the git
  branch `aidev/<task-id>`. Nothing is merged or pushed automatically; merging is your decision.
- Evidence: `...\aidev-projects\runs\<task-id>\` — `report.md`, `result.json`, `events.jsonl`, logs.

### What the team can and cannot do

- Codex works only inside the task's git worktree, **without network access and without
  installing packages**; tests run in a sandbox without network or home-directory access. In
  practice: **Python standard-library projects**. No browser, image generation or external
  services.
- Budget per task (owner-approved `live_limits` in `live/live_config.json`): 2 review cycles,
  7 model calls, 15 minutes per call, 60 minutes in total. Bigger requests are scoped to a
  complete first version.
- Codex model and reasoning effort come from `codex_policy` (default `gpt-6.1-sol`:
  `simple`→medium, `normal`→high, `complex`→xhigh). See
  [ADAPTIVE-COLLABORATION-AND-MODEL-SELECTION.md](ADAPTIVE-COLLABORATION-AND-MODEL-SELECTION.md).

## Troubleshooting

- **`readiness failed; no model was started`** — the CLIs updated themselves or a login expired.
  Run `python3 live/readiness.py` in agentdev and follow A5.
- **`WSL distribution agentdev not found`** — the name must be exactly `agentdev` (A1).
- **`DIED`** — see `log_tail` in the status output and the run's `logs/` directory.
