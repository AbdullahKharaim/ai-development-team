#!/usr/bin/env python3
"""Deterministic stand-in for the Claude and Codex CLIs (M1 only).

    fake_agent.py --role claude   # handles plan, review
    fake_agent.py --role codex    # handles implement, fix

Reads one request JSON from stdin, prints one response JSON to stdout. Its
behaviour per call comes from a scenario file named by $AIDEV_MOCK_SCENARIO,
looked up as "<stage>:<cycle>" first, then "<stage>". Supported keys:

  write_files {path: text}   create/overwrite files relative to cwd
  delete_files [path]        remove files
  git_commit  bool           commit in cwd itself (an unauthorized action)
  sleep_s     float          sleep before answering (timeouts)
  spawn_orphan bool          leave a background grandchild in the process group
  exit_code   int            exit with this code after printing
  raw_stdout  str            print this verbatim instead of a JSON response
  duplicate   bool           print the response twice (conflicting completion)
  reviewed_sha "parent"|"commit_parent"|"zeros"|<sha>
                             bind the review to a different commit (stale review)
  override    {..}           fields merged into the generated response

It never contacts a network, never reads credentials and never runs a model.
"""

import argparse
import json
import os
import subprocess
import sys
import time

ROLE_STAGES = {"claude": {"plan", "replan", "review"}, "codex": {"implement", "fix"}}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--role", choices=sorted(ROLE_STAGES), required=True)
    role = ap.parse_args().role
    req = json.loads(sys.stdin.read())
    stage, cycle = req["stage"], req["cycle"]
    home = os.environ["HOME"]
    calls_path = os.path.join(home, "mock-calls.jsonl")
    seq = 1  # 1-based number of this call among calls with the same stage and cycle
    if os.path.exists(calls_path):
        with open(calls_path) as fh:
            seq += sum(1 for l in fh if (lambda c: c["stage"] == stage and c["cycle"] == cycle)(json.loads(l)))
    with open(calls_path, "a") as fh:
        fh.write(json.dumps({"role": role, "stage": stage, "cycle": cycle, "seq": seq,
                             "commit_sha": req.get("commit_sha"),
                             "n_findings": len(req.get("findings", [])),
                             "model_selection": req.get("model_selection"),
                             "questions": req.get("questions"),
                             "plan_summary": (req.get("plan") or {}).get("summary")}) + "\n")
    if stage not in ROLE_STAGES[role]:
        print(f"mock {role} does not handle stage {stage}", file=sys.stderr)
        return 3

    scenario = {}
    path = os.environ.get("AIDEV_MOCK_SCENARIO")
    if path:
        with open(path) as fh:
            scenario = json.load(fh)
    b = scenario.get(f"{stage}:{cycle}:{seq}", scenario.get(f"{stage}:{cycle}", scenario.get(stage, {})))

    for rel, text in b.get("write_files", {}).items():
        full = os.path.join(os.getcwd(), rel)
        os.makedirs(os.path.dirname(full) or ".", exist_ok=True)
        with open(full, "w") as fh:
            fh.write(text)
    for rel in b.get("delete_files", []):
        os.remove(os.path.join(os.getcwd(), rel))
    if b.get("git_commit"):
        git = ["/usr/bin/git", "-c", "user.name=mock", "-c", "user.email=mock@invalid"]
        subprocess.run(git + ["add", "--all"], check=True)
        subprocess.run(git + ["commit", "-q", "-m", "mock self-commit"], check=True)
    if b.get("spawn_orphan"):
        child = subprocess.Popen(["/bin/sleep", "300"], stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        with open(os.path.join(home, f"orphan-{stage}-{cycle}.pid"), "w") as fh:
            fh.write(str(child.pid))
    if b.get("sleep_s"):
        time.sleep(b["sleep_s"])

    if "raw_stdout" in b:
        sys.stdout.write(b["raw_stdout"])
        return b.get("exit_code", 0)

    resp = {"protocol": req["protocol"], "task_id": req["task_id"], "stage": stage,
            "cycle": cycle}
    if stage in ("plan", "replan"):
        resp.update(summary=(f"Mock {stage}: {req['objective'][:80]}"),
                    steps=["Implement the change described in the objective",
                           "Add or update unit tests",
                           "Run the test command and report"])
    elif stage in ("implement", "fix"):
        resp.update(status="done", summary=f"Mock {stage} for cycle {cycle}",
                    files_changed=sorted(b.get("write_files", {})))
    else:
        sha = req["commit_sha"]
        alt = b.get("reviewed_sha")
        if alt == "zeros":
            sha = "0" * 40
        elif alt == "parent":
            sha = req["base_sha"]
        elif alt == "commit_parent":
            sha = subprocess.run(["/usr/bin/git", "rev-parse", f"{sha}^"], check=True,
                                 capture_output=True, text=True).stdout.strip()
        elif alt:
            sha = alt
        resp.update(reviewed_sha=sha, verdict="approve", findings=[],
                    summary=f"Mock review of {req['commit_sha'][:12]}")
    resp.update(b.get("override", {}))

    text = json.dumps(resp)
    print(text)
    if b.get("duplicate"):
        print(text)
    return b.get("exit_code", 0)


if __name__ == "__main__":
    sys.exit(main())
