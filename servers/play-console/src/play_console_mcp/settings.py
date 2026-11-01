"""Runtime configuration for the Play Console MCP server.

Configuration is read once from the environment. The package allowlist is
mandatory and fails closed: without it, no Play API call is permitted at all.
Secrets (service-account file paths, delegated subjects) are never included in
``repr`` output or in tool results.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Mapping

from .validation import ValidationError, validate_package_name

__all__ = ["Settings", "SettingsError", "load_settings"]

ALLOWLIST_ENV = "PLAY_CONSOLE_ALLOWED_PACKAGES"
CREDENTIALS_ENV = "PLAY_CONSOLE_CREDENTIALS_FILE"
SUBJECT_ENV = "PLAY_CONSOLE_SUBJECT"
TIMEOUT_ENV = "PLAY_CONSOLE_REQUEST_TIMEOUT"
MAX_PAGE_SIZE_ENV = "PLAY_CONSOLE_MAX_PAGE_SIZE"

DEFAULT_TIMEOUT = 20.0
DEFAULT_MAX_PAGE_SIZE = 100


class SettingsError(RuntimeError):
    """Invalid or unsafe server configuration."""


@dataclass(frozen=True, repr=False)
class Settings:
    """Resolved, immutable server configuration.

    ``packages`` is the hard allowlist. Empty is treated as "deny everything",
    but :func:`load_settings` refuses to construct such a server at all.
    """

    packages: frozenset[str] = field(default_factory=frozenset)
    credentials_path: str | None = None
    subject: str | None = None
    request_timeout: float = DEFAULT_TIMEOUT
    max_page_size: int = DEFAULT_MAX_PAGE_SIZE
    user_project: str | None = None

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return (
            f"Settings(packages={len(self.packages)} allowlisted, "
            f"credentials=<redacted>, subject=<redacted>, "
            f"request_timeout={self.request_timeout}, "
            f"max_page_size={self.max_page_size})"
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
                f"package {validated!r} is not in the allowlist; refusing to query it"
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


def _parse_timeout(raw: str | None) -> float:
    if raw is None or raw.strip() == "":
        return DEFAULT_TIMEOUT
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise SettingsError(
            f"{TIMEOUT_ENV} must be a number of seconds"
        ) from exc
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
            "server may read, comma-separated. Refusing to start with an "
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

    return Settings(
        packages=frozenset(packages),
        credentials_path=credentials_path,
        subject=subject,
        request_timeout=_parse_timeout(env.get(TIMEOUT_ENV)),
        max_page_size=_parse_max_page_size(env.get(MAX_PAGE_SIZE_ENV)),
        user_project=env.get("PLAY_CONSOLE_USER_PROJECT") or None,
    )