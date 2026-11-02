"""Console entry point: ``search-console-writer`` / ``python -m search_console_writer``.

Startup is where the fail-closed default is enforced. If no write allowlist is
configured the process exits non-zero with a one-line explanation rather than
starting a server that could mutate every property the credential can see.

Logging goes to stderr *only*. stdout is the MCP stdio channel.
"""

from __future__ import annotations

import logging
import os
import sys

from .allowlist import PropertyAllowlist
from .errors import EmptyAllowlistError, NotConfiguredError

LOG_LEVEL_ENV = "GSC_WRITE_LOG_LEVEL"


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
    logger = logging.getLogger("search_console_writer")

    try:
        allowlist = PropertyAllowlist.from_env()
    except (EmptyAllowlistError, NotConfiguredError) as exc:
        print(f"search-console-writer: refusing to start: {exc.message}", file=sys.stderr)
        raise SystemExit(2) from None

    logger.info("starting with %d writable propert(y/ies)", len(allowlist))

    from .server import create_server

    server = create_server(allowlist=allowlist)
    server.run(transport="stdio")


if __name__ == "__main__":  # pragma: no cover
    main()