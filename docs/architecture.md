# Architecture

## The shape

```
LiteLLM (or any MCP host)
   │  Streamable HTTP + Authorization: Bearer <per-server token>
   ▼
┌──────────────────────────────┐        ┌──────────────────────────────┐
│ play-console-mcp container    │        │ search-console-mcp container │
│  · play_console_mcp.http_main │        │  · search_console_mcp.http_  │
│    60 lines: load allowlist,  │        │    main — 60 lines           │
│    load token, serve          │        │                              │
│  · (no other server's code)   │        │  · (no other server's code)  │
└──────────────┬───────────────┘        └──────────────┬───────────────┘
               │ both import the same package          │
               ▼                                       ▼
        packages/mcp_http_transport  (auth, Host/Origin allowlist,
                                      path normalisation, serve())
               │
               ▼
   vendored server packages (unmodified): tools, schemas, allowlists,
   read-only guards, error envelopes, lazy Google credentials
```

## Why each layer exists

**The vendored server, unmodified.** Both servers already had a careful trust
boundary: package/property allowlists, identifier validation, GET-only or
read-only operations, redacted errors. Rewriting any of that for HTTP would have
thrown away the tests that prove it. The transport change is additive: one new
entry-point module per server, calling the existing `build_server()` /
`create_server()`.

**The transport package.** It is the only place that knows about HTTP. Four
things make it more than a `uvicorn.run(server.streamable_http_app())` wrapper:

1. **Auth has to be installed before the app is built.** In `mcp==2.3.0`,
   `token_verifier` and `auth` are constructor-only arguments of `MCPServer`, and
   `streamable_http_app()` reads them from the instance. So the package assigns
   `server._token_verifier` and `settings.auth` on the object it was given, then
   verifies the assignment took effect and raises if it did not. Without that
   check, a future SDK refactor would silently produce an unauthenticated
   endpoint.
2. **Host headers arrive with ports.** A bare-hostname allowlist rejects every
   real request with `421`. The package expands each entry to `name` and
   `name:*` — still a closed list, never `*`.
3. **Proxies strip prefixes.** A Host-based router that mounts at `/mcp` and
   strips it can hand the ASGI app an empty path, which the MCP session manager
   answers with a bodiless 404. The shim rewrites only the shapes that a
   prefix-strip can produce; every other unknown path still 404s.
4. **Fail-closed configuration.** Missing/short/padded tokens, empty token files,
   and a server object without `settings.auth` are all startup errors with
   non-zero exit codes, not warnings.

**Two containers, not one.** The isolation requirement is not cosmetic: separate
images mean the Play container has no Search Console code, no GSC allowlist
variable, and no access to the GSC credential path. A defect or an
over-permissive change in one server cannot be exploited through the other's
process.

## Network placement

Both containers are attached to two Docker networks:

- **`coolify`** — declared `external: true`, the network the LiteLLM gateway
  already uses. Each service has one **stable alias** on it
  (`play-console-mcp`, `search-console-mcp`) so the gateway resolves a fixed
  name. Neither service publishes a host port or sets a hostname, so the
  endpoint is not reachable from off-host.
- **`mcp-internal`** — a project-local bridge, kept for the east-west checks
  between the two servers.

The alias must be in the service's `MCP_HTTP_ALLOWED_HOSTS` (an unlisted `Host`
is `421`); the compose file lists exactly the alias, and a test asserts they
agree. The cost of using the shared `coolify` network is that any container on
it can open a connection to `:8080` — the bearer token, not the network, is the
boundary. See `docs/security.md` §9.

## Request lifecycle (authenticated call)

1. Proxy forwards to `:8080/mcp` over the private network.
2. `TransportSecuritySettings` checks `Host` (and `Origin`, if present).
   Unlisted ⇒ `421`/`403`, before auth.
3. `BearerAuthBackend` extracts the token and calls
   `BearerTokenVerifier.verify_token`, which compares with
   `hmac.compare_digest`. No match ⇒ `401`, no session created.
4. `RequireAuthMiddleware` checks the required scope, then hands the request to
   `StreamableHTTPASGIApp`.
5. The MCP session manager routes `initialize` / `tools/list` / `tools/call` to
   the same `MCPServer` the stdio transport uses.
6. A tool call goes through the server's existing validation and allowlist; a
   refusal is returned as `isError: true` with the server's structured
   envelope — identical to stdio, asserted by
   `test_policy_refusal_is_preserved_over_http`.
7. Google credentials are resolved lazily, only on the first call that got past
   validation. `initialize` and `tools/list` never touch them.

## Failure modes and what the operator sees

| Condition | Result |
| --- | --- |
| No token configured | exit 2, one-line stderr, no socket |
| Token file unreadable / empty | exit 2, one-line stderr, no socket |
| Allowlist unset (server's own check) | exit 2, one-line stderr, no socket |
| Missing / wrong / malformed token | `401` + `WWW-Authenticate`, no session |
| Unlisted `Host` | `421` |
| Unlisted `Origin` | `403` |
| Unknown path | `404` |
| Tool refused by policy | `200` with `isError: true` and a redacted envelope |
| Google credential absent | tool call returns a credential error; discovery still works |

## Consequences worth naming

- **Statelessness.** Sessions live in memory per process. A restart drops
  sessions and the client re-initialises; no state is stored on disk.
- **Single worker.** One uvicorn process per container, so sessions can be
  pinned to the process that owns them. Scaling out would need sticky routing
  or `stateless_http=True`; that trade-off is left to the deployment rather than
  assumed.
- **No fan-out.** The two containers do not talk to each other. The gateway sees
  two independent servers, which is what keeps their tokens and credentials
  separable.