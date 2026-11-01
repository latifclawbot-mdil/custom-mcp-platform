"""Tool definitions, argument validation, and result envelopes.

Kept independent of the MCP SDK on purpose: the registry is plain Python, so
every security decision (allowlist, bounds, scope, redaction) is testable
in-process without a transport, and the MCP layer in :mod:`server` is a thin
adapter. The alternative — decorators with SDK-specific validation — would put
the interesting logic behind a handshake.

Every tool returns a JSON envelope as text:

    {"ok": true,  "tool": "...", "site_url": ..., ...payload}
    {"ok": false, "tool": "...", "error": {"code": "...", "message": "..."}}

Errors are envelopes, never raised to the transport, so an MCP client always
gets a parseable, redacted result rather than an opaque isError with a stack
trace. Unknown arguments are rejected rather than ignored, so no undocumented
parameter can reach a URL builder.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

from .allowlist import PropertyAllowlist
from .errors import GscError, InvalidArgumentError

_UNSET = object()


@dataclass(frozen=True)
class ToolSpec:
    """A tool's public contract."""

    name: str
    description: str
    required: tuple[str, ...] = ()
    optional: tuple[str, ...] = ()
    # When True the tool's first required argument is a property, and it is
    # checked against the allowlist *before* a client (and therefore a
    # credential) is constructed.
    has_property_argument: bool = False
    # Optional pure validation that runs before a client is constructed, so
    # argument errors never trigger credential acquisition.
    prevalidate: Callable[[Mapping[str, Any]], None] | None = None
    # Deliberately always False. A free-form kwargs tool could smuggle a host,
    # a token, or an unknown query parameter past validation.
    allows_extra: bool = False
    handler: Callable[..., dict[str, Any]] = field(default=None)  # type: ignore[assignment]

    @property
    def accepted(self) -> tuple[str, ...]:
        return self.required + self.optional

    def input_schema(self) -> dict[str, Any]:
        """A JSON Schema for the tool's arguments, matching the MCP contract."""
        properties: dict[str, Any] = {}
        for name in self.required:
            properties[name] = _PROPERTY_SCHEMAS[name]
        for name in self.optional:
            properties[name] = _PROPERTY_SCHEMAS[name]
        schema: dict[str, Any] = {
            "type": "object",
            "properties": properties,
            "required": list(self.required),
            "additionalProperties": False,
        }
        return schema


_PROPERTY_SCHEMAS: dict[str, dict[str, Any]] = {
    "site_url": {
        "type": "string",
        "description": (
            "Search Console property identifier, exactly as configured in the "
            "allowlist (e.g. 'sc-domain:example.com' or "
            "'https://www.example.com/')."
        ),
    },
    "start_date": {
        "type": "string",
        "description": "Start of the date range, inclusive, as YYYY-MM-DD.",
    },
    "end_date": {
        "type": "string",
        "description": "End of the date range, inclusive, as YYYY-MM-DD.",
    },
    "dimensions": {
        "type": "array",
        "items": {"type": "string"},
        "description": "Zero or more dimensions to group by.",
    },
    "row_limit": {
        "type": "integer",
        "minimum": 1,
        "maximum": 25000,
        "description": "Rows per API page, 1-25000 (default 1000).",
    },
    "start_row": {
        "type": "integer",
        "minimum": 0,
        "description": "Zero-based row offset for pagination.",
    },
    "search_type": {
        "type": "string",
        "description": "web, image, video, news, discover, or googleNews.",
    },
    "aggregation": {
        "type": "string",
        "description": "auto, byPage, byProperty, or byNewsShowcasePanel.",
    },
    "data_state": {
        "type": "string",
        "description": "'final' (default) or 'all' for fresh, possibly incomplete data.",
    },
    "filters": {
        "type": "array",
        "items": {"type": "object"},
        "description": "Dimension filter objects (ANDed together).",
    },
    "url": {
        "type": "string",
        "description": "Fully-qualified http(s) URL to inspect; must be inside site_url.",
    },
    "language_code": {
        "type": "string",
        "description": "BCP-47 language for translated messages, e.g. 'en-US'.",
    },
}


@dataclass
class ToolResult:
    """What a tool call produced, independent of the MCP transport."""

    text: str
    is_error: bool = False


class ToolRegistry:
    """Owns the tool set and dispatches validated calls.

    ``client_factory`` is called at most once per call, and only after argument
    validation has passed. That ordering is what makes credential acquisition
    lazy: a rejected call never constructs a client, so it never triggers ADC.
    """

    def __init__(
        self,
        *,
        client_factory: Callable[[], Any],
        allowlist: PropertyAllowlist | None = None,
    ) -> None:
        self._client_factory = client_factory
        # If an allowlist is supplied it is validated eagerly, so a
        # misconfigured registry fails at construction rather than on first call.
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
                name="gsc_list_sites",
                description=(
                    "List the Search Console properties the configured credential "
                    "can access, marking which of them this server is permitted to "
                    "query. Read-only. Takes no arguments."
                ),
                handler=lambda client, args: client.list_sites(),
            )
        )
        self._add(
            ToolSpec(
                name="gsc_search_analytics",
                description=(
                    "Query Search Console Search Analytics (clicks, impressions, "
                    "CTR, average position) for one allowlisted property over a "
                    "bounded date range. Read-only. Supports grouping dimensions, "
                    "filters, pagination, and an explicit returned-row cap."
                ),
                required=("site_url", "start_date", "end_date"),
                optional=(
                    "dimensions",
                    "row_limit",
                    "start_row",
                    "search_type",
                    "aggregation",
                    "data_state",
                    "filters",
                ),
                has_property_argument=True,
                prevalidate=_prevalidate_search_analytics,
                handler=self._handle_search_analytics,
            )
        )
        self._add(
            ToolSpec(
                name="gsc_list_sitemaps",
                description=(
                    "List sitemaps submitted for one allowlisted property, with "
                    "their submission and download state, warning and error "
                    "counts, and per-content-type entry counts. Read-only."
                ),
                required=("site_url",),
                has_property_argument=True,
                handler=lambda client, args: client.list_sitemaps(
                    site_url=args["site_url"]
                ),
            )
        )
        self._add(
            ToolSpec(
                name="gsc_inspect_url",
                description=(
                    "Inspect Google's indexed information for a single URL that is "
                    "inside an allowlisted property: index verdict, coverage state, "
                    "robots and indexing state, canonical selection, and last crawl "
                    "time. Read-only; it does not request indexing."
                ),
                required=("site_url", "url"),
                optional=("language_code",),
                has_property_argument=True,
                prevalidate=_prevalidate_inspect,
                handler=lambda client, args: client.inspect_url(
                    site_url=args["site_url"],
                    url=args["url"],
                    language_code=args.get("language_code"),
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
            err = InvalidArgumentError("unknown tool")
            return self._error(envelope_tool=name, err=err)

        try:
            args = _validate_arguments(spec, arguments)
            # The property is checked against the allowlist *here*, before any
            # client or credential exists. The client re-checks it (defence in
            # depth), but a disallowed property must never reach credential
            # acquisition at all.
            if spec.has_property_argument and self._allowlist is not None:
                self._allowlist.require(args.get("site_url"))
            # Pure value validation also runs before any client exists.
            if spec.prevalidate is not None:
                spec.prevalidate(args)
        except GscError as exc:
            return self._error(envelope_tool=name, err=exc)

        try:
            client = self._client_factory()
            payload = spec.handler(client, args)
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
            # allow_nan=False keeps the envelope strict JSON: Python would
            # otherwise emit bare NaN/Infinity tokens that strict parsers reject.
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

    def _handle_search_analytics(self, client: Any, args: Mapping[str, Any]) -> dict[str, Any]:
        return client.search_analytics(
            site_url=args["site_url"],
            start_date=args["start_date"],
            end_date=args["end_date"],
            dimensions=args.get("dimensions"),
            row_limit=args.get("row_limit", 1000),
            start_row=args.get("start_row", 0),
            search_type=args.get("search_type"),
            aggregation=args.get("aggregation"),
            data_state=args.get("data_state"),
            filters=args.get("filters"),
        )


def _prevalidate_search_analytics(args: Mapping[str, Any]) -> None:
    """Validate everything about a search-analytics call that needs no credential.

    Runs before the client is constructed. The client re-validates (defence in
    depth), but this ordering means a malformed call never triggers a token
    fetch.
    """
    from .client import build_search_analytics_body

    build_search_analytics_body(
        start_date=args.get("start_date"),
        end_date=args.get("end_date"),
        dimensions=args.get("dimensions"),
        row_limit=args.get("row_limit", 1000),
        start_row=args.get("start_row", 0),
        search_type=args.get("search_type"),
        aggregation=args.get("aggregation"),
        data_state=args.get("data_state"),
        filters=args.get("filters"),
    )


def _prevalidate_inspect(args: Mapping[str, Any]) -> None:
    """Validate URL shape and language code before credential acquisition.

    The property-scope check needs the *normalized* property, which the
    allowlist owns, so it happens in the client. Shape and language are pure and
    are checked here.
    """
    from .client import validate_inspection_target, validate_language_code

    url = args.get("url")
    if not isinstance(url, str) or not url:
        raise InvalidArgumentError("url must be a non-empty string")
    # Scope is enforced against the property in the client; here we only need the
    # URL to be well-formed, which validate_inspection_target does structurally.
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise InvalidArgumentError("url must be an absolute http:// or https:// URL")
    if not parts.netloc or parts.username or parts.password or parts.fragment:
        raise InvalidArgumentError("url must be an absolute URL with a host and no credentials or fragment")
    validate_language_code(args.get("language_code"))


def _validate_arguments(spec: ToolSpec, arguments: object) -> dict[str, Any]:
    """Check argument presence and key set. Value validation happens deeper."""
    # Fail closed: a non-object argument set is an error, not an empty dict.
    # Treating None as {} would let a malformed call slip past validation and
    # reach the "required argument missing" path only by accident.
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