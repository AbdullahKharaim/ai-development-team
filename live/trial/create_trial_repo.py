#!/usr/bin/env python3
"""Create the disposable synthetic repository for the first live run.

    python3 live/trial/create_trial_repo.py [--dest ~/aidev-trial/repo]

Refuses to overwrite an existing destination. Contains no personal data.
"""

import argparse
import os
import subprocess
import sys
from pathlib import Path

FILES = {
    "README.md": "# durations\n\nTiny standard-library helpers for time durations.\n",
    ".gitignore": "__pycache__/\n*.pyc\n",
    "durations.py": (
        '"""Helpers for human-readable durations."""\n\n\n'
        "def format_seconds(total):\n"
        '    """Format a non-negative number of seconds as e.g. \'1h30m5s\' (\'0s\' for zero)."""\n'
        "    if not isinstance(total, int) or isinstance(total, bool) or total < 0:\n"
        '        raise ValueError("total must be a non-negative int")\n'
        "    h, rem = divmod(total, 3600)\n"
        "    m, s = divmod(rem, 60)\n"
        '    out = (f"{h}h" if h else "") + (f"{m}m" if m else "") + (f"{s}s" if s else "")\n'
        '    return out or "0s"\n'),
    "test_durations.py": (
        "import unittest\n\nfrom durations import format_seconds\n\n\n"
        "class FormatSecondsTests(unittest.TestCase):\n"
        "    def test_examples(self):\n"
        '        self.assertEqual(format_seconds(0), "0s")\n'
        '        self.assertEqual(format_seconds(5405), "1h30m5s")\n'
        '        self.assertEqual(format_seconds(120), "2m")\n\n'
        "    def test_rejects_negative(self):\n"
        "        with self.assertRaises(ValueError):\n"
        "            format_seconds(-1)\n"),
}
GIT_ENV = {"PATH": "/usr/bin:/bin", "HOME": str(Path.home()), "LANG": "C.UTF-8",
           "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dest", type=Path, default=Path.home() / "aidev-trial" / "repo")
    dest = ap.parse_args().dest
    if dest.exists():
        print(f"refusing: {dest} already exists", file=sys.stderr)
        return 1
    dest.mkdir(parents=True)
    for name, text in FILES.items():
        (dest / name).write_text(text)
    g = ["git", "-c", "user.name=aidev-trial", "-c", "user.email=trial@localhost.invalid",
         "-c", "init.defaultBranch=main"]
    subprocess.run(g + ["init", "-q"], cwd=dest, env=GIT_ENV, check=True)
    subprocess.run(g + ["add", "--all"], cwd=dest, env=GIT_ENV, check=True)
    subprocess.run(g + ["commit", "-q", "-m", "initial: format_seconds"], cwd=dest, env=GIT_ENV,
                   check=True)
    print(f"created {dest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
