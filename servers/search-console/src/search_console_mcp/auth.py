"""Credential acquisition — lazy, read-only, never logged.

Two properties matter here:

**Laziness.** :func:`get_session` is the only path to Application Default
Credentials, and it is called from the client factory on the *first tool call
that needs it* — never at import, never at server construction. A tool call
rejected by the allowlist or the bounds checks therefore performs no credential
lookup at all, which is observable in tests and valuable in production (no
surprise token refreshes for calls that were never going to be made).

**Scope.** Exactly one scope is requested: ``webmasters.readonly``. The
read/write ``webmasters`` scope is never requested, so even a bug in this
server cannot perform a write — the token simply would not authorise one.

The token itself never enters this code path: ``AuthorizedSession`` attaches
the bearer header internally, so there is nothing here to log or return.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any, Optional

logger = logging.getLogger("search_console_mcp.auth")

#: The single scope this server ever requests.
SCOPES: tuple[str, ...] = ("https://www.googleapis.com/auth/webmasters.readonly",)

#: Test-only seam. When set (and the API base override points at loopback), a
#: static token is used instead of ADC. Guarded on both sides: it has no effect
#: unless the request base is also overridden to a loopback host, so it cannot
#: be used to authenticate against Google's real endpoints.
STATIC_TOKEN_ENV = "GSC_TEST_STATIC_TOKEN"

_lock = threading.Lock()
_session: Optional[Any] = None


class CredentialError(RuntimeError):
    """Raised when ADC cannot be obtained.

    The message is deliberately generic: ADC error strings can embed service
    account file paths and principal addresses.
    """


def _loopback_override_active() -> bool:
    """Whether every API base has been overridden to a loopback host.

    Both bases must be overridden, not just the webmasters one: the static
    token also authenticates inspection calls, and an un-overridden inspection
    endpoint still points at Google.
    """
    from .client import all_bases_loopback

    return all_bases_loopback()


def _build_static_session(token: str) -> Any:
    """A session carrying a fixed bearer token, for offline tests only."""
    try:
        import requests
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise CredentialError("requests is required for the static-token test seam") from exc

    class _StaticAuth(requests.auth.AuthBase):
        def __call__(self, request):
            request.headers["Authorization"] = f"Bearer {token}"
            return request

    session = requests.Session()
    session.auth = _StaticAuth()
    return session


def _build_session() -> Any:
    # Test-only: a static token is honoured *only* alongside a loopback base
    # override, so this branch cannot authenticate against Google.
    static_token = os.environ.get(STATIC_TOKEN_ENV)
    if static_token and _loopback_override_active():
        return _build_static_session(static_token)
    # Imported lazily so that importing this module (and therefore the MCP
    # server module) does not drag in the credential machinery, and so a
    # deployment that never authenticates can still import and introspect.
    try:
        import google.auth
        from google.auth.transport.requests import AuthorizedSession
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise CredentialError(
            "google-auth (and its requests extra) is required for credentialed calls"
        ) from exc

    try:
        credentials, _project = google.auth.default(scopes=list(SCOPES))
    except Exception as exc:
        # Never surface the provider's message: it can contain a file path or a
        # principal email. The type name is enough to debug with.
        raise CredentialError(
            f"could not obtain Application Default Credentials ({type(exc).__name__}); "
            f"set GOOGLE_APPLICATION_CREDENTIALS or run on a workload with an "
            f"attached service account"
        ) from None

    granted = set(getattr(credentials, "scopes", None) or [])
    if granted and not granted.issubset(set(SCOPES)):
        # A credential carrying broader scopes than we asked for is a
        # misconfiguration worth refusing to use.
        raise CredentialError(
            "the resolved credential carries scopes broader than webmasters.readonly"
        )

    session = AuthorizedSession(credentials)
    # google-auth's AuthorizedSession can be given an explicit timeout per
    # request; the client passes one. Nothing else is configured here.
    return session


def get_session(*, force_refresh: bool = False) -> Any:
    """Return a process-wide ``AuthorizedSession``, building it on first use."""
    global _session
    if _session is not None and not force_refresh:
        return _session
    with _lock:
        if _session is None or force_refresh:
            _session = _build_session()
        return _session


def reset_session() -> None:
    """Drop the cached session. Intended for tests."""
    global _session
    with _lock:
        _session = None


def build_client(*, allowlist: Any, timeout: float) -> Any:
    """Construct a :class:`GscClient` backed by real ADC.

    Imported here rather than at module import time so this module stays free of
    a hard dependency on the client for tests that only exercise auth.
    """
    from .client import GscClient

    return GscClient(allowlist=allowlist, session=get_session(), timeout=timeout)