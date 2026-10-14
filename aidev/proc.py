"""Bounded child-process execution with process-group cleanup.

Adapted (rewritten, not imported) from the approach in live_cli/runtime.py:
- the child gets its own session/process group (start_new_session=True);
- stdin/stdout/stderr are pumped non-blockingly with size limits and a deadline;
- the leader's exit is observed with waitid(WNOWAIT) so its PID/PGID cannot be
  reused before we SIGKILL the whole group in `finally`;
- only then is the leader reaped.

Known limit: a descendant that calls setsid()/setpgid() leaves the group and is
not reachable by killpg. Containing that needs cgroups, which M1 does not use.
"""

from __future__ import annotations

import dataclasses
import os
import selectors
import signal
import subprocess
import time
from pathlib import Path

# The real model clients must never be started by M1. The guard runs before any
# spawn and checks the resolved executable, not just the name the caller used.
FORBIDDEN_NAMES = frozenset({"claude", "codex", "claude-code", "codex-cli"})
FORBIDDEN_DIRS = (
    Path.home() / ".local" / "bin",
    Path.home() / ".codex",
    Path.home() / ".claude",
    Path.home() / ".npm-global",
)

SAFE_PATH = "/usr/bin:/bin"
DEFAULT_OUT_LIMIT = 1 << 20  # 1 MiB
DEFAULT_ERR_LIMIT = 1 << 20


class ForbiddenExecutable(RuntimeError):
    pass


@dataclasses.dataclass
class ProcResult:
    argv: list[str]
    returncode: int | None  # None when killed on timeout before exit
    stdout: bytes
    stderr: bytes
    timed_out: bool
    output_limit_exceeded: bool
    duration_s: float
    group_members_killed: bool  # descendants were still alive after the leader exited
    cleanup_failed: bool

    @property
    def ok(self) -> bool:
        return (self.returncode == 0 and not self.timed_out
                and not self.output_limit_exceeded and not self.cleanup_failed)


def resolve_executable(name: str) -> str:
    """Resolve argv[0] against SAFE_PATH only (never the caller's PATH) and guard it."""
    if os.sep in name:
        path = Path(name)
        if not path.is_absolute():
            raise ForbiddenExecutable(f"relative executable path not allowed: {name}")
    else:
        path = None
        for d in SAFE_PATH.split(":"):
            candidate = Path(d) / name
            if candidate.is_file() and os.access(candidate, os.X_OK):
                path = candidate
                break
        if path is None:
            raise ForbiddenExecutable(f"executable not found in {SAFE_PATH}: {name}")
    check_not_forbidden(path)
    return str(path)


def check_not_forbidden(path: Path) -> None:
    for p in (path, Path(os.path.realpath(path))):
        if p.name.lower() in FORBIDDEN_NAMES:
            raise ForbiddenExecutable(f"real model client refused in M1: {path}")
        for d in FORBIDDEN_DIRS:
            if p == d or d in p.parents:
                raise ForbiddenExecutable(f"executable under forbidden directory refused: {path}")


def run_bounded(argv: list[str], *, cwd: str | os.PathLike, env: dict[str, str],
                stdin: bytes = b"", timeout_s: float,
                out_limit: int = DEFAULT_OUT_LIMIT,
                err_limit: int = DEFAULT_ERR_LIMIT) -> ProcResult:
    if not argv or not all(isinstance(a, str) for a in argv):
        raise ValueError("argv must be a non-empty list of strings")
    if timeout_s <= 0:
        raise ValueError("timeout_s must be positive")
    argv = [resolve_executable(argv[0])] + list(argv[1:])
    # Script arguments are also checked so `python3 /path/to/claude` cannot slip past.
    for a in argv[1:]:
        if os.sep in a and Path(a).is_absolute() and Path(a).exists():
            check_not_forbidden(Path(a))

    start = time.monotonic()
    deadline = start + timeout_s
    out, err = bytearray(), bytearray()
    timed_out = limit_hit = members_killed = cleanup_failed = False
    rc: int | None = None
    sel = selectors.DefaultSelector()
    p = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         shell=False, close_fds=True, start_new_session=True)
    try:
        pos = 0
        for f, tag in ((p.stdin, "in"), (p.stdout, "out"), (p.stderr, "err")):
            os.set_blocking(f.fileno(), False)
            sel.register(f, selectors.EVENT_WRITE if tag == "in" else selectors.EVENT_READ, tag)
        while sel.get_map() and not (timed_out or limit_hit):
            if time.monotonic() >= deadline:
                timed_out = True
                break
            for key, _ in sel.select(0.05):
                f, tag = key.fileobj, key.data
                if tag == "in":
                    if pos >= len(stdin):
                        sel.unregister(f)
                        f.close()
                        continue
                    try:
                        pos += os.write(f.fileno(), stdin[pos:pos + 65536])
                    except BlockingIOError:
                        pass
                    except BrokenPipeError:
                        sel.unregister(f)
                        f.close()
                    continue
                try:
                    chunk = os.read(f.fileno(), 65536)
                except BlockingIOError:
                    continue
                if not chunk:
                    sel.unregister(f)
                    f.close()
                    continue
                buf, limit = (out, out_limit) if tag == "out" else (err, err_limit)
                buf.extend(chunk)
                if len(buf) > limit:
                    limit_hit = True
        # Observe exit WITHOUT reaping so the PGID stays ours until killpg below.
        while not (timed_out or limit_hit):
            st = os.waitid(os.P_PID, p.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            if st is not None:
                rc = st.si_status if st.si_code == os.CLD_EXITED else -st.si_status
                break
            if time.monotonic() >= deadline:
                timed_out = True
                break
            time.sleep(0.01)
    finally:
        sel.close()
        # Is anything other than the (possibly zombie) leader still in the group?
        if rc is not None:
            members_killed = _group_has_live_members(p.pid)
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError:
            cleanup_failed = True
        try:
            p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            cleanup_failed = True
        for f in (p.stdin, p.stdout, p.stderr):
            if f is not None and not f.closed:
                f.close()
        if _group_has_live_members(p.pid):
            cleanup_failed = True
    return ProcResult(argv=argv, returncode=None if timed_out else rc,
                      stdout=bytes(out), stderr=bytes(err), timed_out=timed_out,
                      output_limit_exceeded=limit_hit,
                      duration_s=round(time.monotonic() - start, 3),
                      group_members_killed=members_killed, cleanup_failed=cleanup_failed)


def _group_has_live_members(pgid: int) -> bool:
    """True if a non-zombie process other than the leader is in process group `pgid`."""
    try:
        entries = os.listdir("/proc")
    except OSError:
        return False
    for e in entries:
        if not e.isdigit() or int(e) == pgid:
            continue
        try:
            stat = Path(f"/proc/{e}/stat").read_text()
        except OSError:
            continue
        # Fields after the parenthesised comm: state ppid pgrp ...
        fields = stat.rsplit(")", 1)[-1].split()
        if len(fields) >= 3 and int(fields[2]) == pgid and fields[0] != "Z":
            return True
    return False
