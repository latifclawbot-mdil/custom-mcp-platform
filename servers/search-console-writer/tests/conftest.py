"""Test bootstrap: import path, and a hard no-outbound-network guard.

The write server's whole risk surface is an authenticated mutation leaving the
machine. The test suite therefore runs with outbound network *denied*: any
``connect`` to a non-loopback address raises. Loopback is allowed because the
real-protocol tests deliberately talk to an in-process fake upstream on
127.0.0.1, which is what proves request construction without reaching Google.

This is a guard, not a convention: a future test that accidentally points at a
real host fails loudly instead of quietly depending on the internet.
"""

from __future__ import annotations

import ipaddress
import socket
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

_LOOPBACK_NAMES = frozenset({"127.0.0.1", "::1", "localhost", "localhost.localdomain"})


def _is_loopback(address: object) -> bool:
    # AF_UNIX addresses are path strings; nothing to police there.
    if not isinstance(address, tuple) or not address:
        return True
    host = address[0]
    if not isinstance(host, str):
        return False
    if host in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


@pytest.fixture(autouse=True, scope="session")
def deny_outbound_network():
    """Refuse any TCP connect to a non-loopback address for the whole session."""
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def guarded_connect(self, address, *args, **kwargs):
        if not _is_loopback(address):
            raise RuntimeError(
                f"outbound network is denied in unit tests; attempted connect to {address!r}"
            )
        return real_connect(self, address, *args, **kwargs)

    def guarded_connect_ex(self, address, *args, **kwargs):
        if not _is_loopback(address):
            raise RuntimeError(
                f"outbound network is denied in unit tests; attempted connect to {address!r}"
            )
        return real_connect_ex(self, address, *args, **kwargs)

    socket.socket.connect = guarded_connect  # type: ignore[method-assign]
    socket.socket.connect_ex = guarded_connect_ex  # type: ignore[method-assign]
    try:
        yield
    finally:
        socket.socket.connect = real_connect  # type: ignore[method-assign]
        socket.socket.connect_ex = real_connect_ex  # type: ignore[method-assign]