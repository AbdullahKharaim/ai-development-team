---
name: ai-team-status
description: Read-only status of AI Development Team tasks that the user started earlier with /ai-team — use when the user asks about the progress, status or result of their team task (e.g. "what is the status of my team task?"). It never starts, continues or changes a task.
allowed-tools: Bash(bash ${CLAUDE_SKILL_DIR}/scripts/status.sh *)
---

# AI Development Team — status (read-only)

Run one of:

```bash
bash "${CLAUDE_SKILL_DIR}/scripts/status.sh" status            # latest team task
bash "${CLAUDE_SKILL_DIR}/scripts/status.sh" status <task-id>
bash "${CLAUDE_SKILL_DIR}/scripts/status.sh" list              # all team tasks
```

Answer in the user's language, from the JSON only:

- `phase: RUNNING` — still working; name `current_step` (stage, cycle). Not finished.
- `phase: FINISHED` — done; the outcome is `state`. Only `SUCCEEDED` is success. Report
  `review_cycles`, `model_calls`, `final_commit`, the requested Codex model/effort
  (`model_selection`; the effective Codex model is not reported by the CLI), review findings and
  the project path. For details, read `report.md` in the run directory via
  `\\wsl.localhost\agentdev` + `run_dir`.
- `phase: DIED` — the workflow process ended without a result (crash/kill); quote `log_tail`.

Never start, resume or modify a task from this skill. Starting work requires the user to type
`/ai-team <task>`.
