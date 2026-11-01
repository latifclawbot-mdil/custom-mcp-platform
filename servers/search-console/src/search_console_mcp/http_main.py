"""Authenticated Streamable HTTP entry point for the Search Console MCP server.

Thin adapter over :func:`search_console_mcp.server.create_server`: the same
four read-only tools, the same fail-closed property allowlist, the same
``reject_unknown_arguments`` middleware. Only the transport and the bearer
requirement are new.

Startup order:

1. ``GSC_ALLOWED_SITES`` is validated (unchanged behaviour; exit 2 on empty).
2. The bearer token is resolved; a missing token exits before binding.
3. ADC is still acquired lazily on the first validated API call, so
   ``initialize`` / ``tools/list`` need no Google credential.

Run with ``python -m search_console_mcp.http_main``.
"""

from __future__ import annotations

import logging
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

LOG_LEVEL_ENV = "GSC_LOG_LEVEL"


def _allowed_hosts(bind_host: str) -> tuple[str, ...]:
    raw = os.environ.get("MCP_HTTP_ALLOWED_HOSTS", "")
    hosts = tuple(part.strip() for part in raw.split(",") if part.strip())
    return hosts or (bind_host,)


def app_config(bind_host: str = "0.0.0.0") -> AppConfig:
    return AppConfig(
        # The expected bearer token never enters a local variable that could be
        # logged: it goes straight from the resolver into the frozen AppConfig.
        expected_token=resolve_token(),
        path=os.environ.get("MCP_HTTP_PATH") or DEFAULT_PATH,
        allowed_hosts=_allowed_hosts(bind_host),
        max_sessions=int(os.environ.get("MCP_HTTP_MAX_SESSIONS") or 100),
        session_idle_timeout=float(os.environ.get("MCP_HTTP_SESSION_IDLE_TIMEOUT") or 300.0),
    )


def main() -> None:
    logging.basicConfig(
        level=getattr(logging, os.environ.get(LOG_LEVEL_ENV, "INFO").upper(), logging.INFO),
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    logger = logging.getLogger("search_console_mcp.http_main")

    try:
        allowlist = PropertyAllowlist.from_env()
    except (EmptyAllowlistError, NotConfiguredError) as exc:
        print(f"{SERVER_NAME}: refusing to start: {exc.message}", file=sys.stderr)
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