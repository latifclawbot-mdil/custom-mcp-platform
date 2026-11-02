"""Strict validation of identifiers and bodies interpolated into request paths.

Same philosophy as the read-only server: Google Play API calls put
caller-supplied identifiers straight into URL path segments, so anything that
could escape a segment (slashes, dot-segments, query or fragment delimiters,
percent-encoding, whitespace, control characters) is *rejected* rather than
escaped. A caller cannot redirect a request.

Write-specific additions:

* :func:`validate_enum` — image / deobfuscation / latency types are constrained
  to the exact enum published in the discovery document. An unknown value is
  refused here rather than sent upstream.
* :func:`validate_edit_id` — ``editId`` is a path segment with a strict shape.
* :func:`validate_text_field` — listing/reply text is length-capped and
  whitespace-scrubbed with an explicit maximum, so a write cannot smuggle a
  huge or control-character payload.
"""
from __future__ import annotations

import re

__all__ = [
    "ValidationError",
    "validate_package_name",
    "validate_path_segment",
    "validate_integer_id",
    "validate_page_token",
    "validate_enum",
    "validate_edit_id",
    "validate_text_field",
    "validate_https_url",
    "IMAGE_TYPES",
    "DEOBFUSCATION_FILE_TYPES",
    "LATENCY_TOLERANCES",
]


class ValidationError(ValueError):
    """Caller-supplied input failed validation. Never leaks credentials."""


# Android application IDs: dot-separated lowercase segments beginning with a
# letter, alphanumerics and underscores only, at least two segments.
_PACKAGE_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$")
_PACKAGE_MAX = 200

# Opaque identifiers occupying a single path segment.
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]{0,255}$")

# A Play edit id is an opaque, URL-safe token.
_EDIT_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{1,128}$")

_TOKEN_RE = re.compile(r"^[A-Za-z0-9._\-]{1,512}$")

_UNSAFE_CHARS = {"/": "path separator", "\\": "path separator",
                 "?": "query delimiter", "#": "fragment delimiter",
                 ":": "scheme delimiter"}

#: Enum of ``edits.images.upload`` / list / delete ``imageType`` path values.
IMAGE_TYPES = frozenset({
    "appImageTypeUnspecified",
    "phoneScreenshots",
    "sevenInchScreenshots",
    "tenInchScreenshots",
    "tvScreenshots",
    "wearScreenshots",
    "icon",
    "featureGraphic",
    "tvBanner",
})

#: Enum of ``edits.deobfuscationfiles.upload`` ``deobfuscationFileType``.
DEOBFUSCATION_FILE_TYPES = frozenset({
    "deobfuscationFileTypeUnspecified",
    "proguard",
    "nativeCode",
})

#: Enum of the ``latencyTolerance`` query parameter.
LATENCY_TOLERANCES = frozenset({
    "PRODUCT_UPDATE_LATENCY_TOLERANCE_UNSPECIFIED",
    "PRODUCT_UPDATE_LATENCY_TOLERANCE_LATENCY_SENSITIVE",
    "PRODUCT_UPDATE_LATENCY_TOLERANCE_LATENCY_TOLERANT",
})


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


def validate_edit_id(value: str) -> str:
    """Validate an ``editId`` returned by ``edits_insert``."""
    field = "edit_id"
    if not isinstance(value, str):
        raise ValidationError(f"{field} must be a string")
    if not value:
        raise ValidationError(f"{field} must not be empty")
    if not _EDIT_ID_RE.match(value):
        raise ValidationError(
            f"{field} must be 1-128 characters of letters, digits, '_' or '-'"
        )
    return value


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


def validate_enum(value: str, allowed: frozenset[str], field: str) -> str:
    """Constrain a value to the exact enum published in the discovery document."""
    if not isinstance(value, str):
        raise ValidationError(f"{field} must be a string")
    if value not in allowed:
        options = ", ".join(sorted(allowed))
        raise ValidationError(f"{field} must be one of: {options}")
    return value


def validate_text_field(value: str, field: str, *, max_length: int,
                        allow_empty: bool = True) -> str:
    """Validate a free-text field destined for a write body.

    Control characters other than a normalised newline/tab are rejected, the
    length is capped with an explicit maximum, and (unless ``allow_empty``) an
    all-whitespace value is refused so a caller cannot silently blank a field.
    """
    if not isinstance(value, str):
        raise ValidationError(f"{field} must be a string")
    if not value.strip() and not allow_empty:
        raise ValidationError(f"{field} must not be empty")
    if len(value) > max_length:
        raise ValidationError(f"{field} exceeds {max_length} characters")
    for char in value:
        code = ord(char)
        if code == 0x09 or code == 0x0A:  # tab / newline are allowed
            continue
        if code < 0x20 or code == 0x7F:
            raise ValidationError(f"{field} must not contain control characters")
    return value


def validate_https_url(value: str, field: str) -> str:
    """Validate an https URL (contact website, promotional video)."""
    from urllib.parse import urlparse

    if not isinstance(value, str):
        raise ValidationError(f"{field} must be a string")
    if not value:
        raise ValidationError(f"{field} must not be empty")
    if len(value) > 2000:
        raise ValidationError(f"{field} exceeds 2000 characters")
    parsed = urlparse(value)
    if parsed.scheme != "https":
        raise ValidationError(f"{field} must use the https scheme")
    if not parsed.netloc:
        raise ValidationError(f"{field} must include a host")
    return value