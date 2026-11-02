"""Schema validator and validation-helper tests."""
from __future__ import annotations

import pytest

from play_console_writer.schema import SchemaError, validate
from play_console_writer.validation import (
    ValidationError,
    validate_edit_id,
    validate_enum,
    validate_https_url,
    validate_package_name,
    validate_path_segment,
    validate_text_field,
)


# --------------------------------------------------------------------------
# Schema validator
# --------------------------------------------------------------------------

SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "maxLength": 5},
        "count": {"type": "integer", "minimum": 0, "maximum": 10},
        "tags": {"type": "array", "items": {"type": "string"}, "maxItems": 2},
        "mode": {"type": "string", "enum": ["a", "b"]},
    },
    "required": ["name"],
    "additionalProperties": False,
}


def test_valid_arguments_pass():
    validate(SCHEMA, {"name": "ok", "count": 3, "tags": ["x"], "mode": "a"})


def test_unknown_property_is_rejected():
    with pytest.raises(SchemaError, match="not a recognised argument"):
        validate(SCHEMA, {"name": "ok", "price": 9.99})


def test_missing_required_property_is_rejected():
    with pytest.raises(SchemaError, match="name is required"):
        validate(SCHEMA, {"count": 1})


def test_wrong_type_is_rejected():
    with pytest.raises(SchemaError, match="must be of type"):
        validate(SCHEMA, {"name": 5})


def test_bool_is_not_an_integer():
    with pytest.raises(SchemaError, match="must be of type"):
        validate(SCHEMA, {"name": "ok", "count": True})


def test_bounds_are_enforced():
    with pytest.raises(SchemaError, match=">= 0"):
        validate(SCHEMA, {"name": "ok", "count": -1})
    with pytest.raises(SchemaError, match="<= 10"):
        validate(SCHEMA, {"name": "ok", "count": 11})


def test_enum_is_enforced():
    with pytest.raises(SchemaError, match="must be one of"):
        validate(SCHEMA, {"name": "ok", "mode": "c"})


def test_max_length_and_items():
    with pytest.raises(SchemaError, match="at most 5 characters"):
        validate(SCHEMA, {"name": "toolong"})
    with pytest.raises(SchemaError, match="at most 2 items"):
        validate(SCHEMA, {"name": "ok", "tags": ["a", "b", "c"]})


def test_unknown_schema_keyword_is_a_programming_error():
    with pytest.raises(SchemaError, match="unsupported keyword"):
        validate({"type": "string", "oneOf": []}, "x")


def test_nested_item_validation():
    with pytest.raises(SchemaError, match=r"tags\[1\]"):
        validate(SCHEMA, {"name": "ok", "tags": ["a", 2]})


# --------------------------------------------------------------------------
# Field validators
# --------------------------------------------------------------------------

def test_edit_id_shape():
    assert validate_edit_id("abc-123_X") == "abc-123_X"
    for bad in ("", "has space", "has/slash", "a" * 300):
        with pytest.raises(ValidationError):
            validate_edit_id(bad)


def test_package_name_rejects_non_allowlist_shapes():
    with pytest.raises(ValidationError):
        validate_package_name("Single")
    with pytest.raises(ValidationError):
        validate_package_name("com.example.app ")


def test_path_segment_rejects_traversal():
    for bad in ("..", "a/b", "a:b"):
        with pytest.raises(ValidationError):
            validate_path_segment(bad, "field")


def test_enum_membership():
    assert validate_enum("icon", frozenset({"icon", "featureGraphic"}), "image_type") == "icon"
    with pytest.raises(ValidationError, match="must be one of"):
        validate_enum("wrong", frozenset({"icon"}), "image_type")


def test_text_field_rejects_control_chars_but_allows_newline():
    assert validate_text_field("line1\nline2", "f", max_length=100) == "line1\nline2"
    with pytest.raises(ValidationError, match="control characters"):
        validate_text_field("bad\x00", "f", max_length=100)


def test_text_field_empty_handling():
    with pytest.raises(ValidationError, match="must not be empty"):
        validate_text_field("   ", "f", max_length=100, allow_empty=False)
    assert validate_text_field("", "f", max_length=100) == ""


def test_https_url_requires_https_and_host():
    assert validate_https_url("https://example.com/x", "field") == "https://example.com/x"
    with pytest.raises(ValidationError, match="https"):
        validate_https_url("http://example.com", "field")
    with pytest.raises(ValidationError, match="host"):
        validate_https_url("https:///nohost", "field")