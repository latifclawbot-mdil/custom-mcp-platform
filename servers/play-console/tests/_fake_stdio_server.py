"""Test-only stdio launcher that injects the fake transport and credentials.

The production ``play_console_mcp.server`` has no fake-transport branch: an
installed server always talks to real Google. Tests that need the full MCP
protocol path over stdio spawn *this* module instead, which builds the same
server but passes the fake httpx transport and fake credentials explicitly via
``build_server``'s injection points.

Run as ``python tests/_fake_stdio_server.py`` with ``PYTHONPATH`` covering both
``src`` and the repository root (so ``fake_transport`` resolves).
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from play_console_mcp.server import build_server  # noqa: E402
from play_console_mcp.settings import load_settings  # noqa: E402
from fake_transport import FakeCredentials, FakeTransport  # noqa: E402


def main() -> None:
    settings = load_settings()
    server = build_server(
        settings,
        transport=FakeTransport(),
        credentials=FakeCredentials(),
        credential_config=None,
    )
    server.run("stdio")


if __name__ == "__main__":
    main()