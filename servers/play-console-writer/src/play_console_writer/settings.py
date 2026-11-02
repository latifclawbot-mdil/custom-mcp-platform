"""Runtime configuration for the Play Console *writer* MCP server.

Configuration is read once from the environment. Two things are mandatory and
fail closed:

* the package allowlist — without it the server refuses to start, so a write
  server can never run against "whatever the credential can see";
* nothing else. Credentials are resolved lazily, so ``initialize`` and
  ``list_tools`` work with no credential present (useful for discovery).

Secrets are never included in ``repr`` output or tool results.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Mapping

from .validation import ValidationError, validate_package_name

__all__ = ["Settings", "SettingsError", "load_settings"]

ALLOWLIST_ENV = "PLAY_WRITER_ALLOWED_PACKAGES"
CREDENTIALS_ENV = "PLAY_WRITER_CREDENTIALS_FILE"
SUBJECT_ENV = "PLAY_WRITER_SUBJECT"
TIMEOUT_ENV = "PLAY_WRITER_REQUEST_TIMEOUT"
MAX_PAGE_SIZE_ENV = "PLAY_WRITER_MAX_PAGE_SIZE"
MAX_REPLY_LENGTH_ENV = "PLAY_WRITER_MAX_REPLY_LENGTH"
ENABLE_ARTIFACT_UPLOAD_ENV = "PLAY_WRITER_ENABLE_ARTIFACT_UPLOAD"
ENABLE_RELEASE_GATE_ENV = "PLAY_WRITER_ENABLE_RELEASE_GATE"
#: Loopback-only base override, so an end-to-end test can point a subprocess at
#: an in-process fake upstream. Any non-loopback value is a startup error; there
#: is no way to aim production at a host other than Google.
TEST_LOOPBACK_BASE_ENV = "PLAY_WRITER_TEST_LOOPBACK_BASE"
#: Static bearer token for an end-to-end test against the loopback fake. Only
#: honoured together with ``PLAY_WRITER_TEST_LOOPBACK_BASE``; setting it without
#: a loopback base is a startup error, so it can never be used against Google.
TEST_STATIC_TOKEN_ENV = "PLAY_WRITER_TEST_STATIC_TOKEN"

DEFAULT_TIMEOUT = 20.0
DEFAULT_MAX_PAGE_SIZE = 100
DEFAULT_MAX_REPLY_LENGTH = 350


class SettingsError(RuntimeError):
    """Invalid or unsafe server configuration."""


@dataclass(frozen=True, repr=False)
class Settings:
    """Resolved, immutable server configuration.

    ``packages`` is the hard allowlist. Empty is treated as "deny everything",
    but :func:`load_settings` refuses to construct such a server at all.

    ``enable_artifact_upload`` gates the binary upload operations (bundle/apk
    staging and deobfuscation files). It defaults to **off**: those operations
    are safe on their own (nothing publishes without ``edits.commit``, which is
    not exposed) but they consume significant bandwidth and storage, so they are
    an explicit opt-in rather than a default. When off, calling them returns a
    clear "disabled by configuration" refusal naming this environment variable.
    """

    packages: frozenset[str] = field(default_factory=frozenset)
    credentials_path: str | None = None
    subject: str | None = None
    request_timeout: float = DEFAULT_TIMEOUT
    max_page_size: int = DEFAULT_MAX_PAGE_SIZE
    max_reply_length: int = DEFAULT_MAX_REPLY_LENGTH
    enable_artifact_upload: bool = False
    enable_release_gate: bool = False
    test_loopback_base: str | None = None
    test_static_token: str | None = None
    user_project: str | None = None

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return (
            f"Settings(packages={len(self.packages)} allowlisted, "
            f"credentials=<redacted>, subject=<redacted>, "
            f"request_timeout={self.request_timeout}, "
            f"max_page_size={self.max_page_size}, "
            f"max_reply_length={self.max_reply_length}, "
            f"enable_artifact_upload={self.enable_artifact_upload}, "
            f"enable_release_gate={self.enable_release_gate}, "
            f"test_loopback_base={'set' if self.test_loopback_base else 'unset'})"
        )

    __str__ = __repr__

    def require_allowed_package(self, package_name: str) -> str:
        """Validate a package name and confirm it is allowlisted."""
        try:
            validated = validate_package_name(package_name)
        except ValidationError as exc:
            raise SettingsError(f"invalid package_name: {exc}") from exc
        if validated not in self.packages:
            raise SettingsError(
                f"package {validated!r} is not in the allowlist; refusing to "
                "touch it"
            )
        return validated

    def clamp_page_size(self, requested: int | None) -> int:
        """Apply a hard upper bound to a caller-requested page size."""
        if requested is None:
            return self.max_page_size
        if isinstance(requested, bool) or not isinstance(requested, int):
            raise SettingsError("max_results must be an integer")
        if requested < 1:
            raise SettingsError("max_results must be at least 1")
        return min(requested, self.max_page_size)

    def require_artifact_upload_enabled(self, operation: str) -> None:
        """Fail closed on a binary-upload operation when the gate is off."""
        if not self.enable_artifact_upload:
            raise SettingsError(
                f"{operation} is disabled by configuration; set "
                f"{ENABLE_ARTIFACT_UPLOAD_ENV}=true to enable binary uploads"
            )

    def require_release_gate_enabled(self, operation: str) -> None:
        """Fail closed on a release-gated operation when the gate is off.

        ``edits.commit`` and ``edits.tracks.update`` are the two methods that can
        change what users receive. They are off unless an operator deliberately
        turns them on, so the shipped default surface can never publish.
        """
        if not self.enable_release_gate:
            raise SettingsError(
                f"{operation} is disabled by configuration; set "
                f"{ENABLE_RELEASE_GATE_ENV}=true to enable the release gate"
            )


def _parse_timeout(raw: str | None) -> float:
    if raw is None or raw.strip() == "":
        return DEFAULT_TIMEOUT
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise SettingsError(f"{TIMEOUT_ENV} must be a number of seconds") from exc
    if not (0.1 <= value <= 300.0):
        raise SettingsError(f"{TIMEOUT_ENV} must be between 0.1 and 300 seconds")
    return value


def _parse_max_page_size(raw: str | None) -> int:
    if raw is None or raw.strip() == "":
        return DEFAULT_MAX_PAGE_SIZE
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise SettingsError(f"{MAX_PAGE_SIZE_ENV} must be an integer") from exc
    if not (1 <= value <= 1000):
        raise SettingsError(f"{MAX_PAGE_SIZE_ENV} must be between 1 and 1000")
    return value


def _parse_max_reply_length(raw: str | None) -> int:
    if raw is None or raw.strip() == "":
        return DEFAULT_MAX_REPLY_LENGTH
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise SettingsError(f"{MAX_REPLY_LENGTH_ENV} must be an integer") from exc
    if not (1 <= value <= 2000):
        raise SettingsError(f"{MAX_REPLY_LENGTH_ENV} must be between 1 and 2000")
    return value


def _parse_bool(raw: str | None) -> bool:
    if raw is None:
        return False
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _parse_loopback_base(raw: str | None) -> str | None:
    """Accept a base URL only if it is loopback; refuse anything else loudly."""
    if raw is None or raw.strip() == "":
        return None
    from urllib.parse import urlparse

    value = raw.strip()
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or (parsed.hostname or "").lower() not in {
        "127.0.0.1", "::1", "localhost"
    }:
        raise SettingsError(
            f"{TEST_LOOPBACK_BASE_ENV} must point at a loopback address; the "
            "Android Publisher API host is fixed and cannot be redirected"
        )
    return value.rstrip("/")


def _parse_static_token(raw: str | None, loopback_base: str | None) -> str | None:
    """A static test token is only legal alongside a loopback base."""
    if raw is None or raw.strip() == "":
        return None
    if not loopback_base:
        raise SettingsError(
            f"{TEST_STATIC_TOKEN_ENV} is only valid together with "
            f"{TEST_LOOPBACK_BASE_ENV}; refusing to start with a static token "
            "for the real API host"
        )
    return raw.strip()


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    """Build :class:`Settings` from ``env`` (defaults to ``os.environ``).

    Fails closed: a missing, empty, or malformed allowlist is an error, never a
    permissive default.
    """
    env = os.environ if env is None else env

    raw_allowlist = env.get(ALLOWLIST_ENV)
    if raw_allowlist is None:
        raise SettingsError(
            f"{ALLOWLIST_ENV} is required: list the Android package names this "
            "server may modify, comma-separated. Refusing to start with an "
            "implicit allowlist."
        )

    packages: set[str] = set()
    for chunk in raw_allowlist.split(","):
        candidate = chunk.strip()
        if not candidate:
            continue
        try:
            packages.add(validate_package_name(candidate))
        except ValidationError as exc:
            raise SettingsError(
                f"invalid package in {ALLOWLIST_ENV}: {candidate!r} ({exc})"
            ) from exc

    if not packages:
        raise SettingsError(
            f"{ALLOWLIST_ENV} must list at least one package; the allowlist "
            "may not be empty."
        )

    subject = env.get(SUBJECT_ENV) or None
    credentials_path = env.get(CREDENTIALS_ENV) or None
    if subject and not credentials_path:
        raise SettingsError(
            f"{SUBJECT_ENV} requires {CREDENTIALS_ENV}: domain-wide delegation "
            "only applies to a delegated service account."
        )

    loopback_base = _parse_loopback_base(env.get(TEST_LOOPBACK_BASE_ENV))
    return Settings(
        packages=frozenset(packages),
        credentials_path=credentials_path,
        subject=subject,
        request_timeout=_parse_timeout(env.get(TIMEOUT_ENV)),
        max_page_size=_parse_max_page_size(env.get(MAX_PAGE_SIZE_ENV)),
        max_reply_length=_parse_max_reply_length(env.get(MAX_REPLY_LENGTH_ENV)),
        enable_artifact_upload=_parse_bool(env.get(ENABLE_ARTIFACT_UPLOAD_ENV)),
        enable_release_gate=_parse_bool(env.get(ENABLE_RELEASE_GATE_ENV)),
        test_loopback_base=loopback_base,
        test_static_token=_parse_static_token(env.get(TEST_STATIC_TOKEN_ENV), loopback_base),
        user_project=env.get("PLAY_WRITER_USER_PROJECT") or None,
    )