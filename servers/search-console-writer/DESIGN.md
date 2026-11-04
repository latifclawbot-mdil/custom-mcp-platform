# search-console-writer — design

A write-capable MCP server for the Google Search Console API, built as a
separate package from the read-only `search-console` server so that a
credential able to mutate state is never reachable from the read path.

## Why a separate server, not a mode switch

The read server requests `webmasters.readonly` and cannot construct a mutating
verb. Adding writes behind a flag would mean the same process holds a write
credential, and every read-path bug becomes a write-path bug. Separation gives
three independent things:

1. a distinct credential (and, if the operator wishes, a distinct principal)
   for writes;
2. a distinct allowlist — properties may be readable but not writable;
3. a distinct audit surface: the deployed writer either has the write tools or
   it does not, and that is visible in `tools/list`.

## Capability surface

Exactly the four official mutations. Nothing else is registered.

| Tool | Method | Path | Destructive | Idempotent |
|---|---|---|---|---|
| `gsc_add_site` | PUT | `/sites/{siteUrl}` | no | yes |
| `gsc_delete_site` | DELETE | `/sites/{siteUrl}` | yes | yes |
| `gsc_submit_sitemap` | PUT | `/sites/{siteUrl}/sitemaps/{feedpath}` | no | yes |
| `gsc_delete_sitemap` | DELETE | `/sites/{siteUrl}/sitemaps/{feedpath}` | yes | yes |

## Request construction

```
BASE_URL = "https://www.googleapis.com/webmasters/v3"
```

`{siteUrl}` and `{feedpath}` are the only caller-influenced parts of a URL, and
both are `quote(value, safe="")`. With no slash left unescaped, neither value
can add a path segment, a query, a fragment, or `..`. There is no code path that
accepts a base URL, host, scheme, method, header, or body from a tool argument.

`_request` re-checks two invariants as defence in depth: the verb is in
`{"PUT", "DELETE"}`, and the URL starts with the resolved base.

Redirects are never followed. The request is sent with `allow_redirects=False`,
so a `301`/`302`/`303`/`307`/`308` is returned to this code rather than being
re-issued by the HTTP session — `requests` would otherwise re-send a `307`/`308`
with the method and body intact to whatever host the `Location` header names.
Any `3xx` is a refusal (`unexpected_redirect`), never a success; a non-empty
`response.history` is also refused, since it means hops already happened.

Upstream error text and the `Retry-After` header are untrusted. The message is
bounded (4096 chars, marked when truncated); `Retry-After` is accepted only in
the two forms RFC 9110 defines (delta-seconds or an HTTP-date) and dropped
otherwise, so nothing attacker-shaped reaches the envelope.

## Validation order (a security property)

```
argument shape → property shape → allowlist → sitemap scope → build client
```

The client factory is called only after all four checks pass, so a rejected call
never acquires a credential. This is asserted two ways: an in-process spy proves
the factory was not called, and a fresh interpreter proves
`google.auth.default` recorded zero calls.

Property *shape* is checked before the allowlist so that a malformed value is
reported as `invalid_argument` rather than `site_not_allowed`; otherwise a
caller could use the error code to probe allowlist membership.

## Failure semantics for a mutation

| Situation | Code | `may_have_applied` |
|---|---|---|
| 400/401/403/404/409/429 | `bad_request` / `unauthorized` / `not_found` / `rate_limited` | not flagged |
| 5xx | `upstream_error` | `true` |
| 3xx (redirect, not followed) | `unexpected_redirect` | `false` |
| connect timeout (never sent) | `network_error` | `false` |
| read timeout / reset / post-connect TLS failure | `ambiguous_outcome` | `unknown` |
| credential unavailable | `not_configured` | not flagged |

A credential failure is a `not_configured` error, not `upstream_error`: the
message names the variable to set (`GSC_WRITE_SERVICE_ACCOUNT_FILE`,
`GOOGLE_APPLICATION_CREDENTIALS`, or `GSC_WRITE_TEST_STATIC_TOKEN`) and is
redacted like every other message.

No case is retried. A mutation whose delivery is unknown must be reconciled by
the caller against the property's actual state; re-sending it is how duplicate
or inverted effects are produced. Deleting an already-deleted sitemap returns
`not_found` rather than being retried into "success".

## Authentication vs authorisation

The HTTP transport requires a shared bearer token; stdio is authorised by the
spawning process. Both authenticate *access to this endpoint*.

The loopback test override (`GSC_WRITE_API_BASE_OVERRIDE`) changes *which*
credential may be used, not just where requests go: when the base resolves to a
loopback host, the session builder accepts only the explicit test token
(`GSC_WRITE_TEST_STATIC_TOKEN`) and refuses otherwise. It never falls back to a
service-account file or ADC, so a test base cannot cause a real write credential
to be loaded or presented.

The server does not implement, and does not claim, per-operation approval. The
LiteLLM gateway is where a caller is authorised, where policy lives, and where a
human-approval step would be inserted. A test asserts that the server's own
instructions say this, so the documentation cannot drift into implying that an
authenticated write was individually approved.

## MCP annotations

Set explicitly per tool rather than defaulted:
`read_only_hint=False`; `destructive_hint=True` on `gsc_delete_site` and
`gsc_delete_sitemap`; `idempotent_hint=True`; `open_world_hint=True`. A delete
advertised as non-destructive would invite exactly the misuse this server must
not invite.

## Testing model

Two layers, both real:

1. **In-process** — validation, allowlist, tool dispatch order, envelopes,
   middleware, annotation objects. A recording session asserts the exact verb,
   URL, and timeout, and that a failure produced exactly one call.
2. **Real protocol** — `python -m search_console_writer` spawned through the
   SDK's `stdio_client`, driven by a real `ClientSession`: `initialize`,
   `tools/list`, `tools/call`, with real `PUT`/`DELETE` requests to an
   in-process loopback fake upstream. Request construction is asserted from
   *inside* the server process via an opt-in capture file, not reconstructed by
   the test. A separate suite runs the HTTP app under uvicorn on a loopback
   socket and checks 401-without-token, `initialize`, `tools/list`, and a
   refused property.

The whole suite runs with an autouse guard that raises on any `connect()` to a
non-loopback address, so "offline" is enforced rather than assumed.

## Known gaps

- Per-operation approval is out of scope (gateway responsibility, stated in
  `INSTRUCTIONS` and asserted by a test).
- No ownership verification on `sites.add`.
- Sitemap URLs are not fetched; validity beyond scope/shape is Google's call.
- Property hosts must be ASCII/punycode; native-script IDNs are rejected rather
  than converted.
- 4xx is treated as "not applied" (Google does not document a mutate-then-4xx
  case).
- No caching, no batching, no fan-out.
- The package duplicates a small amount of the read server's validation and
  redaction rather than importing it, so the two can be installed, audited and
  versioned independently. A test forbids importing `search_console_mcp`.