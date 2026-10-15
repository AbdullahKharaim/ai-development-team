"""Sandbox for the trusted test command in live runs (bubblewrap, no root).

Codex can edit the repository's test files, so whatever the test command runs is
partly agent-written code. In live runs it therefore executes inside bwrap with:

- every namespace unshared (`--unshare-all`): no network, own PID namespace;
- a fresh tmpfs over /home, so no home data and none of the CLIs' login files
  (~/.claude, ~/.codex) exist inside; only the worktree and the directories of
  absolute paths named in the test command (e.g. the hidden acceptance tests)
  are bound back, read-only;
- /usr read-only (Python + stdlib), private /tmp, minimal /proc and /dev;
- a cleared environment; `--die-with-parent` so a killed bwrap takes the whole
  sandbox (PID namespace) with it.

Verified by tests/test_test_sandbox.py and live/readiness.py. Limits: same
kernel; /usr and /etc/ld.so.cache are readable; CPU/memory are not limited
beyond the orchestrator's timeout.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

from .proc import resolve_executable

BWRAP = "/usr/bin/bwrap"
SANDBOX_ENV = {"PATH": "/usr/bin:/bin", "HOME": "/tmp", "LANG": "C.UTF-8",
               "PYTHONDONTWRITEBYTECODE": "1", "TMPDIR": "/tmp"}


class SandboxUnavailable(RuntimeError):
    pass


def check_bwrap() -> str:
    """bwrap must be the distro's root-owned, non-writable binary."""
    try:
        st = os.stat(BWRAP)
    except FileNotFoundError:
        raise SandboxUnavailable(f"{BWRAP} not found") from None
    if st.st_uid != 0 or st.st_mode & (stat.S_IWGRP | stat.S_IWOTH) or st.st_mode & stat.S_ISUID:
        raise SandboxUnavailable(f"{BWRAP} must be root-owned, not group/world-writable, not setuid")
    return BWRAP


def readonly_dirs(test_command: list[str]) -> list[str]:
    """Directories of absolute, existing path arguments (the trusted runner and its fixtures)."""
    home = Path(os.path.realpath(Path.home()))
    protected = [home / ".claude", home / ".codex", home / ".ssh", home / ".config"]
    dirs: list[str] = []
    for a in test_command[1:]:
        if os.path.isabs(a) and os.path.exists(a):
            d = os.path.realpath(a if os.path.isdir(a) else os.path.dirname(a))
            dp = Path(d)
            # Binding home itself, an ancestor of it, or a credential/config dir would
            # undo the /home tmpfs; such a test command is refused.
            if dp == home or dp in home.parents or any(dp == p or p in dp.parents or dp in p.parents
                                                       for p in protected):
                raise SandboxUnavailable(f"test command path would expose home data: {d}")
            if d not in dirs:
                dirs.append(d)
    return dirs


def wrap(test_command: list[str], worktree: Path) -> list[str]:
    bwrap = check_bwrap()
    exe = resolve_executable(test_command[0])          # guarded, /usr/bin:/bin only
    wt = os.path.realpath(worktree)
    a = [bwrap, "--unshare-all", "--die-with-parent", "--new-session", "--cap-drop", "ALL",
         "--clearenv"]
    for k, v in SANDBOX_ENV.items():
        a += ["--setenv", k, v]
    a += ["--ro-bind", "/usr", "/usr"]
    for name in ("bin", "sbin", "lib", "lib64", "lib32"):
        p = f"/{name}"
        if os.path.islink(p):
            a += ["--symlink", os.readlink(p), p]
        elif os.path.isdir(p):
            a += ["--ro-bind", p, p]
    if os.path.exists("/etc/ld.so.cache"):
        a += ["--ro-bind", "/etc/ld.so.cache", "/etc/ld.so.cache"]
    a += ["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp", "--tmpfs", "/home",
          "--tmpfs", "/root", "--ro-bind", wt, wt]
    for d in readonly_dirs(test_command):
        a += ["--ro-bind", d, d]
    a += ["--chdir", wt, "--", exe, *test_command[1:]]
    return a
