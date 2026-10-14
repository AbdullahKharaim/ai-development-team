#!/usr/bin/env python3
"""Readiness check for a live aidev run. Performs NO model inference.

    cd ~/ai-development-team && python3 live/readiness.py

Runs only: version/help output, `auth status` (reduced to the auth *mode*),
the adapters' exact argv with EMPTY stdin (both CLIs exit before any model call
with a "no prompt" error, which proves the flags parse), the Codex sandbox
probe, and environment-isolation checks. Writes live/readiness-report.json.
Credential files are never opened; auth-status output is reduced to a few
non-secret fields before it is stored.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from aidev.cli_backends import (ClaudeCodeBackend, CodexExecBackend, LiveGate,  # noqa: E402
                                resolve_cli)
from aidev.redact import redact_text  # noqa: E402

PINNED = json.loads((ROOT / "live" / "live_config.json").read_text())
ENV = {"PATH": "/usr/bin:/bin", "HOME": str(Path.home()), "LANG": "C.UTF-8", "TERM": "dumb",
       "DISABLE_AUTOUPDATER": "1"}
EXPECTED_PROBES = {  # label -> expected outcome under Codex's :workspace profile
    "write_worktree": "ok", "write_home": "denied", "write_project": "denied",
    "append_worktree_git": "denied", "write_main_git": "denied", "git_commit": "denied",
    "write_tmp": "ok", "network_https": "denied", "network_dns": "denied", "run_python": "ok",
    "read_home_canary": "ok",   # reads are NOT confined: recorded as a residual risk
}


def run(argv, stdin="", timeout=60, cwd=None):
    p = subprocess.run(argv, input=stdin, capture_output=True, text=True, timeout=timeout,
                       env=ENV, cwd=cwd)
    return p.returncode, redact_text(p.stdout), redact_text(p.stderr)


def check(results, name, ok, detail=""):
    results.append({"check": name, "ok": bool(ok), "detail": detail})


def main() -> int:
    results: list[dict] = []
    claude, codex = resolve_cli("claude"), resolve_cli("codex")
    real = {"claude": os.path.realpath(claude), "codex": os.path.realpath(codex)}

    # --- environment ---------------------------------------------------------
    check(results, "not_root", os.geteuid() != 0, f"uid={os.geteuid()}")
    rc, _, _ = run(["sudo", "-n", "true"])
    check(results, "no_sudo", rc != 0, f"sudo -n rc={rc}")
    mounts = Path("/proc/mounts").read_text()
    check(results, "no_windows_drive_mounts", "drvfs" not in mounts and " /mnt/c " not in mounts)
    check(results, "no_windows_path", "/mnt/" not in os.environ.get("PATH", ""))
    check(results, "no_api_key_env", not any(k in os.environ for k in
                                             ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "CODEX_API_KEY")))
    check(results, "distro_is_agentdev", os.environ.get("WSL_DISTRO_NAME") == "agentdev",
          os.environ.get("WSL_DISTRO_NAME", "?"))

    # --- executables and versions ----------------------------------------------
    for name, path in (("claude", claude), ("codex", codex)):
        home = str(Path.home())
        check(results, f"{name}_user_local", real[name].startswith(home + "/"), real[name])
        with open(real[name], "rb") as fh:
            check(results, f"{name}_native_elf", fh.read(4) == b"\x7fELF")
    rc, out, _ = run([claude, "--version"])
    check(results, "claude_version_pinned", out.split()[0:1] == [PINNED["claude_version"]], out.strip())
    rc, out, _ = run([codex, "--version"])
    check(results, "codex_version_pinned", out.strip().endswith(PINNED["codex_version"]), out.strip())

    # --- flags exist in help ------------------------------------------------------
    _, chelp, _ = run([claude, "--help"])
    for flag in ("--tools", "--safe-mode", "--strict-mcp-config", "--json-schema", "--output-format",
                 "--no-session-persistence", "--permission-mode", "--permission-prompts",
                 "--disable-slash-commands"):
        check(results, f"claude_help_has{flag}", flag in chelp)
    _, xhelp, _ = run([codex, "exec", "--help"])
    for flag in ("--strict-config", "--ignore-user-config", "--ignore-rules", "--ephemeral",
                 "--output-schema", "--output-last-message", "--json", "--cd"):
        check(results, f"codex_help_has{flag}", flag in xhelp)

    # --- exact adapter argv parses (empty stdin => exits before inference) --------
    pending = LiveGate("pending", "pending", "pending")
    with tempfile.TemporaryDirectory(dir=Path.home() / ".cache") as tmp:
        tmp = Path(tmp)
        for stage in ("plan", "review"):
            argv = ClaudeCodeBackend([claude], pending).build_argv(stage)
            rc, _, err = run(argv, stdin="", cwd=tmp)
            check(results, f"claude_{stage}_argv_parses",
                  rc == 1 and "Input must be provided" in err, err.strip()[:200])
        subprocess.run(["git", "init", "-q", str(tmp / "wt")], check=True, env=ENV)
        argv = CodexExecBackend([codex], pending).build_argv(tmp / "wt", tmp / "s.json", tmp / "o.json")
        rc, out, err = run(argv, stdin="", cwd=tmp / "wt")
        check(results, "codex_exec_argv_parses", rc == 1 and "No prompt provided" in (out + err),
              (out + err).strip()[:200])

    # --- subscription auth (mode only) -----------------------------------------
    rc, out, _ = run([claude, "auth", "status"])
    try:
        st = json.loads(out)
        mode = {k: st.get(k) for k in ("loggedIn", "authMethod", "apiProvider", "subscriptionType")}
    except json.JSONDecodeError:
        mode = {}
    check(results, "claude_subscription_login",
          mode.get("loggedIn") is True and mode.get("authMethod") == "claude.ai", json.dumps(mode))
    rc, out, err = run([codex, "login", "status"])
    check(results, "codex_chatgpt_login", rc == 0 and "ChatGPT" in (out + err),
          (out + err).strip().splitlines()[0] if (out + err).strip() else "")

    # --- Codex native sandbox behaviour -----------------------------------------
    rc, out, err = run(["bash", str(ROOT / "live" / "sandbox_probe.sh"), ":workspace"], timeout=180)
    probes = {}
    for line in out.splitlines():
        parts = line.split("\t")
        if parts[0] == "PROBE" and len(parts) >= 3:
            probes[parts[1]] = "ok" if parts[2] == "0" and "no-dns" not in line else "denied"
        elif parts[0] == "HOST" and len(parts) >= 3:
            probes["host:" + parts[1]] = parts[2]
    for label, want in EXPECTED_PROBES.items():
        check(results, f"sandbox_{label}_{want}", probes.get(label) == want, probes.get(label, "missing"))
    for host_check, want in (("home_canary_intact", "yes"), ("project_probe_absent", "yes"),
                             ("main_git_probe_absent", "yes"), ("worktree_commits", "1")):
        check(results, f"sandbox_host_{host_check}", probes.get("host:" + host_check) == want,
              probes.get("host:" + host_check, "missing"))

    passed = all(r["ok"] for r in results)
    report = {"generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
              "generated_epoch": int(time.time()), "passed": passed,
              "executables": {"claude": claude, "codex": codex, "claude_real": real["claude"],
                              "codex_real": real["codex"]},
              "pinned": PINNED, "checks": results,
              "residual_risks": [
                  "Codex :workspace sandbox does not restrict reads: the agent can read files "
                  "readable by dev, including its own login token.",
                  "Claude tool-free mode is configured by flags; its effective behaviour is observed "
                  "per call (permission_denials, scratch cwd, worktree snapshot), not proven in advance.",
                  "Claude and Codex processes themselves have network access to their providers.",
              ]}
    body = json.dumps(report, indent=2, sort_keys=True)
    (ROOT / "live" / "readiness-report.json").write_text(body + "\n")
    print(f"readiness: {'PASSED' if passed else 'FAILED'} "
          f"({sum(r['ok'] for r in results)}/{len(results)} checks); "
          f"sha256={hashlib.sha256(body.encode()).hexdigest()[:16]}")
    for r in results:
        if not r["ok"]:
            print(f"  FAIL {r['check']}: {r['detail']}")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
