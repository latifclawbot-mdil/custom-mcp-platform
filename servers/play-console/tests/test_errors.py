"""RED tests: upstream error redaction."""
from __future__ import annotations

import pytest

from play_console_mcp.errors import UpstreamError, redact_upstream_error

SECRET = "ya29.SUPERSECRETTOKENVALUE"
BODY = (
    '{"error": {"code": 403, "message": "Caller does not have permission", '
    '"status": "PERMISSION_DENIED", "details": [{"@type": "x", '
    '"token": "ya29.SUPERSECRETTOKENVALUE"}]}}'
)


def test_redacts_token_material_from_body():
    err = redact_upstream_error(status=403, body=BODY, url="https://androidpublisher.googleapis.com/x")
    assert SECRET not in str(err)
    assert "PERMISSION_DENIED" in str(err)


def test_redacts_authorization_header():
    err = redact_upstream_error(
        status=401,
        body='{"error":{"message":"unauthorized"}}',
        url="https://x/y",
        headers={"Authorization": f"Bearer {SECRET}"},
    )
    assert SECRET not in str(err)


def test_redacts_bearer_like_strings_generically():
    err = redact_upstream_error(status=500, body=f"boom Authorization: Bearer {SECRET}", url="u")
    assert SECRET not in str(err)


def test_truncates_long_bodies():
    err = redact_upstream_error(status=500, body="x" * 50_000, url="u")
    assert len(str(err)) < 4_000


def test_extracts_google_status_reason():
    err = redact_upstream_error(status=400, body=BODY, url="u")
    assert err.status == 403 or err.status == 400
    assert err.reason in {"PERMISSION_DENIED", "Caller does not have permission"}


def test_upstream_error_is_exception_and_hides_body_from_str_for_html():
    err = UpstreamError(status=502, reason="Bad Gateway", url="https://x/y",
                        body="<html>secret-html-session-cookie=abc</html>")
    assert "secret-html-session-cookie" not in str(err)


def test_map_httplib2_style_response():
    from play_console_mcp.errors import error_from_http_response
    err = error_from_http_response(
        status=401,
        reason="Unauthorized",
        headers={"www-authenticate": f"Bearer error=invalid_token {SECRET}"},
        body=b'{"error":{"message":"Invalid Credentials"}}',
        url="https://androidpublisher.googleapis.com/androidpublisher/v3/x",
    )
    assert isinstance(err, UpstreamError)
    assert SECRET not in str(err)
    assert "Unauthorized" in str(err)