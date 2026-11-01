"""Unit tests for token resolution and the bearer verifier.

These are fast and offline; the wire-level security behaviour is asserted in
``test_http_security.py`` against a real running server.
"""

from __future__ import annotations

import asyncio

import pytest

from mcp_http_transport.auth import (
    ENV_MAX_TOKEN_LENGTH,
    ENV_MIN_TOKEN_LENGTH,
    ENV_TOKEN,
    AuthConfigError,
    BearerTokenVerifier,
    generate_token,
    load_expected_token,
)
from mcp_http_transport.server import ENV_TOKEN_FILE, AppConfigError, resolve_token

GOOD = "a" * 32


def test_missing_token_fails_closed():
    with pytest.raises(AuthConfigError) as excinfo:
        load_expected_token({})
    assert ENV_TOKEN in str(excinfo.value)
    assert "Refusing to start" in str(excinfo.value)


def test_empty_token_fails_closed():
    with pytest.raises(AuthConfigError):
        load_expected_token({ENV_TOKEN: ""})


def test_short_token_rejected_by_default_minimum():
    with pytest.raises(AuthConfigError) as excinfo:
        load_expected_token({ENV_TOKEN: "short"})
    assert "shorter than the 16-character minimum" in str(excinfo.value)


def test_minimum_length_is_configurable_downward_for_tests():
    assert load_expected_token({ENV_TOKEN: "tiny", ENV_MIN_TOKEN_LENGTH: "4"}) == "tiny"


def test_whitespace_around_token_is_an_error_not_a_silent_strip():
    with pytest.raises(AuthConfigError) as excinfo:
        load_expected_token({ENV_TOKEN: GOOD + "\n"})
    assert "whitespace" in str(excinfo.value)


def test_overlong_token_rejected():
    with pytest.raises(AuthConfigError) as excinfo:
        load_expected_token({ENV_TOKEN: "a" * 40, ENV_MAX_TOKEN_LENGTH: "16"})
    assert "longer than the 16-character maximum" in str(excinfo.value)


def test_contradictory_bounds_are_an_error():
    with pytest.raises(AuthConfigError) as excinfo:
        load_expected_token({ENV_TOKEN: GOOD, ENV_MIN_TOKEN_LENGTH: "64", ENV_MAX_TOKEN_LENGTH: "8"})
    assert "exceeds" in str(excinfo.value)


def test_non_numeric_bound_is_an_error():
    with pytest.raises(AuthConfigError):
        load_expected_token({ENV_TOKEN: GOOD, ENV_MIN_TOKEN_LENGTH: "sixteen"})


def test_generated_token_is_long_and_unique():
    first, second = generate_token(), generate_token()
    assert first != second
    assert len(first) >= 43
    assert "=" not in first and "+" not in first and "/" not in first


def test_resolve_token_prefers_env_over_file(tmp_path):
    path = tmp_path / "token"
    path.write_text("b" * 40, encoding="utf-8")
    resolved = resolve_token({ENV_TOKEN: GOOD, ENV_TOKEN_FILE: str(path)})
    assert resolved == GOOD


def test_resolve_token_reads_and_strips_a_mounted_secret_file(tmp_path):
    path = tmp_path / "token"
    # File-mounted secrets usually carry a trailing newline from the writer.
    path.write_text(GOOD + "\n", encoding="utf-8")
    assert resolve_token({ENV_TOKEN_FILE: str(path)}) == GOOD


def test_resolve_token_rejects_an_empty_secret_file(tmp_path):
    path = tmp_path / "token"
    path.write_text("", encoding="utf-8")
    with pytest.raises(AppConfigError) as excinfo:
        resolve_token({ENV_TOKEN_FILE: str(path)})
    assert "unauthenticated" in str(excinfo.value)


def test_resolve_token_reports_an_unreadable_secret_file(tmp_path):
    missing = tmp_path / "nope"
    with pytest.raises(AppConfigError) as excinfo:
        resolve_token({ENV_TOKEN_FILE: str(missing)})
    assert "cannot be read" in str(excinfo.value)


def test_resolve_token_without_any_source_fails_closed():
    with pytest.raises(AuthConfigError):
        resolve_token({})


def test_verifier_rejects_empty_and_non_string_tokens():
    verifier = BearerTokenVerifier(GOOD)
    assert asyncio.run(verifier.verify_token("")) is None
    assert asyncio.run(verifier.verify_token(None)) is None
    assert verifier.rejected == 2
    assert verifier.accepted == 0


def test_verifier_accepts_the_expected_token_and_grants_its_scope():
    verifier = BearerTokenVerifier(GOOD, required_scope="mcp:access")
    token = asyncio.run(verifier.verify_token(GOOD))
    assert token is not None
    assert token.scopes == ["mcp:access"]
    assert token.client_id == "mcp-http-transport"


def test_verifier_rejects_a_near_miss_token():
    verifier = BearerTokenVerifier(GOOD)
    assert asyncio.run(verifier.verify_token(GOOD[:-1] + "b")) is None
    assert asyncio.run(verifier.verify_token(GOOD + "b")) is None
    assert asyncio.run(verifier.verify_token(GOOD.upper())) is None


def test_verifier_never_prints_the_token():
    verifier = BearerTokenVerifier(GOOD)
    assert GOOD not in repr(verifier)
    assert GOOD not in str(verifier)


def test_verifier_requires_a_token_at_construction():
    with pytest.raises(AuthConfigError):
        BearerTokenVerifier("")


def test_verifier_conforms_to_the_sdk_token_verifier_protocol():
    """Structurally satisfy ``TokenVerifier`` (a non-runtime Protocol).

    ``isinstance`` cannot be used — the SDK's protocol is not
    ``@runtime_checkable`` — so the check is the structural one: the exact
    method name the SDK calls, with the exact signature it calls it with.
    """
    import inspect

    from mcp.server.auth.provider import TokenVerifier

    verifier = BearerTokenVerifier(GOOD)
    method = getattr(verifier, "verify_token")
    assert callable(method)
    assert inspect.iscoroutinefunction(method)
    parameters = list(inspect.signature(method).parameters)
    assert parameters == ["token"]
    assert "async def verify_token" in inspect.getsource(TokenVerifier)