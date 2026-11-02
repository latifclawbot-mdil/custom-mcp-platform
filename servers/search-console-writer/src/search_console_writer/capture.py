"""Opt-in request capture, for asserting request construction from inside the
server process.

This module writes only when ``GSC_WRITE_TEST_CAPTURE_DIR`` is set, and only
when the API base has been overridden to a loopback host — so a deployment that
inherits the variable cannot persist request content for calls leaving for
Google. The static-token seam and the capture seam are deliberately gated on the
*same* condition: no traffic leaves the box.

It records the HTTP method and the final URL of a mutating request. It never
records headers (which carry the bearer token) and never records the response.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger("search_console_writer.capture")

CAPTURE_ENV = "GSC_WRITE_TEST_CAPTURE_DIR"


def _capture_path() -> Path | None:
    raw = os.environ.get(CAPTURE_ENV)
    if not raw:
        return None
    from .client import BASE_URL, resolve_base

    if resolve_base() == BASE_URL:
        logger.warning(
            "ignoring %s: capture requires the API base to point at loopback",
            CAPTURE_ENV,
        )
        return None
    directory = Path(raw)
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError:  # pragma: no cover - diagnostics must not affect results
        return None
    return directory / "requests.jsonl"


def record(method: str, url: str) -> None:
    """Append one request record, synchronously.

    Synchronous on purpose: capture is a test seam, and a background writer
    would make "the server recorded what it built" racy against the assertion.
    """
    path = _capture_path()
    if path is None:
        return
    try:
        line = json.dumps({"method": method, "url": url}, sort_keys=True)
        import os as _os

        fd = _os.open(path, _os.O_WRONLY | _os.O_CREAT | _os.O_APPEND, 0o600)
        try:
            _os.write(fd, (line + "\n").encode("utf-8"))
        finally:
            _os.close(fd)
    except Exception:  # pragma: no cover - diagnostics must not affect results
        logger.debug("capture write failed", exc_info=True)


__all__ = ["CAPTURE_ENV", "record"]