"""Structural and behavioural tests for the safety claims that are easiest to
regress: no writes, no secret logging, and no credential use without allowlist
approval.

These are deliberately paranoid. A read-only server is only read-only if
nothing in the package can construct a write, and "we never log the token" is
only true if the token never enters the code path that logs.
"""

import json
import logging
import pathlib
import re
import subprocess
import sys
from pathlib import Path

import pytest

from search_console_mcp import auth
from search_console_mcp.allowlist import PropertyAllowlist
from search_console_mcp.client import BASE_URL, INSPECTION_URL, GscClient
from search_console_mcp.errors import redact

SRC = Path(__file__).resolve().parents[1] / "src" / "search_console_mcp"
ALLOWED = "sc-domain:example.com"


class FakeResponse:
    def __init__(self, payload=None, status=200):
        self.status_code = status
        self._payload = payload or {}
        self.headers = {}

    def json(self):
        return self._payload


class RecordingSession:
    def __init__(self, responses=None):
        self.calls = []
        self._responses = list(responses or [])

    def request(self, method, url, json=None, timeout=None, **kw):
        self.calls.append({"method": method, "url": url, "json": json})
        return self._responses.pop(0) if self._responses else FakeResponse({})


class TestNoWriteCapability:
    """The read-only claim is structural, not a policy comment."""

    def test_no_mutating_http_verbs_in_the_package(self):
        forbidden = re.compile(r"""["'](PUT|PATCH|DELETE|HEAD|OPTIONS)["']""")
        for path in SRC.glob("*.py"):
            match = forbidden.search(path.read_text())
            assert match is None, f"{path.name} mentions {match.group(0) if match else ''}"

    def test_client_refuses_any_non_get_or_post_verb(self):
        client = GscClient(
            allowlist=PropertyAllowlist([ALLOWED]), session=RecordingSession()
        )
        from search_console_mcp.errors import InvalidArgumentError

        with pytest.raises(InvalidArgumentError):
            client._request("DELETE", f"{BASE_URL}/sites/x")

    def test_no_write_endpoints_are_constructed(self):
        """Sitemap/property submission paths must not appear anywhere."""
        for path in SRC.glob("*.py"):
            text = path.read_text()
            for endpoint in ("/sitemaps/submit", "/sites/", "/urlNotifications"):
                if endpoint == "/sites/":
                    continue
                assert endpoint not in text, f"{path.name} references {endpoint}"

    def test_only_the_two_documented_bases_are_used(self):
        assert BASE_URL == "https://www.googleapis.com/webmasters/v3"
        assert INSPECTION_URL == "https://searchconsole.googleapis.com/v1/urlInspection/index:inspect"

    def test_no_write_tool_names_exist(self):
        from search_console_mcp.tools import ToolRegistry

        reg = ToolRegistry(client_factory=lambda: None)
        joined = " ".join(reg.names())
        for verb in ("add", "delete", "submit", "update", "remove", "index_request"):
            assert verb not in joined


class TestNoSecretLogging:
    def test_authorization_header_never_logged(self, caplog):
        """A 403 whose body echoes the header must not log the header."""
        client = GscClient(
            allowlist=PropertyAllowlist([ALLOWED]),
            session=RecordingSession(
                [
                    FakeResponse(
                        status=403,
                        payload={
                            "error": {
                                "message": "denied Authorization: Bearer ya29.SECRETTOKEN123456"
                            }
                        },
                    )
                ]
            ),
        )
        with caplog.at_level(logging.DEBUG):
            result = client.list_sites() if False else None
            try:
                client.list_sites()
            except Exception as exc:
                client_error = exc
        assert "ya29.SECRETTOKEN123456" not in caplog.text
        assert "ya29.SECRETTOKEN123456" not in str(client_error)

    def test_package_never_references_a_token_variable(self):
        """Nothing in the package reads a token into a local it could log."""
        suspicious = re.compile(r"\b(token|access_token|bearer)\s*=\s*")
        for path in SRC.glob("*.py"):
            text = path.read_text()
            for line_no, line in enumerate(text.splitlines(), 1):
                if line.strip().startswith("#"):
                    continue
                match = suspicious.search(line)
                if match:
                    # The static-token test seam is the one allowed exception:
                    # it is read from the environment and handed straight to a
                    # requests auth object.
                    assert "static_token" in line or "STATIC_TOKEN" in line, (
                        f"{path.name}:{line_no} assigns a token-like name: {line.strip()}"
                    )

    def test_redacted_error_carries_no_google_host(self):
        client = GscClient(
            allowlist=PropertyAllowlist([ALLOWED]),
            session=RecordingSession(
                [
                    FakeResponse(
                        status=500,
                        payload={
                            "error": {
                                "message": f"failed at {BASE_URL}/sites 123456789012"
                            }
                        },
                    )
                ]
            ),
        )
        from search_console_mcp.errors import GscError

        with pytest.raises(GscError) as ei:
            client.list_sites()
        blob = json.dumps(ei.value.to_payload())
        assert "googleapis.com" not in blob
        assert "123456789012" not in blob


class TestCredentialLazinessEndToEnd:
    """In a fresh interpreter, a rejected call must never touch ADC."""

    def test_rejected_call_leaves_adc_untouched(self, tmp_path):
        script = tmp_path / "probe.py"
        script.write_text(
            """
import sys, json
import google.auth

calls = []

def tracking_default(*a, **kw):
    calls.append(1)
    raise AssertionError("ADC must not be reached for a rejected call")

google.auth.default = tracking_default

import search_console_mcp.auth as auth
auth.reset_session()

from search_console_mcp.allowlist import PropertyAllowlist
from search_console_mcp.server import build_registry

registry = build_registry(PropertyAllowlist(["sc-domain:example.com"]))

# A non-allowlisted property: must be rejected without building a client.
res = registry.call("gsc_search_analytics", {
    "site_url": "sc-domain:not-mine.example",
    "start_date": "2026-10-01",
    "end_date": "2026-10-02",
})
print(json.dumps({"is_error": res.is_error, "adc_calls": len(calls),
                  "code": json.loads(res.text)["error"]["code"]}))
"""
        )
        proc = subprocess.run(
            [sys.executable, str(script)],
            capture_output=True,
            text=True,
            env={
                "PATH": __import__("os").environ.get("PATH", ""),
                "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
            },
            timeout=120,
        )
        assert proc.returncode == 0, proc.stderr
        out = json.loads(proc.stdout.strip().splitlines()[-1])
        assert out["adc_calls"] == 0
        assert out["is_error"] is True
        assert out["code"] == "site_not_allowed"

    def test_adc_is_reached_only_when_a_valid_call_is_dispatched(self, tmp_path):
        script = tmp_path / "probe2.py"
        script.write_text(
            """
import sys, json
import google.auth

calls = []

class FakeCreds:
    scopes = ["https://www.googleapis.com/auth/webmasters.readonly"]

def tracking_default(*a, **kw):
    calls.append(1)
    raise RuntimeError("simulated: no ADC in this environment")

google.auth.default = tracking_default

import search_console_mcp.auth as auth
auth.reset_session()
from search_console_mcp.allowlist import PropertyAllowlist
from search_console_mcp.server import build_registry

registry = build_registry(PropertyAllowlist(["sc-domain:example.com"]))
res = registry.call("gsc_search_analytics", {
    "site_url": "sc-domain:example.com",
    "start_date": "2026-10-01",
    "end_date": "2026-10-02",
})
print(json.dumps({"adc_calls": len(calls), "code": json.loads(res.text).get("error", {}).get("code")}))
"""
        )
        proc = subprocess.run(
            [sys.executable, str(script)],
            capture_output=True,
            text=True,
            env={
                "PATH": __import__("os").environ.get("PATH", ""),
                "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
            },
            timeout=120,
        )
        assert proc.returncode == 0, proc.stderr
        out = json.loads(proc.stdout.strip().splitlines()[-1])
        # A valid call *does* reach ADC, and its failure is reported cleanly.
        assert out["adc_calls"] == 1
        assert out["code"] == "upstream_error"


class TestRedactionShapes:
    """Credential shapes that previously survived ``redact`` in the clear.

    The keyword rule eats a value only after its own keyword, so a value that
    *itself* contains the scheme word (``Authorization: Bearer <token>``) left
    the token behind: the rule consumed ``Bearer`` as the value and stopped.
    HTTP Basic credentials carry no keyword at all and were never matched.
    """

    @pytest.mark.parametrize(
        "text,secret",
        [
            ("Authorization: Bearer TOKENVALUE123456", "TOKENVALUE123456"),
            ("authorization=bearer TOKENVALUE123456", "TOKENVALUE123456"),
            ("Bearer TOKENVALUE123456", "TOKENVALUE123456"),
            ("denied for Bearer eyJhbGciOiJIUzI1NiJ9.abc.def", "eyJhbGciOiJIUzI1NiJ9"),
            ("Authorization: Basic dXNlcjpwYXNzd29yZA==", "dXNlcjpwYXNzd29yZA=="),
            ("Basic dXNlcjpwYXNzd29yZA==", "dXNlcjpwYXNzd29yZA=="),
            ("proxy-authorization: Basic QWxhZGRpbjpvcGVuIHNlc2FtZQ==", "QWxhZGRpbjpvcGVuIHNlc2FtZQ=="),
        ],
    )
    def test_authorization_schemes_are_redacted(self, text, secret):
        assert secret not in redact(text)
        assert "[redacted-secret]" in redact(text)

    @pytest.mark.parametrize(
        "text,secret",
        [
            ("token=abc123def456", "abc123def456"),
            ("secret: hunter2secret", "hunter2secret"),
            ("password=correcthorse", "correcthorse"),
            ("client_secret: s3cr3tvalue", "s3cr3tvalue"),
            ("x-api-key: abcdef123456", "abcdef123456"),
        ],
    )
    def test_broader_keyword_set_is_redacted(self, text, secret):
        assert secret not in redact(text)

    def test_redaction_is_idempotent(self):
        once = redact("Authorization: Bearer TOKENVALUE123456")
        assert redact(once) == once

    def test_ordinary_prose_is_not_mangled(self):
        text = "permission denied; confirm the Google account is added as a user"
        assert redact(text) == text

    @pytest.mark.parametrize(
        "text",
        [
            "the configured credential was not accepted for this operation",
            "token budget exceeded for this project",
            "secret manager returned an error",
            "password policy rejected the request",
            "invalid credentials",
        ],
    )
    def test_prose_around_a_keyword_word_is_left_readable(self, text):
        # These words appear in our own error messages and in upstream prose; a
        # rule that ate the next word on a bare space would corrupt them.
        assert redact(text) == text

    @pytest.mark.parametrize(
        "text,secret",
        [
            ("invalid access_token abc123", "abc123"),
            ("client_secret is s3cr3t", "s3cr3t"),
            ("refresh_token: 1//0abcdef", "1//0abcdef"),
        ],
    )
    def test_strong_keywords_eat_a_space_separated_value(self, text, secret):
        assert secret not in redact(text)


class TestMcpErrorSignalling:
    """A rejected tool action must arrive as an MCP error, not a success.

    The registry already marked every error envelope ``is_error=True``, but the
    server layer returned only the envelope text and let the SDK default
    ``isError`` to false. The middleware rejections (unknown tool/argument) set
    it correctly, so callers saw an inconsistent signal for the same class of
    failure -- and an LLM client that branches on ``isError`` treated a refused
    property as a successful call.
    """

    def _server(self):
        from search_console_mcp.allowlist import PropertyAllowlist
        from search_console_mcp.server import build_registry, create_server

        allowlist = PropertyAllowlist(["sc-domain:example.com"])
        registry = build_registry(allowlist)
        return create_server(allowlist=allowlist, registry=registry)

    def test_registry_rejection_is_marked_as_error_over_mcp(self):
        import anyio
        from mcp.types import CallToolResult

        server = self._server()

        async def call():
            return await server.call_tool(
                "gsc_search_analytics",
                {
                    "site_url": "sc-domain:not-mine.example",
                    "start_date": "2026-10-01",
                    "end_date": "2026-10-02",
                },
            )

        result = anyio.run(call)
        assert isinstance(result, CallToolResult)
        assert result.is_error is True
        assert json.loads(result.content[0].text)["error"]["code"] == "site_not_allowed"

    def test_invalid_argument_is_marked_as_error_over_mcp(self):
        import anyio

        server = self._server()

        async def call():
            return await server.call_tool(
                "gsc_search_analytics",
                {
                    "site_url": "sc-domain:example.com",
                    "start_date": "not-a-date",
                    "end_date": "2026-10-02",
                },
            )

        result = anyio.run(call)
        assert result.is_error is True

    def test_successful_call_is_not_an_error(self):
        import anyio

        from search_console_mcp.client import GscClient
        from search_console_mcp.server import create_server
        from search_console_mcp.tools import ToolRegistry

        class FakeResponse:
            status_code = 200
            headers = {}

            def json(self):
                return {"siteEntry": []}

        class FakeSession:
            def request(self, *a, **kw):
                return FakeResponse()

        allowlist = PropertyAllowlist(["sc-domain:example.com"])
        client = GscClient(allowlist=allowlist, session=FakeSession())
        registry = ToolRegistry(client_factory=lambda: client, allowlist=allowlist)
        server = create_server(allowlist=allowlist, registry=registry)

        result = anyio.run(server.call_tool, "gsc_list_sites", {})
        assert result.is_error is False
        assert json.loads(result.content[0].text)["ok"] is True


class TestNonFiniteJson:
    """A non-finite upstream number must not produce invalid JSON on the wire.

    ``json.dumps`` writes bare ``NaN``/``Infinity`` tokens by default, which
    Python can read back but strict JSON parsers (and therefore many MCP
    clients) reject. A float field from the API must be dropped rather than
    serialised as a non-token.
    """

    def test_result_envelope_is_strict_json(self):
        from search_console_mcp.client import GscClient
        from search_console_mcp.tools import ToolRegistry

        class FakeResponse:
            status_code = 200
            headers = {}

            def json(self):
                return {
                    "rows": [
                        {
                            "keys": ["k"],
                            "clicks": float("nan"),
                            "impressions": float("inf"),
                            "ctr": 0.1,
                            "position": float("-inf"),
                        }
                    ]
                }

        class FakeSession:
            def request(self, *a, **kw):
                return FakeResponse()

        allowlist = PropertyAllowlist(["sc-domain:example.com"])
        client = GscClient(allowlist=allowlist, session=FakeSession())
        registry = ToolRegistry(client_factory=lambda: client, allowlist=allowlist)
        result = registry.call(
            "gsc_search_analytics",
            {
                "site_url": "sc-domain:example.com",
                "start_date": "2026-10-01",
                "end_date": "2026-10-02",
            },
        )
        # Strict: raises on NaN/Infinity rather than accepting them.
        parsed = json.loads(result.text, parse_constant=_reject_constant)
        row = parsed["rows"][0]
        assert row["clicks"] is None
        assert row["impressions"] is None
        assert row["position"] is None
        assert row["ctr"] == 0.1


def _reject_constant(token):
    raise AssertionError(f"non-finite JSON constant emitted: {token}")


class TestPackagingPins:
    def test_runtime_dependencies_are_pinned_exactly(self):
        text = (Path(__file__).resolve().parents[1] / "requirements.txt").read_text()
        assert "mcp==2.3.0" in text
        assert "google-auth==2.55.1" in text

    def test_scope_is_the_read_only_scope(self):
        assert auth.SCOPES == ("https://www.googleapis.com/auth/webmasters.readonly",)

    def test_project_metadata_matches_runtime_pins(self):
        text = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text()
        assert "mcp==2.3.0" in text
        assert "google-auth==2.55.1" in text