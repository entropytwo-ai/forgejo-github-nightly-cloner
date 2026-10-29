# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 forgejo-github-nightly-cloner contributors
"""Logging setup with secret redaction.

All output goes to stdout (Docker best practice). Every record passes through
:class:`RedactingFilter`, which removes registered secret values and anything
that looks like a GitHub/Forgejo token or an ``Authorization`` header value.
"""

from __future__ import annotations

import json
import logging
import re
import sys
import threading
from datetime import datetime, timezone
from typing import Iterable, List, Optional, Set

REDACTED = "***REDACTED***"

# Patterns for credentials that may appear even if not explicitly registered.
_TOKEN_PATTERNS: List[re.Pattern] = [
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"(?i)(authorization['\"]?\s*[:=]\s*['\"]?(?:bearer|token|basic)\s+)[^\s'\",}]+"),
    re.compile(r"(?i)\b((?:bearer|token)\s+)[A-Za-z0-9_\-\.=]{20,}"),
    re.compile(r"(?i)((?:access_token|auth_token|token|password|passwd)['\"]?\s*[:=]\s*['\"]?)[^\s'\",&}]{6,}"),
    re.compile(r"(https?://)[^/\s:@]+:[^/\s@]+@"),
]

_secrets: Set[str] = set()
_secrets_lock = threading.Lock()


def register_secret(value: Optional[str]) -> None:
    """Register a secret value so that it is replaced in every log line."""
    if value and len(value) >= 4:
        with _secrets_lock:
            _secrets.add(value)


def redact(text: str, extra_secrets: Iterable[str] = ()) -> str:
    """Return ``text`` with all known secrets and token-like strings masked."""
    if not text:
        return text
    with _secrets_lock:
        values = set(_secrets)
    values.update(s for s in extra_secrets if s)
    for secret in sorted(values, key=len, reverse=True):
        text = text.replace(secret, REDACTED)
    for pattern in _TOKEN_PATTERNS:
        if pattern.groups:
            text = pattern.sub(lambda m: m.group(1) + REDACTED + ("@" if m.group(0).endswith("@") else ""), text)
        else:
            text = pattern.sub(REDACTED, text)
    return text


class RedactingFilter(logging.Filter):
    """Logging filter that redacts secrets from the message, args and exceptions."""

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: D401 - logging API
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - malformed format args
            message = str(record.msg)
        record.msg = redact(message)
        record.args = None
        if record.exc_info:
            formatted = logging.Formatter().formatException(record.exc_info)
            record.exc_text = redact(formatted)
            record.exc_info = None
        return True


class JsonFormatter(logging.Formatter):
    """Single-line JSON formatter (enable with ``LOG_FORMAT=json``)."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "module": record.name,
            "message": record.getMessage(),
        }
        if record.exc_text:
            payload["exception"] = record.exc_text
        return redact(json.dumps(payload, ensure_ascii=False))


class TextFormatter(logging.Formatter):
    """Human readable formatter that re-applies redaction as a final safety net."""

    def __init__(self) -> None:
        super().__init__("%(asctime)s %(levelname)-8s %(name)s: %(message)s", "%Y-%m-%dT%H:%M:%S%z")

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def setup_logging(level: str = "INFO", fmt: str = "text", secrets: Iterable[str] = ()) -> logging.Logger:
    """Configure the root logger to write redacted records to stdout.

    Args:
        level: Log level name.
        fmt: ``text`` or ``json``.
        secrets: Secret values that must never appear in output.
    """
    for secret in secrets:
        register_secret(secret)
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, "_fgnc_handler", False):
            root.removeHandler(handler)
    handler = logging.StreamHandler(sys.stdout)
    handler._fgnc_handler = True  # type: ignore[attr-defined]
    handler.addFilter(RedactingFilter())
    handler.setFormatter(JsonFormatter() if fmt == "json" else TextFormatter())
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    # urllib3 debug output can include full URLs and headers; keep it quiet.
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    return root


def get_logger(name: str) -> logging.Logger:
    """Return a module logger."""
    return logging.getLogger(name)
