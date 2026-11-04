"""Tool registry: the four explicit write tools and their validation order.

The registry is plain Python, deliberately independent of the MCP SDK, so the
security decisions are testable without a transport. Two orderings matter and
are asserted here:

1. the property allowlist is checked **before** a client is constructed, so a
   refused call never acquires a credential;
2. pure argument validation also runs **before** a client is constructed.
"""

from __future__ import annotations

import json

import pytest

from search_console_writer.allowlist import PropertyAllowlist
from search_console_writer.tools import ToolRegistry

ALLOWED = "sc-domain:example.com"
ALLOWED_PREFIX = "https://www.example.com/"
SITEMAP = "https://www.example.com/sitemap.xml"


class SpyClient:
    """Records calls and returns a fixed success envelope."""

    def __init__(self, log):
        self._log = log

    def add_site(self, *, site_url):
        self._log.append(("add_site", site_url))
        return {"ok": True, "operation": "sites.add", "site_url": site_url, "applied": True}

    def delete_site(self, *, site_url):
        self._log.append(("delete_site", site_url))
        return {"ok": True, "operation": "sites.delete", "site_url": site_url, "applied": True}

    def submit_sitemap(self, *, site_url, sitemap_url):
        self._log.append(("submit_sitemap", site_url, sitemap_url))
        return {
            "ok": True,
            "operation": "sitemaps.submit",
            "site_url": site_url,
            "sitemap_url": sitemap_url,
            "applied": True,
        }

    def delete_sitemap(self, *, site_url, sitemap_url):
        self._log.append(("delete_sitemap", site_url, sitemap_url))
        return {
            "ok": True,
            "operation": "sitemaps.delete",
            "site_url": site_url,
            "sitemap_url": sitemap_url,
            "applied": True,
        }


def registry_with_spy():
    log = []
    created = []

    def factory():
        created.append(1)
        return SpyClient(log)

    reg = ToolRegistry(
        client_factory=factory,
        allowlist=PropertyAllowlist([ALLOWED, ALLOWED_PREFIX]),
    )
    return reg, log, created


def parse(result):
    return json.loads(result.text)


class TestSurface:
    def test_exactly_the_four_official_write_tools(self):
        reg, _, _ = registry_with_spy()
        assert sorted(reg.names()) == [
            "gsc_add_site",
            "gsc_delete_site",
            "gsc_delete_sitemap",
            "gsc_submit_sitemap",
        ]

    def test_no_generic_request_tool_exists(self):
        reg, _, _ = registry_with_spy()
        for name in reg.names():
            assert "request" not in name
            assert "http" not in name
            assert "raw" not in name

    def test_every_tool_requires_a_property(self):
        reg, _, _ = registry_with_spy()
        for spec in reg.specs():
            assert "site_url" in spec.required
            assert spec.has_property_argument is True

    def test_sitemap_tools_require_a_sitemap_url(self):
        reg, _, _ = registry_with_spy()
        assert reg.get("gsc_submit_sitemap").required == ("site_url", "sitemap_url")
        assert reg.get("gsc_delete_sitemap").required == ("site_url", "sitemap_url")

    def test_schemas_are_closed_objects(self):
        reg, _, _ = registry_with_spy()
        for spec in reg.specs():
            schema = spec.input_schema()
            assert schema["additionalProperties"] is False
            assert schema["type"] == "object"
            assert set(schema["required"]) == set(spec.required)

    def test_descriptions_say_the_operation_writes(self):
        reg, _, _ = registry_with_spy()
        for spec in reg.specs():
            assert "writ" in spec.description.lower() or "mutat" in spec.description.lower()


class TestDispatch:
    def test_add_site_dispatches(self):
        reg, log, created = registry_with_spy()
        result = reg.call("gsc_add_site", {"site_url": ALLOWED})
        assert result.is_error is False
        assert parse(result)["operation"] == "sites.add"
        assert log == [("add_site", ALLOWED)]
        assert len(created) == 1

    def test_delete_site_dispatches(self):
        reg, log, _ = registry_with_spy()
        result = reg.call("gsc_delete_site", {"site_url": ALLOWED})
        assert result.is_error is False
        assert log == [("delete_site", ALLOWED)]

    def test_submit_sitemap_dispatches(self):
        reg, log, _ = registry_with_spy()
        result = reg.call("gsc_submit_sitemap", {"site_url": ALLOWED, "sitemap_url": SITEMAP})
        assert result.is_error is False
        assert log == [("submit_sitemap", ALLOWED, SITEMAP)]

    def test_delete_sitemap_dispatches(self):
        reg, log, _ = registry_with_spy()
        result = reg.call("gsc_delete_sitemap", {"site_url": ALLOWED, "sitemap_url": SITEMAP})
        assert result.is_error is False
        assert log == [("delete_sitemap", ALLOWED, SITEMAP)]


class TestValidationBeforeCredential:
    def test_non_allowlisted_property_never_builds_a_client(self):
        reg, log, created = registry_with_spy()
        result = reg.call("gsc_add_site", {"site_url": "sc-domain:not-mine.example"})
        payload = parse(result)
        assert payload["ok"] is False
        assert payload["error"]["code"] == "site_not_allowed"
        assert created == []
        assert log == []

    def test_out_of_scope_sitemap_never_builds_a_client(self):
        reg, log, created = registry_with_spy()
        result = reg.call(
            "gsc_submit_sitemap",
            {"site_url": ALLOWED, "sitemap_url": "https://evil.example/sitemap.xml"},
        )
        payload = parse(result)
        assert payload["error"]["code"] == "invalid_argument"
        assert created == []
        assert log == []

    def test_malformed_property_never_builds_a_client(self):
        reg, log, created = registry_with_spy()
        result = reg.call("gsc_delete_site", {"site_url": "https://user:pw@example.com/"})
        assert parse(result)["error"]["code"] == "invalid_argument"
        assert created == []

    def test_unknown_argument_never_builds_a_client(self):
        reg, log, created = registry_with_spy()
        result = reg.call("gsc_add_site", {"site_url": ALLOWED, "host": "https://evil.example"})
        assert parse(result)["error"]["code"] == "invalid_argument"
        assert created == []

    def test_missing_required_argument_never_builds_a_client(self):
        reg, log, created = registry_with_spy()
        result = reg.call("gsc_submit_sitemap", {"site_url": ALLOWED})
        assert parse(result)["error"]["code"] == "invalid_argument"
        assert created == []

    def test_unknown_tool_is_an_error_envelope(self):
        reg, log, created = registry_with_spy()
        result = reg.call("gsc_make_request", {"url": "https://evil.example"})
        assert result.is_error is True
        assert parse(result)["error"]["code"] == "invalid_argument"
        assert created == []


class TestEnvelope:
    def test_error_envelope_is_strict_json_and_redacted(self):
        reg, _, _ = registry_with_spy()
        result = reg.call("gsc_add_site", {"site_url": "sc-domain:not-mine.example"})
        parsed = json.loads(result.text, parse_constant=lambda t: (_ for _ in ()).throw(AssertionError(t)))
        assert parsed["ok"] is False
        assert parsed["tool"] == "gsc_add_site"

    def test_unexpected_client_failure_becomes_a_clean_error(self):
        class Exploding:
            def add_site(self, *, site_url):
                raise RuntimeError("boom with ya29.SECRET and https://internal.example/x")

        reg = ToolRegistry(
            client_factory=lambda: Exploding(),
            allowlist=PropertyAllowlist([ALLOWED]),
        )
        result = reg.call("gsc_add_site", {"site_url": ALLOWED})
        assert result.is_error is True
        payload = parse(result)
        assert payload["error"]["code"] == "upstream_error"
        assert "ya29.SECRET" not in result.text
        assert "internal.example" not in result.text

    def test_empty_allowlist_fails_at_construction(self):
        from search_console_writer.errors import EmptyAllowlistError

        with pytest.raises(EmptyAllowlistError):
            ToolRegistry(client_factory=lambda: None, allowlist=PropertyAllowlist([]))


class TestArgumentsAreAnObject:
    @pytest.mark.parametrize("bad", [None, [], "site_url", 5])
    def test_non_object_arguments_are_rejected(self, bad):
        reg, log, created = registry_with_spy()
        result = reg.call("gsc_add_site", bad)
        assert result.is_error is True
        assert created == []