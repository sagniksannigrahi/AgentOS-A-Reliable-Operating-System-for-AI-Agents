"""Secret redaction applied to logs, events, tool output and anything sent to the dashboard."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

REDACTED = "[REDACTED]"

SENSITIVE_KEYS = re.compile(
    r"(pass(word|wd)?|secret|token|api[_-]?key|authorization|auth|cookie|credential|private[_-]?key)",
    re.IGNORECASE,
)

# Patterns for well-known secret formats. Ordered from most to least specific.
SECRET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("anthropic_key", re.compile(r"sk-ant-[A-Za-z0-9_\-]{10,}")),
    ("openai_key", re.compile(r"sk-(?:proj-)?[A-Za-z0-9]{20,}")),
    ("agentos_key", re.compile(r"aos_[A-Za-z0-9_\-]{20,}")),
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b")),
    ("slack_token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")),
    (
        "private_key",
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
    ),
    ("bearer", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-~+/]{8,}=*")),
    ("url_password", re.compile(r"(?<=://)([^:/\s@]+):([^@/\s]+)@")),
    (
        "assignment",
        re.compile(
            r"(?i)\b([A-Z0-9_]*(?:PASSWORD|SECRET|TOKEN|API_KEY|APIKEY)[A-Z0-9_]*)\s*[:=]\s*"
            r"['\"]?([^\s'\"]{6,})"
        ),
    ),
]


def find_secrets(text: str) -> list[str]:
    """Return the names of secret patterns found in ``text`` (for security review)."""
    return [name for name, pat in SECRET_PATTERNS if pat.search(text)]


def redact_text(text: str) -> str:
    if not text:
        return text
    out = text
    for name, pat in SECRET_PATTERNS:
        if name == "url_password":
            out = pat.sub(lambda m: f"{m.group(1)}:{REDACTED}@", out)
        elif name == "assignment":
            out = pat.sub(lambda m: f"{m.group(1)}={REDACTED}", out)
        else:
            out = pat.sub(REDACTED, out)
    return out


def redact(value: Any, _depth: int = 0) -> Any:
    """Recursively redact secrets from arbitrary JSON-like data."""
    if _depth > 12:
        return "[TRUNCATED]"
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for k, v in value.items():
            key = str(k)
            if SENSITIVE_KEYS.search(key) and not isinstance(v, (dict, list)) and v not in (None, ""):
                result[key] = REDACTED
            else:
                result[key] = redact(v, _depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        return [redact(v, _depth + 1) for v in value]
    return value


def truncate(text: str, limit: int) -> tuple[str, bool]:
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= limit:
        return text, False
    return encoded[:limit].decode("utf-8", errors="ignore") + "\n…[truncated]", True
