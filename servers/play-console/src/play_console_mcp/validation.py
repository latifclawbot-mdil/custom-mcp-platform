"""Strict validation of identifiers interpolated into Google API request paths.

Google Play API calls put caller-supplied identifiers straight into URL path
segments. Anything that could escape a segment (slashes, dot-segments, query or
fragment delimiters, percent-encoding, whitespace, control characters) is
rejected rather than escaped, so a caller cannot redirect a request.

Errors name the offending field so an MCP client can self-correct.
"""
from __future__ import annotations

import re

__all__ = [
    "ValidationError",
    "validate_package_name",
    "validate_path_segment",
    "validate_review_id",
    "validate_integer_id",
    "validate_page_token",
]


class ValidationError(ValueError):
    """Caller-supplied input failed validation. Never leaks credentials."""


# Android application IDs: dot-separated lowercase segments beginning with a
# letter, alphanumerics and underscores only, at least two segments.
_PACKAGE_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$")
_PACKAGE_MAX = 200

# Opaque identifiers occupying a single path segment (SKUs, product IDs,
# order IDs, purchase tokens, review IDs).
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]{0,255}$")

_TOKEN_RE = re.compile(r"^[A-Za-z0-9._\-]{1,512}$")

_UNSAFE_CHARS = {"/": "path separator", "\\": "path separator",
                 "?": "query delimiter", "#": "fragment delimiter",
                 ":": "scheme delimiter"}


def _reject_unsafe(value: str, field: str) -> None:
    if "%" in value:
        raise ValidationError(f"{field} must not contain percent-encoding")
    for char, description in _UNSAFE_CHARS.items():
        if char in value:
            raise ValidationError(f"{field} must not contain a {description}")
    if value != value.strip() or any(c.isspace() for c in value):
        raise ValidationError(f"{field} must not contain whitespace")
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in value):
        raise ValidationError(f"{field} must not contain control characters")
    if ".." in value:
        raise ValidationError(f"{field} must not contain '..' (path traversal)")


def validate_package_name(name: str) -> str:
    """Validate an Android package name destined for a URL path segment."""
    field = "package_name"
    if not isinstance(name, str):
        raise ValidationError(f"{field} must be a string")
    if not name:
        raise ValidationError(f"{field} must not be empty")
    if len(name) > _PACKAGE_MAX:
        raise ValidationError(f"{field} exceeds {_PACKAGE_MAX} characters")
    _reject_unsafe(name, field)
    if not _PACKAGE_RE.match(name):
        raise ValidationError(
            f"{field} must be a lowercase dotted identifier (e.g. com.example.app)"
        )
    return name


def validate_path_segment(value: str, field: str) -> str:
    """Validate an opaque identifier that will occupy a single path segment."""
    if not isinstance(value, str):
        raise ValidationError(f"{field} must be a string")
    if not value:
        raise ValidationError(f"{field} must not be empty")
    _reject_unsafe(value, field)
    if not _ID_RE.match(value):
        raise ValidationError(
            f"{field} must be 1-256 characters of letters, digits, '.', '_' or '-'"
        )
    return value


def validate_review_id(review_id: str) -> str:
    """Play review IDs are opaque, long, URL-safe strings."""
    return validate_path_segment(review_id, "review_id")


def validate_integer_id(value, field: str) -> int:
    """Validate a non-negative integer path component (version codes, IDs)."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError(f"{field} must be an integer")
    if value < 0:
        raise ValidationError(f"{field} must not be negative")
    if value > 2**63 - 1:
        raise ValidationError(f"{field} is out of range")
    return value


def validate_page_token(token, field: str = "page_token") -> str:
    """Pagination tokens are opaque but must stay within one query value."""
    if token is None:
        return ""
    if not isinstance(token, str):
        raise ValidationError(f"{field} must be a string")
    if not token:
        return ""
    if not _TOKEN_RE.match(token):
        raise ValidationError(f"{field} contains unsupported characters")
    return token