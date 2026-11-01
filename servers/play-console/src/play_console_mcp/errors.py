"""Error types and redaction for upstream Google API failures.

Google error bodies echo request context — sometimes including bearer tokens in
``www-authenticate`` headers, auth details, or proxy HTML. Nothing from upstream
reaches the client verbatim: errors are reduced to a status, a short reason, and
a redacted, truncated body preview.
"""
from __future__ import annotations

import re

__all__ = [
    "UpstreamError",
    "LocalError",
    "redact_upstream_error",
    "error_from_http_response",
    "redact_text",
]

_MAX_BODY = 800

# Anything resembling a bearer/access token, OAuth code, or key material.
#
# These deliberately avoid leading ``\b`` word boundaries: Google credentials
# are routinely embedded in larger strings (e.g. ``prefixya29.SECRET`` in a
# concatenated log line) where a word boundary would miss the secret.
_TOKEN_PATTERNS = (
    # OAuth2 access tokens: "ya29.<dump>" wherever they appear.
    re.compile(r"(?i)ya29\.[A-Za-z0-9._\-]+"),
    # Google OAuth2 authorization codes: "4/<long opaque value>". The 10-char
    # minimum keeps date-shaped text such as "4/12/2026" untouched.
    re.compile(r"(?<![\w/])4/[A-Za-z0-9._\-]{10,}"),
    re.compile(r"(?i)Bearer\s+[A-Za-z0-9._\-~+/=]+"),
    re.compile(r"(?i)1//[A-Za-z0-9._\-]+"),
    re.compile(r"(?i)(client_secret|refresh_token|private_key|access_token|id_token)"
               r"\s*[\"']?\s*[:=]\s*[\"']?[^\s\"',}]+"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
)
_SECRET_HEADERS = ("authorization", "www-authenticate", "proxy-authorization",
                   "set-cookie", "cookie", "x-goog-iap-jwt-assertion")


def redact_text(text: str) -> str:
    """Strip credential-shaped substrings and truncate to a bounded preview.

    Non-JSON bodies (proxy HTML error pages, stack traces) routinely embed
    session cookies and internal hostnames, so they are summarised rather than
    echoed: only the byte count and a structural hint survive.
    """
    if not text:
        return ""
    stripped = text.lstrip()
    if stripped.startswith("<"):
        return f"[non-JSON body redacted, {len(text)} bytes]"
    out = text
    for pattern in _TOKEN_PATTERNS:
        out = pattern.sub("[REDACTED]", out)
    if len(out) > _MAX_BODY:
        out = out[:_MAX_BODY] + f"... [truncated, {len(text)} bytes total]"
    return out


class LocalError(Exception):
    """A failure raised by this server (validation, config, allowlist)."""


class UpstreamError(Exception):
    """A redacted Google API failure. ``str()`` never contains secrets."""

    def __init__(self, *, status: int, reason: str, url: str = "", body: str = ""):
        self.status = int(status)
        self.reason = redact_text(str(reason)).strip() or f"HTTP {status}"
        self.url = url
        self.body = redact_text(body or "")
        super().__init__(self._message())

    def _message(self) -> str:
        base = f"Google API request failed: HTTP {self.status} ({self.reason})"
        if self.body:
            return f"{base} — {self.body}"
        return base


def _extract_reason(payload) -> tuple[str, int | None]:
    """Pull a human-readable reason and any embedded status out of a body."""
    if isinstance(payload, dict):
        err = payload.get("error")
        if isinstance(err, dict):
            status = err.get("code")
            reason = err.get("status") or err.get("message") or ""
            return str(reason), status if isinstance(status, int) else None
        if isinstance(err, str):
            return err, None
        if isinstance(payload.get("message"), str):
            return payload["message"], None
    return "", None


def redact_upstream_error(*, status: int, body, url: str = "", headers=None) -> UpstreamError:
    """Build an :class:`UpstreamError` from an HTTP status/body/headers triple."""
    import json

    payload = None
    text = body if isinstance(body, str) else ""
    if isinstance(body, (bytes, bytearray)):
        text = bytes(body).decode("utf-8", "replace")
    if text:
        try:
            payload = json.loads(text)
        except (ValueError, TypeError):
            payload = None

    reason, embedded = _extract_reason(payload)
    if not reason and headers:
        for key, value in headers.items():
            if str(key).lower() in _SECRET_HEADERS:
                reason = str(value)
                break
    if not reason and text:
        reason = "see body"

    effective_status = embedded or status
    preview = text if payload is None else json.dumps(
        {k: v for k, v in payload.items() if k != "error"} if isinstance(payload, dict) else payload
    )
    if isinstance(payload, dict) and payload.get("error"):
        err = payload["error"]
        preview = json.dumps({"error": err})

    return UpstreamError(
        status=effective_status,
        reason=reason or f"HTTP {status}",
        url=url,
        body=preview,
    )


def error_from_http_response(*, status, reason, headers=None, body=None, url="") -> UpstreamError:
    """Adapt a response object's status/reason/headers/body to an UpstreamError.

    ``reason`` (the transport's status text, e.g. "Unauthorized") is preferred
    over body text because it is stable and never carries credentials. Secret
    headers are consulted only when nothing else is available.
    """
    import http.client

    if body is None and hasattr(reason, "read"):
        body = reason.read()
        status = getattr(reason, "status", status)
        reason = getattr(reason, "reason", "")
    if isinstance(body, (bytes, bytearray)):
        body = bytes(body).decode("utf-8", "replace")
    if isinstance(reason, (bytes, bytearray)):
        reason = bytes(reason).decode("utf-8", "replace")

    try:
        status = int(status)
    except (TypeError, ValueError):
        status = 0

    if not reason and 100 <= status < 600:
        reason = http.client.responses.get(status, "")

    header_reason = ""
    if headers:
        try:
            items = headers.items()
        except AttributeError:  # pragma: no cover - defensive
            items = headers
        for key, value in items:
            if str(key).lower() in _SECRET_HEADERS:
                header_reason = str(value)
                break

    result = redact_upstream_error(
        status=status or 502,
        body=body or "",
        url=url,
    )
    if reason:
        result.reason = redact_text(str(reason)).strip() or result.reason
    elif header_reason:
        result.reason = redact_text(header_reason)
    result.args = (result._message(),)
    return result