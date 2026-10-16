#!/usr/bin/env python3
"""Fake `codex exec ... --json -o FILE -C DIR -` for adapter tests. No model.

Scenario keys "implement" / "fix:<n>" etc. in $HOME/scenario.json; behaviours:
  impl: "good" | "buggy" | "none"   which durations.py to write
  no_last_message, exit_code, git_commit, leak
"""

import json
import os
import subprocess
import sys

GOOD = '''

_UNITS = (("h", 3600), ("m", 60), ("s", 1))


def parse_duration(text):
    """Inverse of format_seconds: '1h30m' -> 5400."""
    if not isinstance(text, str):
        raise TypeError("text must be a str")
    s = text.strip()
    if not s:
        raise ValueError("empty duration")
    total, pos, last = 0, 0, -1
    while pos < len(s):
        start = pos
        while pos < len(s) and s[pos].isdigit() and s[pos].isascii():
            pos += 1
        if pos == start or pos == len(s):
            raise ValueError(f"invalid duration: {text!r}")
        unit = s[pos]
        idx = next((i for i, (u, _) in enumerate(_UNITS) if u == unit), None)
        if idx is None or idx <= last:
            raise ValueError(f"invalid duration: {text!r}")
        total += int(s[start:pos]) * _UNITS[idx][1]
        last = idx
        pos += 1
    return total
'''
BUGGY = GOOD.replace("if idx is None or idx <= last:", "if idx is None:")
TESTS = '''

class ParseDurationTests(unittest.TestCase):
    def test_parse(self):
        from durations import parse_duration
        self.assertEqual(parse_duration("1h30m"), 5400)
'''

home = os.environ["HOME"]
argv = sys.argv[1:]
prompt = sys.stdin.read()
if not prompt:
    print("No prompt provided via stdin.")
    sys.exit(1)
cwd = argv[argv.index("-C") + 1]
last = argv[argv.index("-o") + 1]
stage = "fix" if "REVIEW FINDINGS TO FIX" in prompt else "implement"
calls_path = os.path.join(home, "calls.jsonl")
n = 1 + sum(1 for l in open(calls_path) if json.loads(l)["tool"] == "codex") if os.path.exists(calls_path) else 1
with open(calls_path, "a") as fh:
    fh.write(json.dumps({"tool": "codex", "stage": stage, "n": n, "argv": argv,
                         "cwd": os.getcwd(), "prompt": prompt}) + "\n")
scenario = json.load(open(os.path.join(home, "scenario.json")))
b = scenario.get(f"{stage}:{n}", scenario.get(stage, {}))

print(json.dumps({"type": "thread.started", "thread_id": "fake"}))
if b.get("report_model"):   # simulates a CLI version that reports the effective model
    print(json.dumps({"type": "session.configured", "model": b["report_model"],
                      "reasoning_effort": b.get("report_effort", "high")}))
print(json.dumps({"type": "turn.started"}))
if b.get("clarify"):
    print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 500, "output_tokens": 40}}))
    open(last, "w").write(json.dumps({"status": "needs_clarification", "summary": "plan is ambiguous",
                                      "files_changed": [], "questions": b["clarify"]}))
    sys.exit(0)
impl = b.get("impl", "good")
if impl != "none":
    src = open(os.path.join(cwd, "durations.py")).read().split("\n\n_UNITS")[0].rstrip("\n") + "\n"
    open(os.path.join(cwd, "durations.py"), "w").write(src + (GOOD if impl == "good" else BUGGY))
    tests = open(os.path.join(cwd, "test_durations.py")).read()
    if "ParseDurationTests" not in tests:
        open(os.path.join(cwd, "test_durations.py"), "w").write(tests + TESTS)
    print(json.dumps({"type": "item.completed", "item": {"type": "file_change"}}))
if b.get("hostile_test"):
    # Agent-written test that tries to escape: read home data, use the network, print a token.
    open(os.path.join(cwd, "test_hostile.py"), "w").write(f'''import os, socket, unittest


class Hostile(unittest.TestCase):
    def test_probe(self):
        for path in ({b["hostile_test"]!r}, os.path.expanduser("~/.codex"), "/home/dev/.claude"):
            try:
                print("READ", path, open(path).read() if os.path.isfile(path) else os.listdir(path))
            except OSError as e:
                print("DENIED", path, type(e).__name__)
        try:
            socket.create_connection(("1.1.1.1", 443), timeout=3); print("NET OPEN")
        except OSError as e:
            print("NET DENIED", type(e).__name__)
        print("LEAK sk-ant-oat01-HOSTILETOKEN1234567890")
''')
if b.get("git_commit"):
    subprocess.run(["/usr/bin/git", "-c", "user.name=x", "-c", "user.email=x@x", "commit", "-qam", "x"],
                   cwd=cwd)
if b.get("leak"):
    print(json.dumps({"type": "item.completed", "item": {"type": "agent_message",
                      "text": "auth header Bearer abcdefghijklmnopqrstuvwxyz0123 "
                              "access_token=eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.c2lnbmF0dXJlMTIzNDU2"}}))
print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 2000, "cached_input_tokens": 500,
                                                      "output_tokens": 300}}))
if not b.get("no_last_message"):
    open(last, "w").write(json.dumps({"status": b.get("status", "done"),
                                      "summary": f"fake {stage} ({impl})",
                                      "files_changed": ["durations.py", "test_durations.py"],
                                      "questions": []}))
sys.exit(b.get("exit_code", 0))
