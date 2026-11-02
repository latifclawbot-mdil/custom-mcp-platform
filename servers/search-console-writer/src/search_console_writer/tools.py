"""The four write tools: definitions, validation order, and result envelopes.

This module is plain Python and knows nothing about the MCP SDK, so every
security decision is testable in-process. It is the *only* place the four
mutations are named.

Every tool returns a JSON envelope as text::

    {"ok": true,  "tool": "...", "operation": "...", "applied": true, ...}
    {"ok": false, "tool": "...", "error": {"code": "...", "message": "..."}}

Validation order, which is a security property rather than an implementation
detail:

1. argument shape (an object, known keys, required keys present);
2. property *shape* (a well-formed Search Console identifier);
3. property *authorisation* (exact match in the write allowlist);
4. sitemap scope (inside the normalised property);
5. only then is a client — and therefore a credential — constructed.

A refused call performs no credential lookup at all.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from .allowlist import PropertyAllowlist
from .errors import GscError, InvalidArgumentError
from .validation import validate_property_identifier, validate_sitemap_url

_PREVALIDATE = Callable[[Mapping[str, Any], str], None]


@dataclass(frozen=True)
class ToolSpec:
    """A write tool's public contract, including its truthful annotations."""

    name: str
    description: str
    required: tuple[str, ...] = ()
    optional: tuple[str, ...] = ()
    has_property_argument: bool = False
    prevalidate: _PREVALIDATE | None = None
    # Truthful MCP hints. All four tools write; only the two deletes destroy.
    # PUT and DELETE are idempotent at the HTTP level.
    destructive: bool = False
    idempotent: bool = True
    open_world: bool = True
    allows_extra: bool = False
    handler: Callable[..., dict[str, Any]] = field(default=None)  # type: ignore[assignment]

    @property
    def accepted(self) -> tuple[str, ...]:
        return self.required + self.optional

    def input_schema(self) -> dict[str, Any]:
        properties: dict[str, Any] = {}
        for name in self.accepted:
            properties[name] = _PROPERTY_SCHEMAS[name]
        return {
            "type": "object",
            "properties": properties,
            "required": list(self.required),
            "additionalProperties": False,
        }


_PROPERTY_SCHEMAS: dict[str, dict[str, Any]] = {
    "site_url": {
        "type": "string",
        "description": (
            "Search Console property identifier, exactly as configured in the "
            "write allowlist (e.g. 'sc-domain:example.com' or "
            "'https://www.example.com/')."
        ),
    },
    "sitemap_url": {
        "type": "string",
        "description": (
            "Absolute http(s) URL of the sitemap, inside site_url. It is "
            "submitted to or deleted from Search Console; Google fetches it "
            "itself, this server never does."
        ),
    },
}


@dataclass
class ToolResult:
    """What a tool call produced, independent of the MCP transport."""

    text: str
    is_error: bool = False


class ToolRegistry:
    """Owns the write tool set and dispatches validated calls."""

    def __init__(
        self,
        *,
        client_factory: Callable[[], Any],
        allowlist: PropertyAllowlist | None = None,
    ) -> None:
        self._client_factory = client_factory
        if allowlist is not None and len(allowlist) == 0:
            from .errors import EmptyAllowlistError

            raise EmptyAllowlistError("allowlist must not be empty")
        self._allowlist = allowlist
        self._specs: dict[str, ToolSpec] = {}
        self._register_defaults()

    # -- registration ------------------------------------------------------

    def _register_defaults(self) -> None:
        self._add(
            ToolSpec(
                name="gsc_add_site",
                description=(
                    "WRITE: add a property to the Search Console account "
                    "(sites.add, HTTP PUT). The property must be in this "
                    "server's write allowlist. Adds a property you already "
                    "own; it does not verify ownership."
                ),
                required=("site_url",),
                has_property_argument=True,
                destructive=False,
                handler=lambda client, args, normalized: client.add_site(site_url=args["site_url"]),
            )
        )
        self._add(
            ToolSpec(
                name="gsc_delete_site",
                description=(
                    "WRITE, DESTRUCTIVE: remove a property from the Search "
                    "Console account (sites.delete, HTTP DELETE). The property "
                    "must be in this server's write allowlist. This removes the "
                    "property from the account; it does not delete the site."
                ),
                required=("site_url",),
                has_property_argument=True,
                destructive=True,
                handler=lambda client, args, normalized: client.delete_site(site_url=args["site_url"]),
            )
        )
        self._add(
            ToolSpec(
                name="gsc_submit_sitemap",
                description=(
                    "WRITE: submit a sitemap URL for an allowlisted property "
                    "(sitemaps.submit, HTTP PUT). The sitemap URL must be inside "
                    "the property. Google fetches the sitemap; this server does "
                    "not download it."
                ),
                required=("site_url", "sitemap_url"),
                has_property_argument=True,
                prevalidate=_prevalidate_sitemap,
                destructive=False,
                handler=lambda client, args, normalized: client.submit_sitemap(
                    site_url=args["site_url"], sitemap_url=args["sitemap_url"]
                ),
            )
        )
        self._add(
            ToolSpec(
                name="gsc_delete_sitemap",
                description=(
                    "WRITE, DESTRUCTIVE: remove a sitemap from an allowlisted "
                    "property (sitemaps.delete, HTTP DELETE). The sitemap URL "
                    "must be inside the property."
                ),
                required=("site_url", "sitemap_url"),
                has_property_argument=True,
                prevalidate=_prevalidate_sitemap,
                destructive=True,
                handler=lambda client, args, normalized: client.delete_sitemap(
                    site_url=args["site_url"], sitemap_url=args["sitemap_url"]
                ),
            )
        )

    def _add(self, spec: ToolSpec) -> None:
        self._specs[spec.name] = spec

    # -- introspection -----------------------------------------------------

    def names(self) -> list[str]:
        return list(self._specs)

    def specs(self) -> list[ToolSpec]:
        return list(self._specs.values())

    def get(self, name: str) -> ToolSpec | None:
        return self._specs.get(name)

    # -- dispatch ----------------------------------------------------------

    def call(self, name: str, arguments: object) -> ToolResult:
        """Validate, dispatch, and envelope the result. Never raises."""
        spec = self._specs.get(name)
        if spec is None:
            return self._error(envelope_tool=name, err=InvalidArgumentError("unknown tool"))

        try:
            args = _validate_arguments(spec, arguments)
            normalized = ""
            if spec.has_property_argument:
                # Shape first, so a malformed property is reported as malformed
                # rather than as "not allowlisted" (which would also disclose
                # allowlist membership probing).
                validate_property_identifier(args.get("site_url"))
                if self._allowlist is None:
                    raise InvalidArgumentError("this server has no configured write allowlist")
                normalized = self._allowlist.require(args.get("site_url"))
            if spec.prevalidate is not None:
                spec.prevalidate(args, normalized)
        except GscError as exc:
            return self._error(envelope_tool=name, err=exc)

        try:
            client = self._client_factory()
            payload = spec.handler(client, args, normalized)
        except GscError as exc:
            return self._error(envelope_tool=name, err=exc)
        except Exception as exc:  # never let a traceback reach the caller
            err = GscError(
                "upstream_error",
                f"unexpected failure in {name} ({type(exc).__name__})",
            )
            return self._error(envelope_tool=name, err=err)

        envelope: dict[str, Any] = {"tool": name}
        if isinstance(payload, Mapping):
            envelope.update(payload)
        else:
            envelope["ok"] = True
            envelope["result"] = payload
        envelope.setdefault("ok", True)
        try:
            text = json.dumps(envelope, ensure_ascii=False, sort_keys=False, allow_nan=False)
        except (TypeError, ValueError):
            return self._error(
                envelope_tool=name,
                err=GscError("invalid_response", "result could not be serialised"),
            )
        return ToolResult(text=text, is_error=False)

    @staticmethod
    def _error(*, envelope_tool: str, err: GscError) -> ToolResult:
        envelope = {"tool": envelope_tool}
        envelope.update(err.to_payload())
        return ToolResult(
            text=json.dumps(envelope, ensure_ascii=False, allow_nan=False),
            is_error=True,
        )


def _prevalidate_sitemap(args: Mapping[str, Any], normalized: str) -> None:
    """Prove the sitemap URL is inside the property, before any credential."""
    validate_sitemap_url(normalized, args.get("sitemap_url"))


def _validate_arguments(spec: ToolSpec, arguments: object) -> dict[str, Any]:
    """Check argument presence and key set. Value validation happens deeper."""
    if arguments is None:
        raise InvalidArgumentError("arguments must be an object")
    if not isinstance(arguments, Mapping):
        raise InvalidArgumentError("arguments must be an object")
    unknown = set(arguments) - set(spec.accepted)
    if unknown and not spec.allows_extra:
        raise InvalidArgumentError(
            "unknown argument(s) were supplied",
            details={"count": str(len(unknown))},
        )
    missing = [name for name in spec.required if arguments.get(name) in (None, "")]
    if missing:
        raise InvalidArgumentError(
            "required argument(s) are missing", details={"count": str(len(missing))}
        )
    return {k: v for k, v in arguments.items() if k in spec.accepted}


__all__ = ["ToolRegistry", "ToolResult", "ToolSpec"]