"""The suite's no-outbound-network guard is real, not aspirational.

If this test ever passes only because the guard was removed, the rest of the
suite stops proving that request construction is exercised against loopback
fakes rather than the internet.
"""

from __future__ import annotations

import socket

import pytest


def test_connecting_to_a_public_address_is_blocked():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(RuntimeError) as excinfo:
            sock.connect(("203.0.113.7", 443))
        assert "outbound network is denied" in str(excinfo.value)
    finally:
        sock.close()


def test_connecting_to_loopback_is_allowed():
    """The guard must not break the loopback fakes the suite depends on."""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.send_response(204)
            self.end_headers()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        sock.close()
    finally:
        server.shutdown()
        server.server_close()


def test_the_production_bases_are_google_hosts_not_loopback():
    """Confirms the suite and production disagree on purpose."""
    from search_console_writer.client import BASE_URL

    assert BASE_URL == "https://www.googleapis.com/webmasters/v3"