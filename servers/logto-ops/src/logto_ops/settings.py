"""Runtime configuration for the Logto Management API MCP server.

Everything here is resolved once, at startup, and is immutable afterwards. The
credential is read from a file (never an environment value) so it cannot be
recovered from ``docker inspect`` or from a dumped process environment, and the
file's mode is checked: a group- or world-readable secret file is a startup
error, not a warning.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Mapping

__all__ = [
    "DEFAULT_API_INDICATOR",
    "ENV_APP_ID",
    "ENV_BASE_URL",
    "ENV_CF_ID",
    "ENV_CF_SECRET_FILE",
    "ENV_INDICATOR",
    "ENV_SECRET_FILE",
    "Settings",
    "SettingsError",
    "load_settings",
]

ENV_BASE_URL = "LOGTO_BASE_URL"
ENV_APP_ID = "LOGTO_M2M_APP_ID"
ENV_SECRET_FILE = "LOGTO_M2M_SECRET_FILE"
ENV_INDICATOR = "LOGTO_API_INDICATOR"
ENV_CF_ID = "LOGTO_CF_ACCESS_CLIENT_ID"
ENV_CF_SECRET_FILE = "LOGTO_CF_ACCESS_SECRET_FILE"
ENV_TIMEOUT = "LOGTO_REQUEST_TIMEOUT"
ENV_MAX_PAGE_SIZE = "LOGTO_MAX_PAGE_SIZE"
ENV_HTTP_TIMEOUT = "LOGTO_HTTP_TIMEOUT"

#: The identifier a self-hosted Logto registers for its own Management API.
#: Deliberately a constant, not a guess at the deployment's hostname: a wrong
#: audience fails identically to a wrong secret, so it must be one known value
#: that an operator overrides explicitly.
DEFAULT_API_INDICATOR = "https://default.logto.app/api"

DEFAULT_TIMEOUT = 20.0
DEFAULT_MAX_PAGE_SIZE = 100
#: Hard cap on a response body. Logs and application lists are the largest
#: things this server reads; anything beyond this is not a page we asked for.
MAX_RESPONSE_BYTES = 4 * 1024 * 1024


class SettingsError(RuntimeError):
    """Invalid or unsafe server configuration. Never contains secret material."""


def _read_secret_file(path: str, *, label: str) -> str:
    try:
        st = os.stat(path)
    except OSError as exc:
        raise SettingsError(
            f"{label} points at {path!r}, which cannot be read ({exc.strerror})"
        ) from exc
    if not os.path.isfile(path):
        raise SettingsError(f"{label} points at {path!r}, which is not a regular file")
    if st.st_mode & 0o077:
        raise SettingsError(
            f"{label} file {path!r} is accessible by group or others "
            f"(mode {oct(st.st_mode & 0o777)}); expected 0600 or stricter"
        )
    try:
        with open(path, encoding="utf-8") as handle:
            value = handle.read().strip()
    except OSError as exc:
        raise SettingsError(
            f"{label} file {path!r} cannot be read ({exc.strerror})"
        ) from exc
    if not value:
        raise SettingsError(f"{label} file {path!r} is empty")
    if "\n" in value or "\r" in value:
        raise SettingsError(f"{label} file {path!r} contains more than one line")
    return value


@dataclass(frozen=True, repr=False)
class Settings:
    """Resolved, immutable configuration for one server process."""

    base_url: str
    app_id: str
    app_secret: str
    api_indicator: str = DEFAULT_API_INDICATOR
    cf_access_client_id: str | None = None
    cf_access_client_secret: str | None = None
    request_timeout: float = DEFAULT_TIMEOUT
    max_page_size: int = DEFAULT_MAX_PAGE_SIZE
    http_timeout: float = DEFAULT_TIMEOUT
    max_response_bytes: int = MAX_RESPONSE_BYTES
    extra_headers: Mapping[str, str] = field(default_factory=dict)

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return (
            f"Settings(base_url={self.base_url!r}, app_id={self.app_id!r}, "
            f"secret=<redacted>, indicator={self.api_indicator!r}, "
            f"cf_access={'yes' if self.cf_access_client_id else 'no'}, "
            f"request_timeout={self.request_timeout}, max_page_size={self.max_page_size})"
        )

    __str__ = __repr__

    @property
    def api_root(self) -> str:
        return f"{self.base_url.rstrip('/')}/api"

    @property
    def token_url(self) -> str:
        return f"{self.base_url.rstrip('/')}/oidc/token"

    def clamp_page_size(self, requested: int | None) -> int:
        """Bound a caller-supplied page size; the caller never widens the cap."""
        if requested is None:
            return self.max_page_size
        if not isinstance(requested, int) or isinstance(requested, bool):
            raise SettingsError("page_size must be an integer")
        if requested < 1:
            raise SettingsError("page_size must be >= 1")
        return min(requested, self.max_page_size)


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    """Build :class:`Settings` from the environment, failing closed."""
    source = os.environ if env is None else env

    base_url = (source.get(ENV_BASE_URL) or "").strip().rstrip("/")
    if not base_url:
        raise SettingsError(f"{ENV_BASE_URL} is required (for example https://auth.example.org)")
    if not base_url.startswith("https://"):
        # The M2M secret is presented as Basic auth to the token endpoint. There
        # is no supported plaintext deployment, so refuse rather than warn.
        raise SettingsError(f"{ENV_BASE_URL} must be an https:// URL; refused {base_url!r}")

    app_id = (source.get(ENV_APP_ID) or "").strip()
    if not app_id:
        raise SettingsError(f"{ENV_APP_ID} is required")

    secret_path = (source.get(ENV_SECRET_FILE) or "").strip()
    if not secret_path:
        raise SettingsError(
            f"{ENV_SECRET_FILE} is required: the M2M client secret must be provided as a "
            "file, not as an environment value"
        )
    app_secret = _read_secret_file(secret_path, label=ENV_SECRET_FILE)

    indicator = (source.get(ENV_INDICATOR) or "").strip() or DEFAULT_API_INDICATOR

    cf_id = (source.get(ENV_CF_ID) or "").strip() or None
    cf_secret_path = (source.get(ENV_CF_SECRET_FILE) or "").strip()
    cf_secret = None
    if cf_secret_path:
        cf_secret = _read_secret_file(cf_secret_path, label=ENV_CF_SECRET_FILE)
        if not cf_id:
            raise SettingsError(
                f"{ENV_CF_SECRET_FILE} is set but {ENV_CF_ID} is not; both are required "
                "to pass an upstream Cloudflare Access service token"
            )
    elif cf_id:
        raise SettingsError(f"{ENV_CF_ID} is set but {ENV_CF_SECRET_FILE} is not")

    def _float(name: str, default: float) -> float:
        raw = (source.get(name) or "").strip()
        if not raw:
            return default
        try:
            value = float(raw)
        except ValueError as exc:
            raise SettingsError(f"{name} must be a number") from exc
        if value <= 0:
            raise SettingsError(f"{name} must be positive")
        return value

    def _int(name: str, default: int) -> int:
        raw = (source.get(name) or "").strip()
        if not raw:
            return default
        try:
            value = int(raw)
        except ValueError as exc:
            raise SettingsError(f"{name} must be an integer") from exc
        if value < 1:
            raise SettingsError(f"{name} must be >= 1")
        return value

    return Settings(
        base_url=base_url,
        app_id=app_id,
        app_secret=app_secret,
        api_indicator=indicator,
        cf_access_client_id=cf_id,
        cf_access_client_secret=cf_secret,
        request_timeout=_float(ENV_TIMEOUT, DEFAULT_TIMEOUT),
        max_page_size=_int(ENV_MAX_PAGE_SIZE, DEFAULT_MAX_PAGE_SIZE),
        http_timeout=_float(ENV_HTTP_TIMEOUT, DEFAULT_TIMEOUT),
    )
