"""HTTP client for the Android Publisher API v3.

Design notes
------------
* **Read-only by construction.** :meth:`GoogleApiClient.request` accepts a verb
  only to reject everything except ``GET``. There is no code path in this
  package that issues a mutating request, so no ``edits.insert`` / ``commit`` /
  ``publish`` is reachable.
* **Real paths, not a wrapper.** The client resolves ``{placeholder}`` templates
  against caller-supplied, already-validated parameters with percent-free
  substitution, then asserts the final path still lives under the
  ``androidpublisher/v3`` root. The tests assert the literal upstream paths.
* **Bounded and quiet.** Every call carries an explicit timeout that is a
  *total* budget shared across retries (a single request can never exceed one
  timeout period, not 3×), retries only idempotent GETs on 429/5xx with backoff
  inside that budget, and converts any non-2xx response into a redacted
  :class:`UpstreamError`.
"""
from __future__ import annotations

import random
import re
import time
from typing import Any, Mapping

import httpx

from .auth import CredentialConfig
from .errors import LocalError, UpstreamError, redact_upstream_error
from .settings import Settings

__all__ = ["GoogleApiClient", "API_ROOT", "READ_ONLY_VERB"]

API_ROOT = "https://androidpublisher.googleapis.com"
PATH_ROOT = "/androidpublisher/v3/"
READ_ONLY_VERB = "GET"

_PLACEHOLDER = re.compile(r"\{(\+?)([A-Za-z_][A-Za-z0-9_]*)\}")
_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
_MAX_ATTEMPTS = 3


class GoogleApiClient:
    """Minimal, auditable read-only transport for the Play Console API."""

    def __init__(
        self,
        *,
        settings: Settings,
        credentials: Any | None = None,
        credential_config: CredentialConfig | None = None,
        transport: httpx.BaseTransport | None = None,
        api_root: str = API_ROOT,
        sleep=time.sleep,
        clock=time.monotonic,
    ) -> None:
        self._settings = settings
        self._credentials = credentials
        self._credential_config = credential_config
        self._api_root = api_root.rstrip("/")
        self._sleep = sleep
        self._clock = clock
        self._last_request: dict[str, Any] | None = None
        self._client = httpx.Client(
            base_url=self._api_root,
            transport=transport,
            timeout=settings.request_timeout,
            headers={"accept": "application/json"},
        )

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "GoogleApiClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- request -----------------------------------------------------------

    def request(
        self,
        verb: str,
        path_template: str,
        *,
        params: Mapping[str, Any] | None = None,
        timeout: float | None = None,
    ) -> Any:
        """Issue a read-only GET against ``path_template``.

        ``path_template`` contains ``{name}`` placeholders replaced from
        ``params``; any parameter consumed by a placeholder is removed from the
        query string so it is not sent twice. Remaining params become query
        parameters.
        """
        if verb.upper() != READ_ONLY_VERB:
            raise LocalError(
                f"this server is read-only; {verb.upper()} requests are refused "
                f"before any network call (allowed: {READ_ONLY_VERB})"
            )

        params = dict(params or {})
        path = self._render_path(path_template, params)

        query = {
            key: _stringify(value)
            for key, value in params.items()
            if value is not None and value != ""
        }

        return self._send(path, query, timeout=timeout)

    def get(self, *args: Any, **kwargs: Any) -> Any:
        """Convenience alias for :meth:`request` with ``GET``."""
        return self.request(READ_ONLY_VERB, *args, **kwargs)

    # -- internals ---------------------------------------------------------

    def _render_path(self, template: str, params: dict[str, Any]) -> str:
        """Substitute placeholders and forbid escaping the API root."""

        def replace(match: re.Match[str]) -> str:
            name = match.group(2)
            if name not in params:
                raise LocalError(f"missing path parameter: {name}")
            raw = params.pop(name)
            if raw is None or raw == "":
                raise LocalError(f"path parameter {name} must not be empty")
            text = _stringify(raw)
            if match.group(1) == "+":
                if not text.startswith("applications/") or ".." in text:
                    raise LocalError(f"invalid resource name for {name}")
                return text
            if "/" in text or "?" in text or "#" in text or "%" in text or "\\" in text:
                raise LocalError(f"path parameter {name} must be a single segment")
            return text

        path = _PLACEHOLDER.sub(replace, template)
        if not path.startswith(PATH_ROOT):
            raise LocalError(f"refusing request outside {PATH_ROOT}")
        if ".." in path:
            raise LocalError("refusing request containing a parent-directory segment")
        return path

    def _send(self, path: str, query: dict[str, str], *, timeout: float | None) -> Any:
        headers = {"authorization": f"Bearer {self._token()}"}
        effective_timeout = timeout or self._settings.request_timeout
        # Total wall-clock budget for *all* attempts, not per attempt. Without
        # this, 3 attempts x a 300 s timeout meant a single GET could block for
        # 15 minutes. Retries now share one deadline: each attempt only gets
        # whatever time is left, and once the budget is spent no further
        # request is issued.
        deadline = self._clock() + effective_timeout
        # Audit trail: what was actually requested upstream. Never contains
        # credentials or requested token material.
        self._last_request = {
            "method": READ_ONLY_VERB,
            "root": self._api_root,
            "path": path,
            "query": dict(query),
        }

        last: UpstreamError | None = None
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            attempt_timeout = effective_timeout
            if attempt > 1:
                remaining = deadline - self._clock()
                if remaining <= 0:
                    # Budget exhausted: surface the previous failure instead of
                    # issuing another request that can only time out.
                    if last is not None:
                        raise last
                    raise UpstreamError(
                        status=504,
                        reason="upstream timeout",
                        url=f"{self._api_root}{path}",
                        body=f"request budget of {effective_timeout}s exhausted",
                    )
                attempt_timeout = min(effective_timeout, remaining)
            try:
                response = self._client.get(
                    path, params=query, headers=headers, timeout=attempt_timeout
                )
            except httpx.TimeoutException as exc:
                last = UpstreamError(
                    status=504,
                    reason="upstream timeout",
                    url=f"{self._api_root}{path}",
                    body=f"{type(exc).__name__}: request exceeded "
                         f"{attempt_timeout}s (total budget {effective_timeout}s)",
                )
                if attempt < _MAX_ATTEMPTS:
                    self._backoff(attempt)
                    continue
                raise last from None
            except httpx.HTTPError as exc:
                raise UpstreamError(
                    status=502,
                    reason="transport error",
                    url=f"{self._api_root}{path}",
                    body=type(exc).__name__,
                ) from None

            if response.status_code < 300:
                try:
                    return response.json()
                except ValueError:
                    raise UpstreamError(
                        status=502,
                        reason="upstream returned a non-JSON body",
                        url=str(response.request.url),
                        body=response.text,
                    ) from None

            error = _response_error(response, self._api_root)
            if response.status_code in _RETRY_STATUSES and attempt < _MAX_ATTEMPTS:
                last = error
                self._backoff(attempt)
                continue
            raise error

        assert last is not None  # pragma: no cover - loop always raises
        raise last

    def _backoff(self, attempt: int) -> None:
        # Bounded exponential backoff with jitter; small so tests stay fast.
        self._sleep(min(0.25 * (2 ** (attempt - 1)), 2.0) + random.uniform(0, 0.05))

    def _token(self) -> str:
        """Return an access token, materialising credentials on first use."""
        creds = self._credentials
        if creds is None:
            if self._credential_config is None:
                raise LocalError(
                    "no Google credentials configured; the server cannot "
                    "authenticate to the Play API"
                )
            creds = self._credential_config.resolve()
            self._credentials = creds

        token = getattr(creds, "token", None)
        if token is None or getattr(creds, "valid", True) is False:
            # Refresh when absent or expired, not only when token is missing.
            import google.auth.transport.requests

            request = google.auth.transport.requests.Request()
            try:
                creds.refresh(request)
            except Exception as exc:  # noqa: BLE001
                raise _refresh_error(exc) from None
            token = getattr(creds, "token", None)

        if not token:
            raise LocalError("Google credentials produced an empty access token")
        return str(token)


def _refresh_error(exc: Exception) -> UpstreamError:
    from .auth import CredentialConfig

    return UpstreamError(
        status=401,
        reason="credential refresh failed",
        body=CredentialConfig._describe(exc),
    )


def _stringify(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple)):
        return ",".join(_stringify(item) for item in value)
    return str(value)


def _response_error(response: httpx.Response, api_root: str) -> UpstreamError:
    url = str(response.request.url) if response.request is not None else api_root
    return redact_upstream_error(
        status=response.status_code,
        body=response.text,
        url=url,
        headers=dict(response.headers),
    )