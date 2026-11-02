"""Percent-encoding of property and sitemap path segments.

Both the property identifier and the sitemap URL occupy a path segment in the
webmasters v3 write URLs::

    PUT    /webmasters/v3/sites/{siteUrl}
    DELETE /webmasters/v3/sites/{siteUrl}
    PUT    /webmasters/v3/sites/{siteUrl}/sitemaps/{feedpath}
    DELETE /webmasters/v3/sites/{siteUrl}/sitemaps/{feedpath}

Google's own examples encode a property with ``%3A`` and ``%2F``
(``https%3A%2F%2Fwww.example.com%2F``), which is what ``quote(safe="")``
produces. Using ``safe=""`` — rather than the usual ``safe="/"`` — is also a
security property: with no slash left unescaped, a value cannot add a path
segment, perform ``..`` traversal, or inject a query or fragment. Combined with
the fixed base host, the encoded segments are the *only* caller-influenced part
of the URL, and they cannot change the host.
"""

from __future__ import annotations

from urllib.parse import quote

from .errors import InvalidArgumentError

MAX_SEGMENT_LEN = 2048


def encode_segment(value: object, *, field: str = "value") -> str:
    """Percent-encode a caller-supplied value for use as a URL path segment."""
    if not isinstance(value, str) or not value:
        raise InvalidArgumentError(f"{field} must be a non-empty string")
    if len(value) > MAX_SEGMENT_LEN:
        raise InvalidArgumentError(
            f"{field} exceeds maximum length",
            details={"max_length": str(MAX_SEGMENT_LEN)},
        )
    return quote(value, safe="")


def encode_property(property_id: object) -> str:
    """Percent-encode a property identifier (the ``{siteUrl}`` segment)."""
    return encode_segment(property_id, field="property identifier")


def encode_feedpath(feedpath: object) -> str:
    """Percent-encode a sitemap URL (the ``{feedpath}`` segment)."""
    return encode_segment(feedpath, field="sitemap url")