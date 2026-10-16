"""Human-readable final report, rendered only from result.json data."""

from __future__ import annotations

from pathlib import Path


def write_report(path: Path, r: dict) -> None:
    ok = r["success"]
    L = [f"# aidev run report — `{r['task_id']}`", ""]
    L.append(f"**Outcome: {'SUCCEEDED' if ok else 'NOT SUCCESSFUL'}** — state `{r['state']}`")
    L.append("")
    L.append(f"> {r['detail']}")
    L.append("")
    if not ok:
        L += ["Human action required: inspect the evidence below; nothing was merged.", ""]
    L += ["## Repository", "",
          f"- Repository: `{r['repo']}`",
          f"- Base branch: `{r['base_branch']}` at `{r['base_sha']}`",
          f"- Base branch unchanged after run: `{r['base_branch_unchanged']}`",
          f"- Task branch: `{r['task_branch']}` (worktree `{r['worktree']}`)",
          f"- Final commit: `{r['final_commit']}`",
          "- Merged: **no** · Pushed: **no** (the orchestrator never merges or pushes)",
          f"- Duration: {r['duration_s']} s", ""]
    if "budget" in r:
        b = r["budget"]
        L += ["## Budget and model selection", "",
              f"- Review cycles used: {len(r['cycles'])} of {b['max_cycles']}",
              f"- Model calls used: {r['model_calls']} of {b['max_model_calls']}",
              f"- Clarification rounds: {r['clarifications']} of {b['max_clarifications']}",
              f"- Stall rule: the same finding in {b['stall_repeat_limit']} reviews, or identical "
              "findings twice in a row, stops the run"]
        if r.get("continued_from"):
            L.append(f"- Continued from run `{r['continued_from']}`")
        s = r.get("model_selection")
        if s:
            L += [f"- Codex requested: `{s['model']}` / reasoning effort `{s['effort']}` "
                  f"(complexity `{s['complexity']}`, from {s['complexity_source']})",
                  f"- Selection rationale: {s['rationale']}",
                  "- Effective Codex model: not reported by the CLI (see `backend_meta` in events.jsonl)"]
        L.append("")
    if r["plan"]:
        L += ["## Plan", "", r["plan"]["summary"], ""]
        L += [f"{i}. {s}" for i, s in enumerate(r["plan"]["steps"], 1)]
        L.append("")
    for c in r["cycles"]:
        t, v = c["tests"], c["review"]
        L += [f"## Cycle {c['cycle']} ({c['stage']})", "",
              f"- Commit: `{c['commit']}`",
              f"- Implementation summary: {c['implementation']['summary']}",
              f"- Tests: {'PASSED' if t['passed'] else 'FAILED'} "
              f"(rc={t['returncode']}, timed_out={t['timed_out']}, {t['duration_s']} s)",
              f"- Review verdict: **{v['verdict']}** on `{v['reviewed_sha']}` — {v['summary']}"]
        for f in v["findings"]:
            L.append(f"  - [{f['severity']}] {f['id']}: {f['description']}")
        L.append("")
    if r["diffstat"]:
        L += ["## Change summary (base..final)", "", "```", r["diffstat"].rstrip(), "```", ""]
    L += ["## Evidence", "",
          "- `events.jsonl` — hash-chained event journal (`python3 -m aidev verify-journal`)",
          "- `result.json` — machine-readable result",
          "- `logs/` — raw stdout/stderr of every agent and test process", "",
          "## Next steps for the human", ""]
    if ok:
        L += ["Review the branch, then merge it yourself if you agree:", "", "```bash",
              f"git -C {r['repo']} log --oneline {r['base_sha'][:12]}..{r['task_branch']}",
              f"git -C {r['repo']} diff {r['base_sha'][:12]} {r['task_branch']}", "```", ""]
    L += ["Discard the task worktree and branch when no longer needed:", "", "```bash",
          f"git -C {r['repo']} worktree remove --force {r['worktree']}",
          f"git -C {r['repo']} branch -D {r['task_branch']}", "```", ""]
    path.write_text("\n".join(L))
