"""Console entry point: ``search-console-mcp`` / ``python -m search_console_mcp``.

Startup is where the fail-closed default is enforced. If no allowlist is
configured the process exits non-zero with a one-line explanation rather than
starting a server that could reach every property the credential can see.

Logging goes to stderr *only*. stdout is the MCP stdio channel: a single stray
print on stdout corrupts the protocol stream, so nothing here writes to it.
"""

from __future__ import annotations

import logging
import os
import sys

from .allowlist import PropertyAllowlist
from .errors import EmptyAllowlistError, NotConfiguredError

LOG_LEVEL_ENV = "GSC_LOG_LEVEL"


def _configure_logging() -> None:
    level_name = os.environ.get(LOG_LEVEL_ENV, "WARNING").upper()
    level = getattr(logging, level_name, logging.WARNING)
    logging.basicConfig(
        level=level,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def main() -> None:
    _configure_logging()
    logger = logging.getLogger("search_console_mcp")

    try:
        allowlist = PropertyAllowlist.from_env()
    except (EmptyAllowlistError, NotConfiguredError) as exc:
        # Deliberately terse and stderr-only: this message is for an operator
        # reading a service log, and it must not be mistaken for protocol output.
        print(f"search-console-mcp: refusing to start: {exc.message}", file=sys.stderr)
        raise SystemExit(2) from None

    logger.info("starting with %d allowlisted propert(y/ies)", len(allowlist))

    # Imported after configuration so an import-time error is reported the same
    # way as a configuration error.
    from .server import create_server

    server = create_server(allowlist=allowlist)
    server.run(transport="stdio")


if __name__ == "__main__":  # pragma: no cover
    main()