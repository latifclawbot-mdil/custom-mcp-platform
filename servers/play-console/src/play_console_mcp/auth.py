"""Credential resolution.

Two supported modes, both resolved lazily on first API use:

* **Application Default Credentials (ADC)** — ``google.auth.default()``, the
  standard path for a workload identity / metadata server / gcloud ADC file.
* **Service account file** — optionally with ``with_subject`` for domain-wide
  delegation (required for Play Console service accounts acting on behalf of a
  human account).

Nothing is loaded at import time and no credential material is ever logged or
included in ``repr``. The only scope requested is ``androidpublisher``, which is
the narrowest scope that permits Play Console reads.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

__all__ = ["SCOPE", "CredentialError", "CredentialConfig", "load_credentials"]

SCOPE = "https://www.googleapis.com/auth/androidpublisher"


class CredentialError(RuntimeError):
    """Credentials could not be resolved. Message never contains key material."""


def _adc_factory() -> Any:
    """Return ADC credentials, discarding the project id google.auth also yields."""
    import google.auth

    credentials, _project_id = google.auth.default(scopes=[SCOPE])
    return credentials


def _service_account_factory(path: str, subject: str | None) -> Any:
    """Load a service-account credential, optionally with domain-wide delegation.

    ``with_subject`` returns a credential that already carries the requested
    scopes, so scopes are applied once, at load time.
    """
    from google.oauth2 import service_account

    if not Path(path).is_file():
        raise CredentialError(f"credentials file not found: {Path(path).name}")

    creds = service_account.Credentials.from_service_account_file(path, scopes=[SCOPE])
    if subject:
        creds = creds.with_subject(subject)
    return creds


@dataclass(repr=False)
class CredentialConfig:
    """Lazy credential holder. Thread-safe single-flight resolution."""

    credentials_path: str | None = None
    subject: str | None = None
    lazy: bool = True
    _loader: Callable[[], Any] | None = field(default=None, repr=False)
    _credentials: Any = field(default=None, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return (
            "CredentialConfig(mode="
            + ("service_account" if self.credentials_path else "adc")
            + ", subject=<redacted>, resolved="
            + ("yes" if self._credentials is not None else "no")
            + ")"
        )

    __str__ = __repr__

    @property
    def deferred(self) -> bool:
        """True while credentials have not yet been materialised."""
        return self._credentials is None

    def resolve(self) -> Any:
        """Return credentials, loading them on first call."""
        if self._credentials is not None:
            return self._credentials
        with self._lock:
            if self._credentials is not None:
                return self._credentials
            loader = self._loader
            if loader is None:
                loader = self._default_loader()
            try:
                self._credentials = loader()
            except CredentialError:
                raise
            except Exception as exc:  # noqa: BLE001 - normalise for callers
                raise CredentialError(self._describe(exc)) from exc
        return self._credentials

    def _default_loader(self) -> Callable[[], Any]:
        if self.credentials_path:
            path, subject = self.credentials_path, self.subject
            return lambda: _service_account_factory(path, subject)
        return _adc_factory

    @staticmethod
    def _describe(exc: Exception) -> str:
        import google.auth.exceptions as gexc

        if isinstance(exc, gexc.DefaultCredentialsError):
            return (
                "Application Default Credentials are not configured. Set "
                "GOOGLE_APPLICATION_CREDENTIALS to a service-account file, run "
                "`gcloud auth application-default login`, or point "
                "PLAY_CONSOLE_CREDENTIALS_FILE at a service-account JSON file."
            )
        if isinstance(exc, gexc.RefreshError):
            return (
                "Google rejected the credentials (refresh failed). The service "
                "account may be disabled, the key revoked, or the delegated "
                "subject not granted domain-wide delegation."
            )
        return f"could not load Google credentials: {type(exc).__name__}"


def load_credentials(
    *,
    credentials_path: str | None = None,
    subject: str | None = None,
    lazy: bool = True,
    loader: Callable[[], Any] | None = None,
) -> CredentialConfig:
    """Describe how to obtain credentials without obtaining them yet.

    With ``lazy=True`` (the default) no network or filesystem credential access
    happens until :meth:`CredentialConfig.resolve` is called.
    """
    return CredentialConfig(
        credentials_path=credentials_path,
        subject=subject,
        lazy=lazy,
        _loader=loader,
    )