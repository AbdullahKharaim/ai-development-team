#!/usr/bin/env python3
"""Fake `claude -p --output-format json --json-schema ...` for adapter tests. No model.

State lives in $HOME (the backend's real_home, a temp dir in tests):
  $HOME/scenario.json   behaviour per call key "plan" / "review:<n>" / "review"
  $HOME/calls.jsonl     argv, cwd and prompt of every call (for assertions)
"""

import json
import os
import re
import subprocess
import sys
import time

home = os.environ["HOME"]
argv = sys.argv[1:]
prompt = sys.stdin.read()
if not prompt:
    print("Error: Input must be provided either through stdin or as a prompt argument when using --print",
          file=sys.stderr)
    sys.exit(1)
schema = argv[argv.index("--json-schema") + 1]
stage = ("review" if "reviewed_sha" in schema
         else "replan" if "CLARIFICATION QUESTIONS" in prompt else "plan")
calls_path = os.path.join(home, "calls.jsonl")
n = 1 + sum(1 for l in open(calls_path) if json.loads(l)["stage"] == stage) if os.path.exists(calls_path) else 1
with open(calls_path, "a") as fh:
    fh.write(json.dumps({"tool": "claude", "stage": stage, "n": n, "argv": argv,
                         "cwd": os.getcwd(), "prompt": prompt}) + "\n")
scenario = json.load(open(os.path.join(home, "scenario.json")))
b = scenario.get(f"{stage}:{n}", scenario.get(stage, {}))

if b.get("spawn_orphan"):
    child = subprocess.Popen(["/bin/sleep", "300"], stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    open(os.path.join(home, f"orphan-{stage}-{n}.pid"), "w").write(str(child.pid))
if b.get("sleep_s"):
    time.sleep(b["sleep_s"])
if b.get("write_scratch"):
    open("notes.txt", "w").write("tool use")
if b.get("leak"):
    print("debug: token sk-ant-oat01-ABCDEFGHIJKLMNOPQRSTUV and user person@example.com",
          file=sys.stderr)
if "raw" in b:
    sys.stdout.write(b["raw"])
    sys.exit(b.get("exit_code", 0))

if stage in ("plan", "replan"):
    out = {"summary": ("Revised plan answering the questions" if stage == "replan"
                       else "Add parse_duration with a strict regex and tests"),
           "steps": ["Implement parse_duration in durations.py", "Add unit tests"],
           "complexity": b.get("complexity", "normal"),
           "complexity_rationale": b.get("complexity_rationale", "typical scoped feature")}
else:
    sha = re.search(r"^COMMIT_SHA: ([0-9a-f]{40})$", prompt, re.M).group(1)
    if b.get("wrong_sha"):
        sha = "0" * 40
    out = {"reviewed_sha": sha, "verdict": b.get("verdict", "approve"),
           "summary": b.get("summary", "Looks correct"), "findings": b.get("findings", [])}
result = {"type": "result", "subtype": "success", "is_error": bool(b.get("is_error", False)),
          "num_turns": 1, "duration_ms": 12, "total_cost_usd": 0.0,
          "usage": {"input_tokens": 1000 + len(prompt) // 4, "output_tokens": 50},
          "permission_denials": [{"tool_name": "Bash"}] * b.get("permission_denials", 0),
          "modelUsage": {"claude-fake-model": {"inputTokens": 1000, "outputTokens": 50}},
          "structured_output": out, "result": ""}
print(json.dumps(result))
sys.exit(b.get("exit_code", 0))
