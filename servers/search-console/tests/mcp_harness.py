"""Spawning a real `python -m search_console_mcp` subprocess over stdio.

This harness exists to test the transport, not to fake it. The test process:

1. starts a fake Search Console upstream on a loopback port, which records every
   request it receives;
2. spawns the server with `mcp.client.stdio.stdio_client`, pointing it at that
   upstream through the same loopback override seam production exposes;
3. performs a real `initialize`, `tools/list`, and `tools/call`.

`GSC_TEST_CAPTURE_DIR` makes the *server process* dump what it actually built
(request URL and body) to a file. That matters for the property-encoding and
URL-scope assertions: they are checked on the server's own construction, not on
anything the test computed independently.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"

ALLOWED_SITE = "sc-domain:example.com"
ALLOWED_URL_PREFIX = "https://www.example.com/"


class RecordedRequest:
    def __init__(self, method: str, path: str, body, headers):
        self.method = method
        self.path = path
        self.body = body
        self.headers = headers

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"RecordedRequest({self.method} {self.path})"


class FakeUpstream:
    """A tiny HTTP server standing in for Google's API.

    Routes by path suffix so the real request shape is what gets exercised:

    - ``/sites`` -> site list
    - ``/searchAnalytics/query`` -> analytics rows
    - ``/sitemaps`` -> sitemap list
    - ``/urlInspection/index:inspect`` -> inspection result
    """

    def __init__(self) -> None:
        self.requests: list[RecordedRequest] = []
        self.responses: dict[str, tuple[int, dict]] = {}
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):  # silence the test output
                pass

            def _handle(self):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                try:
                    body = json.loads(raw) if raw else None
                except ValueError:
                    body = raw.decode("utf-8", "replace")
                outer.requests.append(
                    RecordedRequest(self.command, self.path, body, dict(self.headers))
                )
                status, payload = outer._route(self.path)
                encoded = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            do_GET = _handle
            do_POST = _handle
            do_PUT = _handle
            do_DELETE = _handle

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self.port = self._server.server_address[1]

    def _route(self, path: str) -> tuple[int, dict]:
        for suffix, response in self.responses.items():
            if path.endswith(suffix):
                return response
        if path.endswith("/sites"):
            return 200, {
                "siteEntry": [
                    {"siteUrl": ALLOWED_SITE, "permissionLevel": "siteOwner"},
                    {"siteUrl": ALLOWED_URL_PREFIX, "permissionLevel": "siteOwner"},
                    {"siteUrl": "sc-domain:not-allowlisted.example", "permissionLevel": "siteFullUser"},
                ]
            }
        if path.endswith("/searchAnalytics/query"):
            return 200, {
                "rows": [
                    {
                        "keys": ["gsc api"],
                        "clicks": 4.0,
                        "impressions": 80.0,
                        "ctr": 0.05,
                        "position": 3.25,
                    }
                ],
                "responseAggregationType": "byProperty",
            }
        if path.endswith("/sitemaps"):
            return 200, {
                "sitemap": [
                    {
                        "path": "https://example.com/sitemap.xml",
                        "type": "sitemap",
                        "isPending": False,
                        "isSitemapsIndex": True,
                        "lastSubmitted": "2026-09-01T00:00:00Z",
                        "lastDownloaded": "2026-10-01T00:00:00Z",
                        "errors": "0",
                        "warnings": "2",
                        "contents": [{"type": "web", "submitted": "10", "indexed": "9"}],
                    }
                ]
            }
        if path.endswith("/urlInspection/index:inspect"):
            return 200, {
                "inspectionResult": {
                    "inspectionResultLink": "https://search.google.com/search-console/inspect?x=1",
                    "indexStatusResult": {
                        "verdict": "PASS",
                        "coverageState": "Submitted and indexed",
                        "robotsTxtState": "ALLOWED",
                        "indexingState": "INDEXING_ALLOWED",
                        "pageFetchState": "SUCCESSFUL",
                        "lastCrawlTime": "2026-10-20T00:00:00Z",
                        "googleCanonical": "https://example.com/pricing",
                        "userCanonical": "https://example.com/pricing",
                        "crawledAs": "MOBILE",
                    },
                    "mobileUsabilityResult": {"verdict": "PASS"},
                }
            }
        return 404, {"error": {"message": "unexpected path", "code": 404}}

    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> "FakeUpstream":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def __enter__(self) -> "FakeUpstream":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()


def server_env(upstream: FakeUpstream, capture_dir: Path | None = None, **extra) -> dict:
    """Environment for the server subprocess.

    `HOME` is redirected to an empty directory so `google.auth.default()` cannot
    find real user credentials, and `GOOGLE_APPLICATION_CREDENTIALS` is cleared.
    Any test that reached ADC would fail loudly rather than silently use a real
    account.
    """
    env = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": str(SRC),
        "PYTHONUNBUFFERED": "1",
        "GSC_ALLOWED_SITES": f"{ALLOWED_SITE},{ALLOWED_URL_PREFIX}",
        "GSC_API_BASE_OVERRIDE": upstream.base_url() + "/webmasters/v3",
        "GSC_INSPECTION_BASE_OVERRIDE": upstream.base_url() + "/v1/urlInspection/index:inspect",
        "HOME": str(_empty_home()),
    }
    env.pop("GOOGLE_APPLICATION_CREDENTIALS", None)
    if capture_dir is not None:
        env["GSC_TEST_CAPTURE_DIR"] = str(capture_dir)
    env.update({k: str(v) for k, v in extra.items()})
    return env


_HOME_CACHE: Path | None = None


def _empty_home() -> Path:
    global _HOME_CACHE
    if _HOME_CACHE is None:
        import tempfile

        _HOME_CACHE = Path(tempfile.mkdtemp(prefix="gsc-mcp-nohome-"))
    return _HOME_CACHE


def read_capture(capture_dir: Path) -> list[dict]:
    """Read what the *server process* recorded for its own request construction."""
    path = capture_dir / "requests.jsonl"
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out