"""Credential acquisition for the write server — lazy, write-scoped, separate.

Design rules, each one a lesson from the read-only server applied to a component
that can actually mutate state:

**One scope.** Exactly ``https://www.googleapis.com/auth/webmasters`` is
requested. That is the *narrowest* scope under which the four mutation methods
work; the read-only scope cannot perform them. A resolved credential that also
carries other scopes is refused rather than used, because a wider grant is a
misconfiguration on the one component where that matters most.

**Separate identity.** The credential comes from
``GSC_WRITE_SERVICE_ACCOUNT_FILE`` when configured, which lets an operator run
the writer as a different principal from the read server. Only when that is
absent does it fall back to Application Default Credentials — still with the
write scope, never the read server's scope.

**Laziness.** Nothing here runs at import. :func:`build_session` is called from
the client factory on the first *validated* call, so a call refused by the
allowlist or by URL validation never touches a credential.

The token never enters this code: ``AuthorizedSession`` attaches the bearer
header internally, and the service-account file's contents are handed to
``google-auth`` without being read, stored, or logged here.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any, Optional

from . import client
from .errors import CredentialError

logger = logging.getLogger("search_console_writer.auth")

#: The single scope this server ever requests.
SCOPES: tuple[str, ...] = ("https://www.googleapis.com/auth/webmasters",)

#: Write-specific credential source. Preferred over ambient ADC so the writer
#: can run as a separate principal from the read-only server.
SERVICE_ACCOUNT_ENV = "GSC_WRITE_SERVICE_ACCOUNT_FILE"

#: Test-only seam. Honoured only when the API base is a loopback host, so it
#: cannot be used to authenticate against Google's real endpoints.
STATIC_TOKEN_ENV = "GSC_WRITE_TEST_STATIC_TOKEN"

_lock = threading.Lock()
_session: Optional[Any] = None


def _loopback_override_active() -> bool:
    return client.resolve_base() != client.BASE_URL


def _build_static_session(token: str) -> Any:
    if not token.strip():
        raise CredentialError(
            f"{STATIC_TOKEN_ENV} is set but empty; a loopback test credential "
            f"must be a non-empty token"
        )
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


def _load_service_account(path: str) -> Any:
    """Load a service-account credential from ``path`` with the write scope.

    Split out so it can be substituted in tests without a real key file.
    """
    try:
        from google.oauth2 import service_account
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise CredentialError(
            "google-auth's service_account module is required for "
            f"{SERVICE_ACCOUNT_ENV}"
        ) from exc
    try:
        return service_account.Credentials.from_service_account_file(
            path, scopes=list(SCOPES)
        )
    except Exception as exc:
        # The path is operator-supplied configuration, not a secret; the file's
        # contents are never read by this code. Report the failure type only.
        raise CredentialError(
            f"could not load the service-account credential named by "
            f"{SERVICE_ACCOUNT_ENV} ({type(exc).__name__})"
        ) from None


def _load_adc() -> Any:
    try:
        import google.auth
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise CredentialError(
            "google-auth is required for credentialed writes"
        ) from exc
    try:
        credentials, _project = google.auth.default(scopes=list(SCOPES))
    except Exception as exc:
        raise CredentialError(
            f"could not obtain Application Default Credentials ({type(exc).__name__}); "
            f"set {SERVICE_ACCOUNT_ENV} or GOOGLE_APPLICATION_CREDENTIALS, or run on "
            f"a workload with an attached service account"
        ) from None
    return credentials


def _wrap(credentials: Any) -> Any:
    """Validate the grant and wrap the credential in an authorised session."""
    granted = set(getattr(credentials, "scopes", None) or [])
    if granted and not granted.issubset(set(SCOPES)):
        raise CredentialError(
            "the resolved credential carries scopes broader than "
            "https://www.googleapis.com/auth/webmasters; refusing to use it for writes"
        )
    try:
        from google.auth.transport.requests import AuthorizedSession
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise CredentialError(
            "google-auth's requests transport is required for credentialed writes"
        ) from exc
    return AuthorizedSession(credentials)


def build_session() -> Any:
    """Build the write session. Called lazily, on the first valid call.

    When the API base has been overridden to a loopback host (the test seam),
    the static test token is the *only* credential this function will use. It
    will not fall back to a real service-account file or ambient ADC: a test
    override pointing at a fake upstream must never cause an operator's real
    write credential to be loaded or presented. With no test token the call
    fails closed instead of silently authenticating with a production
    credential.
    """
    static_token = os.environ.get(STATIC_TOKEN_ENV)
    if _loopback_override_active():
        if not static_token:
            raise CredentialError(
                "the API base has been overridden to a loopback host, but no "
                f"test credential is configured; set {STATIC_TOKEN_ENV} to a "
                "non-empty test token. A loopback override never falls back to "
                f"{SERVICE_ACCOUNT_ENV} or Application Default Credentials"
            )
        return _build_static_session(static_token)

    sa_path = os.environ.get(SERVICE_ACCOUNT_ENV)
    if sa_path:
        return _wrap(_load_service_account(sa_path))
    return _wrap(_load_adc())


def _cached_session() -> Optional[Any]:
    return _session


def get_session(*, force_refresh: bool = False) -> Any:
    """Return the process-wide write session, building it on first use."""
    global _session
    with _lock:
        if _session is None or force_refresh:
            _session = build_session()
        return _session


def reset_session() -> None:
    """Drop the cached session. Intended for tests."""
    global _session
    with _lock:
        _session = None


def build_client(*, allowlist: Any, timeout: float) -> Any:
    """Construct a :class:`GscWriteClient` backed by the write credential."""
    return client.GscWriteClient(
        allowlist=allowlist, session=get_session(), timeout=timeout
    )


__all__ = [
    "SCOPES",
    "SERVICE_ACCOUNT_ENV",
    "STATIC_TOKEN_ENV",
    "CredentialError",
    "build_client",
    "build_session",
    "get_session",
    "reset_session",
]