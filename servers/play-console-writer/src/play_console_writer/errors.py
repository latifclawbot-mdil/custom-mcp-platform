"""Error types and redaction for upstream Google API failures.

Identical contract to the read-only server: nothing from an upstream body or
header reaches the caller verbatim. Errors are reduced to a status, a short
reason, and a redacted, truncated preview.

Write-specific addition: :class:`WriteOutcomeUnknown`. A non-idempotent staged
write that fails at the transport layer (timeout, connection reset) may or may
not have been applied by Google. That is *not* a retryable condition and must
never be reported as a clean failure, so it carries an explicit flag and a
message that tells the caller to verify state before re-issuing.
"""
from __future__ import annotations

import re

__all__ = [
    "UpstreamError",
    "WriteOutcomeUnknown",
    "LocalError",
    "PolicyError",
    "redact_upstream_error",
    "error_from_http_response",
    "redact_text",
]

_MAX_BODY = 800

#: A complete PEM private-key block, body included. The body is the secret; a
#: pattern that matches only the BEGIN/END markers leaves the key material in
#: the preview verbatim. ``[\s\S]`` spans real newlines *and* the ``\n`` escape
#: sequences a JSON body contains. If the END marker never arrives (a truncated
#: body) everything from BEGIN to the end is treated as secret.
_PEM_BLOCK = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"
    r"[\s\S]*?"
    r"(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|\Z)",
    re.IGNORECASE,
)

#: ``key=value`` / ``"key": "value"`` pairs whose value is credential material.
#: This runs *after* the PEM block rule so a ``"private_key": "-----BEGIN...``
#: value is consumed by the block rule first, and the body cannot leak through
#: the assignment rule stopping at its first whitespace.
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(?:access_token|refresh_token|id_token|private_key_id|private_key|"
    r"client_secret|client_id|api_key|apikey|token|password|passwd|secret|"
    r"session_id|sessionid|set-cookie|cookie|sidcc|sid|authorization|"
    r"error_description)\b"
    r"\s*[\"']?\s*[:=]\s*"
    r"(?:\"[^\"]*\"|'[^']*'|[^\s\"',;}\]]+)"
)

# Anything resembling a bearer/access token, OAuth code, or key marker left over
# after the two rules above.
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
    # A private-key marker with no END marker (truncated preview).
    re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----", re.IGNORECASE),
)
_SECRET_HEADERS = ("authorization", "www-authenticate", "proxy-authorization",
                   "set-cookie", "cookie", "x-goog-iap-jwt-assertion")
#: Header names whose value is opaque session/credential material. Such a value
#: is never used as a diagnostic reason; a fixed structural phrase stands in so
#: the cookie/token text cannot ride out inside the error message.
_OPAQUE_SECRET_HEADERS = frozenset({
    "authorization", "www-authenticate", "proxy-authorization", "set-cookie",
    "cookie", "x-goog-iap-jwt-assertion",
})


def redact_text(text: str) -> str:
    """Strip credential-shaped substrings and truncate to a bounded preview.

    Non-JSON bodies (proxy HTML error pages, stack traces) routinely embed
    session cookies and internal hostnames, so they are summarised rather than
    echoed: only the byte count and a structural hint survive.

    Ordering matters: a private-key block is stripped *before* the generic
    ``key=value`` rule, because that rule would otherwise stop at the first
    newline inside a PEM body and leave the key material behind.
    """
    if not text:
        return ""
    stripped = text.lstrip()
    if stripped.startswith("<"):
        return f"[non-JSON body redacted, {len(text)} bytes]"
    out = _PEM_BLOCK.sub("[REDACTED PRIVATE KEY BLOCK]", text)
    out = _SECRET_ASSIGNMENT.sub("[REDACTED]", out)
    for pattern in _TOKEN_PATTERNS:
        out = pattern.sub("[REDACTED]", out)
    if len(out) > _MAX_BODY:
        out = out[:_MAX_BODY] + f"... [truncated, {len(text)} bytes total]"
    return out


class LocalError(Exception):
    """A failure raised by this server (validation, config, allowlist)."""


class PolicyError(LocalError):
    """A request refused by the server's own policy allowlist.

    Raised before any network call, and always naming the policy that refused
    it so a caller can tell "not implemented here" from "not permitted at all".
    """


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


class WriteOutcomeUnknown(UpstreamError):
    """A write failed after the request may have reached Google.

    ``outcome_unknown`` is always true. The message says so explicitly and
    instructs the caller to verify the resource before retrying, because a
    blind retry of a non-idempotent call can duplicate a staged change or, for
    ``commit``, apply an edit twice.
    """

    outcome_unknown = True

    def _message(self) -> str:
        base = (
            f"Google API request failed: HTTP {self.status} ({self.reason}) — "
            "OUTCOME UNKNOWN: the request may have been applied. Verify the "
            "resource state before re-issuing; this server will not retry a "
            "write automatically."
        )
        if self.body:
            return f"{base} Body: {self.body}"
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
        for key, _value in headers.items():
            name = str(key).lower()
            if name in _SECRET_HEADERS:
                # The value of a session/auth header is credential material, not
                # a diagnostic string: a cookie, a bearer token, or an OAuth
                # error_description. Naming the header is enough for a caller to
                # act; echoing the value would leak it into a log line.
                reason = f"rejected by upstream ({name} header present)"
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

    header_names: list[str] = []
    if headers:
        try:
            items = headers.items()
        except AttributeError:  # pragma: no cover - defensive
            items = headers
        header_names = [str(key).lower() for key, _value in items
                        if str(key).lower() in _OPAQUE_SECRET_HEADERS]

    result = redact_upstream_error(
        status=status or 502,
        body=body or "",
        url=url,
    )
    if reason:
        result.reason = redact_text(str(reason)).strip() or result.reason
    elif header_names:
        # Never use the secret header's *value* as the reason; name the header.
        result.reason = f"rejected by upstream ({', '.join(sorted(set(header_names)))} present)"
    result.args = (result._message(),)
    return result
