"""Fail-closed allowlist tests for the write server.

The write server's allowlist is the only thing standing between an MCP caller
and a property mutation on an account that may hold many properties. It must be
exact, normalising only the two spellings Google itself treats as the same
thing, and it must never widen a match into a prefix or substring.
"""

from __future__ import annotations

import pytest

from search_console_writer.allowlist import PropertyAllowlist, normalize_property
from search_console_writer.errors import EmptyAllowlistError, SiteNotAllowedError


class TestNormalisation:
    def test_domain_property_is_lowercased(self):
        assert normalize_property("sc-domain:Example.COM") == "sc-domain:example.com"

    def test_domain_property_prefix_case_is_canonicalised(self):
        assert normalize_property("SC-DOMAIN:example.com") == "sc-domain:example.com"

    def test_url_prefix_gains_a_trailing_slash(self):
        assert normalize_property("https://www.example.com") == "https://www.example.com/"

    def test_url_prefix_keeps_its_path_case(self):
        assert normalize_property("https://www.example.com/Blog") == "https://www.example.com/Blog/"

    def test_url_prefix_with_trailing_slash_is_unchanged(self):
        assert normalize_property("https://www.example.com/") == "https://www.example.com/"

    def test_empty_is_rejected(self):
        with pytest.raises(EmptyAllowlistError):
            normalize_property("   ")


class TestConstruction:
    def test_empty_list_raises(self):
        with pytest.raises(EmptyAllowlistError):
            PropertyAllowlist([])

    def test_only_blank_entries_raise(self):
        with pytest.raises(EmptyAllowlistError):
            PropertyAllowlist(["", "  ", None])

    def test_duplicates_are_collapsed(self):
        allow = PropertyAllowlist(["sc-domain:example.com", "SC-DOMAIN:example.com"])
        assert allow.entries == ("sc-domain:example.com",)


class TestExactMatching:
    def test_exact_domain_property_matches(self):
        allow = PropertyAllowlist(["sc-domain:example.com"])
        assert allow.contains("sc-domain:example.com") is True

    def test_subdomain_is_not_the_same_property(self):
        allow = PropertyAllowlist(["sc-domain:example.com"])
        assert allow.contains("sc-domain:sub.example.com") is False

    def test_suffix_appended_to_host_is_not_the_same_property(self):
        allow = PropertyAllowlist(["sc-domain:example.com"])
        assert allow.contains("sc-domain:example.com.evil.example") is False

    def test_path_appended_to_a_domain_property_is_not_allowed(self):
        allow = PropertyAllowlist(["sc-domain:example.com"])
        assert allow.contains("sc-domain:example.com/extra") is False

    def test_url_prefix_does_not_match_a_longer_path(self):
        allow = PropertyAllowlist(["https://www.example.com/blog/"])
        assert allow.contains("https://www.example.com/blogroll/") is False

    def test_url_prefix_does_not_match_another_host(self):
        allow = PropertyAllowlist(["https://www.example.com/"])
        assert allow.contains("https://www.example.com.evil.example/") is False

    def test_non_string_is_denied(self):
        allow = PropertyAllowlist(["sc-domain:example.com"])
        assert allow.contains(None) is False
        assert allow.contains(["sc-domain:example.com"]) is False

    def test_require_returns_the_normalised_form(self):
        allow = PropertyAllowlist(["https://www.example.com"])
        assert allow.require("https://www.example.com") == "https://www.example.com/"

    def test_require_raises_for_a_non_allowlisted_property(self):
        allow = PropertyAllowlist(["sc-domain:example.com"])
        with pytest.raises(SiteNotAllowedError) as excinfo:
            allow.require("sc-domain:not-mine.example")
        assert excinfo.value.code == "site_not_allowed"
        assert excinfo.value.details["reason"] == "not_allowlisted"

    def test_require_raises_for_an_empty_value(self):
        allow = PropertyAllowlist(["sc-domain:example.com"])
        with pytest.raises(SiteNotAllowedError):
            allow.require("")

    def test_from_env_reads_the_write_specific_variable(self):
        allow = PropertyAllowlist.from_env(
            {"GSC_WRITE_ALLOWED_SITES": "sc-domain:example.com,https://www.example.com/"}
        )
        assert allow.entries == ("sc-domain:example.com", "https://www.example.com/")

    def test_from_env_without_the_variable_raises(self):
        with pytest.raises(EmptyAllowlistError):
            PropertyAllowlist.from_env({})
