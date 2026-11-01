import pytest

from search_console_mcp.errors import GscError, redact


class TestAllowlist:
    def test_empty_allowlist_refuses_to_construct(self):
        from search_console_mcp.allowlist import PropertyAllowlist, EmptyAllowlistError

        with pytest.raises(EmptyAllowlistError):
            PropertyAllowlist([])

    def test_whitespace_only_entries_are_not_properties(self):
        from search_console_mcp.allowlist import PropertyAllowlist, EmptyAllowlistError

        with pytest.raises(EmptyAllowlistError):
            PropertyAllowlist(["", "  ", "\t"])

    def test_from_env_requires_the_variable(self, monkeypatch):
        from search_console_mcp.allowlist import PropertyAllowlist, EmptyAllowlistError

        monkeypatch.delenv("GSC_ALLOWED_SITES", raising=False)
        with pytest.raises(EmptyAllowlistError):
            PropertyAllowlist.from_env({})

    def test_from_env_empty_string_fails_closed(self, monkeypatch):
        from search_console_mcp.allowlist import PropertyAllowlist, EmptyAllowlistError

        with pytest.raises(EmptyAllowlistError):
            PropertyAllowlist.from_env({"GSC_ALLOWED_SITES": ""})

    def test_exact_match_allowed(self):
        from search_console_mcp.allowlist import PropertyAllowlist

        a = PropertyAllowlist(["sc-domain:example.com"])
        assert a.contains("sc-domain:example.com") is True

    def test_unknown_property_denied(self):
        from search_console_mcp.allowlist import PropertyAllowlist

        a = PropertyAllowlist(["sc-domain:example.com"])
        assert a.contains("sc-domain:evil.com") is False

    @pytest.mark.parametrize(
        "candidate",
        [
            "sc-domain:example.com.evil.com",
            "sc-domain:example.com#",
            "sc-domain:example.com/../other",
            "sc-domain:example.com/extra",
            "sc-domain:example.com:8443",
            "sc-domainx:example.com",
            "example.com",
            "sc-domain:example.com2",
        ],
    )
    def test_near_misses_denied(self, candidate):
        from search_console_mcp.allowlist import PropertyAllowlist

        a = PropertyAllowlist(["sc-domain:example.com"])
        assert a.contains(candidate) is False

    def test_domain_property_normalizes_to_lowercase(self):
        from search_console_mcp.allowlist import PropertyAllowlist

        a = PropertyAllowlist(["sc-domain:Example.COM"])
        assert a.contains("sc-domain:example.com") is True

    def test_url_prefix_gains_trailing_slash(self):
        from search_console_mcp.allowlist import PropertyAllowlist

        a = PropertyAllowlist(["https://www.example.com"])
        assert a.contains("https://www.example.com/") is True

    def test_url_prefix_path_is_case_sensitive(self):
        from search_console_mcp.allowlist import PropertyAllowlist

        a = PropertyAllowlist(["https://www.example.com/Blog/"])
        assert a.contains("https://www.example.com/blog/") is False

    def test_substring_of_allowed_property_is_not_allowed(self):
        """A property that merely starts with an allowed one must not pass."""
        from search_console_mcp.allowlist import PropertyAllowlist

        a = PropertyAllowlist(["https://www.example.com/"])
        assert a.contains("https://www.example.com.evil.com/") is False

    def test_non_string_candidate_denied(self):
        from search_console_mcp.allowlist import PropertyAllowlist

        a = PropertyAllowlist(["sc-domain:example.com"])
        for bad in (None, 1, [], {}):
            assert a.contains(bad) is False  # type: ignore[arg-type]

    def test_entries_are_listed_sorted_and_stable(self):
        from search_console_mcp.allowlist import PropertyAllowlist

        a = PropertyAllowlist(["https://b.example/", "sc-domain:a.com"])
        assert a.entries == ("https://b.example/", "sc-domain:a.com")


class TestPropertyEncoding:
    def test_domain_property_percent_encodes_colon(self):
        from search_console_mcp.encoding import encode_property

        assert encode_property("sc-domain:example.com") == "sc-domain%3Aexample.com"

    def test_url_prefix_property_encodes_slashes(self):
        from search_console_mcp.encoding import encode_property

        assert (
            encode_property("https://www.example.com/")
            == "https%3A%2F%2Fwww.example.com%2F"
        )

    def test_encoding_leaves_no_raw_slash(self):
        from search_console_mcp.encoding import encode_property

        out = encode_property("https://www.example.com/a/b/")
        assert "/" not in out

    def test_encoding_percent_escapes_traversal_slashes(self):
        from search_console_mcp.encoding import encode_property

        # ".." survives as literal dots (harmless inside a single segment) but
        # must never be separated by a raw slash, which is what would make it
        # traversable. Assert the separator is gone, not the dots.
        out = encode_property("sc-domain:x.com/../../sites")
        assert "/" not in out
        assert "%2F" in out

    def test_encoding_output_is_a_single_path_segment(self):
        from search_console_mcp.encoding import encode_property

        for prop in (
            "https://www.example.com/a/b/c/",
            "sc-domain:x.com/../../sites",
            "https://x.com/?a=1#frag",
        ):
            out = encode_property(prop)
            assert "/" not in out
            assert "?" not in out
            assert "#" not in out

    def test_encoding_neutralizes_query_and_fragment(self):
        from search_console_mcp.encoding import encode_property

        out = encode_property("sc-domain:x.com?a=b#c")
        assert "?" not in out and "#" not in out

    def test_non_string_rejected(self):
        from search_console_mcp.encoding import encode_property
        from search_console_mcp.errors import GscError

        with pytest.raises(GscError) as ei:
            encode_property(None)  # type: ignore[arg-type]
        assert ei.value.code == "invalid_argument"


class TestRedaction:
    def test_long_numeric_ids_are_redacted(self):
        assert "123456789012" not in redact("error for 123456789012 here")

    def test_short_numbers_are_kept(self):
        assert "404" in redact("status 404")

    def test_tokenish_words_are_redacted(self):
        # A realistic OAuth token plus a long numeric principal id, as they
        # would appear in an upstream error body we failed to parse.
        secret = "ya29.a0AfH6SMBexampleTOKENvalue"
        out = redact(f"Authorization: Bearer {secret} principal 123456789012345")
        assert secret not in out
        assert "123456789012345" not in out
        assert "[redacted-secret]" in out

    def test_access_token_literal_is_redacted(self):
        out = redact('{"error":"invalid access_token abc123"}')
        assert "abc123" not in out

    def test_urls_are_redacted(self):
        out = redact("failed calling https://www.googleapis.com/webmasters/v3/sites")
        assert "googleapis.com" not in out
        assert "[redacted-url]" in out

    def test_private_key_material_is_redacted(self):
        out = redact("-----BEGIN PRIVATE KEY----- MIIEvQIBADANBg -----END PRIVATE KEY-----")
        assert "MIIEvQIBADANBg" not in out

    def test_plain_message_survives(self):
        assert redact("property not allowed") == "property not allowed"

    def test_none_becomes_empty(self):
        assert redact(None) == ""


class TestErrorEnvelope:
    def test_gsc_error_carries_stable_code(self):
        err = GscError("invalid_argument", "bad date")
        assert err.code == "invalid_argument"
        assert err.message == "bad date"
        assert err.to_payload() == {
            "ok": False,
            "error": {"code": "invalid_argument", "message": "bad date"},
        }

    def test_error_message_is_redacted_on_construction(self):
        err = GscError("upstream_error", "boom 987654321098")
        assert "987654321098" not in err.message

    def test_details_are_preserved_and_redacted(self):
        err = GscError("upstream_error", "boom", details={"retry_after": "30"})
        payload = err.to_payload()
        assert payload["error"]["details"] == {"retry_after": "30"}

    def test_details_values_are_redacted(self):
        err = GscError(
            "upstream_error", "boom", details={"raw": "https://www.googleapis.com/v3"}
        )
        assert "googleapis.com" not in err.to_payload()["error"]["details"]["raw"]

    def test_unknown_code_is_rejected(self):
        with pytest.raises(ValueError):
            GscError("not_a_code", "x")  # type: ignore[arg-type]