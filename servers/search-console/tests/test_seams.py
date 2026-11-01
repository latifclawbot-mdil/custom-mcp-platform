"""The test-only seams must be inert unless the whole request path is local.

Three environment variables exist for tests: a request-capture directory, a
static bearer token, and the loopback base overrides. Each is useful in a test
and dangerous in a deployment that inherits one by accident:

* ``GSC_TEST_CAPTURE_DIR`` made every request's URL and body get written to
  disk regardless of where the request was going.
* ``GSC_TEST_STATIC_TOKEN`` was honoured when *only* the webmasters base was
  overridden, so an inspection call still left for Google's real endpoint
  carrying that token.

Both are now gated on every API base resolving to loopback, which is the
condition that actually guarantees no traffic (and no token) leaves the box.
"""

import os

import pytest

from search_console_mcp import auth, capture
from search_console_mcp.client import (
    BASE_OVERRIDE_ENV,
    BASE_URL,
    INSPECTION_URL,
    _INSPECTION_OVERRIDE_ENV,
    _override_base,
)


@pytest.fixture
def clean_env(monkeypatch):
    for name in (
        BASE_OVERRIDE_ENV,
        _INSPECTION_OVERRIDE_ENV,
        auth.STATIC_TOKEN_ENV,
        capture.CAPTURE_ENV,
    ):
        monkeypatch.delenv(name, raising=False)
    auth.reset_session()
    yield monkeypatch
    auth.reset_session()


class TestCaptureSeam:
    def test_capture_writes_when_the_whole_path_is_loopback(self, clean_env, tmp_path):
        clean_env.setenv(BASE_OVERRIDE_ENV, "http://127.0.0.1:9/webmasters/v3")
        clean_env.setenv(
            _INSPECTION_OVERRIDE_ENV, "http://127.0.0.1:9/v1/urlInspection/index:inspect"
        )
        clean_env.setenv(capture.CAPTURE_ENV, str(tmp_path / "cap"))
        capture.record("GET", "http://127.0.0.1:9/webmasters/v3/sites", None)
        assert (tmp_path / "cap" / "requests.jsonl").exists()

    def test_capture_is_inert_without_any_override(self, clean_env, tmp_path):
        """A deployment that inherits the variable must not write request bodies."""
        clean_env.setenv(capture.CAPTURE_ENV, str(tmp_path / "cap"))
        capture.record("POST", "https://www.googleapis.com/webmasters/v3/sites", {"a": 1})
        assert not (tmp_path / "cap" / "requests.jsonl").exists()

    def test_capture_is_inert_when_the_override_is_not_loopback(self, clean_env, tmp_path):
        clean_env.setenv(BASE_OVERRIDE_ENV, "https://evil.example/webmasters/v3")
        clean_env.setenv(capture.CAPTURE_ENV, str(tmp_path / "cap"))
        capture.record("GET", "https://www.googleapis.com/webmasters/v3/sites", None)
        assert not (tmp_path / "cap" / "requests.jsonl").exists()


class TestStaticTokenSeam:
    def test_static_token_needs_both_bases_local(self, clean_env):
        """One local base is not enough: the other endpoint would still be Google."""
        clean_env.setenv(BASE_OVERRIDE_ENV, "http://127.0.0.1:9/webmasters/v3")
        clean_env.setenv(auth.STATIC_TOKEN_ENV, "test-token")
        assert auth._loopback_override_active() is False

    def test_static_token_is_honoured_when_both_bases_are_local(self, clean_env):
        clean_env.setenv(BASE_OVERRIDE_ENV, "http://127.0.0.1:9/webmasters/v3")
        clean_env.setenv(
            _INSPECTION_OVERRIDE_ENV, "http://127.0.0.1:9/v1/urlInspection/index:inspect"
        )
        clean_env.setenv(auth.STATIC_TOKEN_ENV, "test-token")
        assert auth._loopback_override_active() is True
        session = auth._build_session()
        assert session is not None

    def test_a_remote_override_is_ignored_and_reported(self, clean_env, caplog):
        clean_env.setenv(BASE_OVERRIDE_ENV, "https://evil.example/webmasters/v3")
        assert _override_base(BASE_URL, BASE_OVERRIDE_ENV) == BASE_URL
        assert _override_base(INSPECTION_URL, _INSPECTION_OVERRIDE_ENV) == INSPECTION_URL


class TestOverrideHostParsing:
    """A userinfo trick must not make a remote host look like loopback."""

    @pytest.mark.parametrize(
        "raw",
        [
            "http://127.0.0.1@evil.example/",
            "http://127.0.0.1:9@evil.example/",
            "https://evil.example/?127.0.0.1",
            "http://not-localhost/",
            "http://127.0.0.1.evil.example/",
        ],
    )
    def test_non_loopback_overrides_are_rejected(self, raw, clean_env):
        clean_env.setenv(BASE_OVERRIDE_ENV, raw)
        assert _override_base(BASE_URL, BASE_OVERRIDE_ENV) == BASE_URL
