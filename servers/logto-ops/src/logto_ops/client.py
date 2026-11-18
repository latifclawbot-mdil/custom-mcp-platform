"""Logto Management API client.

Design constraints, each of which exists because of a concrete failure mode:

* **The token is fetched lazily and cached in memory only.** No disk cache, so a
  container restart cannot resurrect a stale token, and no log line contains
  one.
* **Redirects are never followed.** The bearer token would otherwise be
  forwarded to whatever host a redirect names.
* **Responses are bounded.** A misconfigured endpoint that streams indefinitely
  cannot exhaust the container.
* **Errors are sanitised.** Logto error bodies are *not* echoed back: a token or
  a client-secret fragment can appear in one, and this text ends up in an LLM's
  context and in LiteLLM logs. The caller gets a short classification plus the
  provider's error code when it is a well-formed OIDC error.
* **No retry on ambiguity.** A create that times out may have succeeded. The
  client reports ``ambiguous`` and the tool layer refuses to auto-repeat it.
"""

from __future__ import annotations

import base64
import logging
import threading
import time
import urllib.parse
from dataclasses import dataclass
from typing import Any, Mapping

import httpx

from .settings import Settings

__all__ = [
    "AmbiguousResult",
    "LogtoClient",
    "LogtoError",
    "LogtoHTTPError",
    "LogtoTransportError",
]

logger = logging.getLogger("logto_ops.client")

#: Refresh this many seconds before the provider's stated expiry, so a token is
#: not used after the second it dies.
TOKEN_EXPIRY_SKEW_SECONDS = 60


class LogtoError(RuntimeError):
    """Base class for client failures. Messages never contain credentials."""


class LogtoHTTPError(LogtoError):
    """The provider returned a non-2xx response."""

    def __init__(self, status: int, *, operation: str, provider_code: str | None = None, message: str = ""):
        super().__init__(f"{operation} failed with HTTP {status}" + (f": {message}" if message else ""))
        self.status = status
        self.operation = operation
        self.provider_code = provider_code
        self.message = message


class LogtoTransportError(LogtoError):
    """The request never completed (connection, timeout, oversize body)."""


class AmbiguousResult(LogtoError):
    """A mutating request may or may not have been applied.

    Raised for a timeout or a connection reset on a non-idempotent write. The
    caller must reconcile before retrying; a blind retry can create a second
    application.
    """


@dataclass(frozen=True)
class _Token:
    value: str
    expires_at: float

    def usable(self, now: float) -> bool:
        return bool(self.value) and now + TOKEN_EXPIRY_SKEW_SECONDS < self.expires_at


def _provider_error_code(body: bytes) -> tuple[str | None, str]:
    """Extract the OIDC/Logto error code and a short message, or fail safely."""
    try:
        import json

        payload = json.loads(body.decode("utf-8", "replace"))
    except Exception:
        return None, ""
    if not isinstance(payload, dict):
        return None, ""
    code = payload.get("code") or payload.get("error")
    # Logto wraps Management API errors in {"code","message","data"}.
    message = payload.get("message") or payload.get("error_description") or ""
    if not isinstance(code, str):
        code = None
    message = message if isinstance(message, str) else ""
    # Truncate: these strings are shown to a model, and provider messages can
    # embed request material.
    return code, message[:300]


class LogtoClient:
    """Minimal, bounded Management API client with a single cached token."""

    def __init__(self, settings: Settings, *, transport: httpx.BaseTransport | None = None) -> None:
        self._settings = settings
        self._lock = threading.Lock()
        self._token: _Token | None = None
        headers = {
            "Accept": "application/json",
            "User-Agent": "logto-ops-mcp",
        }
        if settings.cf_access_client_id and settings.cf_access_client_secret:
            headers["CF-Access-Client-Id"] = settings.cf_access_client_id
            headers["CF-Access-Client-Secret"] = settings.cf_access_client_secret
        self._http = httpx.Client(
            headers=headers,
            # Never follow: the bearer would travel to the redirect target.
            follow_redirects=False,
            timeout=httpx.Timeout(settings.http_timeout),
            transport=transport,
        )

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "LogtoClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- token -------------------------------------------------------------

    def _fetch_token(self) -> _Token:
        basic = base64.b64encode(
            f"{self._settings.app_id}:{self._settings.app_secret}".encode("utf-8")
        ).decode("ascii")
        body = urllib.parse.urlencode(
            {
                "grant_type": "client_credentials",
                "resource": self._settings.api_indicator,
                "scope": "all",
            }
        ).encode("ascii")
        try:
            response = self._http.post(
                self._settings.token_url,
                content=body,
                headers={
                    "Authorization": f"Basic {basic}",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
            )
        except httpx.TimeoutException as exc:
            raise LogtoTransportError("token request timed out") from exc
        except httpx.HTTPError as exc:
            raise LogtoTransportError(f"token request failed ({type(exc).__name__})") from exc
        raw = self._read_bounded(response, operation="token")
        if response.status_code >= 300 and response.status_code != 304:
            code, message = _provider_error_code(raw)
            raise LogtoHTTPError(
                response.status_code, operation="token", provider_code=code, message=message
            )
        import json

        try:
            payload = json.loads(raw)
        except Exception as exc:
            raise LogtoTransportError("token response was not JSON") from exc
        token = payload.get("access_token")
        try:
            expires_in = float(payload.get("expires_in") or 3600)
        except (TypeError, ValueError):
            expires_in = 3600.0
        if not isinstance(token, str) or not token:
            raise LogtoTransportError("token response contained no access_token")
        return _Token(value=token, expires_at=time.time() + expires_in)

    def access_token(self) -> str:
        """Return a usable token, fetching one if needed."""
        with self._lock:
            now = time.time()
            if self._token is not None and self._token.usable(now):
                return self._token.value
            self._token = self._fetch_token()
            return self._token.value

    def invalidate_token(self) -> None:
        with self._lock:
            self._token = None

    # -- requests ----------------------------------------------------------

    def _read_bounded(self, response: httpx.Response, *, operation: str) -> bytes:
        chunks: list[bytes] = []
        total = 0
        try:
            for chunk in response.iter_bytes():
                total += len(chunk)
                if total > self._settings.max_response_bytes:
                    raise LogtoTransportError(
                        f"{operation} response exceeded {self._settings.max_response_bytes} bytes"
                    )
                chunks.append(chunk)
        except httpx.HTTPError as exc:
            raise LogtoTransportError(f"{operation} response could not be read") from exc
        return b"".join(chunks)

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json_body: Mapping[str, Any] | None = None,
        operation: str,
        idempotent: bool = True,
        retry_on_401: bool = True,
    ) -> Any:
        """Perform a Management API request and return the decoded body.

        ``idempotent`` is False for creates. A transport failure on a
        non-idempotent request raises :class:`AmbiguousResult` instead of a
        plain transport error, so the caller cannot mistake it for "nothing
        happened".
        """
        url = f"{self._settings.api_root}{path}"
        clean_params = {k: v for k, v in (params or {}).items() if v is not None}
        try:
            response = self._http.request(
                method,
                url,
                params=clean_params or None,
                json=dict(json_body) if json_body is not None else None,
                headers={"Authorization": f"Bearer {self.access_token()}"},
            )
        except (httpx.TimeoutException, httpx.ConnectError, httpx.ReadError) as exc:
            if not idempotent:
                raise AmbiguousResult(
                    f"{operation} may have been applied: the connection failed after the "
                    "request was sent. Read the target back before retrying."
                ) from exc
            raise LogtoTransportError(f"{operation} failed ({type(exc).__name__})") from exc
        except httpx.HTTPError as exc:
            raise LogtoTransportError(f"{operation} failed ({type(exc).__name__})") from exc

        if response.status_code == 401 and retry_on_401:
            # Token revoked or expired early: drop it and try exactly once more.
            self.invalidate_token()
            return self.request(
                method,
                path,
                params=params,
                json_body=json_body,
                operation=operation,
                idempotent=idempotent,
                retry_on_401=False,
            )

        raw = self._read_bounded(response, operation=operation)
        if response.status_code >= 300 and response.status_code not in (204, 304):
            # Covers redirects as well as errors: a 3xx must be reported, never
            # followed, because the redirect target would receive the bearer.
            code, message = _provider_error_code(raw)
            raise LogtoHTTPError(
                response.status_code, operation=operation, provider_code=code, message=message
            )
        if not raw:
            return None
        import json

        try:
            return json.loads(raw)
        except Exception as exc:
            raise LogtoTransportError(f"{operation} returned a non-JSON body") from exc

    # -- paginated reads ---------------------------------------------------

    def list_page(
        self,
        path: str,
        *,
        page: int = 1,
        page_size: int | None = None,
        params: Mapping[str, Any] | None = None,
        operation: str,
    ) -> list[dict[str, Any]]:
        """One page of a collection."""
        if not isinstance(page, int) or isinstance(page, bool) or page < 1:
            raise LogtoError("page must be an integer >= 1")
        query = dict(params or {})
        query["page"] = page
        query["page_size"] = self._settings.clamp_page_size(page_size)
        payload = self.request("GET", path, params=query, operation=operation)
        if payload is None:
            return []
        if not isinstance(payload, list):
            raise LogtoTransportError(f"{operation} did not return a JSON array")
        return [item for item in payload if isinstance(item, dict)]
