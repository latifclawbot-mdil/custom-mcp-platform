"""Search Console HTTP client: request construction, bounds, scope, error mapping.

Boundaries this module enforces, all *before* a request is sent:

- the property is in the allowlist (via :class:`PropertyAllowlist`);
- the property is percent-encoded into a single path segment, so it cannot
  alter the host or add a path segment;
- the host comes only from module constants;
- an inspected URL is under the property it claims to belong to;
- dates, row limits, dimensions, search type, aggregation, and filter sizes are
  within range.

It enforces one boundary *after* the response: only a bounded number of rows is
ever returned to the caller, and truncation is signalled explicitly.

The session is injected. Production passes an ``AuthorizedSession`` from
``google-auth``; tests pass a recorder. This module never imports credentials.
"""

from __future__ import annotations

import datetime as _dt
import json as _json
import logging
import math
import os
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import urlsplit

from .allowlist import PropertyAllowlist
from .capture import record
from .encoding import encode_property
from .errors import (
    GscError,
    InvalidArgumentError,
)

logger = logging.getLogger("search_console_mcp.client")

# Fixed API origins. The only caller-influenced component of any URL built here
# is the encoded property path segment; there is no parameter that can reach a
# host or scheme.
BASE_URL = "https://www.googleapis.com/webmasters/v3"
INSPECTION_URL = "https://searchconsole.googleapis.com/v1/urlInspection/index:inspect"

# Test-only seam. When set, requests go to a local base instead of Google's.
# Restricted to loopback so a stray value cannot redirect credentials.
BASE_OVERRIDE_ENV = "GSC_API_BASE_OVERRIDE"
_INSPECTION_OVERRIDE_ENV = "GSC_INSPECTION_BASE_OVERRIDE"

REQUEST_TIMEOUT_SECONDS = 30.0

MAX_ROW_LIMIT = 25000
MAX_START_ROW = 200_000
MAX_DATE_SPAN_DAYS = 1640  # ~4.5 years inclusive; well past Search Console's 16-month retention
MAX_DIMENSIONS = 5
MAX_FILTER_EXPRESSIONS = 10
MAX_EXPRESSION_LEN = 1024

DIMENSIONS = ("query", "page", "country", "device", "date", "hour", "searchAppearance")
SEARCH_TYPES = ("web", "image", "video", "news", "discover", "googleNews")
AGGREGATIONS = ("auto", "byPage", "byProperty", "byNewsShowcasePanel")
DATA_STATES = ("all", "final", "hourly_all", "hourlyAll")
FILTER_OPERATORS = (
    "contains",
    "equals",
    "notContains",
    "notEquals",
    "includingRegex",
    "excludingRegex",
)
FILTER_DIMENSIONS = ("query", "page", "country", "device", "searchAppearance")

_ABSOLUTE_SCHEMES = ("http", "https")


def _today_utc() -> _dt.date:
    return _dt.datetime.now(_dt.timezone.utc).date()


def parse_date(value: object, field: str) -> _dt.date:
    """Strict ``YYYY-MM-DD`` parse. Rejects anything else, including substrings."""
    if not isinstance(value, str):
        raise InvalidArgumentError(f"{field} must be a YYYY-MM-DD string")
    if len(value) != 10 or value[4] != "-" or value[7] != "-":
        raise InvalidArgumentError(f"{field} must be exactly YYYY-MM-DD")
    try:
        parsed = _dt.date.fromisoformat(value)
    except ValueError:
        raise InvalidArgumentError(f"{field} is not a valid calendar date") from None
    # fromisoformat accepts some forms len 10 only in this shape, but double-check
    # round-trip so "2026-1-01"-style inputs can never slip through.
    if parsed.isoformat() != value:
        raise InvalidArgumentError(f"{field} must be zero-padded YYYY-MM-DD")
    return parsed


def _validate_date_range(start_date: object, end_date: object) -> tuple[str, str]:
    start = parse_date(start_date, "start_date")
    end = parse_date(end_date, "end_date")
    if start > end:
        raise InvalidArgumentError("start_date must be less than or equal to end_date")
    span = (end - start).days + 1
    if span > MAX_DATE_SPAN_DAYS:
        raise InvalidArgumentError(
            "date range exceeds the maximum span",
            details={"max_days": str(MAX_DATE_SPAN_DAYS), "requested_days": str(span)},
        )
    today = _today_utc()
    if end > today:
        raise InvalidArgumentError(
            "end_date is in the future; Search Console exposes only finalized past data",
            details={"today_utc": today.isoformat()},
        )
    return start.isoformat(), end.isoformat()


def _validate_row_limit(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidArgumentError("row_limit must be an integer")
    if value < 1 or value > MAX_ROW_LIMIT:
        raise InvalidArgumentError(
            "row_limit is out of range",
            details={"min": "1", "max": str(MAX_ROW_LIMIT)},
        )
    return value


def _validate_start_row(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidArgumentError("start_row must be an integer")
    if value < 0 or value > MAX_START_ROW:
        raise InvalidArgumentError(
            "start_row is out of range",
            details={"min": "0", "max": str(MAX_START_ROW)},
        )
    return value


def _validate_dimensions(dimensions: object) -> list[str]:
    if dimensions is None:
        return []
    if isinstance(dimensions, str) or not isinstance(dimensions, (list, tuple)):
        raise InvalidArgumentError("dimensions must be a list of dimension names")
    out: list[str] = []
    for dim in dimensions:
        if not isinstance(dim, str) or dim not in DIMENSIONS:
            raise InvalidArgumentError(
                "unknown dimension",
                details={"allowed": ",".join(DIMENSIONS)},
            )
        if dim in out:
            raise InvalidArgumentError("a dimension may not be repeated")
        out.append(dim)
    if len(out) > MAX_DIMENSIONS:
        raise InvalidArgumentError(
            "too many dimensions", details={"max": str(MAX_DIMENSIONS)}
        )
    return out


def _validate_filters(filters: object) -> list[dict[str, Any]]:
    if filters is None:
        return []
    if not isinstance(filters, (list, tuple)):
        raise InvalidArgumentError("filters must be a list of filter objects")
    if len(filters) == 0:
        return []
    if len(filters) > MAX_FILTER_EXPRESSIONS:
        raise InvalidArgumentError(
            "too many filters", details={"max": str(MAX_FILTER_EXPRESSIONS)}
        )
    out: list[dict[str, Any]] = []
    for raw in filters:
        if not isinstance(raw, Mapping):
            raise InvalidArgumentError("each filter must be an object")
        dimension = raw.get("dimension")
        operator = raw.get("operator", "equals")
        expression = raw.get("expression")
        if dimension not in FILTER_DIMENSIONS:
            raise InvalidArgumentError(
                "unknown filter dimension", details={"allowed": ",".join(FILTER_DIMENSIONS)}
            )
        if operator not in FILTER_OPERATORS:
            raise InvalidArgumentError(
                "unknown filter operator", details={"allowed": ",".join(FILTER_OPERATORS)}
            )
        if not isinstance(expression, str) or not expression:
            raise InvalidArgumentError("filter expression must be a non-empty string")
        if len(expression) > MAX_EXPRESSION_LEN:
            raise InvalidArgumentError(
                "filter expression too long", details={"max": str(MAX_EXPRESSION_LEN)}
            )
        out.append({"dimension": dimension, "operator": operator, "expression": expression})
    return out


def build_search_analytics_body(
    *,
    start_date: object,
    end_date: object,
    dimensions: object = None,
    row_limit: object = 1000,
    start_row: object = 0,
    search_type: object = None,
    aggregation: object = None,
    data_state: object = None,
    filters: object = None,
) -> dict[str, Any]:
    """Validate and assemble a ``searchAnalytics.query`` request body.

    Optional fields are *omitted* when unset rather than defaulted, so the
    request only ever asks for what the caller explicitly did.
    """
    s, e = _validate_date_range(start_date, end_date)
    body: dict[str, Any] = {
        "startDate": s,
        "endDate": e,
        "dimensions": _validate_dimensions(dimensions),
        "rowLimit": _validate_row_limit(row_limit),
        "startRow": _validate_start_row(start_row),
    }
    if not body["dimensions"]:
        del body["dimensions"]
    if search_type is not None:
        if search_type not in SEARCH_TYPES:
            raise InvalidArgumentError(
                "unknown search type", details={"allowed": ",".join(SEARCH_TYPES)}
            )
        body["type"] = search_type
    if aggregation is not None:
        if aggregation not in AGGREGATIONS:
            raise InvalidArgumentError(
                "unknown aggregation type", details={"allowed": ",".join(AGGREGATIONS)}
            )
        body["aggregationType"] = aggregation
    if data_state is not None:
        if data_state not in DATA_STATES:
            raise InvalidArgumentError(
                "unknown data state", details={"allowed": ",".join(DATA_STATES)}
            )
        body["dataState"] = data_state
    validated_filters = _validate_filters(filters)
    if validated_filters:
        body["dimensionFilterGroups"] = [
            {"groupType": "and", "filters": validated_filters}
        ]
    return body


def validate_inspection_target(property_id: str, url: object) -> str:
    """Check that ``url`` is a well-formed, in-property http(s) URL.

    Returns the URL unchanged when valid. Raises for anything that is not an
    absolute http(s) URL, carries credentials or a fragment, or falls outside
    the property. This is what stops an allowlisted property from being used to
    inspect arbitrary third-party pages.
    """
    if not isinstance(url, str) or not url:
        raise InvalidArgumentError("url must be a non-empty string")
    if len(url) > 4096:
        raise InvalidArgumentError("url is too long")
    # Reject control characters outright: they are never legitimate in this field
    # and can be used to confuse downstream logging or parsing.
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in url):
        raise InvalidArgumentError("url contains control characters")

    parts = urlsplit(url)
    if parts.scheme not in _ABSOLUTE_SCHEMES:
        raise InvalidArgumentError("url must be an absolute http:// or https:// URL")
    if not parts.netloc:
        raise InvalidArgumentError("url must include a host")
    if parts.username or parts.password:
        raise InvalidArgumentError("url must not contain credentials")
    if parts.fragment:
        raise InvalidArgumentError("url must not contain a fragment")

    host = (parts.hostname or "").lower()
    if not host:
        raise InvalidArgumentError("url must include a host")

    # A dot-segment ("." or "..", raw or percent-encoded) resolves to a
    # different path than the text shows, so a raw prefix check cannot prove the
    # URL stays inside the property. Fail closed on any of them.
    if _has_dot_segment(parts.path):
        raise InvalidArgumentError(
            "url must not contain '.' or '..' path segments",
            details={"reason": "path_traversal"},
        )

    if property_id.startswith("sc-domain:"):
        domain = property_id[len("sc-domain:") :].lower()
        if host != domain and not host.endswith("." + domain):
            raise InvalidArgumentError(
                "url is not under the specified property",
                details={"reason": "host_out_of_scope"},
            )
        return url

    if property_id.startswith("http://") or property_id.startswith("https://"):
        prop = urlsplit(property_id)
        prop_host = (prop.hostname or "").lower()
        if host != prop_host:
            raise InvalidArgumentError(
                "url is not under the specified property",
                details={"reason": "host_out_of_scope"},
            )
        if not url.startswith(property_id):
            raise InvalidArgumentError(
                "url is not under the specified property",
                details={"reason": "path_out_of_scope"},
            )
        return url

    raise InvalidArgumentError("property identifier form is not supported for inspection")


_VALID_LANGUAGE_RE = __import__("re").compile(r"^[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})*$")

# Matches a percent-encoded "." (%2e, any case) or "/" (%2f, any case). A
# decoder that applies these turns the encoded text into the same bytes RFC
# 3986 dot-segment removal operates on, so we can detect traversal that a raw
# prefix check on the encoded string would miss.
_ENC_DOT_RE = __import__("re").compile(r"%2e", __import__("re").IGNORECASE)
_ENC_SLASH_RE = __import__("re").compile(r"%2f", __import__("re").IGNORECASE)


def _has_dot_segment(path: str) -> bool:
    """Whether ``path`` contains a ``.`` or ``..`` segment, raw or encoded.

    The path is first mapped to the form a consumer would resolve it in: every
    percent-encoded ``.`` and ``/`` is decoded, so ``/a/%2e%2e%2fb`` is seen as
    ``/a/../b``. Then each ``/``-delimited segment is checked for exact ``.`` or
    ``..`` (which is what a resolver actually removes). A literal dot inside a
    segment (``report.v2.html``) is not a dot-segment and is left alone.
    """
    # Reject encoded percent signs and backslashes, which can conceal a second
    # decoding pass or a platform-dependent path separator. This conservative
    # policy avoids interpreting an ambiguous URL as in-scope.
    if __import__("re").search(r"%(?:25|5c)", path, __import__("re").IGNORECASE):
        return True
    decoded = _ENC_SLASH_RE.sub("/", _ENC_DOT_RE.sub(".", path))
    # A backslash is a path separator on some consumers (and Windows); treat it
    # as one for the purposes of the check, so "..\..\" cannot slip through.
    decoded = decoded.replace("\\", "/")
    for segment in decoded.split("/"):
        if segment in (".", ".."):
            return True
    return False


def validate_language_code(value: object) -> str:
    if value is None or value == "":
        return "en-US"
    if not isinstance(value, str) or not _VALID_LANGUAGE_RE.match(value):
        raise InvalidArgumentError("language_code must be a BCP-47 tag such as en-US")
    return value


def _override_base(default: str, env_name: str) -> str:
    """Resolve the API base, honouring the test-only loopback override."""
    raw = os.environ.get(env_name)
    if not raw:
        return default
    parts = urlsplit(raw)
    if parts.scheme not in ("http", "https") or (parts.hostname or "") not in (
        "127.0.0.1",
        "localhost",
        "::1",
    ):
        # Refuse anything that is not loopback: this seam must never be usable to
        # aim an authenticated client at a remote host.
        logger.warning("ignoring %s: override must point at a loopback host", env_name)
        return default
    return raw.rstrip("/")


def all_bases_loopback() -> bool:
    """Whether *every* API base resolves to a loopback host.

    This is the single condition the test seams gate on. It has to be *all*
    bases, not any: the static token and the request capture are only safe when
    nothing can leave the machine, and a single un-overridden base (the
    inspection endpoint, say) still points at Google.
    """
    return (
        _override_base(BASE_URL, BASE_OVERRIDE_ENV) != BASE_URL
        and _override_base(INSPECTION_URL, _INSPECTION_OVERRIDE_ENV) != INSPECTION_URL
    )


def _error_from_response(response: Any) -> GscError:
    """Map an upstream HTTP response to a redacted :class:`GscError`."""
    status = getattr(response, "status_code", 0)
    message = ""
    try:
        payload = response.json()
        if isinstance(payload, Mapping):
            err = payload.get("error")
            if isinstance(err, Mapping):
                message = str(err.get("message") or "")
            elif isinstance(err, str):
                message = err
    except Exception:
        message = ""

    headers = getattr(response, "headers", None) or {}
    details: dict[str, Any] = {}
    retry_after = None
    try:
        retry_after = headers.get("Retry-After")
    except Exception:
        retry_after = None
    if retry_after:
        details["retry_after"] = str(retry_after)

    if status == 400:
        return GscError("bad_request", message or "the request was rejected", details)
    if status in (401, 403):
        hint = (
            "the configured credential was not accepted for this operation; "
            "confirm the Google account is added as a user of the property"
        )
        return GscError("unauthorized", f"{message or 'access denied'}; {hint}", details)
    if status == 404:
        return GscError("not_found", message or "the property or resource was not found", details)
    if status == 429:
        return GscError("rate_limited", message or "the API quota was exceeded", details)
    if status >= 500:
        return GscError("upstream_error", message or "the upstream service failed", details)
    return GscError("upstream_error", message or f"unexpected status {status}", details)


def _decode_json(response: Any) -> Mapping[str, Any]:
    try:
        payload = response.json()
    except Exception:
        raise GscError(
            "invalid_response", "the upstream service returned a non-JSON body"
        ) from None
    if payload is None:
        return {}
    if not isinstance(payload, Mapping):
        raise GscError("invalid_response", "the upstream response was not a JSON object")
    return payload


def _as_float(value: Any) -> float | None:
    """Return a finite float, or ``None``.

    ``NaN``/``Infinity`` are rejected rather than passed through: they are not
    valid JSON numbers, and ``json.dumps`` would emit them as bare tokens that
    strict parsers (and therefore many MCP clients) refuse.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            number = float(value)
        except (OverflowError, ValueError):
            return None
        if not math.isfinite(number):
            return None
        return number
    return None


# ASCII digits only. ``str.isdigit()`` is true for Arabic-Indic and fullwidth
# digits, and ``int()`` accepts them, so an upstream string would be silently
# reinterpreted as a different number.
_ASCII_INT_RE = __import__("re").compile(r"[0-9]+")


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value) and value.is_integer():
        return int(value)
    if isinstance(value, str) and _ASCII_INT_RE.fullmatch(value):
        return int(value)
    return None


class GscClient:
    """Read-only Search Console operations over an injected HTTP session."""

    def __init__(
        self,
        allowlist: PropertyAllowlist,
        session: Any,
        *,
        timeout: float = REQUEST_TIMEOUT_SECONDS,
        max_returned_rows: int = 2000,
    ) -> None:
        self._allowlist = allowlist
        self._session = session
        self._timeout = timeout
        self._max_returned_rows = max_returned_rows

    @property
    def session(self) -> Any:
        """The injected session. Exposed for tests and traceability."""
        return self._session

    @property
    def allowlist(self) -> PropertyAllowlist:
        return self._allowlist

    # -- transport ---------------------------------------------------------

    def _request(
        self,
        method: str,
        url: str,
        *,
        body: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        if method not in ("GET", "POST"):
            # Defence in depth: no write verb can be constructed anywhere.
            raise InvalidArgumentError("only GET and POST are permitted")
        # Records the method/URL/body only, and only when capture is enabled.
        record(method, url, body)
        try:
            response = self._session.request(
                method, url, json=body, timeout=self._timeout
            )
        except GscError:
            raise
        except Exception as exc:  # network/DNS/TLS and transport-level failures
            raise GscError(
                "network_error", f"transport failure: {type(exc).__name__}"
            ) from None
        status = getattr(response, "status_code", None)
        if not isinstance(status, int):
            raise GscError("invalid_response", "the transport returned no status code")
        if status >= 400:
            raise _error_from_response(response)
        return _decode_json(response)

    def _site_endpoint(self, method: str, site_url: object, suffix: str) -> str:
        """Build a sites endpoint URL for an allowlisted property.

        The property is normalized by the allowlist and then percent-encoded, so
        what reaches the URL is the allowlist's own canonical form, never the
        caller's raw string.
        """
        normalized = self._allowlist.require(site_url)
        base = _override_base(BASE_URL, BASE_OVERRIDE_ENV)
        return f"{base}/sites/{encode_property(normalized)}{suffix}"

    # -- operations --------------------------------------------------------

    def list_sites(self) -> dict[str, Any]:
        """List the allowlisted properties the configured credential can access.

        Only properties this server is permitted to serve are disclosed. A
        credential may reach properties that are deliberately absent from the
        allowlist; naming those would leak the account's property inventory
        into an LLM context (and to any client of this server) without giving
        the caller the ability to query them. They are summarised as a count
        instead, which is enough to notice a misconfigured allowlist.
        """
        base = _override_base(BASE_URL, BASE_OVERRIDE_ENV)
        payload = self._request("GET", f"{base}/sites")
        entries = payload.get("siteEntry")
        if not isinstance(entries, list):
            entries = []
        sites = []
        withheld = 0
        for entry in entries:
            if not isinstance(entry, Mapping):
                continue
            site_url = entry.get("siteUrl")
            if not isinstance(site_url, str):
                continue
            if not self._allowlist.contains(site_url):
                withheld += 1
                continue
            sites.append(
                {
                    "site_url": site_url,
                    "permission_level": entry.get("permissionLevel"),
                    "allowlisted": True,
                }
            )
        return {
            "ok": True,
            "sites": sites,
            "sites_returned": len(sites),
            "sites_withheld": withheld,
        }

    def search_analytics(
        self,
        *,
        site_url: object,
        start_date: object,
        end_date: object,
        dimensions: object = None,
        row_limit: object = 1000,
        start_row: object = 0,
        search_type: object = None,
        aggregation: object = None,
        data_state: object = None,
        filters: object = None,
    ) -> dict[str, Any]:
        """Run a bounded ``searchAnalytics.query`` against an allowlisted property."""
        url = self._site_endpoint("POST", site_url, "/searchAnalytics/query")
        body = build_search_analytics_body(
            start_date=start_date,
            end_date=end_date,
            dimensions=dimensions,
            row_limit=row_limit,
            start_row=start_row,
            search_type=search_type,
            aggregation=aggregation,
            data_state=data_state,
            filters=filters,
        )
        payload = self._request("POST", url, body=body)

        raw_rows = payload.get("rows")
        if not isinstance(raw_rows, list):
            raw_rows = []
        # The cap admits raw upstream rows; mapping then drops any that are not
        # objects. Advancing pagination by the post-filter count would re-read
        # rows already consumed, so track how many raw rows the cap admitted.
        window = raw_rows[: self._max_returned_rows]
        consumed = len(window)
        rows: list[dict[str, Any]] = []
        for row in window:
            if not isinstance(row, Mapping):
                continue
            keys = row.get("keys")
            rows.append(
                {
                    "keys": [str(k) for k in keys] if isinstance(keys, list) else [],
                    "clicks": _as_float(row.get("clicks")),
                    "impressions": _as_float(row.get("impressions")),
                    "ctr": _as_float(row.get("ctr")),
                    "position": _as_float(row.get("position")),
                }
            )
        # Truncation is our own cap dropping rows. A page that exactly fills the
        # upstream allowance is not truncated by us, but the caller still needs
        # to know more rows may exist -- Google returns at most `rowLimit`.
        truncated = len(raw_rows) > consumed
        page_full = len(raw_rows) >= body["rowLimit"]
        out: dict[str, Any] = {
            "ok": True,
            "rows": rows,
            "rows_returned": len(rows),
            "rows_truncated": truncated,
            "row_limit": body["rowLimit"],
            "start_row": body["startRow"],
        }
        agg = payload.get("responseAggregationType")
        if isinstance(agg, str):
            out["response_aggregation_type"] = agg
        if consumed and (truncated or page_full):
            out["next_start_row"] = body["startRow"] + consumed
            out["truncation_note"] = (
                "the returned rows are a partial page; continue with "
                "next_start_row as start_row to fetch the next page"
            )
        return out

    def list_sitemaps(self, *, site_url: object) -> dict[str, Any]:
        """List sitemaps submitted for an allowlisted property."""
        url = self._site_endpoint("GET", site_url, "/sitemaps")
        payload = self._request("GET", url)
        entries = payload.get("sitemap")
        if not isinstance(entries, list):
            entries = []
        sitemaps = []
        for entry in entries:
            if not isinstance(entry, Mapping):
                continue
            contents = []
            raw_contents = entry.get("contents")
            if isinstance(raw_contents, list):
                for item in raw_contents:
                    if not isinstance(item, Mapping):
                        continue
                    contents.append(
                        {
                            "type": item.get("type"),
                            "submitted": _as_int(item.get("submitted")),
                            "indexed": _as_int(item.get("indexed")),
                        }
                    )
            sitemaps.append(
                {
                    "path": entry.get("path"),
                    "type": entry.get("type"),
                    "is_pending": entry.get("isPending"),
                    "is_sitemaps_index": entry.get("isSitemapsIndex"),
                    "last_submitted": entry.get("lastSubmitted"),
                    "last_downloaded": entry.get("lastDownloaded"),
                    "errors": _as_int(entry.get("errors")),
                    "warnings": _as_int(entry.get("warnings")),
                    "contents": contents,
                }
            )
        return {"ok": True, "sitemaps": sitemaps, "sitemaps_returned": len(sitemaps)}

    def inspect_url(
        self,
        *,
        site_url: object,
        url: object,
        language_code: object = None,
    ) -> dict[str, Any]:
        """Inspect the indexed status of a URL that is inside an allowlisted property.

        Both the property (allowlist) and the URL (property scope) are validated
        before any request is built, so a rejected call never reaches the network
        and never touches a credential.
        """
        normalized = self._allowlist.require(site_url)
        validate_inspection_target(normalized, url)
        language = validate_language_code(language_code)
        endpoint = _override_base(INSPECTION_URL, _INSPECTION_OVERRIDE_ENV)
        payload = self._request(
            "POST",
            endpoint,
            body={
                "inspectionUrl": url,
                "siteUrl": normalized,
                "languageCode": language,
            },
        )
        result = payload.get("inspectionResult")
        if not isinstance(result, Mapping):
            raise GscError(
                "invalid_response", "the inspection response contained no inspectionResult"
            )
        return {
            "ok": True,
            "site_url": normalized,
            "inspection_url": url,
            "inspection": _flatten_inspection(result),
        }


def _flatten_inspection(result: Mapping[str, Any]) -> dict[str, Any]:
    """Flatten a ``UrlInspectionResult`` into the fields an operator reads.

    Only fields Search Console actually returns are surfaced; unknown keys are
    dropped rather than echoed, so the output shape is stable and no upstream
    string reaches the caller unfiltered.
    """
    out: dict[str, Any] = {}

    link = result.get("inspectionResultLink")
    if isinstance(link, str):
        out["inspection_result_link"] = link

    index = result.get("indexStatusResult")
    if isinstance(index, Mapping):
        out["verdict"] = index.get("verdict")
        out["coverage_state"] = index.get("coverageState")
        out["robots_txt_state"] = index.get("robotsTxtState")
        out["indexing_state"] = index.get("indexingState")
        out["page_fetch_state"] = index.get("pageFetchState")
        out["last_crawl_time"] = index.get("lastCrawlTime")
        out["crawled_as"] = index.get("crawledAs")
        out["google_canonical"] = index.get("googleCanonical")
        out["user_canonical"] = index.get("userCanonical")
        sitemaps = index.get("sitemap")
        if isinstance(sitemaps, list):
            out["sitemaps"] = [str(s) for s in sitemaps]
        referring = index.get("referringUrls")
        if isinstance(referring, list):
            out["referring_urls"] = [str(u) for u in referring]

    mobile = result.get("mobileUsabilityResult")
    if isinstance(mobile, Mapping):
        out["mobile_usability"] = {"verdict": mobile.get("verdict")}

    rich = result.get("richResultsResult")
    if isinstance(rich, Mapping):
        out["rich_results"] = {"verdict": rich.get("verdict")}

    amp = result.get("ampResult")
    if isinstance(amp, Mapping):
        out["amp"] = {"verdict": amp.get("verdict")}

    return out


__all__ = [
    "BASE_URL",
    "INSPECTION_URL",
    "GscClient",
    "build_search_analytics_body",
    "validate_inspection_target",
]