# Adaptive collaboration and Codex model selection

Verified with deterministic mock agents and fake CLIs, plus non-inference checks against the
installed CLIs. In real use, **explicit model selection** has been exercised once (the
`csv-quality-checker` run requested `gpt-6.1-sol`/`high` and succeeded); the **multi-cycle,
clarification, stall and continuation** paths have not yet occurred in a real run
(see [REAL-RUNS.md](REAL-RUNS.md)).

## 1. Codex model and reasoning-effort selection

### What the installed CLI supports (Codex CLI 0.162.1, checked without inference)

- `codex exec -m <model>` and `-c model_reasoning_effort="<effort>"` are accepted together with the
  existing flags (`--strict-config --ignore-user-config ...`): with empty stdin the CLI stops with
  "No prompt provided via stdin." before any model call.
- **The CLI does not validate either value at that point** — `-m no-such-model` and
  `model_reasoning_effort="bogus"` are accepted the same way. Validation therefore happens in
  trusted code, against the catalog the CLI itself renders.
- `codex debug models` (no inference) lists `gpt-6.1-sol` with reasoning levels
  `low, medium, high, xhigh, max, ultra` and **default level `low`**. Listing in this catalog,
  rendered for the logged-in ChatGPT account, is the strongest pre-call evidence available; it is
  not proof that a call will be accepted — only a real call can show that.
- `codex exec --json` events contain **no model or effort field**, so the effective model of a
  call cannot be verified from the CLI's output.

### What the two earlier real runs used

Neither run requested a model or effort (`codex_model: null`, no `-m`, user config ignored), so
Codex used its built-in default. Which model that was is **not recorded** anywhere in the
evidence and is not claimed.

### Policy (`live/live_config.json` → `codex_policy`)

```json
"codex_policy": {
  "model": "gpt-6.1-sol",
  "effort_by_complexity": {"simple": "medium", "normal": "high", "complex": "xhigh"},
  "allowed_models": ["gpt-6.1-sol"],
  "allowed_efforts": ["medium", "high", "xhigh"]
}
```

1. The owner sets `complexity` in the task (default `normal` → `high`).
2. Claude's plan carries a `complexity` recommendation with a rationale. Trusted code accepts it
   only if it is **higher** (e.g. `complex` → `xhigh`); a lower recommendation is recorded and
   ignored, so there is no silent downgrade. A re-plan can raise it again.
3. The selection must be in the policy's allow-lists **and** in the CLI catalog captured by
   `live/readiness.py`. Otherwise: `PreflightError` before any call (owner's own class), or final
   state `MODEL_UNSUPPORTED` before Codex is invoked (planner-raised class). There is no fallback
   model and no API-key path.
4. Codex is started with `exec -m <model> -c model_reasoning_effort="<effort>" ...`; the adapter
   refuses to build an argv without an explicit selection.

To change the model or levels, the owner edits `codex_policy` and reruns `live/readiness.py`
(which re-checks the catalog and the exact argv for every level). `low`, `max` and `ultra` are not
approved by default.

### Audit metadata

- `model_selected` event: model, effort, complexity, `complexity_source` (`task` / `planner-raised`),
  rationale, policy SHA-256, whether the catalog was checked.
- Codex `backend_meta`: `requested_model`, `requested_effort`, `cli_version`,
  `effective_model: null`, `effective_effort: null`, `effective_model_source:
  "not reported by codex exec --json (0.162.1); unverified"`. If a future CLI reports the model in
  an event, it is recorded and `model_mismatch: true` is set when it differs.
- Claude `backend_meta`: `effective_models` from the CLI's own `modelUsage` field.

## 2. Adaptive collaboration

The agents decide *whether* more work is needed; the orchestrator decides *whether it is still
allowed*.

- **Review/fix cycles** repeat until Claude approves the exact commit and the trusted tests pass.
  No fixed number of exchanges; approval ends the run immediately.
- **Clarification:** Codex may answer `needs_clarification` with questions. Claude re-plans (no tools,
  read-only); Codex continues in the same worktree. Limited by `max_clarifications`.
- **Stall detection:** `STALLED` if a review's findings equal the previous review's, or if one
  finding (severity + normalised text) appears in `stall_repeat_limit` reviews.
- **Budgets:** `max_cycles`, `max_model_calls` (checked *before* each step, so no commit is left
  without its review), `max_clarifications`, `stage_timeout_s`, `task_timeout_s`.
  Live runs are capped by `live_limits` in `live/live_config.json`; the protocol has absolute
  ceilings (10 cycles, 60 calls, 3 clarifications).
- **Escalation states:** `BLOCKED` (reviewer blocks, or clarification limit), `STALLED`,
  `BUDGET_EXHAUSTED`, `TASK_TIMEOUT`, `MODEL_UNSUPPORTED`.
- **Human extension:** a new task with `"continue_from": "<run dir>"` and its own budget continues
  from the last reviewed commit of a run that ended `BUDGET_EXHAUSTED`, `STALLED` or `TASK_TIMEOUT`.
  It requires an intact journal, the same repository, an unchanged base branch, and the previous
  task branch still at its final reviewed commit. Plan and findings carry over; stall history spans
  both runs. Raising `live_limits` itself is an explicit edit of `live/live_config.json` followed by
  a new readiness run.

## 3. Evidence recorded per run

`result.json`: `model_calls`, `budget`, `clarifications`, `model_selection`, per-cycle commit, test
result, review verdict/findings and selection, `continued_from`. `events.jsonl`: every stage start
(with model-call number), process exit, backend metadata (tokens, turns, requested/effective model),
model selection, commit, tests, review binding, final state. `report.md` summarises budget use and
the selection.
