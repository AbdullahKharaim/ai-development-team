"""Append-only JSONL event journal with a SHA-256 hash chain.

Each line carries `prev`, the SHA-256 of the previous line's bytes, so a later
edit, deletion or reordering of lines is detectable by `verify_journal`. This is
tamper-evidence for accidental or casual edits, not a signature: anyone who can
write the file can also rewrite the whole chain.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

GENESIS = "0" * 64


class Journal:
    def __init__(self, path: Path, task_id: str):
        self.path = Path(path)
        self.task_id = task_id
        self.seq = 0
        self.prev = GENESIS
        # 'x' mode: a journal is never reopened or overwritten.
        self._fh = open(self.path, "xb")

    def append(self, event: str, **data) -> dict:
        self.seq += 1
        record = {
            "seq": self.seq,
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "task_id": self.task_id,
            "event": event,
            "data": data,
            "prev": self.prev,
        }
        line = json.dumps(record, sort_keys=True, ensure_ascii=False).encode() + b"\n"
        self._fh.write(line)
        self._fh.flush()
        os.fsync(self._fh.fileno())
        self.prev = hashlib.sha256(line).hexdigest()
        return record

    def close(self) -> None:
        if not self._fh.closed:
            self._fh.close()


def verify_journal(path: Path) -> tuple[bool, str]:
    prev = GENESIS
    expected_seq = 1
    with open(path, "rb") as fh:
        for n, line in enumerate(fh, 1):
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                return False, f"line {n}: not JSON"
            if rec.get("prev") != prev:
                return False, f"line {n}: hash chain broken"
            if rec.get("seq") != expected_seq:
                return False, f"line {n}: sequence gap"
            prev = hashlib.sha256(line).hexdigest()
            expected_seq += 1
    if expected_seq == 1:
        return False, "empty journal"
    return True, f"{expected_seq - 1} events, chain intact"
