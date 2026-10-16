#!/usr/bin/env bash
# Read-only status bridge for the ai-team-status skill. It can only run
# `team.py status` / `team.py list`; it cannot start or change any task.
set -euo pipefail
export MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*'
run() { wsl.exe -d agentdev -u dev --cd '~' -e /usr/bin/python3 /home/dev/ai-development-team/live/team.py "$@"; }
case "${1:-status}" in
  list) run list ;;
  status)
    if [[ -n "${2:-}" ]]; then
      [[ "$2" =~ ^[a-z0-9-]+$ ]] || { echo '{"error": "invalid task id"}'; exit 1; }
      run status "$2"
    else
      run status
    fi ;;
  *) echo '{"error": "usage: status.sh status [TASK]|list"}'; exit 2 ;;
esac
