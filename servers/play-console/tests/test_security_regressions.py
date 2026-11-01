"""Regression tests for the security/correctness review findings.

Each test pins one finding so it cannot silently regress:

1. Redaction bypass when a ``ya29.`` access token is glued to a word character
   (``prefixya29.SECRET``) — the old ``\\bya29\\.`` pattern missed it.
2. Redaction bypass for a bare Google OAuth authorization code (``4/0A...``).
3. The audit echo leaked caller-supplied pagination tokens from the query string.
4. The retry budget was ``3 x request_timeout`` (up to 900 s); it is now one
   bounded total deadline.
5. ``get_subscription_purchase`` targeted a v1 endpoint that is absent from the
   current official Android Publisher v3 discovery document.
6. The fake transport could be activated in a production install via an
   environment variable, returning fabricated data. It now lives only in tests.
"""
from __future__ import annotations

import importlib.util
from types import SimpleNamespace

import httpx
import pytest

from play_console_mcp.errors import UpstreamError, redact_text
from play_console_mcp.google_client import GoogleApiClient
from play_console_mcp.operations import TOOL_SPECS
from play_console_mcp.server import _last_request

REVIEWS = "/androidpublisher/v3/applications/{packageName}/reviews"


class _Creds:
    token = "t"
    valid = True
    expired = False


# -- 1. redaction bypass: glued access token ---------------------------------


def test_glued_ya29_access_token_is_redacted():
    leaked = "prefixya29.SUPERSECRETVALUE"
    out = redact_text(leaked)
    assert "SUPERSECRETVALUE" not in out
    assert "ya29." not in out


def test_ya29_token_without_word_boundary_is_redacted():
    out = redact_text("token=ya29.AAAAAAAAAAAA,Bearer ya29.BBBBBBBBBBBB")
    assert "AAAAAAAAAAAA" not in out
    assert "BBBBBBBBBBBB" not in out


# -- 2. redaction bypass: bare OAuth authorization code ----------------------


def test_bare_oauth_authorization_code_is_redacted():
    code = "4/0AX4XfWhSUPERSECRETAUTHCODE"
    out = redact_text(f"exchange failed for code={code}&redirect_uri=x")
    assert "SUPERSECRETAUTHCODE" not in out
    assert "4/0AX" not in out


def test_oauth_code_shape_in_error_body_is_redacted():
    err = UpstreamError(
        status=400,
        reason="invalid_grant",
        body='{"error":"invalid_grant","code":"4/0AX4XfWhLEAKYLEAKYLEAKY"}',
    )
    assert "LEAKYLEAKYLEAKY" not in str(err)


# -- 3. audit echo: query token leak ----------------------------------------


def test_audit_echo_masks_pagination_token_in_query():
    class _Client:
        _last_request = {
            "method": "GET",
            "root": "https://androidpublisher.googleapis.com",
            "path": "/androidpublisher/v3/applications/com.example.app/reviews",
            "query": {"token": "PAGETOKEN-SHOULD-BE-MASKED", "maxResults": "10"},
        }

    echo = _last_request(_Client())
    assert echo["query"]["token"] == "***"
    assert echo["query"]["maxResults"] == "10"
    assert echo["token_masked"] is True


def test_audit_echo_masks_page_token_key_in_query():
    class _Client:
        _last_request = {
            "method": "GET",
            "root": "https://androidpublisher.googleapis.com",
            "path": "/androidpublisher/v3/applications/com.example.app/subscriptions",
            "query": {"pageToken": "SECRETPAGE", "pageSize": "25"},
        }

    echo = _last_request(_Client())
    assert echo["query"]["pageToken"] == "***"
    assert echo["query"]["pageSize"] == "25"


def test_audit_echo_leaves_ordinary_query_untouched():
    class _Client:
        _last_request = {
            "method": "GET",
            "root": "https://androidpublisher.googleapis.com",
            "path": "/androidpublisher/v3/applications/com.example.app/purchases/voidedpurchases",
            "query": {"startTime": "1000", "endTime": "2000", "type": "1"},
        }

    echo = _last_request(_Client())
    assert echo["query"] == {"startTime": "1000", "endTime": "2000", "type": "1"}
    assert "token_masked" not in echo


# -- 4. retry budget is a single bounded deadline ---------------------------


def test_retry_budget_is_bounded_by_total_deadline():
    """A request that consumes the whole budget must not be retried 3x."""
    clock = {"now": 1_000.0}
    timeouts: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        timeouts.append(request.extensions["timeout"]["read"])
        clock["now"] += 1.0  # each attempt burns the entire 1.0 s budget
        raise httpx.ReadTimeout("slow upstream", request=request)

    client = GoogleApiClient(
        settings=SimpleNamespace(request_timeout=1.0),
        credentials=_Creds(),
        transport=httpx.MockTransport(handler),
        sleep=lambda _s: None,
        clock=lambda: clock["now"],
    )

    with pytest.raises(UpstreamError):
        client.get(REVIEWS, params={"packageName": "com.example.app"})

    assert len(timeouts) == 1, f"consumed-budget request retried: {timeouts}"
    assert timeouts[0] == pytest.approx(1.0)


def test_retry_timeout_shrinks_to_remaining_budget():
    """Second attempt may only use what is left of the total budget."""
    clock = {"now": 0.0}
    timeouts: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        timeouts.append(request.extensions["timeout"]["read"])
        clock["now"] += 0.4  # 1.0 s budget => 0.6 s remains for attempt 2
        return httpx.Response(503, json={"error": {"message": "unavailable"}})

    client = GoogleApiClient(
        settings=SimpleNamespace(request_timeout=1.0),
        credentials=_Creds(),
        transport=httpx.MockTransport(handler),
        sleep=lambda _s: None,
        clock=lambda: clock["now"],
    )

    with pytest.raises(UpstreamError):
        client.get(REVIEWS, params={"packageName": "com.example.app"})

    assert len(timeouts) >= 2
    assert timeouts[0] == pytest.approx(1.0)
    assert timeouts[1] <= 0.6 + 1e-6


# -- 5. unsupported v1 subscription-purchase endpoint removed ----------------


def test_v1_subscription_purchase_tool_is_not_exposed():
    assert "get_subscription_purchase" not in TOOL_SPECS


def test_supported_v2_subscription_purchase_tool_remains():
    assert "get_subscription_purchase_v2" in TOOL_SPECS


def test_no_operation_targets_removed_v1_path():
    import play_console_mcp.operations as ops_module

    assert not hasattr(ops_module.Operations, "get_subscription_purchase")


# -- 6. fake transport cannot be activated in a production install ----------


def test_fake_transport_module_is_not_shipped():
    assert importlib.util.find_spec("play_console_mcp.fake_transport") is None


def test_server_module_has_no_fake_transport_branch():
    from pathlib import Path

    import play_console_mcp.server as server_module

    source = Path(server_module.__file__).read_text()
    assert "fake_transport" not in source
    assert "PLAY_CONSOLE_FAKE_TRANSPORT" not in source
