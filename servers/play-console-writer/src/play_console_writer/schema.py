"""A small, strict JSON-Schema subset validator for tool arguments.

The MCP SDK advertises the JSON schema of each tool, but does not enforce it:
an unknown property can still arrive at a handler. ``additionalProperties:
false`` is only a real guarantee if something checks it, so this module does.

Supported keywords: ``type`` (object/array/string/integer/number/boolean/null),
``properties``, ``required``, ``additionalProperties`` (bool or schema),
``items``, ``enum``, ``minimum``, ``maximum``, ``minItems``, ``maxItems``,
``minLength``, ``maxLength``, ``pattern``, ``nullable``.

Anything unsupported in a schema is a programming error, not a silent pass:
:func:`validate` raises :class:`SchemaError` for unknown keywords so a schema
mistake cannot quietly disable validation.
"""
from __future__ import annotations

import re
from typing import Any, Mapping

__all__ = ["SchemaError", "validate"]

_KNOWN_KEYWORDS = frozenset({
    "type", "properties", "required", "additionalProperties", "items", "enum",
    "minimum", "maximum", "minItems", "maxItems", "minLength", "maxLength",
    "pattern", "description", "title", "default", "examples", "format",
})


class SchemaError(ValueError):
    """Arguments do not match the tool's declared schema."""


def _check_keywords(schema: Mapping[str, Any], path: str) -> None:
    unknown = set(schema) - _KNOWN_KEYWORDS
    if unknown:
        raise SchemaError(
            f"internal schema error at {path or '<root>'}: unsupported keyword(s) "
            f"{sorted(unknown)}"
        )


def _type_of(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _matches_type(value: Any, expected: str) -> bool:
    actual = _type_of(value)
    if expected == "number":
        return actual in {"number", "integer"}
    return actual == expected


def validate(schema: Mapping[str, Any], value: Any, path: str = "") -> Any:
    """Validate ``value`` against ``schema``; return the value unchanged.

    Raises :class:`SchemaError` with a path-qualified message on the first
    violation, so an MCP client can self-correct.
    """
    _check_keywords(schema, path)
    where = path or "arguments"

    expected = schema.get("type")
    if expected is not None:
        allowed = [expected] if isinstance(expected, str) else list(expected)
        if not any(_matches_type(value, kind) for kind in allowed):
            raise SchemaError(
                f"{where} must be of type {'/'.join(allowed)}, got {_type_of(value)}"
            )

    if "enum" in schema and value not in schema["enum"]:
        raise SchemaError(f"{where} must be one of: {', '.join(map(str, schema['enum']))}")

    if isinstance(value, str):
        min_length = schema.get("minLength")
        max_length = schema.get("maxLength")
        if min_length is not None and len(value) < min_length:
            raise SchemaError(f"{where} must be at least {min_length} characters")
        if max_length is not None and len(value) > max_length:
            raise SchemaError(f"{where} must be at most {max_length} characters")
        pattern = schema.get("pattern")
        if pattern is not None and not re.search(pattern, value):
            raise SchemaError(f"{where} does not match the required pattern {pattern!r}")

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        minimum = schema.get("minimum")
        maximum = schema.get("maximum")
        if minimum is not None and value < minimum:
            raise SchemaError(f"{where} must be >= {minimum}")
        if maximum is not None and value > maximum:
            raise SchemaError(f"{where} must be <= {maximum}")

    if isinstance(value, list):
        min_items = schema.get("minItems")
        max_items = schema.get("maxItems")
        if min_items is not None and len(value) < min_items:
            raise SchemaError(f"{where} must contain at least {min_items} items")
        if max_items is not None and len(value) > max_items:
            raise SchemaError(f"{where} must contain at most {max_items} items")
        item_schema = schema.get("items")
        if item_schema is not None:
            for index, item in enumerate(value):
                validate(item_schema, item, f"{where}[{index}]")

    if isinstance(value, dict):
        properties: Mapping[str, Any] = schema.get("properties") or {}
        required = schema.get("required") or []
        for name in required:
            if name not in value:
                raise SchemaError(f"{where}.{name} is required")
        extra = schema.get("additionalProperties", True)
        for name, item in value.items():
            if name in properties:
                validate(properties[name], item, f"{where}.{name}")
            elif extra is False:
                raise SchemaError(
                    f"{where}.{name} is not a recognised argument"
                )
            elif isinstance(extra, Mapping):
                validate(extra, item, f"{where}.{name}")

    return value