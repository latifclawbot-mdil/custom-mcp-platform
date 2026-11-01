"""Tests for request construction, bounds, URL-scope checks, and error mapping.

The seam is :class:`GscClient`, which takes an injectable session. A fake
session records the exact method/URL/body it was asked to send, so the request
that *would* go to Google is asserted byte-for-byte without network, without
credentials, and without private data.
"""

import json

import pytest

from search_console_mcp.allowlist import PropertyAllowlist
from search_console_mcp.client import (
    BASE_URL,
    INSPECTION_URL,
    GscClient,
    build_search_analytics_body,
    validate_inspection_target,
)
from search_console_mcp.errors import GscError, InvalidArgumentError, SiteNotAllowedError


class FakeResponse:
    def __init__(self, status_code=200, payload=None, headers=None, text=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.headers = headers or {}
        self.text = text if text is not None else ""
        self.content = b"{}"

    def json(self):
        return self._payload


class RecordingSession:
    """Stands in for google.auth's AuthorizedSession.

    Records every request. Returns queued responses in order. Deliberately does
    not implement anything else, so a stray call is a loud AttributeError.
    """

    def __init__(self, responses=None):
        self.calls = []
        self._responses = list(responses or [])

    def request(self, method, url, json=None, timeout=None, headers=None, params=None):
        self.calls.append(
            {
                "method": method,
                "url": url,
                "json": json,
                "timeout": timeout,
                "headers": headers,
                "params": params,
            }
        )
        if not self._responses:
            return FakeResponse(200, {})
        return self._responses.pop(0)


def make_client(sites=("sc-domain:example.com", "https://www.example.com/"), responses=None):
    return GscClient(
        allowlist=PropertyAllowlist(sites),
        session=RecordingSession(responses),
    )


ALLOWED = "sc-domain:example.com"


class TestListSites:
    def test_get_request_shape(self):
        c = make_client(responses=[FakeResponse(200, {"siteEntry": [
            {"siteUrl": "sc-domain:example.com", "permissionLevel": "siteOwner"},
            {"siteUrl": "https://other.example/", "permissionLevel": "siteRestrictedUser"},
        ]})])
        out = c.list_sites()
        call = c.session.calls[0]
        assert call["method"] == "GET"
        assert call["url"] == f"{BASE_URL}/sites"
        assert out["ok"] is True
        # Only the allowlisted property is named; the other is counted, not listed.
        assert out["sites"] == [
            {"site_url": "sc-domain:example.com", "permission_level": "siteOwner", "allowlisted": True},
        ]
        assert out["sites_withheld"] == 1

    def test_flags_which_sites_are_allowlisted(self):
        c = make_client(responses=[FakeResponse(200, {"siteEntry": [
            {"siteUrl": "sc-domain:example.com", "permissionLevel": "siteOwner"},
            {"siteUrl": "https://not-mine.example/", "permissionLevel": "siteFullUser"},
        ]})])
        out = c.list_sites()
        assert all(s["allowlisted"] is True for s in out["sites"])
        assert out["sites_withheld"] == 1

    def test_list_sites_does_not_require_a_site_argument(self):
        c = make_client(responses=[FakeResponse(200, {"siteEntry": []})])
        assert c.list_sites()["sites"] == []

    def test_empty_response_yields_empty_list(self):
        c = make_client(responses=[FakeResponse(200, {})])
        assert c.list_sites()["sites"] == []

    def test_non_list_site_entry_is_treated_as_empty(self):
        c = make_client(responses=[FakeResponse(200, {"siteEntry": "nope"})])
        assert c.list_sites()["sites"] == []


class TestSearchAnalyticsRequest:
    def test_post_url_percent_encodes_property(self):
        c = make_client(responses=[FakeResponse(200, {"rows": []})])
        c.search_analytics(site_url=ALLOWED, start_date="2026-10-01", end_date="2026-10-31")
        call = c.session.calls[0]
        assert call["method"] == "POST"
        assert call["url"] == (
            f"{BASE_URL}/sites/sc-domain%3Aexample.com/searchAnalytics/query"
        )

    def test_url_prefix_property_encoding_matches_google_example(self):
        c = make_client(responses=[FakeResponse(200, {"rows": []})])
        c.search_analytics(
            site_url="https://www.example.com/",
            start_date="2026-10-01",
            end_date="2026-10-31",
        )
        assert c.session.calls[0]["url"] == (
            f"{BASE_URL}/sites/https%3A%2F%2Fwww.example.com%2F/searchAnalytics/query"
        )

    def test_body_carries_dates_and_dimensions(self):
        c = make_client(responses=[FakeResponse(200, {"rows": []})])
        c.search_analytics(
            site_url=ALLOWED,
            start_date="2026-10-01",
            end_date="2026-10-31",
            dimensions=["query", "page"],
        )
        assert c.session.calls[0]["json"] == {
            "startDate": "2026-10-01",
            "endDate": "2026-10-31",
            "dimensions": ["query", "page"],
            "rowLimit": 1000,
            "startRow": 0,
        }

    def test_row_limit_is_passed_through(self):
        c = make_client(responses=[FakeResponse(200, {"rows": []})])
        c.search_analytics(
            site_url=ALLOWED, start_date="2026-10-01", end_date="2026-10-02", row_limit=25000
        )
        assert c.session.calls[0]["json"]["rowLimit"] == 25000

    def test_search_type_and_aggregation_are_included_when_set(self):
        c = make_client(responses=[FakeResponse(200, {"rows": []})])
        c.search_analytics(
            site_url=ALLOWED,
            start_date="2026-10-01",
            end_date="2026-10-02",
            search_type="image",
            aggregation="byPage",
            data_state="all",
        )
        body = c.session.calls[0]["json"]
        assert body["type"] == "image"
        assert body["aggregationType"] == "byPage"
        assert body["dataState"] == "all"

    def test_defaults_are_omitted_not_guessed(self):
        """Unset optional fields must not appear in the body at all."""
        c = make_client(responses=[FakeResponse(200, {"rows": []})])
        c.search_analytics(site_url=ALLOWED, start_date="2026-10-01", end_date="2026-10-02")
        body = c.session.calls[0]["json"]
        for absent in ("type", "aggregationType", "dataState", "dimensionFilterGroups"):
            assert absent not in body

    def test_rows_are_mapped_to_snake_case(self):
        c = make_client(responses=[FakeResponse(200, {
            "rows": [
                {"keys": ["gsc api"], "clicks": 3.0, "impressions": 40.0, "ctr": 0.075, "position": 2.5},
            ],
            "responseAggregationType": "byProperty",
        })])
        out = c.search_analytics(site_url=ALLOWED, start_date="2026-10-01", end_date="2026-10-02")
        assert out["rows"] == [
            {"keys": ["gsc api"], "clicks": 3.0, "impressions": 40.0, "ctr": 0.075, "position": 2.5}
        ]
        assert out["response_aggregation_type"] == "byProperty"
        assert out["rows_returned"] == 1
        assert out["rows_truncated"] is False

    def test_filters_are_passed_through_in_api_shape(self):
        c = make_client(responses=[FakeResponse(200, {"rows": []})])
        c.search_analytics(
            site_url=ALLOWED,
            start_date="2026-10-01",
            end_date="2026-10-02",
            filters=[{"dimension": "country", "operator": "equals", "expression": "ind"}],
        )
        assert c.session.calls[0]["json"]["dimensionFilterGroups"] == [
            {"groupType": "and", "filters": [{"dimension": "country", "operator": "equals", "expression": "ind"}]}
        ]

    def test_in_operator_is_rejected(self):
        c = make_client()
        with pytest.raises(InvalidArgumentError):
            c.search_analytics(
                site_url=ALLOWED,
                start_date="2026-10-01",
                end_date="2026-10-02",
                filters=[{"dimension": "country", "operator": "in", "expression": "ind"}],
            )


class TestSearchAnalyticsBounds:
    def test_disallowed_site_makes_no_request(self):
        c = make_client()
        with pytest.raises(SiteNotAllowedError):
            c.search_analytics(site_url="sc-domain:evil.com", start_date="2026-10-01", end_date="2026-10-02")
        assert c.session.calls == []

    @pytest.mark.parametrize("limit", [0, -1, 25001, 10_000_000])
    def test_row_limit_out_of_range_rejected(self, limit):
        c = make_client()
        with pytest.raises(InvalidArgumentError):
            c.search_analytics(
                site_url=ALLOWED, start_date="2026-10-01", end_date="2026-10-02", row_limit=limit
            )
        assert c.session.calls == []

    @pytest.mark.parametrize("start", [-1, -1000])
    def test_negative_start_row_rejected(self, start):
        c = make_client()
        with pytest.raises(InvalidArgumentError):
            c.search_analytics(
                site_url=ALLOWED, start_date="2026-10-01", end_date="2026-10-02", start_row=start
            )

    def test_start_row_above_own_cap_rejected(self):
        c = make_client()
        with pytest.raises(InvalidArgumentError):
            c.search_analytics(
                site_url=ALLOWED, start_date="2026-10-01", end_date="2026-10-02", start_row=200_001
            )

    @pytest.mark.parametrize(
        "bad",
        ["2026-13-01", "2026-02-30", "01-10-2026", "2026-1-1", "", "not-a-date", "2026-10-01T00:00:00"],
    )
    def test_malformed_dates_rejected(self, bad):
        c = make_client()
        with pytest.raises(InvalidArgumentError):
            c.search_analytics(site_url=ALLOWED, start_date=bad, end_date="2026-10-31")

    def test_start_after_end_rejected(self):
        c = make_client()
        with pytest.raises(InvalidArgumentError):
            c.search_analytics(site_url=ALLOWED, start_date="2026-10-31", end_date="2026-10-01")

    def test_range_longer_than_cap_rejected(self):
        c = make_client()
        with pytest.raises(InvalidArgumentError):
            c.search_analytics(site_url=ALLOWED, start_date="2020-01-01", end_date="2026-10-01")

    def test_future_end_date_rejected(self, monkeypatch):
        c = make_client()
        with pytest.raises(InvalidArgumentError):
            c.search_analytics(site_url=ALLOWED, start_date="2099-01-01", end_date="2099-01-02")

    def test_unknown_dimension_rejected(self):
        c = make_client()
        with pytest.raises(InvalidArgumentError):
            c.search_analytics(
                site_url=ALLOWED, start_date="2026-10-01", end_date="2026-10-02", dimensions=["secrets"]
            )

    def test_duplicate_dimension_rejected(self):
        c = make_client()
        with pytest.raises(InvalidArgumentError):
            c.search_analytics(
                site_url=ALLOWED, start_date="2026-10-01", end_date="2026-10-02",
                dimensions=["query", "query"],
            )

    def test_too_many_dimensions_rejected(self):
        c = make_client()
        with pytest.raises(InvalidArgumentError):
            c.search_analytics(
                site_url=ALLOWED, start_date="2026-10-01", end_date="2026-10-02",
                dimensions=["query", "page", "country", "device", "date", "searchAppearance"],
            )

    def test_unknown_search_type_rejected(self):
        c = make_client()
        with pytest.raises(InvalidArgumentError):
            c.search_analytics(
                site_url=ALLOWED, start_date="2026-10-01", end_date="2026-10-02", search_type="amp"
            )

    def test_oversized_filter_expression_rejected(self):
        c = make_client()
        with pytest.raises(InvalidArgumentError):
            c.search_analytics(
                site_url=ALLOWED, start_date="2026-10-01", end_date="2026-10-02",
                filters=[{"dimension": "query", "operator": "contains", "expression": "x" * 5000}],
            )

    def test_too_many_filters_rejected(self):
        c = make_client()
        with pytest.raises(InvalidArgumentError):
            c.search_analytics(
                site_url=ALLOWED, start_date="2026-10-01", end_date="2026-10-02",
                filters=[{"dimension": "query", "operator": "equals", "expression": "a"}] * 11,
            )


class TestBuildBodyUnit:
    def test_body_builder_rejects_reversed_dates(self):
        with pytest.raises(InvalidArgumentError):
            build_search_analytics_body(start_date="2026-10-02", end_date="2026-10-01")


class TestUrlScope:
    def test_domain_property_allows_apex_and_subdomains(self):
        for url in (
            "https://example.com/",
            "https://example.com/a/b",
            "https://www.example.com/x",
            "http://deep.sub.example.com/y",
        ):
            validate_inspection_target("sc-domain:example.com", url)

    @pytest.mark.parametrize(
        "url",
        [
            "https://example.com.evil.com/",
            "https://notexample.com/",
            "http://example.co/",
            "https://example.com@evil.com/",
            "https://user:pw@example.com/",
            "https://example.com/#frag",
            "ftp://example.com/f",
            "javascript:alert(1)",
            "//example.com/protocol-relative",
            "example.com/no-scheme",
            "",
        ],
    )
    def test_domain_property_rejects_out_of_scope_or_malformed(self, url):
        with pytest.raises(InvalidArgumentError):
            validate_inspection_target("sc-domain:example.com", url)

    def test_url_prefix_property_requires_prefix(self):
        validate_inspection_target("https://www.example.com/blog/", "https://www.example.com/blog/post")
        with pytest.raises(InvalidArgumentError):
            validate_inspection_target("https://www.example.com/blog/", "https://www.example.com/other")

    def test_url_prefix_property_rejects_other_host_with_same_prefix_text(self):
        with pytest.raises(InvalidArgumentError):
            validate_inspection_target(
                "https://www.example.com/blog/", "https://www.example.com.evil.com/blog/"
            )

    def test_url_prefix_property_requires_trailing_slash_boundary(self):
        """A file-prefix property must not admit a sibling that merely extends it."""
        validate_inspection_target("https://www.example.com/blog/", "https://www.example.com/blog/x")
        with pytest.raises(InvalidArgumentError):
            validate_inspection_target("https://www.example.com/blog/", "https://www.example.com/blogroll")


class TestInspectUrl:
    def test_request_shape_and_host(self):
        c = make_client(responses=[FakeResponse(200, {"inspectionResult": {"inspectionResultLink": "https://search.google.com/x"}})])
        out = c.inspect_url(site_url=ALLOWED, url="https://example.com/pricing")
        call = c.session.calls[0]
        assert call["method"] == "POST"
        assert call["url"] == INSPECTION_URL
        assert call["json"] == {
            "inspectionUrl": "https://example.com/pricing",
            "siteUrl": "sc-domain:example.com",
            "languageCode": "en-US",
        }
        assert out["ok"] is True

    def test_property_and_scope_checked_before_credential_use(self):
        c = make_client()
        with pytest.raises(SiteNotAllowedError):
            c.inspect_url(site_url="sc-domain:evil.com", url="https://evil.com/")
        assert c.session.calls == []

    def test_out_of_scope_url_makes_no_request(self):
        c = make_client()
        with pytest.raises(InvalidArgumentError):
            c.inspect_url(site_url=ALLOWED, url="https://other.example/page")
        assert c.session.calls == []

    def test_language_code_validated(self):
        c = make_client()
        with pytest.raises(InvalidArgumentError):
            c.inspect_url(
                site_url=ALLOWED, url="https://example.com/", language_code="not a lang!!"
            )
        assert c.session.calls == []

    def test_inspection_result_is_flattened(self):
        c = make_client(responses=[FakeResponse(200, {"inspectionResult": {
            "inspectionResultLink": "https://search.google.com/search-console/inspect?x",
            "indexStatusResult": {
                "verdict": "PASS",
                "coverageState": "Submitted and indexed",
                "robotsTxtState": "ALLOWED",
                "indexingState": "INDEXING_ALLOWED",
                "lastCrawlTime": "2026-10-20T00:00:00Z",
                "pageFetchState": "SUCCESSFUL",
                "googleCanonical": "https://example.com/pricing",
                "userCanonical": "https://example.com/pricing",
                "crawledAs": "MOBILE",
            },
            "mobileUsabilityResult": {"verdict": "PASS"},
        }})])
        out = c.inspect_url(site_url=ALLOWED, url="https://example.com/pricing")
        r = out["inspection"]
        assert r["verdict"] == "PASS"
        assert r["coverage_state"] == "Submitted and indexed"
        assert r["robots_txt_state"] == "ALLOWED"
        assert r["last_crawl_time"] == "2026-10-20T00:00:00Z"
        assert r["google_canonical"] == "https://example.com/pricing"
        assert r["mobile_usability"]["verdict"] == "PASS"

    def test_missing_inspection_result_is_invalid_response(self):
        c = make_client(responses=[FakeResponse(200, {})])
        with pytest.raises(GscError) as ei:
            c.inspect_url(site_url=ALLOWED, url="https://example.com/")
        assert ei.value.code == "invalid_response"


class TestSitemaps:
    def test_get_request_shape(self):
        c = make_client(responses=[FakeResponse(200, {"sitemap": [
            {"path": "https://example.com/sitemap.xml", "type": "sitemap",
             "lastSubmitted": "2026-09-01T00:00:00Z", "isPending": False,
             "isSitemapsIndex": True, "lastDownloaded": "2026-10-01T00:00:00Z",
             "warnings": "0", "errors": "0", "contents": [{"type": "web", "submitted": "12", "indexed": "10"}]},
        ]})])
        out = c.list_sitemaps(site_url=ALLOWED)
        call = c.session.calls[0]
        assert call["method"] == "GET"
        assert call["url"] == f"{BASE_URL}/sites/sc-domain%3Aexample.com/sitemaps"
        assert out["sitemaps"][0]["path"] == "https://example.com/sitemap.xml"
        assert out["sitemaps"][0]["is_sitemaps_index"] is True
        assert out["sitemaps"][0]["contents"] == [{"type": "web", "submitted": 12, "indexed": 10}]

    def test_disallowed_site_denied(self):
        c = make_client()
        with pytest.raises(SiteNotAllowedError):
            c.list_sitemaps(site_url="https://evil.example/")
        assert c.session.calls == []

    def test_missing_sitemap_key_yields_empty(self):
        c = make_client(responses=[FakeResponse(200, {})])
        assert c.list_sitemaps(site_url=ALLOWED)["sitemaps"] == []


class TestUpstreamErrors:
    @pytest.mark.parametrize(
        "status,code",
        [
            (400, "bad_request"),
            (401, "unauthorized"),
            (403, "unauthorized"),
            (404, "not_found"),
            (429, "rate_limited"),
            (500, "upstream_error"),
            (503, "upstream_error"),
            (418, "upstream_error"),
        ],
    )
    def test_status_mapping(self, status, code):
        c = make_client(responses=[FakeResponse(status, {"error": {"message": "nope"}})])
        with pytest.raises(GscError) as ei:
            c.list_sites()
        assert ei.value.code == code

    def test_403_message_mentions_property_membership(self):
        c = make_client(responses=[FakeResponse(403, {"error": {"message": "denied"}})])
        with pytest.raises(GscError) as ei:
            c.list_sites()
        assert "user" in ei.value.message.lower() or "permission" in ei.value.message.lower()

    def test_429_preserves_retry_after(self):
        c = make_client(responses=[FakeResponse(429, {}, headers={"Retry-After": "30"})])
        with pytest.raises(GscError) as ei:
            c.list_sites()
        assert ei.value.details.get("retry_after") == "30"

    def test_upstream_body_is_not_echoed_verbatim(self):
        leaky = {
            "error": {
                "message": "Error for site https://www.googleapis.com/x token ya29.SECRETVALUE123456",
                "code": 403,
            }
        }
        c = make_client(responses=[FakeResponse(403, leaky)])
        with pytest.raises(GscError) as ei:
            c.list_sites()
        assert "ya29.SECRETVALUE123456" not in ei.value.message
        assert "googleapis.com" not in ei.value.message

    def test_non_json_200_is_invalid_response(self):
        class BadJson(FakeResponse):
            def json(self):
                raise ValueError("not json")

        c = make_client(responses=[BadJson(200, None)])
        with pytest.raises(GscError) as ei:
            c.list_sites()
        assert ei.value.code == "invalid_response"

    def test_network_failure_is_network_error(self):
        class Boom(RecordingSession):
            def request(self, *a, **kw):
                raise ConnectionError("dns fail")

        c = GscClient(allowlist=PropertyAllowlist([ALLOWED]), session=Boom())
        with pytest.raises(GscError) as ei:
            c.list_sites()
        assert ei.value.code == "network_error"

    def test_request_timeout_is_bounded(self):
        c = make_client(responses=[FakeResponse(200, {})])
        c.list_sites()
        assert 0 < c.session.calls[0]["timeout"] <= 60


class TestInspectionPathTraversal:
    """A ``..``/``.`` segment (raw or percent-encoded) must not escape the property.

    ``validate_inspection_target`` compared the raw URL string against the
    property prefix, so ``https://www.example.com/../admin`` counted as
    "under" ``https://www.example.com/`` even though a browser (and Google's
    crawler) resolves it to ``https://www.example.com/admin`` -- or, with more
    segments, to a wholly different path. The check must fail closed on any
    dot-segment, raw or percent-encoded, because the API receives the string
    verbatim but resolves it per RFC 3986.
    """

    @pytest.mark.parametrize(
        "url",
        [
            "https://www.example.com/../secret/",
            "https://www.example.com/a/../../etc/passwd",
            "https://www.example.com/%2e%2e/secret",
            "https://www.example.com/%2E%2E/secret",
            "https://www.example.com/%2e/secret",
            "https://www.example.com/./secret",
            "https://www.example.com/a/%2e%2e%2fb",
        ],
    )
    def test_url_prefix_property_rejects_dot_segments(self, url):
        with pytest.raises(InvalidArgumentError):
            validate_inspection_target("https://www.example.com/", url)

    @pytest.mark.parametrize(
        "url",
        [
            "https://example.com/../other",
            "https://example.com/a/%2e%2e/b",
            "https://example.com/./x",
        ],
    )
    def test_domain_property_rejects_dot_segments(self, url):
        with pytest.raises(InvalidArgumentError):
            validate_inspection_target("sc-domain:example.com", url)

    def test_dot_in_a_normal_path_segment_is_allowed(self):
        # A literal dot inside a filename is not a dot-segment and must still pass.
        validate_inspection_target(
            "https://www.example.com/report.v2.html", "https://www.example.com/report.v2.html"
        )
        validate_inspection_target(
            "sc-domain:example.com", "https://example.com/a.b/c..d"
        )

    def test_traversal_url_is_refused_without_a_request(self):
        c = make_client()
        with pytest.raises(InvalidArgumentError):
            c.inspect_url(site_url=ALLOWED, url="https://example.com/../admin")
        assert c.session.calls == []


class TestNumericRowMapping:
    """``_as_int`` / ``_as_float`` must not accept values Python would coerce.

    ``str.isdigit()`` is true for non-ASCII digits (Arabic-Indic, fullwidth),
    and ``int("١٢٣")`` is 123, so a numeric *string* from the upstream body
    could carry Unicode digits and be silently reinterpreted. The parser must
    restrict itself to ASCII ``[0-9]``.
    """

    @pytest.mark.parametrize(
        "value",
        ["\u0661\u0662\u0663", "\uff11\uff12\uff13", "\u09e7\u09e8\u09e9", "1_000", "1 000"],
    )
    def test_unicode_digit_strings_are_not_parsed(self, value):
        from search_console_mcp.client import _as_int

        assert _as_int(value) is None

    def test_ascii_digit_strings_still_parse(self):
        from search_console_mcp.client import _as_int

        assert _as_int("123") == 123

    def test_non_finite_floats_are_rejected(self):
        from search_console_mcp.client import _as_float

        assert _as_float(float("nan")) is None
        assert _as_float(float("inf")) is None
        assert _as_float(float("-inf")) is None
        assert _as_float(1.5) == 1.5


class TestTruncationPagination:
    """``next_start_row`` must advance past every row consumed from upstream.

    The row cap slices the upstream list, then per-row mapping drops malformed
    entries. Advancing by the *post-filter* count re-reads rows already
    consumed; advancing by the number of raw rows the cap admitted is correct.
    """

    def test_next_start_row_advances_by_rows_consumed(self):
        # Cap 3 admits three raw rows; the first two are not objects and are
        # dropped during mapping. Only 1 row is returned, but the client has
        # consumed 3 upstream rows, so the next page starts at row 3.
        raw = [None, "x", {"keys": ["a"], "clicks": 1.0}, {"keys": ["b"], "clicks": 1.0}]
        c = GscClient(
            allowlist=PropertyAllowlist([ALLOWED]),
            session=RecordingSession([FakeResponse(200, {"rows": raw})]),
            max_returned_rows=3,
        )
        out = c.search_analytics(
            site_url=ALLOWED, start_date="2026-10-01", end_date="2026-10-02"
        )
        assert out["rows_returned"] == 1
        assert out["rows_truncated"] is True
        assert out["next_start_row"] == 3

    def test_next_start_row_accounts_for_start_row_offset(self):
        raw = [{"keys": ["a"], "clicks": 1.0}, {"keys": ["b"], "clicks": 1.0}]
        c = GscClient(
            allowlist=PropertyAllowlist([ALLOWED]),
            session=RecordingSession([FakeResponse(200, {"rows": raw})]),
            max_returned_rows=1,
        )
        out = c.search_analytics(
            site_url=ALLOWED,
            start_date="2026-10-01",
            end_date="2026-10-02",
            start_row=10,
        )
        assert out["next_start_row"] == 11

    def test_no_next_start_row_when_not_truncated(self):
        raw = [{"keys": ["a"], "clicks": 1.0}]
        c = GscClient(
            allowlist=PropertyAllowlist([ALLOWED]),
            session=RecordingSession([FakeResponse(200, {"rows": raw})]),
            max_returned_rows=10,
        )
        out = c.search_analytics(
            site_url=ALLOWED, start_date="2026-10-01", end_date="2026-10-02"
        )
        assert out["rows_truncated"] is False
        assert "next_start_row" not in out


class TestListSitesDisclosure:
    """``list_sites`` must not disclose properties the caller cannot query.

    The tool exists so an operator can see what this server will serve. It
    previously returned every property the *credential* can reach, including
    names that are deliberately absent from the allowlist -- disclosing the
    existence of other properties in the account. Only allowlisted properties
    (with their permission) may be reported; the rest are summarised by count.
    """

    def test_non_allowlisted_properties_are_not_disclosed(self):
        c = make_client(
            responses=[
                FakeResponse(
                    200,
                    {
                        "siteEntry": [
                            {"siteUrl": "sc-domain:example.com", "permissionLevel": "siteOwner"},
                            {
                                "siteUrl": "sc-domain:secret-other.example",
                                "permissionLevel": "siteFullUser",
                            },
                        ]
                    },
                )
            ]
        )
        out = c.list_sites()
        names = [s["site_url"] for s in out["sites"]]
        assert names == ["sc-domain:example.com"]
        assert "secret-other.example" not in json.dumps(out)
        assert out["sites_withheld"] == 1

    def test_only_allowlisted_sites_are_returned(self):
        c = make_client(
            responses=[
                FakeResponse(
                    200,
                    {
                        "siteEntry": [
                            {"siteUrl": "sc-domain:example.com", "permissionLevel": "siteOwner"},
                            {"siteUrl": "https://www.example.com/", "permissionLevel": "siteOwner"},
                            {"siteUrl": "https://other.example/", "permissionLevel": "siteRestrictedUser"},
                        ]
                    },
                )
            ]
        )
        out = c.list_sites()
        assert [s["site_url"] for s in out["sites"]] == [
            "sc-domain:example.com",
            "https://www.example.com/",
        ]
        assert all(s["allowlisted"] is True for s in out["sites"])
        assert out["sites_withheld"] == 1