"""Thin git wrapper. Every call goes through run_bounded with an isolated env.

The orchestrator's git identity and config are fixed on the command line; the
user's global/system git config (signing, hooks, aliases) is not loaded, and
repository hooks are disabled for orchestrator commits.
"""

from __future__ import annotations

import os
from pathlib import Path

from .proc import SAFE_PATH, run_bounded

GIT_TIMEOUT_S = 60
IDENTITY = ("aidev-orchestrator", "aidev-orchestrator@localhost.invalid")


class GitError(RuntimeError):
    pass


class Git:
    def __init__(self, home: Path):
        self.env = {
            "PATH": SAFE_PATH,
            "HOME": str(home),
            "LANG": "C.UTF-8",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
        }

    def run(self, cwd: Path, *args: str, check: bool = True) -> str:
        argv = ["git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false",
                "-c", f"user.name={IDENTITY[0]}", "-c", f"user.email={IDENTITY[1]}", *args]
        r = run_bounded(argv, cwd=cwd, env=self.env, timeout_s=GIT_TIMEOUT_S)
        if check and not r.ok:
            raise GitError(f"git {' '.join(args)} failed rc={r.returncode}: "
                           f"{r.stderr.decode(errors='replace').strip()[:500]}")
        return r.stdout.decode(errors="replace")

    # ---------------------------------------------------------------- queries
    def toplevel(self, path: Path) -> Path:
        return Path(self.run(path, "rev-parse", "--show-toplevel").strip())

    def head(self, cwd: Path) -> str:
        return self.run(cwd, "rev-parse", "--verify", "HEAD^{commit}").strip()

    def current_branch(self, cwd: Path) -> str:
        return self.run(cwd, "symbolic-ref", "--quiet", "--short", "HEAD").strip()

    def branch_exists(self, cwd: Path, name: str) -> bool:
        out = self.run(cwd, "branch", "--list", "--format=%(refname:short)", name)
        return name in out.split()

    def status(self, cwd: Path, *, include_ignored: bool) -> str:
        """Porcelain status of tracked changes and untracked files.

        With include_ignored, ignored paths are listed too, so a read-only stage
        cannot hide a new file behind .gitignore (content changes inside an
        already-ignored file are not visible to git; see README limitations).
        """
        args = ["status", "--porcelain=v1", "--untracked-files=all"]
        if include_ignored:
            args.append("--ignored")
        return self.run(cwd, *args)

    def is_clean(self, cwd: Path) -> bool:
        """No tracked changes and no untracked (non-ignored) files."""
        return self.status(cwd, include_ignored=False) == ""

    def snapshot(self, cwd: Path) -> tuple[str, str]:
        """(HEAD, full status incl. ignored) -- compared around read-only stages."""
        return self.head(cwd), self.status(cwd, include_ignored=True)

    def diff(self, cwd: Path, a: str, b: str) -> str:
        return self.run(cwd, "diff", "--no-color", "--no-ext-diff", a, b)

    def diff_names(self, cwd: Path, a: str, b: str) -> list[str]:
        return [l for l in self.run(cwd, "diff", "--name-only", a, b).splitlines() if l]

    def diff_stat(self, cwd: Path, a: str, b: str) -> str:
        return self.run(cwd, "diff", "--stat", "--no-color", a, b)

    def parent(self, cwd: Path, sha: str) -> str:
        return self.run(cwd, "rev-parse", f"{sha}^").strip()

    # -------------------------------------------------------------- mutations
    def add_worktree(self, repo: Path, path: Path, branch: str, base_sha: str) -> None:
        self.run(repo, "worktree", "add", "--quiet", "-b", branch, str(path), base_sha)

    def commit_all(self, cwd: Path, message: str) -> str:
        self.run(cwd, "add", "--all")
        self.run(cwd, "commit", "--quiet", "--no-verify", "-m", message)
        return self.head(cwd)
