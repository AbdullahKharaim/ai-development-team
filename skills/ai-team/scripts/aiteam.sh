#!/usr/bin/env bash
# Windows-side bridge for the /ai-team skill (runs in Claude Desktop's Git Bash).
# Forwards a small, validated set of commands to live/team.py inside the isolated
# WSL2 distribution "agentdev" as the unprivileged user "dev".
#
#   aiteam.sh check            environment and limits (read-only)
#   aiteam.sh prepare SLUG     create a staging dir for a new task
#   aiteam.sh start STAGING    launch the real workflow (detached) for that staging dir
#   aiteam.sh status [TASK]    read-only status (latest task if omitted)
#   aiteam.sh list             read-only status of all tasks
set -euo pipefail
export MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*'   # keep Linux paths untouched in Git Bash

DISTRO=agentdev
ROOT=/home/dev/ai-development-team
run() { wsl.exe -d "$DISTRO" -u dev --cd '~' -e /usr/bin/python3 "$ROOT/live/team.py" "$@"; }

cmd="${1:-}"; shift || true
case "$cmd" in
  check)
    if ! wsl.exe -l -q 2>/dev/null | tr -d '\0\r' | grep -qx "$DISTRO"; then
      echo '{"error": "WSL distribution agentdev not found"}'; exit 1
    fi
    wsl.exe -d "$DISTRO" -u dev --cd '~' -e /usr/bin/python3 -c '
import json, subprocess
root = "/home/dev/ai-development-team"
git = lambda *a: subprocess.run(["git", "-C", root, *a], capture_output=True, text=True).stdout.strip()
cfg = json.load(open(root + "/live/live_config.json"))
print(json.dumps({"distro": "agentdev", "orchestrator_branch": git("branch", "--show-current"),
                  "orchestrator_commit": git("rev-parse", "--short", "HEAD"),
                  "uncommitted_changes": bool(git("status", "--porcelain")),
                  "live_limits": cfg["live_limits"], "codex_policy": cfg["codex_policy"],
                  "pinned_versions": {"claude": cfg["claude_version"], "codex": cfg["codex_version"]}},
                 indent=2))'
    ;;
  prepare)
    [[ "${1:-}" =~ ^[a-z0-9][a-z0-9-]{1,40}$ ]] || { echo '{"error": "invalid project name"}'; exit 1; }
    run prepare "$1" ;;
  start)
    [[ "${1:-}" =~ ^/home/dev/aidev-projects/_staging/[a-z0-9-]+$ ]] || { echo '{"error": "invalid staging path"}'; exit 1; }
    run start "$1" ;;
  status)
    if [[ -n "${1:-}" ]]; then
      [[ "$1" =~ ^[a-z0-9-]+$ ]] || { echo '{"error": "invalid task id"}'; exit 1; }
      run status "$1"
    else
      run status
    fi ;;
  list) run list ;;
  *) echo '{"error": "usage: aiteam.sh check|prepare SLUG|start STAGING|status [TASK]|list"}'; exit 2 ;;
esac
