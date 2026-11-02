"""Spawning a real ``python -m play_console_writer`` subprocess over stdio.

This harness tests the transport, it does not fake it. The test process:

1. starts a fake Android Publisher upstream on a loopback port, which records
   every request;
2. spawns the server with the SDK's own ``stdio_client``, pointing it at that
   upstream through the loopback override seam;
3. performs a real ``initialize``, ``tools/list`` and ``tools/call``.

``HOME`` is redirected to an empty directory and ``GOOGLE_APPLICATION_CREDENTIALS``
cleared, so a stray real credential could not be picked up.
"""
from __future__ import annotations

import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"

PKG = "com.example.app"
ALLOWED = PKG


class RecordedRequest:
    def __init__(self, method: str, path: str, body, headers):
        self.method = method
        self.path = path
        self.body = body
        self.headers = headers

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"RecordedRequest({self.method} {self.path})"


class FakeUpstream:
    """A tiny HTTP server standing in for the Android Publisher API."""

    def __init__(self) -> None:
        self.requests: list[RecordedRequest] = []
        self.responses: dict[str, tuple[int, dict]] = {}
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
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
                status, payload = outer._route(self.command, self.path)
                encoded = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            do_GET = _handle
            do_POST = _handle
            do_PUT = _handle
            do_PATCH = _handle
            do_DELETE = _handle

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self.port = self._server.server_address[1]

    def _route(self, method: str, path: str) -> tuple[int, dict]:
        for needle, response in self.responses.items():
            if needle in path:
                return response
        if path.endswith("/edits") and method == "POST":
            return 200, {"id": "edit-1", "expiryTimeSeconds": "1800000000"}
        if path.endswith(":validate"):
            return 200, {"id": "edit-1"}
        if path.endswith(":commit"):
            return 200, {"id": "edit-1"}
        if "/details" in path:
            return 200, {"contactEmail": "dev@example.com", "defaultLanguage": "en-US"}
        if "/listings" in path:
            return 200, {"title": "Example", "language": "en-US"}
        if "/tracks" in path:
            return 200, {"track": "production", "releases": []}
        if "/testers/" in path:
            return 200, {"googleGroups": ["testers@example.com"]}
        if ":reply" in path:
            return 200, {"result": {"replyText": "thanks"}}
        if "/dataSafety" in path:
            return 200, {}
        if "/deviceTierConfigs" in path:
            return 200, {"deviceTierConfigId": "7"}
        if path.endswith("/edits/edit-1"):
            return 200, {"id": "edit-1"}
        return 200, {}

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


def server_env(upstream: FakeUpstream, **extra) -> dict:
    env = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": str(SRC),
        "PYTHONUNBUFFERED": "1",
        "PLAY_WRITER_ALLOWED_PACKAGES": ALLOWED,
        "PLAY_WRITER_TEST_LOOPBACK_BASE": upstream.base_url(),
        "PLAY_WRITER_TEST_STATIC_TOKEN": "loopback-test-token-not-a-secret",
        "HOME": str(_empty_home()),
    }
    env.pop("GOOGLE_APPLICATION_CREDENTIALS", None)
    env.update({k: str(v) for k, v in extra.items()})
    return env


_HOME_CACHE: Path | None = None


def _empty_home() -> Path:
    global _HOME_CACHE
    if _HOME_CACHE is None:
        import tempfile

        _HOME_CACHE = Path(tempfile.mkdtemp(prefix="play-writer-nohome-"))
    return _HOME_CACHE


def params(upstream: FakeUpstream, **extra):
    from mcp.client.stdio import StdioServerParameters

    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "play_console_writer"],
        env=server_env(upstream, **extra),
        cwd=str(REPO_ROOT),
    )