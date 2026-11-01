"""Percent-encoding of the property path segment.

Search Console's webmasters v3 URLs place the property identifier in the final
path segment::

    /webmasters/v3/sites/{siteUrl}/searchAnalytics/query

Google's own examples encode it with ``%3A`` and ``%2F``
(``https%3A%2F%2Fwww.example.com%2F``), which is what ``quote(safe="")``
produces. Using ``safe=""`` — rather than the usual ``safe="/"`` — is also a
security property: with no slash left unescaped, a property string cannot add a
path segment, perform ``..`` traversal, or inject a query or fragment. Combined
with the fixed base host, the property segment is the *only* caller-influenced
part of the URL, and it cannot change the host.
"""

from __future__ import annotations

from urllib.parse import quote

from .errors import InvalidArgumentError

MAX_PROPERTY_LEN = 2048


def encode_property(property_id: object) -> str:
    """Percent-encode a property identifier for use as a URL path segment."""
    if not isinstance(property_id, str) or not property_id:
        raise InvalidArgumentError("property identifier must be a non-empty string")
    if len(property_id) > MAX_PROPERTY_LEN:
        raise InvalidArgumentError(
            "property identifier exceeds maximum length",
            details={"max_length": str(MAX_PROPERTY_LEN)},
        )
    # quote() with safe="" escapes "/", ":", "?", "#", "%", "." stays literal
    # inside a segment and is harmless once "/" is escaped.
    return quote(property_id, safe="")