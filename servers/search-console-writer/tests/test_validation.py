"""URL and sitemap validation for the write surface.

Every one of the four write methods takes a property identifier, and two of them
take a sitemap URL. Both values become request path segments, so they are
validated structurally *and* against the property's scope before any request
object exists.
"""

from __future__ import annotations

import pytest

from search_console_writer.errors import InvalidArgumentError
from search_console_writer.validation import (
    validate_property_identifier,
    validate_sitemap_url,
)


class TestPropertyIdentifier:
    def test_domain_property_is_accepted(self):
        assert validate_property_identifier("sc-domain:example.com") == "sc-domain:example.com"

    def test_url_prefix_property_is_accepted(self):
        assert validate_property_identifier("https://www.example.com/") == "https://www.example.com/"

    def test_bare_hostname_is_rejected(self):
        with pytest.raises(InvalidArgumentError):
            validate_property_identifier("example.com")

    def test_unsupported_scheme_is_rejected(self):
        with pytest.raises(InvalidArgumentError):
            validate_property_identifier("ftp://example.com/")

    def test_sc_domain_without_a_host_is_rejected(self):
        with pytest.raises(InvalidArgumentError):
            validate_property_identifier("sc-domain:")

    def test_credentials_in_a_property_are_rejected(self):
        with pytest.raises(InvalidArgumentError):
            validate_property_identifier("https://user:pw@example.com/")

    def test_query_string_in_a_property_is_rejected(self):
        with pytest.raises(InvalidArgumentError):
            validate_property_identifier("https://example.com/?x=1")

    def test_fragment_in_a_property_is_rejected(self):
        with pytest.raises(InvalidArgumentError):
            validate_property_identifier("https://example.com/#frag")

    def test_control_characters_are_rejected(self):
        with pytest.raises(InvalidArgumentError):
            validate_property_identifier("https://example.com/\x00")

    def test_whitespace_is_rejected(self):
        with pytest.raises(InvalidArgumentError):
            validate_property_identifier("https://exa mple.com/")

    def test_too_long_is_rejected(self):
        with pytest.raises(InvalidArgumentError):
            validate_property_identifier("sc-domain:" + "a" * 3000)

    def test_non_string_is_rejected(self):
        with pytest.raises(InvalidArgumentError):
            validate_property_identifier(None)


class TestSitemapUrlScope:
    def test_sitemap_on_a_subdomain_of_a_domain_property_is_accepted(self):
        url = "https://www.example.com/sitemap.xml"
        assert validate_sitemap_url("sc-domain:example.com", url) == url

    def test_sitemap_on_the_apex_of_a_domain_property_is_accepted(self):
        url = "https://example.com/sitemap_index.xml"
        assert validate_sitemap_url("sc-domain:example.com", url) == url

    def test_sitemap_on_an_unrelated_host_is_rejected(self):
        with pytest.raises(InvalidArgumentError) as excinfo:
            validate_sitemap_url("sc-domain:example.com", "https://evil.example/sitemap.xml")
        assert excinfo.value.details["reason"] == "host_out_of_scope"

    def test_sitemap_on_a_lookalike_host_is_rejected(self):
        with pytest.raises(InvalidArgumentError):
            validate_sitemap_url(
                "sc-domain:example.com", "https://example.com.evil.example/sitemap.xml"
            )

    def test_sitemap_outside_a_url_prefix_property_is_rejected(self):
        with pytest.raises(InvalidArgumentError) as excinfo:
            validate_sitemap_url("https://www.example.com/blog/", "https://www.example.com/shop/sitemap.xml")
        assert excinfo.value.details["reason"] == "path_out_of_scope"

    def test_sitemap_inside_a_url_prefix_property_is_accepted(self):
        url = "https://www.example.com/blog/sitemap.xml"
        assert validate_sitemap_url("https://www.example.com/blog/", url) == url

    def test_scheme_mismatch_against_a_url_prefix_property_is_rejected(self):
        with pytest.raises(InvalidArgumentError):
            validate_sitemap_url("https://www.example.com/", "http://www.example.com/sitemap.xml")


class TestSitemapUrlShape:
    def test_relative_url_is_rejected(self):
        with pytest.raises(InvalidArgumentError):
            validate_sitemap_url("sc-domain:example.com", "/sitemap.xml")

    def test_non_http_scheme_is_rejected(self):
        with pytest.raises(InvalidArgumentError):
            validate_sitemap_url("sc-domain:example.com", "file:///etc/passwd")

    def test_credentials_are_rejected(self):
        with pytest.raises(InvalidArgumentError):
            validate_sitemap_url("sc-domain:example.com", "https://u:p@example.com/sitemap.xml")

    def test_fragment_is_rejected(self):
        with pytest.raises(InvalidArgumentError):
            validate_sitemap_url("sc-domain:example.com", "https://example.com/sitemap.xml#x")

    def test_control_characters_are_rejected(self):
        with pytest.raises(InvalidArgumentError):
            validate_sitemap_url("sc-domain:example.com", "https://example.com/sitemap.xml\nHost: evil")

    def test_too_long_is_rejected(self):
        with pytest.raises(InvalidArgumentError):
            validate_sitemap_url(
                "sc-domain:example.com", "https://example.com/" + "a" * 5000
            )

    def test_raw_dot_segment_is_rejected(self):
        with pytest.raises(InvalidArgumentError) as excinfo:
            validate_sitemap_url("sc-domain:example.com", "https://example.com/../sitemap.xml")
        assert excinfo.value.details["reason"] == "path_traversal"

    def test_encoded_dot_segment_is_rejected(self):
        with pytest.raises(InvalidArgumentError) as excinfo:
            validate_sitemap_url("sc-domain:example.com", "https://example.com/a/%2e%2e/sitemap.xml")
        assert excinfo.value.details["reason"] == "path_traversal"

    def test_empty_value_is_rejected(self):
        with pytest.raises(InvalidArgumentError):
            validate_sitemap_url("sc-domain:example.com", "")

    def test_non_string_is_rejected(self):
        with pytest.raises(InvalidArgumentError):
            validate_sitemap_url("sc-domain:example.com", 123)
