"""Best-effort redaction of credentials and e-mail addresses in captured CLI output.

Applied to every log file the live backends write. It is pattern-based: it
removes the token shapes the two CLIs and their providers are known to use, not
arbitrary secrets. Logs must still be treated as private.
"""

from __future__ import annotations

import re

_PATTERNS = [
    re.compile(rb"sk-ant-[A-Za-z0-9_\-]{8,}"),                       # Anthropic keys/tokens
    re.compile(rb"sk-(?:proj-|svcacct-)?[A-Za-z0-9_\-]{20,}"),       # OpenAI keys
    re.compile(rb"eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"),  # JWTs
    re.compile(rb"(?i)(bearer\s+)[A-Za-z0-9._\-~+/]{12,}=*"),
    re.compile(rb'(?i)("?(?:access|refresh|id|session)_?token"?\s*[:=]\s*"?)[^"\s,}]{8,}'),
    re.compile(rb"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"),  # e-mail addresses
]
MARK = b"[REDACTED]"


def redact(data: bytes) -> bytes:
    for pat in _PATTERNS:
        if pat.groups:
            data = pat.sub(lambda m: m.group(1) + MARK, data)
        else:
            data = pat.sub(MARK, data)
    return data


def redact_text(text: str) -> str:
    return redact(text.encode()).decode(errors="replace")
