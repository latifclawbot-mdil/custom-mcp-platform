"""Authenticated Streamable HTTP entry point for the Search Console write server.

Thin adapter over :func:`search_console_writer.server.create_server`: the same
four write tools, the same truthful annotations, the same fail-closed write
allowlist, the same ``reject_unknown_arguments`` middleware. Only the transport
and the bearer requirement are new.

The HTTP transport authenticates the *caller of this endpoint* with a shared
bearer token. That is endpoint authentication, not per-operation approval: a
caller holding the token is authorised to call any of the four tools on any
allowlisted property. Per-operation policy (rate limits, human approval, which
principal may delete) belongs to the LiteLLM gateway in front of this server and
is deliberately not claimed here.

Startup order:

1. ``GSC_WRITE_ALLOWED_SITES`` is validated (exit 2 on empty).
2. The bearer token is resolved; a missing token exits before binding.
3. The Google credential is still acquired lazily on the first validated call,
   so ``initialize`` / ``tools/list`` need no Google credential.

Run with ``python -m search_console_writer.http_main``.
"""

from __future__ import annotations

import logging
import math
import os
import sys

from mcp_http_transport.auth import AuthConfigError
from mcp_http_transport.server import (
    DEFAULT_PATH,
    AppConfig,
    AppConfigError,
    ServeConfig,
    resolve_token,
    serve,
)

from .allowlist import PropertyAllowlist
from .errors import EmptyAllowlistError, NotConfiguredError
from .server import SERVER_NAME, create_server

LOG_LEVEL_ENV = "GSC_WRITE_LOG_LEVEL"


def _allowed_hosts(bind_host: str) -> tuple[str, ...]:
    raw = os.environ.get("MCP_HTTP_ALLOWED_HOSTS", "")
    hosts = tuple(part.strip() for part in raw.split(",") if part.strip())
    return hosts or (bind_host,)


#: Bounds for the numeric HTTP settings. They exist so a typo cannot become an
#: unbounded allocation (``max_sessions``) or a session that never expires.
MAX_SESSIONS_LIMIT = 10_000
MAX_IDLE_TIMEOUT_SECONDS = 86_400.0


def _env_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    """Parse a positive integer setting, failing as a clean config error.

    ``int("many")`` raises ``ValueError``, which at startup is an uncaught
    traceback rather than the documented "refusing to start" message. Every
    numeric setting goes through here so a malformed value is an
    :class:`AppConfigError` and the process exits 2 with one line.
    """
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw.strip())
    except (TypeError, ValueError):
        raise AppConfigError(f"{name} must be an integer") from None
    if value < minimum or value > maximum:
        raise AppConfigError(
            f"{name} must be between {minimum} and {maximum}"
        ) from None
    return value


def _env_float(
    name: str, default: float, *, minimum: float, maximum: float
) -> float:
    """Parse a positive finite float setting, failing as a clean config error."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw.strip())
    except (TypeError, ValueError):
        raise AppConfigError(f"{name} must be a number") from None
    # ``float('nan')`` and ``float('inf')`` parse successfully but are not
    # usable timeouts; reject them explicitly.
    if not math.isfinite(value):
        raise AppConfigError(f"{name} must be a finite number") from None
    if value < minimum or value > maximum:
        raise AppConfigError(f"{name} must be between {minimum} and {maximum}") from None
    return value


def app_config(bind_host: str = "0.0.0.0") -> AppConfig:
    return AppConfig(
        # The expected bearer token goes straight from the resolver into the
        # frozen AppConfig; it never enters a local that could be logged.
        expected_token=resolve_token(),
        path=os.environ.get("MCP_HTTP_PATH") or DEFAULT_PATH,
        allowed_hosts=_allowed_hosts(bind_host),
        max_sessions=_env_int(
            "MCP_HTTP_MAX_SESSIONS",
            100,
            minimum=1,
            maximum=MAX_SESSIONS_LIMIT,
        ),
        session_idle_timeout=_env_float(
            "MCP_HTTP_SESSION_IDLE_TIMEOUT",
            300.0,
            minimum=0.001,
            maximum=MAX_IDLE_TIMEOUT_SECONDS,
        ),
    )


def main() -> None:
    logging.basicConfig(
        level=getattr(logging, os.environ.get(LOG_LEVEL_ENV, "INFO").upper(), logging.INFO),
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    logger = logging.getLogger("search_console_writer.http_main")

    try:
        allowlist = PropertyAllowlist.from_env()
    except (EmptyAllowlistError, NotConfiguredError) as exc:
        print(f"{SERVER_NAME}: refusing to start: {exc.message}", file=sys.stderr)
        raise SystemExit(2) from None

    serve_config = ServeConfig.from_env()
    try:
        config = app_config(serve_config.host)
    except (AppConfigError, AuthConfigError) as exc:
        print(f"{SERVER_NAME}: refusing to start: {exc}", file=sys.stderr)
        raise SystemExit(2) from None

    server = create_server(allowlist=allowlist)
    logger.info(
        "serving %s over authenticated streamable HTTP on %s:%s%s",
        SERVER_NAME,
        serve_config.host,
        serve_config.port,
        config.path,
    )
    serve(server, config, serve_config)


if __name__ == "__main__":  # pragma: no cover - exercised as a subprocess
    main()