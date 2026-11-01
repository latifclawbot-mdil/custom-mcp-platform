# search-console MCP — design

Read-only, stdio-transport MCP server for the Google Search Console API, built for
later consumption by a LiteLLM MCP gateway. Nothing here writes to Search Console,
and nothing here can read credentials it was not explicitly handed.

## Non-goals

- No property add/remove, no sitemap submit/delete, no re-index requests.
- No writes of any kind. Every HTTP method used is `GET` or the read-only `POST`
  of `searchAnalytics.query` / `urlInspection.index:inspect`.
- No OAuth browser flow, no token storage, no secret logging.

## Capability surface (4 tools)

| Tool | Underlying call | Method |
|---|---|---|
| `gsc_list_sites` | `GET /webmasters/v3/sites` | GET |
| `gsc_search_analytics` | `POST /webmasters/v3/sites/{siteUrl}/searchAnalytics/query` | POST (read-only) |
| `gsc_list_sitemaps` | `GET /webmasters/v3/sites/{siteUrl}/sitemaps` | GET |
| `gsc_inspect_url` | `POST https://searchconsole.googleapis.com/v1/urlInspection/index:inspect` | POST (read-only) |

## Security model

Four independent gates, each fail-closed. A caller must pass all of them; there is
no "enforcement off" switch.

### 1. Property allowlist (fail-closed)

`GSC_ALLOWED_SITES` is a comma-separated list of exact property identifiers, e.g.
`sc-domain:example.com,https://www.example.com/`. It is read once at startup from
the environment (config file / HTTP are deliberately not supported — no dynamic
configuration source that a caller could influence).

- Empty or unset `GSC_ALLOWED_SITES` → the server refuses to start. There is no
  implicit "all sites" mode.
- Comparison is exact string equality against the normalized form: a Domain
  property is normalized to lowercase; a URL-prefix property is normalized only by
  ensuring a trailing `/`. No case-folding on the path, no scheme rewriting.
- Every tool takes `site_url` and validates it against the allowlist **before**
  any credential is touched or any URL is built.

### 2. Property is a path segment, percent-encoded

The property appears as the last path segment of the webmasters v3 URL. It is
encoded with `urllib.parse.quote(property, safe="")`, which leaves `/` and `:` as
`%2F` / `%3A` — so `sc-domain:example.com` becomes `sc-domain%3Aexample.com` and
`https://www.example.com/` becomes `https%3A%2F%2Fwww.example.com%2F`, matching
Google's documented encoding. Because `safe=""` has no allowance for `/`, `..`,
`?`, `#`, a property string cannot introduce a new host or a new path segment.

### 3. Fixed hosts only

Every request URL is assembled from module constants:
`https://www.googleapis.com/webmasters/v3` and
`https://searchconsole.googleapis.com/v1`. The only caller-influenced component in
a URL is the encoded property path segment. There is no code path that accepts a
host, scheme, or base URL from a tool argument.

### 4. URL inspection is scoped to the property

`gsc_inspect_url` takes both `site_url` (gated by the allowlist) and `url`
(the page to inspect). Before the call is built:

- `url` must be a fully-qualified `http`/`https` URL with no userinfo
  (`user:pass@`) and no fragment.
- It must be *under* the property. For `sc-domain:example.com`, the URL host must
  be `example.com` or a subdomain of it. For a URL-prefix property, the URL must
  start with the property prefix. Mismatch → rejected, no HTTP call.
- It must contain no `.` or `..` path segment, raw or percent-encoded
  (`%2e`, `%2f`). A raw prefix check cannot prove scope when the same string
  resolves to a different path per RFC 3986 (`/a/../admin` → `/admin`), so any
  dot-segment is rejected as `invalid_argument` with `reason: path_traversal`.

This prevents using an allowlisted property as a stepping stone to inspect
arbitrary third-party URLs — including URLs that leave the property only after
resolution.

### Bounds

| Bound | Value | Enforced |
|---|---|---|
| `rowLimit` | 1..25000 (API max) | pre-call |
| `startRow` | 0..200000 (own cap, not API floor) | pre-call |
| date range span | <= 1640 days incl. | pre-call |
| `endDate` | not in the future (UTC+5:30 local date) | pre-call |
| `startDate <= endDate` | required | pre-call |
| dimensions | allowlisted names, no duplicates | pre-call |
| search type / aggregation | allowlisted values | pre-call |
| response rows returned | `max_returned_rows` (default 2000) applied to the upstream page | post-call |
| filter expression | <= 1024 chars | pre-call |

Truncation is explicit: the response carries `rows_returned`, `rows_truncated`
and, when a partial page was returned, `next_start_row`. `next_start_row`
advances by the number of *upstream* rows consumed (the rows the cap admitted),
not the number of rows that survived mapping, so a page containing malformed
rows does not cause the next page to re-read rows already consumed.
`list_sites` discloses only allowlisted properties; properties the credential
can reach but the allowlist excludes are summarised as `sites_withheld` and
their names are never returned.

## Credentials

- Scope: exactly `https://www.googleapis.com/auth/webmasters.readonly`. Never the
  read/write `webmasters` scope.
- `google.auth.default(scopes=[...])` is called lazily — on the first tool call
  that needs it, not at import and not at server construction. A call that is
  rejected by the allowlist or the bounds never triggers ADC at all.
- A service-account JSON path may be supplied via `GOOGLE_APPLICATION_CREDENTIALS`
  (standard ADC discovery). The file's *contents* are never read by this code,
  never echoed, and never logged.
- Transport is `google.auth.transport.requests.AuthorizedSession`, which attaches
  the bearer token itself. The token never enters our code path, so it cannot be
  logged or returned.
- Adding the token to URLs as a query parameter is not done — that is the classic
  way credentials end up in proxy logs.

## Error handling

All failures return a structured JSON envelope with a stable `code`, never a
traceback and never upstream response bodies verbatim. Redaction removes, in
order: PEM private-key blocks, URLs, credential-carrying scheme headers
(`Bearer`/`Basic`/`Digest` and their value, so `Authorization: Bearer <token>`
cannot leave the token behind), OAuth `ya29.` token shapes, key/value secrets
(`Authorization`, `access_token`, `client_secret`, `private_key`, `api_key`,
`secret`, `password`, `token`, … assigned with `:` or `=`), and long numeric
IDs. The envelope is emitted as strict JSON (`allow_nan=False`), so a
non-finite upstream float is dropped to `null` rather than serialised as a bare
`NaN`/`Infinity` token strict parsers reject. A failed call also sets the MCP
`isError` flag, so a client branching on it does not read a refusal as success.

| code | meaning |
|---|---|
| `not_configured` | `GSC_ALLOWED_SITES` missing/empty |
| `site_not_allowed` | property not in the allowlist |
| `invalid_argument` | bad URL, date, bound, dimension, or filter |
| `bad_request` | upstream 400 |
| `unauthorized` | upstream 401/403 — includes a hint to check that the ADC principal was added as a user of the property |
| `not_found` | upstream 404 |
| `rate_limited` | upstream 429 with `Retry-After` if present |
| `upstream_error` | other 4xx/5xx, redacted |
| `network_error` | transport failure |
| `invalid_response` | upstream 200 with a non-JSON body |

## Testing

Two layers, both real:

1. **Unit/seam tests** (`tests/test_*.py`) — allowlist, property encoding, tool
   request construction, bounds, current-error mapping, URL-scope checks. These
   use a fake session that records the method/URL/body it was asked to send, so the
   request that *would* hit Google is asserted exactly, without network or
   credentials.
2. **Real stdio MCP tests** (`tests/mcp_harness.py` + `tests/test_stdio_mcp.py`) —
   spawn `python -m search_console_mcp` as a subprocess through the SDK's own
   `stdio_client`, perform `initialize`, `tools/list`, and `tools/call`. The
   subprocess gets the allowlist and a stub bearer token via env and never reaches
   the network: the fake upstream is an in-process HTTP server the client points
   itself at through the same `GSC_API_BASE_OVERRIDE` seam the production code
   exposes. Origin and property-scope checks are asserted *from inside the
   subprocess* using `GSC_TEST_CAPTURE_DIR`, which the server only writes when the
   variable is set.

The override seam is deliberately one variable, restricted to `http://127.0.0.1`
hosts; the production default is unreachable from tool arguments.

## Packaging

Pinned in `requirements.txt` as `mcp==2.3.0` and `google-auth==2.55.1`; both are
listable runtime dependencies in `pyproject.toml`. Console entry point:
`search-console-mcp` → `search_console_mcp.__main__:main`.

## Known gaps

- On mcp 2.3.0 the SDK derives a tool's pydantic arg model from the handler
  signature and *silently drops* keys not in it, and its derived JSON schema
  reports `additionalProperties: true`. Both are handled: handlers carry an
  explicit generated signature (so arguments arrive addressably), and a
  transport-layer middleware (`reject_unknown_arguments`) rejects unknown keys
  before SDK parsing, with the advertised schema corrected to
  `additionalProperties: false`. The registry also rejects unknown keys, so the
  check exists at two layers.
- Generated handler parameters default to `None`, and `null` is treated as
  "argument absent" so the registry's own defaults apply. An explicit JSON
  `null` for an optional argument is therefore equivalent to omitting it.
- `GSC_API_BASE_OVERRIDE` / `GSC_INSPECTION_BASE_OVERRIDE` / `GSC_TEST_CAPTURE_DIR`
  / `GSC_TEST_STATIC_TOKEN` exist for testability; they are not a supported
  production configuration surface and are ignored unless they point at a
  loopback host.
- URL-prefix scope check is prefix-string based against the normalized property,
  so matching is segment-aligned (normalization supplies the trailing `/`).
- No response caching. Each call is one upstream request.
- No multi-property fan-out: one call, one property.