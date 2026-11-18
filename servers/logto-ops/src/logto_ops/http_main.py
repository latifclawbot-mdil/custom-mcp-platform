"""Authenticated Streamable HTTP entry point for the Logto ops MCP server.

Startup order, each step fail-closed:

1. Gate configuration is resolved; an ``admin`` mode without its explicit flag,
   or a request to enable deletes, exits 2 before a socket is opened.
2. The operation set is checked against the gate: a read-only process refuses
   to build a registry that contains writes.
3. Settings (base URL, M2M credential file, confirmation secret) are resolved.
   A missing or group-readable secret file exits 2.
4. The bearer token for *this* HTTP endpoint is resolved before binding.

Logto credentials are therefore validated at startup, but the Management API
token is fetched lazily on the first tool call — ``initialize`` and
``tools/list`` work without contacting Logto at all.

Run with ``python -m logto_ops.http_main``.
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

from .confirm import (
    ConfirmationError,
    ConfirmationVerifier,
    ENV_CONFIRM_SECRET,
    ENV_CONFIRM_SECRET_FILE,
    load_confirm_secret,
)
from .gate import GateConfigError, assert_servable, load_gate
from .operations import REGISTRY
from .server import SERVER_NAME, build_server
from .settings import SettingsError, load_settings

LOG_LEVEL_ENV = "LOGTO_MCP_LOG_LEVEL"


def _allowed_hosts(bind_host: str) -> tuple[str, ...]:
    raw = os.environ.get("MCP_HTTP_ALLOWED_HOSTS", "")
    hosts = tuple(part.strip() for part in raw.split(",") if part.strip())
    return hosts or (bind_host,)


def app_config(bind_host: str = "0.0.0.0") -> AppConfig:
    return AppConfig(
        # The bearer never enters a local that could be logged.
        expected_token=resolve_token(),
        path=os.environ.get("MCP_HTTP_PATH") or DEFAULT_PATH,
        allowed_hosts=_allowed_hosts(bind_host),
        max_sessions=int(os.environ.get("MCP_HTTP_MAX_SESSIONS") or 50),
        session_idle_timeout=float(os.environ.get("MCP_HTTP_SESSION_IDLE_TIMEOUT") or 300.0),
    )


def _ttl() -> int:
    raw = (os.environ.get("LOGTO_MCP_CONFIRM_TTL_SECONDS") or "").strip()
    if not raw:
        return 900
    try:
        value = int(raw)
    except ValueError:
        raise SystemExit(f"{SERVER_NAME}: LOGTO_MCP_CONFIRM_TTL_SECONDS must be an integer")
    if value < 30 or value > 86400:
        raise SystemExit(f"{SERVER_NAME}: LOGTO_MCP_CONFIRM_TTL_SECONDS must be 30..86400")
    return value


def main() -> None:
    logging.basicConfig(
        level=getattr(logging, os.environ.get(LOG_LEVEL_ENV, "INFO").upper(), logging.INFO),
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    logger = logging.getLogger("logto_ops.http_main")

    try:
        gate = load_gate()
        assert_servable([operation.name for operation in REGISTRY], gate)
        settings = load_settings()
        verifier = ConfirmationVerifier(secret=load_confirm_secret(), ttl_seconds=_ttl())
    except (GateConfigError, SettingsError, ConfirmationError) as exc:
        # One line, secret-free, exit 2: orchestrators key restart policy off it.
        print(f"{SERVER_NAME}: refusing to start: {exc}", file=sys.stderr)
        raise SystemExit(2) from None

    serve_config = ServeConfig.from_env()
    try:
        config = app_config(serve_config.host)
    except (AppConfigError, AuthConfigError) as exc:
        print(f"{SERVER_NAME}: refusing to start: {exc}", file=sys.stderr)
        raise SystemExit(2) from None

    server = build_server(settings=settings, gate=gate, verifier=verifier)
    logger.info(
        "serving %s over authenticated streamable HTTP on %s:%s%s (mode=%s, "
        "confirmation=%s, base_url=%s, secret=%s)",
        SERVER_NAME,
        serve_config.host,
        serve_config.port,
        config.path,
        gate.mode,
        "required" if gate.writes_need_confirmation else "not required",
        settings.base_url,
        "set" if os.environ.get(ENV_CONFIRM_SECRET_FILE) or os.environ.get(ENV_CONFIRM_SECRET) else "missing",
    )
    serve(server, config, serve_config)


if __name__ == "__main__":  # pragma: no cover - exercised as a subprocess
    main()
