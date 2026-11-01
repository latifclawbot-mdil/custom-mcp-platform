"""Unit tests for auth-less ``AppConfig`` validation (no server spawned).

These are the cheap failure modes: a config that cannot possibly produce a
correct endpoint should raise at build time, not at request time.
"""

from __future__ import annotations

import pytest

from mcp_http_transport.server import AppConfig, AppConfigError, _host_patterns, _origin_patterns

TOKEN = "unit-test-token-0123456789abcdef"


def test_host_patterns_expand_bare_names_to_wildcard_ports_without_a_global_wildcard():
    patterns = _host_patterns(("localhost", "127.0.0.1", "mcp-internal"))
    assert patterns == [
        "localhost",
        "localhost:*",
        "127.0.0.1",
        "127.0.0.1:*",
        "mcp-internal",
        "mcp-internal:*",
    ]
    assert "*" not in patterns


def test_host_patterns_do_not_double_expand_an_explicit_wildcard():
    assert _host_patterns(("host:*",)) == ["host:*"]


def test_origin_patterns_are_empty_when_nothing_is_allowed():
    assert _origin_patterns(()) == []


def test_origin_patterns_expand_bare_origins():
    assert _origin_patterns(("http://localhost",)) == ["http://localhost", "http://localhost:*"]


def test_config_repr_never_contains_the_token():
    config = AppConfig(expected_token=TOKEN)
    assert TOKEN not in repr(config)
    assert TOKEN not in str(config)


def test_build_rejects_a_path_without_a_leading_slash():
    from mcp_http_transport.server import build_streamable_http_app

    with pytest.raises(AppConfigError) as excinfo:
        build_streamable_http_app(object(), AppConfig(expected_token=TOKEN, path="mcp"))
    assert "must start with" in str(excinfo.value)


def test_build_rejects_an_empty_host_allowlist():
    from mcp_http_transport.server import build_streamable_http_app

    with pytest.raises(AppConfigError) as excinfo:
        build_streamable_http_app(object(), AppConfig(expected_token=TOKEN, allowed_hosts=()))
    assert "allowed_hosts" in str(excinfo.value)


def test_build_refuses_a_server_without_settings_auth():
    """A duck-typed object can never silently produce an open endpoint."""

    class NotAServer:
        pass

    from mcp_http_transport.server import build_streamable_http_app

    with pytest.raises(AppConfigError) as excinfo:
        build_streamable_http_app(NotAServer(), AppConfig(expected_token=TOKEN))
    assert "settings.auth" in str(excinfo.value)


def test_build_rejects_an_unknown_transport_path_but_accepts_a_custom_one():
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "servers" / "search-console" / "src"))
    from mcp_http_transport.server import build_streamable_http_app
    from search_console_mcp.allowlist import PropertyAllowlist
    from search_console_mcp.server import create_server

    server = create_server(allowlist=PropertyAllowlist.from_env({"GSC_ALLOWED_SITES": "sc-domain:example.com"}))
    app = build_streamable_http_app(
        server,
        AppConfig(expected_token=TOKEN, path="/internal/mcp", allowed_hosts=("127.0.0.1",)),
    )
    assert app is not None
    # The verifier and config are exposed for operators to assert wiring.
    assert app.state.mcp_app_config.path == "/internal/mcp"
    assert app.state.mcp_token_verifier is server._token_verifier