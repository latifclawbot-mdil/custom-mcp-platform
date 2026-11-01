"""MCP Streamable HTTP transport: build and serve an authenticated ASGI app.

This module is a *library*, not a runnable server: it has no MCP server of its
own to expose. Running it prints that fact and exits non-zero rather than
starting something that serves nothing. A server's own ``http_main`` calls
:func:`mcp_http_transport.serve`.
"""

from __future__ import annotations

import sys

from .server import AppConfig, parse_args, resolve_token, serve


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    print(
        "mcp-http-transport: nothing to serve. This is a library; a server entry\n"
        "point must call mcp_http_transport.serve(build_server(), config).\n"
        f"Parsed options (host={args.host}, port={args.port}, path={args.path}) "
        "are shown for a misconfiguration check only.",
        file=sys.stderr,
    )
    return 2


def _resolve_for_diagnostics() -> str:  # pragma: no cover - operator aid
    """Resolve the token so an operator can confirm the variable is visible.

    Never prints the token; only whether resolution succeeded.
    """
    try:
        resolve_token()
    except Exception as exc:  # noqa: BLE001 - diagnostics only
        return f"token resolution failed: {type(exc).__name__}"
    return "token resolution succeeded"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = ["AppConfig", "main", "serve"]