"""Structural and scope validation for write arguments.

Two caller-supplied values reach a URL path: the property identifier and, for
the sitemap methods, a sitemap URL. Both are validated here, before any request
object or credential exists:

* the *shape* check rejects anything that is not the exact identifier form
  Search Console accepts, plus control characters, credentials, query strings,
  fragments and over-long values;
* the *scope* check for a sitemap URL proves it is inside the property it is
  being submitted to, so an allowlisted property cannot be used to submit or
  delete a sitemap on a host the caller never had rights to.

The dot-segment rejection is not cosmetic: a raw string prefix check cannot
prove scope when the same string resolves to a different path under RFC 3986
(``/a/../admin`` -> ``/admin``), so any ``.``/``..`` segment — raw or
percent-encoded — fails closed.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from .errors import InvalidArgumentError

MAX_PROPERTY_LEN = 2048
MAX_SITEMAP_URL_LEN = 2048

_ABSOLUTE_SCHEMES = ("http", "https")
_ENC_DOT_RE = re.compile(r"%2e", re.IGNORECASE)
_ENC_SLASH_RE = re.compile(r"%2f", re.IGNORECASE)
_ENCODED_PERCENT_OR_BACKSLASH_RE = re.compile(r"%(?:25|5c)", re.IGNORECASE)
_DOMAIN_HOST_RE = re.compile(r"^[a-z0-9]([a-z0-9\-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9\-]*[a-z0-9])?)+$")


def _reject_control_characters(value: str, field: str) -> None:
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        raise InvalidArgumentError(f"{field} contains control characters")


def _has_dot_segment(path: str) -> bool:
    """Whether ``path`` contains a ``.`` or ``..`` segment, raw or encoded."""
    if _ENCODED_PERCENT_OR_BACKSLASH_RE.search(path):
        return True
    decoded = _ENC_SLASH_RE.sub("/", _ENC_DOT_RE.sub(".", path))
    decoded = decoded.replace("\\", "/")
    return any(segment in (".", "..") for segment in decoded.split("/"))


def validate_property_identifier(value: object) -> str:
    """Validate a property identifier's shape and return it unchanged.

    Accepts the two forms Search Console uses — ``sc-domain:<host>`` and
    ``http(s)://<host><path>`` — and refuses everything else. The allowlist is
    the authority on *which* properties are permitted; this only proves the
    string is a well-formed property, so no malformed value reaches a URL
    builder.
    """
    if not isinstance(value, str) or not value:
        raise InvalidArgumentError("site_url must be a non-empty string")
    if len(value) > MAX_PROPERTY_LEN:
        raise InvalidArgumentError(
            "site_url is too long", details={"max_length": str(MAX_PROPERTY_LEN)}
        )
    _reject_control_characters(value, "site_url")
    if value != value.strip():
        raise InvalidArgumentError("site_url must not contain surrounding whitespace")
    if any(ch.isspace() for ch in value):
        raise InvalidArgumentError("site_url must not contain whitespace")

    lowered = value.lower()
    if lowered.startswith("sc-domain:"):
        host = value[len("sc-domain:") :]
        if not host or not _DOMAIN_HOST_RE.match(host.lower()):
            raise InvalidArgumentError(
                "sc-domain property must name a valid domain host",
                details={"reason": "invalid_domain_property"},
            )
        return value

    parts = urlsplit(value)
    if parts.scheme not in _ABSOLUTE_SCHEMES:
        raise InvalidArgumentError(
            "site_url must be a sc-domain: property or an absolute http:// or "
            "https:// URL"
        )
    if not parts.netloc or not parts.hostname:
        raise InvalidArgumentError("site_url must include a host")
    if parts.username or parts.password:
        raise InvalidArgumentError("site_url must not contain credentials")
    if parts.query:
        raise InvalidArgumentError("site_url must not contain a query string")
    if parts.fragment:
        raise InvalidArgumentError("site_url must not contain a fragment")
    if _has_dot_segment(parts.path):
        raise InvalidArgumentError(
            "site_url must not contain '.' or '..' path segments",
            details={"reason": "path_traversal"},
        )
    return value


def validate_sitemap_url(property_id: str, value: object) -> str:
    """Validate that ``value`` is a sitemap URL inside ``property_id``.

    ``property_id`` must be the *normalized* allowlisted property (the value
    :meth:`PropertyAllowlist.require` returned), so scope is checked against the
    server's canonical form and not the caller's raw string. Returns the URL
    unchanged when valid.
    """
    if not isinstance(value, str) or not value:
        raise InvalidArgumentError("sitemap_url must be a non-empty string")
    if len(value) > MAX_SITEMAP_URL_LEN:
        raise InvalidArgumentError(
            "sitemap_url is too long", details={"max_length": str(MAX_SITEMAP_URL_LEN)}
        )
    _reject_control_characters(value, "sitemap_url")
    if any(ch.isspace() for ch in value):
        raise InvalidArgumentError("sitemap_url must not contain whitespace")

    parts = urlsplit(value)
    if parts.scheme not in _ABSOLUTE_SCHEMES:
        raise InvalidArgumentError("sitemap_url must be an absolute http:// or https:// URL")
    if not parts.netloc:
        raise InvalidArgumentError("sitemap_url must include a host")
    if parts.username or parts.password:
        raise InvalidArgumentError("sitemap_url must not contain credentials")
    if parts.fragment:
        raise InvalidArgumentError("sitemap_url must not contain a fragment")
    if _has_dot_segment(parts.path):
        raise InvalidArgumentError(
            "sitemap_url must not contain '.' or '..' path segments",
            details={"reason": "path_traversal"},
        )

    host = (parts.hostname or "").lower()
    if not host:
        raise InvalidArgumentError("sitemap_url must include a host")

    if property_id.startswith("sc-domain:"):
        domain = property_id[len("sc-domain:") :].lower()
        if host != domain and not host.endswith("." + domain):
            raise InvalidArgumentError(
                "sitemap_url is not under the specified property",
                details={"reason": "host_out_of_scope"},
            )
        return value

    if property_id.startswith("http://") or property_id.startswith("https://"):
        prop = urlsplit(property_id)
        prop_host = (prop.hostname or "").lower()
        if host != prop_host:
            raise InvalidArgumentError(
                "sitemap_url is not under the specified property",
                details={"reason": "host_out_of_scope"},
            )
        # Scheme must match too: a URL-prefix property is defined by its scheme
        # and host, and Search Console treats http:// and https:// as different
        # properties.
        if parts.scheme != prop.scheme:
            raise InvalidArgumentError(
                "sitemap_url scheme does not match the property",
                details={"reason": "scheme_out_of_scope"},
            )
        if not value.startswith(property_id):
            raise InvalidArgumentError(
                "sitemap_url is not under the specified property",
                details={"reason": "path_out_of_scope"},
            )
        return value

    raise InvalidArgumentError("property identifier form is not supported for sitemap operations")


__all__ = [
    "MAX_PROPERTY_LEN",
    "MAX_SITEMAP_URL_LEN",
    "validate_property_identifier",
    "validate_sitemap_url",
]