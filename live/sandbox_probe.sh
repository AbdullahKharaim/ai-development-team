#!/bin/bash
# Deterministic probe of Codex's native Linux sandbox (`codex sandbox`). No model is invoked.
# Usage: live/sandbox_probe.sh [permission-profile]   (default :workspace)
# Prints one line per probe: label, exit code, first output. Uses only scratch
# and canary files; never touches CLI credential files.
set -u
PROFILE="${1:-:workspace}"
CODEX="${CODEX_BIN:-$HOME/.local/bin/codex}"
PROJECT="$(cd "$(dirname "$0")/.." && pwd)"
S=$(mktemp -d "$HOME/.cache/aidev-sbx-XXXX" 2>/dev/null || mktemp -d /tmp/aidev-sbx-XXXX)
git init -q "$S/repo"
git -C "$S/repo" -c user.name=t -c user.email=t@t commit -q --allow-empty -m init
git -C "$S/repo" worktree add -q -b t "$S/wt"
echo canary > "$HOME/aidev-canary-outside.txt"
echo secret-canary > "$HOME/aidev-canary-secret.txt"

probe() {
  label=$1; shift
  out=$(cd "$S/wt" && "$CODEX" sandbox -P "$PROFILE" -C "$S/wt" -- /bin/sh -c "$1" 2>&1)
  rc=$?
  printf 'PROBE\t%s\t%s\t%s\n' "$label" "$rc" "$(printf '%s' "$out" | tr '\n\t' '  ' | cut -c1-120)"
}

probe write_worktree      'echo ok > inside.txt && cat inside.txt'
probe write_home          "echo x > $HOME/aidev-canary-outside.txt"
probe write_project       "touch $PROJECT/SANDBOX_PROBE"
probe append_worktree_git 'echo x >> .git'
probe write_main_git      "touch $S/repo/.git/PROBE"
probe git_commit          'git -c user.name=a -c user.email=a@a commit -q --allow-empty -m x && echo committed'
probe write_tmp           'touch /tmp/aidev-sbx-probe && echo ok'
probe read_home_canary    "cat $HOME/aidev-canary-secret.txt"
probe network_https       'python3 -c "import urllib.request;print(urllib.request.urlopen(\"https://example.com\",timeout=8).status)"'
probe network_dns         'getent hosts example.com || echo no-dns'
probe run_python          'python3 -c "print(2+2)"'

printf 'HOST\thome_canary_intact\t%s\n' "$( [ "$(cat "$HOME/aidev-canary-outside.txt")" = canary ] && echo yes || echo NO)"
printf 'HOST\tproject_probe_absent\t%s\n' "$( [ -e "$PROJECT/SANDBOX_PROBE" ] && echo NO || echo yes)"
printf 'HOST\tmain_git_probe_absent\t%s\n' "$( [ -e "$S/repo/.git/PROBE" ] && echo NO || echo yes)"
printf 'HOST\tworktree_commits\t%s\n' "$(git -C "$S/wt" log --oneline | wc -l)"
printf 'HOST\tworktree_file_written\t%s\n' "$( [ -e "$S/wt/inside.txt" ] && echo yes || echo no)"
rm -rf "$S" /tmp/aidev-sbx-probe "$HOME/aidev-canary-outside.txt" "$HOME/aidev-canary-secret.txt" "$PROJECT/SANDBOX_PROBE"
