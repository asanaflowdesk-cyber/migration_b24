from __future__ import annotations

import re
from typing import Any

# Bitrix webhook paths contain both a numeric user id and a secret token.
_WEBHOOK_PATH_RE = re.compile(r"(?i)(/rest(?:/api)?/\d+/)[^/\s?'\"<>]+")
_EMAIL_RE = re.compile(r"(?i)\b([A-Z0-9._%+-])[A-Z0-9._%+-]*@([A-Z0-9.-]+\.[A-Z]{2,})\b")
# Conservative phone redaction: only long digit sequences, so ordinary Bitrix IDs stay readable.
_PHONE_RE = re.compile(r"(?<!\d)(?:\+?\d[\d ()-]{8,}\d)(?!\d)")


def sanitize_secret_text(value: Any, *, pii: bool = False) -> str:
    """Return log/report-safe text without webhook tokens.

    ``pii=True`` additionally masks likely email addresses and telephone numbers.
    This is intentionally conservative: free-form comments can contain arbitrary PII,
    so workflows should avoid publishing raw payloads altogether.
    """
    text = str(value or "")
    text = _WEBHOOK_PATH_RE.sub(r"\1***", text)
    if pii:
        text = _EMAIL_RE.sub(r"\1***@\2", text)
        text = _PHONE_RE.sub("***PHONE***", text)
    return text


def excel_literal(value: Any, *, pii: bool = True) -> str:
    """Neutralize formula injection and optionally redact likely PII."""
    text = sanitize_secret_text(value, pii=pii)
    stripped = text.lstrip()
    if stripped.startswith(("=", "+", "-", "@")):
        return "'" + text
    return text
