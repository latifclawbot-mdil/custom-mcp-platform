"""Opt-in request capture, for asserting request construction from *inside* the
server process.

The property-encoding and URL-scope requirements are properties of the request
the server builds, so the strongest possible test asserts on that request from
the process that built it — not on a request the test reconstructed separately.

This module writes only when ``GSC_TEST_CAPTURE_DIR`` is set. It records the
HTTP method, the final URL, and the JSON body. It never records headers (which
carry the bearer token) and never records the response.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Mapping

logger = logging.getLogger("search_console_mcp.capture")

CAPTURE_ENV = "GSC_TEST_CAPTURE_DIR"


def _capture_path() -> Path | None:
    raw = os.environ.get(CAPTURE_ENV)
    if not raw:
        return None
    # Gate on *every* API base resolving to loopback. Capture writes request
    # URLs and bodies to disk; a deployment that inherited this variable must
    # not persist request content for calls that are leaving for Google.
    from .client import all_bases_loopback

    if not all_bases_loopback():
        logger.warning(
            "ignoring %s: capture requires every API base to point at loopback",
            CAPTURE_ENV,
        )
        return None
    directory = Path(raw)
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError:  # pragma: no cover - diagnostics must not affect results
        return None
    return directory / "requests.jsonl"


def record(method: str, url: str, body: Mapping[str, Any] | None) -> None:
    """Append one request record, synchronously.

    Synchronous on purpose: capture is a test seam, and a background writer
    would make "the server recorded what it built" racy against the assertion.
    The cost is one small file append per request when the variable is set, and
    nothing at all when it is not.
    """
    path = _capture_path()
    if path is None:
        return
    try:
        line = json.dumps({"method": method, "url": url, "body": body}, sort_keys=True)
        # O_APPEND keeps concurrent workers from interleaving partial lines.
        import os as _os

        fd = _os.open(path, _os.O_WRONLY | _os.O_CREAT | _os.O_APPEND, 0o600)
        try:
            _os.write(fd, (line + "\n").encode("utf-8"))
        finally:
            _os.close(fd)
    except Exception:  # pragma: no cover - diagnostics must not affect results
        logger.debug("capture write failed", exc_info=True)