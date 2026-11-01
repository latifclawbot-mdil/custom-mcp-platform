"""Tests for the MCP tool layer: schemas, envelopes, and error conversion.

The tool functions are exercised directly (in-process) here; the real stdio
handshake is covered in test_stdio_mcp.py.
"""

import json

import pytest

from search_console_mcp import tools as tools_mod
from search_console_mcp.allowlist import PropertyAllowlist
from search_console_mcp.client import GscClient
from search_console_mcp.errors import EmptyAllowlistError


class FakeResponse:
    def __init__(self, status_code=200, payload=None, headers=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.headers = headers or {}

    def json(self):
        return self._payload


class RecordingSession:
    def __init__(self, responses=None):
        self.calls = []
        self._responses = list(responses or [])

    def request(self, method, url, json=None, timeout=None, **kw):
        self.calls.append({"method": method, "url": url, "json": json, "timeout": timeout})
        return self._responses.pop(0) if self._responses else FakeResponse(200, {})


ALLOWED = "sc-domain:example.com"


def make_registry(responses=None, sites=(ALLOWED,)):
    """A registry whose client is backed by a recording session."""
    session = RecordingSession(responses)
    allowlist = PropertyAllowlist(sites)
    client = GscClient(allowlist=allowlist, session=session)
    reg = tools_mod.ToolRegistry(
        client_factory=lambda: client, allowlist=allowlist
    )
    return reg, session


class TestRegistration:
    def test_exactly_four_tools_are_registered(self):
        reg, _ = make_registry()
        assert sorted(reg.names()) == [
            "gsc_inspect_url",
            "gsc_list_sitemaps",
            "gsc_list_sites",
            "gsc_search_analytics",
        ]

    def test_every_tool_has_a_description(self):
        reg, _ = make_registry()
        for spec in reg.specs():
            assert spec.description and len(spec.description) > 40

    def test_every_tool_declares_required_arguments(self):
        reg, _ = make_registry()
        for spec in reg.specs():
            assert isinstance(spec.required, tuple)

    def test_no_tool_schema_permits_arbitrary_keys(self):
        """A tool that accepted free-form kwargs could smuggle a host or token."""
        reg, _ = make_registry()
        for spec in reg.specs():
            assert spec.allows_extra is False

    def test_write_like_tools_are_absent(self):
        reg, _ = make_registry()
        names = " ".join(reg.names())
        for forbidden in ("add", "delete", "submit", "put", "update", "remove", "request_index"):
            assert forbidden not in names

    def test_empty_allowlist_refuses_construction(self):
        with pytest.raises(EmptyAllowlistError):
            tools_mod.ToolRegistry(client_factory=lambda: GscClient(
                allowlist=PropertyAllowlist([ALLOWED]), session=RecordingSession()
            ), allowlist=PropertyAllowlist([]))


class TestEnvelope:
    def test_success_envelope_is_json_text(self):
        reg, _ = make_registry(responses=[FakeResponse(200, {"siteEntry": []})])
        result = reg.call("gsc_list_sites", {})
        assert result.is_error is False
        payload = json.loads(result.text)
        assert payload["ok"] is True
        assert payload["tool"] == "gsc_list_sites"

    def test_error_envelope_is_json_text_and_marked_error(self):
        reg, _ = make_registry(responses=[FakeResponse(403, {"error": {"message": "nope"}})])
        result = reg.call("gsc_list_sites", {})
        assert result.is_error is True
        payload = json.loads(result.text)
        assert payload["ok"] is False
        assert payload["error"]["code"] == "unauthorized"

    def test_disallowed_site_is_an_error_envelope_not_a_crash(self):
        reg, session = make_registry()
        result = reg.call("gsc_search_analytics", {
            "site_url": "sc-domain:evil.com",
            "start_date": "2026-10-01",
            "end_date": "2026-10-02",
        })
        assert result.is_error is True
        assert json.loads(result.text)["error"]["code"] == "site_not_allowed"
        assert session.calls == []

    def test_missing_required_argument_is_rejected_cleanly(self):
        reg, _ = make_registry()
        result = reg.call("gsc_search_analytics", {"site_url": ALLOWED})
        assert result.is_error is True
        assert json.loads(result.text)["error"]["code"] == "invalid_argument"

    def test_unknown_tool_is_an_error_envelope(self):
        reg, _ = make_registry()
        result = reg.call("gsc_delete_site", {})
        assert result.is_error is True
        assert json.loads(result.text)["error"]["code"] == "invalid_argument"

    def test_unexpected_exception_does_not_leak_a_traceback(self):
        reg, _ = make_registry()
        reg._client_factory = lambda: (_ for _ in ()).throw(RuntimeError("boom /home/secret"))
        result = reg.call("gsc_list_sites", {})
        assert result.is_error is True
        text = result.text
        assert "Traceback" not in text
        assert "/home/secret" not in text
        assert json.loads(text)["error"]["code"] == "upstream_error"

    def test_secrets_never_appear_in_any_error_output(self):
        leaky = {"error": {"message": "bad token ya29.SECRETVALUE123456789 for 123456789012"}}
        reg, _ = make_registry(responses=[FakeResponse(500, leaky)])
        result = reg.call("gsc_list_sites", {})
        assert "ya29.SECRETVALUE123456789" not in result.text
        assert "123456789012" not in result.text


class TestArgumentTypes:
    def test_string_where_int_expected_is_rejected(self):
        reg, _ = make_registry()
        result = reg.call("gsc_search_analytics", {
            "site_url": ALLOWED,
            "start_date": "2026-10-01",
            "end_date": "2026-10-02",
            "row_limit": "1000",
        })
        assert result.is_error is True
        assert json.loads(result.text)["error"]["code"] == "invalid_argument"

    def test_bool_where_int_expected_is_rejected(self):
        reg, _ = make_registry()
        result = reg.call("gsc_search_analytics", {
            "site_url": ALLOWED,
            "start_date": "2026-10-01",
            "end_date": "2026-10-02",
            "row_limit": True,
        })
        assert result.is_error is True

    def test_dimensions_must_be_a_list(self):
        reg, _ = make_registry()
        result = reg.call("gsc_search_analytics", {
            "site_url": ALLOWED,
            "start_date": "2026-10-01",
            "end_date": "2026-10-02",
            "dimensions": "query",
        })
        assert result.is_error is True

    def test_non_dict_arguments_rejected(self):
        reg, _ = make_registry()
        result = reg.call("gsc_list_sites", None)  # type: ignore[arg-type]
        assert result.is_error is True

    def test_extra_arguments_are_rejected(self):
        """Unknown keys are refused so no undocumented parameter can reach a URL."""
        reg, _ = make_registry()
        result = reg.call("gsc_list_sites", {"host": "https://evil.example"})
        assert result.is_error is True
        assert json.loads(result.text)["error"]["code"] == "invalid_argument"


class TestToolRouting:
    def test_search_analytics_passes_all_arguments_through(self):
        reg, session = make_registry(responses=[FakeResponse(200, {"rows": []})])
        reg.call("gsc_search_analytics", {
            "site_url": ALLOWED,
            "start_date": "2026-10-01",
            "end_date": "2026-10-02",
            "dimensions": ["query"],
            "row_limit": 5,
            "start_row": 10,
            "search_type": "web",
            "aggregation": "byProperty",
            "data_state": "all",
            "filters": [{"dimension": "country", "operator": "equals", "expression": "ind"}],
        })
        body = session.calls[0]["json"]
        assert body["rowLimit"] == 5
        assert body["startRow"] == 10
        assert body["dimensions"] == ["query"]
        assert body["type"] == "web"
        assert body["aggregationType"] == "byProperty"
        assert body["dataState"] == "all"
        assert body["dimensionFilterGroups"][0]["filters"][0]["expression"] == "ind"

    def test_inspect_url_requires_both_arguments(self):
        reg, _ = make_registry()
        for args in (
            {"site_url": ALLOWED},
            {"url": "https://example.com/"},
            {},
        ):
            result = reg.call("gsc_inspect_url", args)
            assert result.is_error is True
            assert json.loads(result.text)["error"]["code"] == "invalid_argument"

    def test_client_is_built_lazily_per_call(self):
        """A rejected call must not construct a client (and so must not touch ADC)."""
        builds = []

        def factory():
            builds.append(1)
            return GscClient(allowlist=PropertyAllowlist([ALLOWED]), session=RecordingSession())

        reg = tools_mod.ToolRegistry(
            client_factory=factory, allowlist=PropertyAllowlist([ALLOWED])
        )
        reg.call("gsc_search_analytics", {
            "site_url": "sc-domain:evil.com",
            "start_date": "2026-10-01",
            "end_date": "2026-10-02",
        })
        assert builds == []

    def test_bounds_failure_also_avoids_building_a_client(self):
        """Validation that can be done without a credential must be done first."""
        builds = []

        def factory():
            builds.append(1)
            return GscClient(allowlist=PropertyAllowlist([ALLOWED]), session=RecordingSession())

        reg = tools_mod.ToolRegistry(
            client_factory=factory, allowlist=PropertyAllowlist([ALLOWED])
        )
        result = reg.call("gsc_search_analytics", {
            "site_url": ALLOWED,
            "start_date": "2026-10-01",
            "end_date": "2026-10-02",
            "row_limit": 999999,
        })
        assert result.is_error is True
        assert json.loads(result.text)["error"]["code"] == "invalid_argument"
        assert builds == []

    def test_out_of_scope_inspect_url_avoids_building_a_client(self):
        builds = []

        def factory():
            builds.append(1)
            return GscClient(allowlist=PropertyAllowlist([ALLOWED]), session=RecordingSession())

        reg = tools_mod.ToolRegistry(
            client_factory=factory, allowlist=PropertyAllowlist([ALLOWED])
        )
        result = reg.call("gsc_inspect_url", {
            "site_url": ALLOWED,
            "url": "not-a-url",
        })
        assert result.is_error is True
        assert builds == []