"""Error types and output redaction for the write server.

A write server's error messages are more dangerous than a read server's: they
land in an LLM context and can reveal that a mutation was attempted, against
which property, and whether it succeeded. Upstream text is treated as
untrusted and redacted before it is ever returned, exactly as in the read-only
server, with the redaction module kept identical in behaviour so the two
servers cannot drift on what a secret looks like.
"""

from __future__ import annotations

import re
from typing import Any, Literal

ErrorCode = Literal[
    "not_configured",
    "site_not_allowed",
    "invalid_argument",
    "bad_request",
    "unauthorized",
    "not_found",
    "rate_limited",
    "upstream_error",
    "network_error",
    "invalid_response",
    "ambiguous_outcome",
]

ERROR_CODES: frozenset[str] = frozenset(
    [
        "not_configured",
        "site_not_allowed",
        "invalid_argument",
        "bad_request",
        "unauthorized",
        "not_found",
        "rate_limited",
        "upstream_error",
        "network_error",
        "invalid_response",
        "ambiguous_outcome",
    ]
)

# Ordered: longer, more specific patterns first so a URL is not half-eaten by
# the numeric rule before the URL rule sees it.
_URL_RE = re.compile(r"https?://[^\s\"'<>]+")
_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN[^-]*PRIVATE KEY-----.*?(?:-----END[^-]*PRIVATE KEY-----|\Z)",
    re.DOTALL,
)
_AUTH_SCHEME_RE = re.compile(
    r"(?i)\b(?:bearer|basic|digest)\b[\s:=]+([^\s\"',;)]+)"
)
_STRONG_KV_RE = re.compile(
    r"(?i)\b(authorization|proxy-authorization|access_token|refresh_token|"
    r"id_token|client_secret|private_key|api[_-]?key|x-api-key)s?\b"
    r"[\s:=]+(?:\b(?:is|was|are|were)\b[\s:=]*)?([^\s\"',;)]+)?"
)
_KV_SECRET_RE = re.compile(
    r"(?i)\b(secret|password|passwd|credential|token)s?\b[\s]*[:=][\s]*"
    r"([^\s\"',;)]+)?"
)
_TOKEN_SHAPE_RE = re.compile(r"ya29\.[A-Za-z0-9._\-]+", re.IGNORECASE)
_LONG_NUMERIC_RE = re.compile(r"\b\d[\d\-]{10,}\b|\b\d{4,}\b")


def redact(value: Any) -> str:
    """Strip credential-shaped and high-entropy material from ``value``.

    Returns ``""`` for ``None``. Never raises: redaction runs on the error path,
    where a second failure would mask the first.
    """
    if value is None:
        return ""
    if not isinstance(value, str):
        try:
            value = str(value)
        except Exception:  # pragma: no cover - objects with a broken __str__
            return "[unprintable]"

    out = _PRIVATE_KEY_RE.sub("[redacted-private-key]", value)
    out = _URL_RE.sub("[redacted-url]", out)
    out = _AUTH_SCHEME_RE.sub("[redacted-secret]", out)
    out = _TOKEN_SHAPE_RE.sub("[redacted-secret]", out)
    out = _STRONG_KV_RE.sub("[redacted-secret]", out)
    out = _KV_SECRET_RE.sub("[redacted-secret]", out)
    out = _LONG_NUMERIC_RE.sub("[redacted]", out)
    return out


class GscError(Exception):
    """An error that is safe to return to the MCP caller.

    ``code`` is a stable identifier the caller can branch on; ``message`` is
    already redacted.
    """

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        if code not in ERROR_CODES:
            raise ValueError(f"unknown error code: {code!r}")
        self.code = code
        self.message = redact(message)
        self.details = {k: redact(v) for k, v in (details or {}).items()}
        super().__init__(f"{code}: {self.message}")

    def to_payload(self) -> dict[str, Any]:
        error: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details:
            error["details"] = self.details
        return {"ok": False, "error": error}


class NotConfiguredError(GscError):
    def __init__(self, message: str) -> None:
        super().__init__("not_configured", message)


class SiteNotAllowedError(GscError):
    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__("site_not_allowed", message, details)


class InvalidArgumentError(GscError):
    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__("invalid_argument", message, details)


class AmbiguousOutcomeError(GscError):
    """A write may or may not have been applied upstream.

    Retrying automatically is exactly the wrong response: the effect is
    unknown, so a second request could duplicate or invert the first. The
    caller must reconcile state before deciding. This code exists so that
    ambiguity is reported as its own outcome instead of being flattened into a
    generic transport error that a client might retry.
    """

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__("ambiguous_outcome", message, details)


class EmptyAllowlistError(NotConfiguredError):
    """Raised at startup when no property allowlist is configured.

    Subclasses :class:`NotConfiguredError` so callers that only know about the
    general case still catch it, while ``__main__`` can catch this specific type
    to print a startup message and exit non-zero.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)