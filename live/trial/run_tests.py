#!/usr/bin/env python3
"""Trusted test command for the live trial; run by the orchestrator with cwd = worktree.

1. The repository's own unit tests (`python3 -m unittest discover`).
2. The hidden acceptance tests in this directory, against the worktree's code.
Exit 0 only if both pass.
"""

import os
import subprocess
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent


def main() -> int:
    print("== repository unit tests", flush=True)
    own = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", ".", "-v"],
                         env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    print(f"== repository tests exit code {own.returncode}", flush=True)

    print("== acceptance tests", flush=True)
    sys.path.insert(0, os.getcwd())
    sys.path.insert(1, str(HERE))
    sys.dont_write_bytecode = True
    try:
        suite = unittest.defaultTestLoader.loadTestsFromName("acceptance_test")
        ok = unittest.TextTestRunner(verbosity=2, stream=sys.stdout).run(suite).wasSuccessful()
    except Exception as e:  # e.g. ImportError: parse_duration missing
        print(f"acceptance tests could not run: {type(e).__name__}: {e}")
        ok = False
    print(f"== acceptance {'PASSED' if ok else 'FAILED'}", flush=True)
    return 0 if own.returncode == 0 and ok else 1


if __name__ == "__main__":
    sys.exit(main())
