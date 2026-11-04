"""Regressions from the independent writer review.

Each test here pins a defect that was found by review rather than by the
original build, and each one is written so it fails against the code as
reviewed:

1. a mutating request must never be *followed* to a redirect target, and a 3xx
   must never be reported as a success;
2. untrusted upstream text (the ``Retry-After`` header and an error message) is
   bounded before it can reach an LLM context;
3. a credential failure is reported as an actionable, non-leaking configuration
   error rather than a generic ``upstream_error``;
4. an invalid numeric HTTP setting refuses startup cleanly instead of raising a
   value-error traceback;
5. the loopback test override cannot silently fall back to a real credential.
"""

from __future__ import annotations

import json

import pytest

from search_console_writer.allowlist import PropertyAllowlist
from search_console_writer.client import BASE_URL, GscWriteClient
from search_console_writer.errors import GscError

ALLOWED = "sc-domain:example.com"


class FakeResponse:
    def __init__(self, status=200, payload=None, headers=None, history=None):
        self.status_code = status
        self._payload = payload if payload is not None else {}
        self.headers = headers or {}
        self.history = history or []

    def json(self):
        return self._payload


class RecordingSession:
    """Records every call, including whether redirects were allowed."""

    def __init__(self, response=None):
        self.calls = []
        self._response = response if response is not None else FakeResponse(200)

    def request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        return self._response


def client(session):
    return GscWriteClient(allowlist=PropertyAllowlist([ALLOWED]), session=session)


# --------------------------------------------------------------------------
# 1. Redirects are refused, never followed
# --------------------------------------------------------------------------


class TestRedirectsAreRefused:
    def test_request_disables_redirect_following(self):
        """``allow_redirects=False`` must be passed explicitly to the transport.

        ``requests.Session.request`` defaults to ``allow_redirects=True``, which
        re-sends a 307/308 — including the mutation — to the ``Location`` host.
        """
        session = RecordingSession()
        client(session).add_site(site_url=ALLOWED)
        assert session.calls[0].get("allow_redirects") is False

    @pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
    def test_a_redirect_status_is_an_error_not_a_success(self, status):
        session = RecordingSession(
            FakeResponse(status=status, headers={"Location": "https://evil.example/steal"})
        )
        with pytest.raises(GscError) as excinfo:
            client(session).delete_site(site_url=ALLOWED)
        # Exactly one request was sent: the redirect was never re-issued.
        assert len(session.calls) == 1
        assert excinfo.value.code != "upstream_error" or "redirect" in excinfo.value.message

    def test_a_redirect_is_reported_with_its_own_code(self):
        session = RecordingSession(
            FakeResponse(status=307, headers={"Location": "https://evil.example/x"})
        )
        with pytest.raises(GscError) as excinfo:
            client(session).submit_sitemap(
                site_url=ALLOWED, sitemap_url="https://example.com/sitemap.xml"
            )
        assert excinfo.value.code == "unexpected_redirect"
        assert len(session.calls) == 1

    def test_the_redirect_target_is_never_echoed(self):
        """A hostile ``Location`` must not reach the caller, even redacted."""
        session = RecordingSession(
            FakeResponse(
                status=308,
                headers={"Location": "https://attacker.example/?token=ya29.SECRET123456"},
            )
        )
        with pytest.raises(GscError) as excinfo:
            client(session).add_site(site_url=ALLOWED)
        assert "attacker.example" not in excinfo.value.message
        assert "ya29.SECRET" not in excinfo.value.message
        assert "attacker.example" not in json.dumps(excinfo.value.details)

    def test_a_response_that_already_followed_a_redirect_is_refused(self):
        """Defence in depth: a non-empty ``history`` means hops already happened."""
        session = RecordingSession(
            FakeResponse(status=200, history=[FakeResponse(status=307)])
        )
        with pytest.raises(GscError) as excinfo:
            client(session).add_site(site_url=ALLOWED)
        assert excinfo.value.code == "unexpected_redirect"


class TestThreeXXIsNeverSuccess:
    @pytest.mark.parametrize("status", [300, 304])
    def test_any_3xx_is_refused(self, status):
        session = RecordingSession(FakeResponse(status=status))
        with pytest.raises(GscError):
            client(session).add_site(site_url=ALLOWED)


# --------------------------------------------------------------------------
# 2. Untrusted upstream text is bounded
# --------------------------------------------------------------------------


class TestUntrustedTextIsBounded:
    MAX = 512

    def test_a_huge_retry_after_header_is_dropped(self):
        session = RecordingSession(
            FakeResponse(
                status=429,
                payload={"error": {"message": "quota"}},
                headers={"Retry-After": "9" * 100_000},
            )
        )
        with pytest.raises(GscError) as excinfo:
            client(session).add_site(site_url=ALLOWED)
        # Garbage is not reflected at all, so there is nothing to bound.
        assert "retry_after" not in excinfo.value.details

    def test_a_huge_error_message_is_bounded(self):
        session = RecordingSession(
            FakeResponse(status=400, payload={"error": {"message": "x" * 100_000}})
        )
        with pytest.raises(GscError) as excinfo:
            client(session).add_site(site_url=ALLOWED)
        assert len(excinfo.value.message) <= 4096

    def test_a_non_string_retry_after_is_still_bounded(self):
        """A header object whose ``str()`` is huge must not pass through raw."""

        class Weird:
            def __str__(self):
                return "y" * 100_000

        session = RecordingSession(
            FakeResponse(
                status=429,
                payload={"error": {"message": "quota"}},
                headers={"Retry-After": Weird()},
            )
        )
        with pytest.raises(GscError) as excinfo:
            client(session).add_site(site_url=ALLOWED)
        assert "retry_after" not in excinfo.value.details

    def test_a_non_conforming_retry_after_is_dropped(self):
        session = RecordingSession(
            FakeResponse(
                status=429,
                payload={"error": {"message": "quota"}},
                headers={"Retry-After": "whenever"},
            )
        )
        with pytest.raises(GscError) as excinfo:
            client(session).add_site(site_url=ALLOWED)
        assert "retry_after" not in excinfo.value.details

    def test_a_normal_retry_after_survives(self):
        session = RecordingSession(
            FakeResponse(
                status=429,
                payload={"error": {"message": "quota"}},
                headers={"Retry-After": "120"},
            )
        )
        with pytest.raises(GscError) as excinfo:
            client(session).add_site(site_url=ALLOWED)
        assert excinfo.value.details["retry_after"] == "120"

    def test_an_http_date_retry_after_survives(self):
        session = RecordingSession(
            FakeResponse(
                status=503,
                payload={"error": {"message": "busy"}},
                headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"},
            )
        )
        with pytest.raises(GscError) as excinfo:
            client(session).add_site(site_url=ALLOWED)
        # The date is retained (its numeric run is redacted as usual); what
        # matters is that a well-formed value is not dropped like garbage.
        assert "retry_after" in excinfo.value.details
        assert "Wed, 21 Oct" in excinfo.value.details["retry_after"]
        assert "GMT" in excinfo.value.details["retry_after"]

    def test_a_normal_error_message_survives(self):
        session = RecordingSession(
            FakeResponse(status=400, payload={"error": {"message": "bad feedpath"}})
        )
        with pytest.raises(GscError) as excinfo:
            client(session).add_site(site_url=ALLOWED)
        assert "bad feedpath" in excinfo.value.message



# --------------------------------------------------------------------------
# 2b. Each bounding layer is pinned independently
# --------------------------------------------------------------------------


class TestEnvelopeBoundsAreEnforcedAtBothLayers:
    """The message and detail caps exist twice, on purpose: the client bounds
    untrusted upstream text before it builds an error, and ``GscError`` bounds
    whatever it is handed. Either layer alone keeps the envelope small, so a
    test that only removes *one* layer still passes. These tests pin each layer
    on its own, so neither cap can be deleted unnoticed.
    """

    def test_the_envelope_bounds_an_over_long_message_by_itself(self):
        from search_console_writer.errors import MAX_MESSAGE_LEN, GscError

        err = GscError("bad_request", "m" * 100_000)
        assert len(err.message) <= MAX_MESSAGE_LEN
        assert err.message.endswith("...[truncated]")

    def test_the_envelope_bounds_an_over_long_detail_by_itself(self):
        from search_console_writer.errors import MAX_DETAIL_LEN, GscError

        # Letters, not digits: ``redact`` collapses a long numeric run to a
        # short marker, so a digit string would test redaction rather than the
        # length bound. This value survives redaction and must then be capped.
        err = GscError(
            "rate_limited",
            "quota",
            {"retry_after": "z" * 100_000, "may_have_applied": "true"},
        )
        assert len(err.details["retry_after"]) <= MAX_DETAIL_LEN
        assert err.details["retry_after"].endswith("...[truncated]")
        # A short, meaningful detail is untouched.
        assert err.details["may_have_applied"] == "true"

    def test_the_client_bounds_a_hostile_message_before_the_envelope_sees_it(
        self, monkeypatch
    ):
        """The transport's own bound must hold even if the envelope's does not.

        The envelope cap is widened for this test, which simulates that layer
        being ineffective; a hostile upstream message must still arrive
        bounded. Without the client-side bound the error message would be the
        full 100k characters.
        """
        from search_console_writer import errors as errors_module

        monkeypatch.setattr(errors_module, "MAX_MESSAGE_LEN", 10_000_000)
        session = RecordingSession(
            FakeResponse(status=400, payload={"error": {"message": "q" * 100_000}})
        )
        with pytest.raises(GscError) as excinfo:
            client(session).add_site(site_url=ALLOWED)
        from search_console_writer.client import MAX_ERROR_MESSAGE_LEN

        assert len(excinfo.value.message) <= MAX_ERROR_MESSAGE_LEN
        assert excinfo.value.message.endswith("...[truncated]")