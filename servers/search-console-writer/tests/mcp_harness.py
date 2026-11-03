"""Spawning a real ``python -m search_console_writer`` subprocess over stdio.

This harness exists to test the transport, not to fake it. The test process:

1. starts a fake Search Console write upstream on a loopback port, recording
   every request including its HTTP verb;
2. spawns the server with ``mcp.client.stdio.stdio_client``, pointing it at
   that upstream through the loopback-only base override;
3. performs a real ``initialize``, ``tools/list``, and ``tools/call``.

The fake upstream answers ``PUT``/``DELETE`` with the empty bodies Google's
write methods return, so the *real* request shape is what gets exercised.

``GSC_WRITE_TEST_CAPTURE_DIR`` makes the *server process* dump what it built
(method, URL) to a file, so construction is asserted from inside the process
that built it — not from a reconstruction the test made up.
"""

from __future__ import annotations

import contextlib
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"

ALLOWED_SITE = "sc-domain:example.com"
ALLOWED_URL_PREFIX = "https://www.example.com/"
SITEMAP = "https://www.example.com/sitemap.xml"


class RecordedRequest:
    def __init__(self, method: str, path: str, body, headers):
        self.method = method
        self.path = path
        self.body = body
        self.headers = headers

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"RecordedRequest({self.method} {self.path})"


class FakeWriteUpstream:
    """A tiny HTTP server standing in for Google's webmasters v3 write API.

    ``status`` selects the response status; ``redirect_location`` is sent as
    ``Location`` (so a test can prove a redirect is *not* followed), and
    ``extra_headers`` lets a test put a hostile or over-long header on the
    response.
    """

    def __init__(self) -> None:
        self.requests: list[RecordedRequest] = []
        self.status = 200
        self.error_message = "denied"
        self.redirect_location: str | None = None
        self.extra_headers: dict[str, str] = {}
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
                # The four write methods answer with an empty body on success.
                payload = b"" if outer.status < 400 else json.dumps(
                    {"error": {"message": outer.error_message}}
                ).encode("utf-8")
                self.send_response(outer.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                if outer.redirect_location is not None:
                    self.send_header("Location", outer.redirect_location)
                for name, value in outer.extra_headers.items():
                    self.send_header(name, value)
                self.end_headers()
                if payload:
                    self.wfile.write(payload)

            do_GET = _handle
            do_POST = _handle
            do_PUT = _handle
            do_DELETE = _handle

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self.port = self._server.server_address[1]

    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> "FakeWriteUpstream":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def __enter__(self) -> "FakeWriteUpstream":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()


def server_env(upstream: FakeWriteUpstream, capture_dir: Path | None = None, **extra) -> dict:
    """Environment for the server subprocess.

    ``HOME`` is redirected to an empty directory so ``google.auth.default()``
    cannot find real user credentials, and the ADC/service-account variables are
    cleared. Any test that reached a real credential would fail loudly.
    """
    env = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": str(SRC),
        "PYTHONUNBUFFERED": "1",
        "GSC_WRITE_ALLOWED_SITES": f"{ALLOWED_SITE},{ALLOWED_URL_PREFIX}",
        "GSC_WRITE_API_BASE_OVERRIDE": upstream.base_url() + "/webmasters/v3",
        "HOME": str(_empty_home()),
    }
    for name in (
        "GOOGLE_APPLICATION_CREDENTIALS",
        "GSC_WRITE_SERVICE_ACCOUNT_FILE",
        "GSC_WRITE_TEST_STATIC_TOKEN",
    ):
        env.pop(name, None)
    if capture_dir is not None:
        env["GSC_WRITE_TEST_CAPTURE_DIR"] = str(capture_dir)
    env.update({k: str(v) for k, v in extra.items()})
    return env


_HOME_CACHE: Path | None = None


def _empty_home() -> Path:
    global _HOME_CACHE
    if _HOME_CACHE is None:
        import tempfile

        _HOME_CACHE = Path(tempfile.mkdtemp(prefix="gsc-write-nohome-"))
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