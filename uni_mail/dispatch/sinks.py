"""What a destination returns, and the redaction of its diagnostics."""

from __future__ import annotations

import re
from dataclasses import dataclass

OUTPUT_LIMIT = 4000
PATTERNS = (
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"(?i)((?:access|refresh|id)_token|password|passwd|secret|api[_-]?key|authorization)(\"?\s*[:=]\s*\"?)[^\"\s,;}]+"),
)
SECRET_NAME = re.compile(r"(?i)token|secret|password|passwd|key|credential")


@dataclass(frozen=True)
class Result:
    # delivered: acknowledged. retry: try again after a backoff. dead: stop; a person must act.
    outcome: str
    code: int | None = None
    error: str = ""
    output: str = ""
    retry_after: float | None = None


def redact(text: str, secrets: list[str] | tuple[str, ...] = ()) -> str:
    """Remove known secret values and token-like text, and keep the last OUTPUT_LIMIT characters."""
    for secret in secrets:
        if secret and len(secret) >= 4:
            text = text.replace(secret, "[redacted]")
    text = PATTERNS[0].sub(r"\1[redacted]", text)
    text = PATTERNS[1].sub(r"\1\2[redacted]", text)
    if len(text) > OUTPUT_LIMIT:
        text = "[...]" + text[-OUTPUT_LIMIT:]
    return text
