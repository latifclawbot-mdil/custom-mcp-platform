"""Authenticated Streamable HTTP entry point for the Play Console *writer*.

Thin adapter over :func:`play_console_writer.server.build_server`: the same
tools, the same schemas, the same annotations and the same package allowlist.
Only the transport and the bearer requirement are new. Before this module existed
the Play writer was **stdio-only**, so it had no HTTP entry point at all; nothing
about the server object changes here.

The HTTP transport authenticates the *caller of this endpoint* with a shared
bearer token. That is endpoint authentication, not per-operation approval: a
caller holding the token may call any tool on any allowlisted package, subject to
the server's own gates. Per-operation policy (rate limits, human approval, which
principal may delete) belongs to the LiteLLM gateway in front of this server and
is deliberately not claimed here.

Startup order:

1. ``PLAY_WRITER_ALLOWED_PACKAGES`` is validated first via ``load_settings``
   (exit 2 on a missing/empty allowlist) — the server's own fail-closed contract.
2. The bearer token is resolved next; a missing token exits before binding.
3. Google credentials are still acquired lazily on the first API call, so
   ``initialize`` / ``tools/list`` need no Google credential.

Run with ``python -m play_console_writer.http_main``.
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

from .server import SERVER_NAME, build_server
from .settings import SettingsError, load_settings

LOG_LEVEL_ENV = "PLAY_WRITER_HTTP_LOG_LEVEL"

#: Bounds for the numeric HTTP settings, so a typo cannot become an unbounded
#: allocation (``max_sessions``) or a session that never expires.
MAX_SESSIONS_LIMIT = 10_000
MAX_IDLE_TIMEOUT_SECONDS = 86_400.0


def _allowed_hosts(bind_host: str) -> tuple[str, ...]:
    raw = os.environ.get("MCP_HTTP_ALLOWED_HOSTS", "")
    hosts = tuple(part.strip() for part in raw.split(",") if part.strip())
    return hosts or (bind_host,)


def _env_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    """Parse a positive integer setting, failing as a clean config error.

    ``int("many")`` raises ``ValueError``, which at startup would be an uncaught
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
        raise AppConfigError(f"{name} must be between {minimum} and {maximum}") from None
    return value


def _env_float(name: str, default: float, *, minimum: float, maximum: float) -> float:
    """Parse a positive finite float setting, failing as a clean config error."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw.strip())
    except (TypeError, ValueError):
        raise AppConfigError(f"{name} must be a number") from None
    # ``float('nan')``/``float('inf')`` parse but are not usable timeouts.
    if not math.isfinite(value):
        raise AppConfigError(f"{name} must be a finite number") from None
    if value < minimum or value > maximum:
        raise AppConfigError(f"{name} must be between {minimum} and {maximum}") from None
    return value


def app_config(bind_host: str = "0.0.0.0") -> AppConfig:
    """Build the transport config, resolving the token and the Host allowlist."""
    return AppConfig(
        # The expected token goes straight from the resolver into the frozen
        # AppConfig; it never enters a local that could be logged.
        expected_token=resolve_token(),
        path=os.environ.get("MCP_HTTP_PATH") or DEFAULT_PATH,
        allowed_hosts=_allowed_hosts(bind_host),
        max_sessions=_env_int(
            "MCP_HTTP_MAX_SESSIONS", 100, minimum=1, maximum=MAX_SESSIONS_LIMIT
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
    logger = logging.getLogger("play_console_writer.http_main")

    try:
        settings = load_settings()
    except SettingsError as exc:
        print(f"{SERVER_NAME}: configuration error: {exc}", file=sys.stderr)
        raise SystemExit(2) from None

    serve_config = ServeConfig.from_env()
    try:
        config = app_config(serve_config.host)
    except (AppConfigError, AuthConfigError) as exc:
        # Both are startup configuration errors: exit 2 with a one-line,
        # secret-free explanation rather than a traceback. Container
        # orchestrators key restart policy off this code.
        print(f"{SERVER_NAME}: refusing to start: {exc}", file=sys.stderr)
        raise SystemExit(2) from None

    server = build_server(settings)
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