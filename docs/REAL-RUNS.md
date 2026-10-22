# Real runs (Claude Code + Codex CLI)

Three real, subscription-authenticated runs have been made with this orchestrator (Claude Pro
login for Claude Code 2.1.300, ChatGPT login for Codex CLI 0.162.1, inside the isolated
`agentdev` WSL2 distribution). All figures come from each run's hash-chained `events.jsonl` and
`result.json`. The run directories themselves are not part of this repository.

| | Run 1 | Run 2 | Run 3 |
|---|---|---|---|
| Task | `parse_duration()` for a tiny synthetic repo | `authlog-threat-analyzer` MVP (SSH auth-log analyser) | `csv-quality-checker` (CSV quality CLI) |
| Started via | `aidev run-live` | `aidev run-live` | **`/ai-team`** skill in Claude Desktop |
| Final state | `SUCCEEDED` | `SUCCEEDED` | `SUCCEEDED` |
| Orchestrator duration | 62.4 s | 352.4 s | 256.7 s |
| Model calls | 3 (plan, implement, review) | 3 | 3 |
| Review cycles | 1 | 1 | 1 |
| Claude PLAN / Codex IMPLEMENT / Claude REVIEW | 18.0 s / 37.1 s / 7.0 s | 79.2 s / 243.0 s / 28.8 s | 51.6 s / 179.5 s / 24.5 s |
| Codex model requested | none (CLI default; not recorded) | none (CLI default; not recorded) | `gpt-6.1-sol`, reasoning effort `high` |
| Trusted tests (sandboxed) | passed; hidden acceptance 4/4 | unit 14/14; hidden acceptance 29/29 | unit 41/41; hidden acceptance 16/16 |
| Review findings | 1 low (cosmetic) | 3 low | 1 low (see below) |
| Reviewed SHA = implementation commit | yes | yes | yes |
| Base branch unchanged; merged/pushed | yes; no/no | yes; no/no | yes; no/no |

Run 3 is the first run started from the `/ai-team` skill; it also was the first with an explicit
Codex model and effort. The call with `-m gpt-6.1-sol -c model_reasoning_effort="high"` completed
normally. Codex CLI does not report which model actually served the call, so the **effective Codex
model is unverified**; Claude Code reported `claude-opus-5-5` for planning and review.

Run 3's single finding was about this system, not the generated code: log/prompt redaction masks
all e-mail addresses, so the reviewer saw the synthetic `@example.com` addresses as `[REDACTED]`
(the committed files were correct).

## Not yet demonstrated in a real run

All of the following are implemented and covered by deterministic mock-agent tests, but every
real run so far was approved in its first review cycle:

- more than one review/fix cycle with real models,
- implementer clarification → re-plan,
- stall detection and `BUDGET_EXHAUSTED` with real models,
- continuation of a stopped run (`continue_from`),
- planner-raised complexity (`complex` → `xhigh`).

No comparison with human developers has been measured, so none is claimed.
