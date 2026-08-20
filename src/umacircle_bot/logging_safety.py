from __future__ import annotations

import logging
import re
import traceback

_URI_USERINFO_PATTERN = re.compile(
    r"(?P<prefix>\b[a-z][a-z0-9+.-]*://[^\s/:@]+:)(?P<secret>[^\s/@]+)(?P<suffix>@)",
    re.IGNORECASE,
)
_SENSITIVE_ASSIGNMENT_PATTERN = re.compile(
    r"(?P<key>\b(?:database_url|discord_token|password|passwd|pwd|token|secret|api[_-]?key)\b)"
    r"[\"']?\s*[:=]\s*[\"']?(?P<secret>[^\s,;\"']+)",
    re.IGNORECASE,
)
_BEARER_PATTERN = re.compile(r"\bBearer\s+[^\s,;]+", re.IGNORECASE)
_REDACTED = "[REDACTED]"


def redact_sensitive_text(value: str) -> str:
    redacted = _URI_USERINFO_PATTERN.sub(
        lambda match: f"{match.group('prefix')}{_REDACTED}{match.group('suffix')}",
        value,
    )
    redacted = _SENSITIVE_ASSIGNMENT_PATTERN.sub(
        lambda match: f"{match.group('key')}={_REDACTED}",
        redacted,
    )
    return _BEARER_PATTERN.sub(f"Bearer {_REDACTED}", redacted)


def log_sanitized_exception(logger: logging.Logger, message: str, *args: object) -> None:
    context = redact_sensitive_text(message % args if args else message)
    sanitized_traceback = redact_sensitive_text(traceback.format_exc())
    logger.error("%s\nsanitized_traceback:\n%s", context, sanitized_traceback)
