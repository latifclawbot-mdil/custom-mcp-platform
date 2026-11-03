"""The four Search Console write operations, over an injected HTTP session.

The official mutation surface of the webmasters v3 API is exactly four methods:

======================  ======  ==================================================
API method              verb    path
======================  ======  ==================================================
``sites.add``           PUT     ``/sites/{siteUrl}``
``sites.delete``        DELETE  ``/sites/{siteUrl}``
``sitemaps.submit``     PUT     ``/sites/{siteUrl}/sitemaps/{feedpath}``
``sitemaps.delete``     DELETE  ``/sites/{siteUrl}/sitemaps/{feedpath}``
======================  ======  ==================================================

All four require ``https://www.googleapis.com/auth/webmasters``. There is no
fifth operation and no generic "make a request" primitive: the only verbs this
module can construct are ``PUT`` and ``DELETE``, and the only paths are the two
templates above. A caller cannot influence the host, the scheme, the method, or
the number of path segments.

Three behavioural rules are enforced here and asserted by tests:

1. **Validate before dispatch.** The property is checked against the allowlist
   and the sitemap URL against the property's scope *before* a request object
   exists, so a refused call never reaches the network.
2. **One request, one call.** There is no retry loop anywhere. A write that
   failed — or whose result is unknown — is reported to the caller, who must
   reconcile. Automatic retry of an ambiguous mutation is how duplicate or
   double-deleted state is created.
3. **Bounded time.** Every request is sent with one explicit timeout; the
   default never exceeds :data:`REQUEST_TIMEOUT_SECONDS`.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Mapping
from urllib.parse import urlsplit

from .allowlist import PropertyAllowlist
from .capture import record
from .encoding import encode_feedpath, encode_property
from .errors import (
    AmbiguousOutcomeError,
    GscError,
    InvalidArgumentError,
    SiteNotAllowedError,
)
from .validation import validate_property_identifier, validate_sitemap_url

logger = logging.getLogger("search_console_writer.client")

#: The only API origin any request may target. Never assembled from input.
BASE_URL = "https://www.googleapis.com/webmasters/v3"

#: Test-only seam. Ignored unless it resolves to a loopback host, so inheriting
#: it in a deployment cannot redirect an authenticated write.
BASE_OVERRIDE_ENV = "GSC_WRITE_API_BASE_OVERRIDE"

#: A single, explicit timeout applied to every request. Writes are not retried,
#: so this bounds how long one attempt may take, not a retry budget.
REQUEST_TIMEOUT_SECONDS = 30.0

#: The only HTTP verbs this package can emit.
ALLOWED_WRITE_METHODS: frozenset[str] = frozenset({"PUT", "DELETE"})

#: Hard cap on the length of untrusted upstream text (an error message) carried
#: into the envelope.
MAX_ERROR_MESSAGE_LEN = 4096

#: Hard cap on the length of a reflected ``Retry-After`` value. A well-formed
#: value is a small integer or an HTTP-date, never thousands of bytes.
MAX_RETRY_AFTER_LEN = 512

_RETRY_AFTER_SECONDS_RE = re.compile(r"[0-9]{1,9}")

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def _bounded(value: Any, limit: int) -> str:
    """Coerce ``value`` to ``str`` and truncate it to ``limit`` characters.

    Upstream error text and headers are untrusted: a server (or a MITM on a
    compromised route) can return an arbitrarily large string, and an unbounded
    reflection puts that whole string into the calling model's context. The
    truncation is marked so a truncated value is not mistaken for the whole.
    """
    try:
        text = value if isinstance(value, str) else str(value)
    except Exception:  # pragma: no cover - objects with a broken __str__
        return ""
    if len(text) <= limit:
        return text
    marker = "...[truncated]"
    if limit <= len(marker):
        return text[:limit]
    return text[: limit - len(marker)] + marker


def _sanitise_retry_after(raw: Any) -> str | None:
    """Return a well-formed ``Retry-After`` value, or ``None``.

    The header is untrusted. Rather than reflect it bounded, it is accepted
    only in the two forms RFC 9110 defines — delta-seconds (a short run of
    ASCII digits) or an HTTP-date — and dropped otherwise. Dropping garbage is
    stronger than truncating it: nothing attacker-chosen reaches the envelope.
    """
    if raw is None:
        return None
    try:
        text = raw if isinstance(raw, str) else str(raw)
    except Exception:  # pragma: no cover - objects with a broken __str__
        return None
    text = text.strip()
    if not text or len(text) > MAX_RETRY_AFTER_LEN:
        return None
    # Reject control characters and any non-ASCII byte before parsing.
    if any(ord(ch) < 0x20 or ord(ch) > 0x7E for ch in text):
        return None
    if _RETRY_AFTER_SECONDS_RE.fullmatch(text):
        return text
    try:
        from email.utils import parsedate_to_datetime

        parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError):
        return None
    return text


def _is_loopback_base(raw: str) -> bool:
    parts = urlsplit(raw)
    if parts.scheme not in ("http", "https"):
        return False
    return (parts.hostname or "") in _LOOPBACK_HOSTS


def resolve_base() -> str:
    """Resolve the API base, honouring the loopback-only test override."""
    raw = os.environ.get(BASE_OVERRIDE_ENV)
    if not raw:
        return BASE_URL
    if not _is_loopback_base(raw):
        logger.warning("ignoring %s: override must point at a loopback host", BASE_OVERRIDE_ENV)
        return BASE_URL
    return raw.rstrip("/")


def _error_from_response(response: Any, *, may_have_applied: bool) -> GscError:
    """Map an upstream response to a redacted :class:`GscError`.

    ``may_have_applied`` is true for a mutating verb: a 5xx genuinely may mean
    the write committed before the failure, so it is carried in ``details``
    rather than hidden.
    """
    status = getattr(response, "status_code", 0)
    message = ""
    try:
        payload = response.json()
        if isinstance(payload, Mapping):
            err = payload.get("error")
            if isinstance(err, Mapping):
                message = str(err.get("message") or "")
            elif isinstance(err, str):
                message = err
    except Exception:
        message = ""
    # Untrusted upstream text is bounded before it can enter the envelope.
    message = _bounded(message, MAX_ERROR_MESSAGE_LEN)

    details: dict[str, Any] = {}
    if may_have_applied and status >= 500:
        details["may_have_applied"] = "true"
    headers = getattr(response, "headers", None) or {}
    try:
        retry_after = headers.get("Retry-After")
    except Exception:
        retry_after = None
    retry_after = _sanitise_retry_after(retry_after)
    if retry_after is not None:
        details["retry_after"] = retry_after

    if status == 400:
        return GscError("bad_request", message or "the request was rejected", details)
    if status in (401, 403):
        hint = (
            "the configured credential was not accepted for this write; "
            "confirm the Google account was added as a user of the property "
            "with owner or full permission"
        )
        return GscError("unauthorized", f"{message or 'access denied'}; {hint}", details)
    if status == 404:
        return GscError(
            "not_found",
            message or "the property or sitemap was not found, or is not managed by this credential",
            details,
        )
    if status == 429:
        return GscError("rate_limited", message or "the API quota was exceeded", details)
    if status == 409:
        return GscError("bad_request", message or "the resource is in a conflicting state", details)
    if status >= 500:
        return GscError("upstream_error", message or "the upstream service failed", details)
    return GscError("upstream_error", message or f"unexpected status {status}", details)


class GscWriteClient:
    """The four official Search Console mutations over an injected session."""

    def __init__(
        self,
        *,
        allowlist: PropertyAllowlist,
        session: Any,
        timeout: float = REQUEST_TIMEOUT_SECONDS,
    ) -> None:
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
            raise InvalidArgumentError("timeout must be a positive number of seconds")
        self._allowlist = allowlist
        self._session = session
        self._timeout = float(timeout)

    @property
    def allowlist(self) -> PropertyAllowlist:
        return self._allowlist

    @property
    def session(self) -> Any:
        """The injected session. Exposed for traceability and tests."""
        return self._session

    # -- URL construction --------------------------------------------------

    def _site_url(self, normalized_property: str) -> str:
        return f"{resolve_base()}/sites/{encode_property(normalized_property)}"

    def _sitemap_url(self, normalized_property: str, sitemap_url: str) -> str:
        return (
            f"{resolve_base()}/sites/{encode_property(normalized_property)}"
            f"/sitemaps/{encode_feedpath(sitemap_url)}"
        )

    # -- transport ---------------------------------------------------------

    def _request(self, method: str, url: str) -> dict[str, Any]:
        """Send exactly one request. Never retries.

        ``url`` is only ever produced by :meth:`_site_url` or
        :meth:`_sitemap_url`; the method is a module constant chosen by the
        caller's operation. Both are re-checked here as defence in depth.
        """
        if method not in ALLOWED_WRITE_METHODS:
            raise InvalidArgumentError(
                "only PUT and DELETE are permitted",
                details={"allowed": ",".join(sorted(ALLOWED_WRITE_METHODS))},
            )
        base = resolve_base()
        if not url.startswith(base + "/"):
            raise InvalidArgumentError("refusing to send a request outside the fixed API base")

        # Records method and URL only, and only when capture is enabled and the
        # base is loopback.
        record(method, url)
        try:
            # Redirects are never followed. ``requests`` defaults to
            # ``allow_redirects=True`` and re-sends a 307/308 — method and body
            # intact — to whatever host the ``Location`` names. On a mutating
            # verb that hands the write to an arbitrary third party. Whatever
            # the status, exactly one request leaves this process.
            response = self._session.request(
                method, url, timeout=self._timeout, allow_redirects=False
            )
        except Exception as exc:
            raise _transport_error(exc) from None

        status = getattr(response, "status_code", None)
        if not isinstance(status, int):
            raise GscError("invalid_response", "the transport returned no status code")

        # Defence in depth: a session that ignored ``allow_redirects=False`` (or
        # a custom transport) still exposes any hops it took in ``history``. A
        # non-empty history means the mutation may already have been re-sent.
        history = getattr(response, "history", None) or ()
        try:
            hops = len(history)
        except Exception:  # pragma: no cover - exotic history object
            hops = 0
        if hops:
            raise GscError(
                "unexpected_redirect",
                "the upstream answered with a redirect and the request was "
                "re-issued; the write may have been sent more than once and to "
                "more than one host",
                {
                    "may_have_applied": "unknown",
                    "redirects_followed": "true",
                    "hops": str(hops),
                },
            )

        # A 3xx is a refusal to perform the write here, not a success. Treating
        # it as success would report "applied" for a mutation that never ran.
        if 300 <= status < 400:
            raise GscError(
                "unexpected_redirect",
                "the upstream answered with a redirect; a write is never "
                "re-sent to the redirect target. The mutation was not applied",
                {
                    "may_have_applied": "false",
                    "redirects_followed": "false",
                    "status": str(status),
                },
            )
        if status >= 400:
            raise _error_from_response(response, may_have_applied=True)
        # All four methods answer with an empty body on success. A body is
        # accepted if present but nothing from it is echoed.
        return {"http_status": status}

    # -- operations --------------------------------------------------------

    def add_site(self, *, site_url: object) -> dict[str, Any]:
        """``sites.add`` — add a property to the credential's Search Console set."""
        validate_property_identifier(site_url)
        normalized = self._allowlist.require(site_url)
        result = self._request("PUT", self._site_url(normalized))
        return {
            "ok": True,
            "operation": "sites.add",
            "site_url": normalized,
            "applied": True,
            "http_status": result["http_status"],
        }

    def delete_site(self, *, site_url: object) -> dict[str, Any]:
        """``sites.delete`` — remove a property from the credential's set."""
        validate_property_identifier(site_url)
        normalized = self._allowlist.require(site_url)
        result = self._request("DELETE", self._site_url(normalized))
        return {
            "ok": True,
            "operation": "sites.delete",
            "site_url": normalized,
            "applied": True,
            "http_status": result["http_status"],
        }

    def submit_sitemap(self, *, site_url: object, sitemap_url: object) -> dict[str, Any]:
        """``sitemaps.submit`` — submit a sitemap URL for an allowlisted property."""
        validate_property_identifier(site_url)
        normalized = self._allowlist.require(site_url)
        validated = validate_sitemap_url(normalized, sitemap_url)
        result = self._request("PUT", self._sitemap_url(normalized, validated))
        return {
            "ok": True,
            "operation": "sitemaps.submit",
            "site_url": normalized,
            "sitemap_url": validated,
            "applied": True,
            "http_status": result["http_status"],
        }

    def delete_sitemap(self, *, site_url: object, sitemap_url: object) -> dict[str, Any]:
        """``sitemaps.delete`` — remove a sitemap from an allowlisted property."""
        validate_property_identifier(site_url)
        normalized = self._allowlist.require(site_url)
        validated = validate_sitemap_url(normalized, sitemap_url)
        result = self._request("DELETE", self._sitemap_url(normalized, validated))
        return {
            "ok": True,
            "operation": "sitemaps.delete",
            "site_url": normalized,
            "sitemap_url": validated,
            "applied": True,
            "http_status": result["http_status"],
        }


def _transport_error(exc: BaseException) -> GscError:
    """Classify a transport failure on a mutating verb.

    A failure to *establish* the connection means the request was never sent, so
    the effect is unambiguously "not applied". Anything that could have happened
    after the bytes left is ambiguous, and saying so is the honest answer: the
    caller must reconcile rather than retry.
    """
    try:
        import requests
    except ImportError:  # pragma: no cover - requests is a runtime dependency
        requests = None  # type: ignore[assignment]

    if requests is not None:
        if isinstance(exc, requests.exceptions.ConnectTimeout):
            return GscError(
                "network_error",
                "could not establish a connection; the write was not sent",
                {"reason": "connect_timeout", "may_have_applied": "false"},
            )
    # Read timeouts, resets, TLS failures after connect, and any other error are
    # treated as ambiguous: the request may have been delivered.
    ambiguity = (
        "the write may or may not have been applied; "
        "re-read the property or sitemap state before retrying"
    )
    return AmbiguousOutcomeError(
        f"transport failure of unknown effect ({type(exc).__name__}); {ambiguity}",
        {"may_have_applied": "unknown"},
    )


__all__ = [
    "ALLOWED_WRITE_METHODS",
    "BASE_URL",
    "BASE_OVERRIDE_ENV",
    "GscWriteClient",
    "REQUEST_TIMEOUT_SECONDS",
    "resolve_base",
]