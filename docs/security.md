# Security model

Every claim below is marked with how it is checked. Nothing in this repository
should be described as secure on the strength of this document; use the test
names to re-verify.

## What is enforced, and by what

### 1. Authentication is required, and rejection happens before MCP dispatch

`BearerTokenVerifier` implements the MCP SDK's `TokenVerifier` protocol. Because
a verifier is installed, the SDK wraps the `/mcp` route in
`RequireAuthMiddleware`, which answers `401` (no/invalid token) or `403`
(insufficient scope) and never calls into the MCP session manager.

*Checked by:* `tests/test_http_security.py`

- `test_initialize_and_list_tools_succeed_with_a_valid_token` — 200, session
  opened, tool list non-empty, read-only annotations intact.
- `test_missing_token_is_rejected_with_401_and_opens_no_session` — 401, no
  `Mcp-Session-Id`, `WWW-Authenticate` present, no `serverInfo` in the body.
- `test_wrong_token_is_rejected_and_leaks_nothing` /
  `test_near_miss_token_is_rejected` — wrong and off-by-one tokens both 401.
- `test_malformed_authorization_headers_are_rejected` — no scheme, wrong scheme,
  wrong keyword, bare `Bearer`, `Bearerx <token>`.
- `test_rejection_precedes_tool_dispatch_even_for_a_valid_body` — an
  unauthenticated `tools/call` body produces 401 and no tool result.
- `test_wrong_token_cannot_hijack_an_existing_session` — a session id from a
  valid token is not resumable with a wrong token.

### 2. The token is compared in constant time and never leaves the process

`hmac.compare_digest` on UTF-8 bytes; tokens are never logged, never returned,
and never included in an error message; `repr()` of the verifier and of
`AppConfig` redacts them.

*Checked by:* `tests/test_auth_unit.py` (comparison, redaction, protocol shape)
and `tests/test_http_security.py` (response bodies and server logs contain no
token material).

### 3. The endpoint fails closed when authentication is not configured

Implementation notes:

- `load_expected_token` refuses a missing, empty, whitespace-padded, too short,
  or too long token. The default minimum is 16 characters. The **environment
  variable** `MCP_HTTP_AUTH_TOKEN` is not trimmed: a value with a trailing
  newline (the classic `echo "$TOKEN"` capture) is a startup error, not a
  silent trim.
- The **token file** path behaves differently, and this is deliberate:
  `_read_token_from_file` calls `.strip()` on the file contents, so a secret
  file written with a trailing newline is accepted with that newline removed.
  That is the common and safe case for a Docker/Kubernetes secret, which almost
  always ends in a newline; refusing it would make every correct deployment
  fail. The asymmetry matters: `MCP_HTTP_AUTH_TOKEN="$(echo …)"` is rejected,
  while `MCP_HTTP_AUTH_TOKEN_FILE=<file with trailing \n>` is accepted. (An
  earlier version of this document claimed a trailing newline "is an error, not
  a silent trim" without distinguishing the two sources; that was wrong for the
  file path.)
- `resolve_token` reads `MCP_HTTP_AUTH_TOKEN` or `MCP_HTTP_AUTH_TOKEN_FILE`;
  the environment variable wins if both are set. An empty token file is a
  startup error.
- `build_streamable_http_app` refuses to build if the server object does not
  expose `settings.auth`, and verifies afterwards that its own verifier is the
  one installed, so a future SDK change cannot silently produce an open
  endpoint.
- Each server's `http_main` exits with status 2 and a one-line stderr message
  before binding a socket.

*Checked by:* `tests/test_auth_unit.py` (both the env-var rejection and the
file-strip acceptance are asserted directly), `tests/test_app_config.py`, and
`tests/test_http_security.py::test_startup_refuses_without_a_token`, which
asserts the real exit code (2) and the real stderr text.

### 4. Host and Origin allowlists (DNS-rebinding defence)

`TransportSecuritySettings(enable_dns_rebinding_protection=True)` with an
explicit closed list. Bare host names are expanded to `name` and `name:*`; a
global `*` is never added. Unlisted `Host` ⇒ `421`; unlisted `Origin` ⇒ `403`.

*Checked by:* `tests/test_http_security.py::test_untrusted_host_header_is_rejected_even_with_a_valid_token`,
`::test_unlisted_origin_is_rejected_with_a_valid_token`, and
`tests/test_deployment_artifacts.py::test_host_allowlist_is_explicit_and_has_no_wildcard`.

This is defence in depth, **not** the security boundary: any non-browser client
sets `Host` freely. The boundary is the bearer token plus network placement.

### 5. Unknown paths are not rewritten into the endpoint

A prefix-stripping proxy leaves `""` or `"/"`; those are routed to `/mcp`. Any
other unknown path keeps its own 404, so the endpoint does not become a
catch-all.

*Checked by:* `test_unknown_path_is_not_rewritten_into_the_mcp_endpoint`,
`test_prefix_stripped_request_is_routed_to_the_mcp_endpoint`.

### 6. Bounded resources

Request bodies are capped at 1 MiB (SDK default is 4 MiB), sessions at 100
concurrent, idle sessions expire after 300 s. All three are `AppConfig` fields,
so an operator can tighten them per deployment.

### 7. Server isolation (process and credential separation)

Each image installs exactly one server package; each container has its own
token, its own Google credential path, and its own Host allowlist. A defect or
an over-permissive change in one server cannot be reached through the other's
process, and a stolen token for one server is not a token for the other.

This is **isolation of processes and credentials, not network isolation.** Both
containers are attached to the shared external `coolify` network (see §9) so
the LiteLLM gateway can reach them, which means every other container on that
network can also open a TCP connection to `:8080`. What stops an unauthorised
caller is the bearer token, not the network. An earlier version of this
document described the two servers as reachable "only by service name inside
`mcp-internal`"; that was true only before the gateway had to reach them and is
no longer claimed.

*Checked by:* `tests/test_isolation.py` — both servers are run at once and the
tests prove cross-token rejection (401 in both directions), cross-session
rejection, disjoint tool sets, and distinct `serverInfo` names; plus
`tests/test_deployment_artifacts.py` for the compose/Dockerfile properties
(no published port, no hostname, per-service alias in the Host allowlist,
distinct secret files, distinct credential variables).

### 8. No secret, and no guessed identifier, in the repository

*Checked by:* `test_repository_contains_no_google_identifiers_or_credentials`
scans everything authored here for OAuth client ids, `ya29.`-style tokens,
private-key markers, and service-account fields; the compose test asserts tokens
are passed as mounted files, never as inline environment values; allowlists are
required variables that abort the deploy when unset.

The one static token in the repository (`tests/harness.py`) is a placeholder
used to start a local test server and is not valid anywhere.

### 9. Network reachability and the alias↔allowlist contract

The MCP servers publish **no host port** and set **no `hostname`**. They are
attached to two networks:

- `coolify`, declared `external: true` — the network the LiteLLM gateway
  already uses. Each service is given one stable alias on it
  (`play-console-mcp`, `search-console-mcp`) so the gateway resolves a fixed
  name regardless of which Compose project created the container.
- `mcp-internal`, a project-local bridge, used for the east-west checks.

The alias is not cosmetic: a request whose `Host` header is not in
`MCP_HTTP_ALLOWED_HOSTS` is answered `421`. The compose file therefore lists
exactly the alias each service answers to, and
`tests/test_deployment_artifacts.py::test_each_service_has_a_stable_coolify_alias_matching_its_allowlist`
asserts the alias and the allowlist agree, so a mismatch fails at test time
rather than as a run-time `421`.

*Residual risk, accepted:* the `coolify` network is shared with the gateway and
anything else the operator attaches to it. Any container on that network can
reach `:8080` and attempt authentication. There is no per-container network
policy here. If the `coolify` network is ever shared with a container that must
not reach these servers, move them to a dedicated network and attach only the
gateway — the compose file is structured so that is a one-network edit.

### 10. Secret files and their permissions (the bind-mount rule)

The token and credential secrets use Compose's `file:` driver. Compose delivers
a `file:` secret with a **bind mount**, and bind mounts do not remap ownership
or permission bits: the file arrives in the container with the **host file's**
uid/gid and mode. The `uid`, `gid`, and `mode` keys of a Compose secret are
therefore ignored when the source is a file, so the compose file deliberately
does **not** set them (`test_file_secrets_declare_no_uid_gid_or_mode`).

The consequence is an operational requirement, not a code property: the
container runs as uid/gid 10001, so **each host secret file must be readable by
10001**. A token file created in the usual way (`root:root`, mode `0600`) is
*not* readable by 10001 and the container exits at startup with a "cannot be
read" error — fail-closed, but it will not run. The working setup is:

```bash
chown 10001:10001 /path/to/secret   # or make its group 10001 with mode 0440
chmod 0400        /path/to/secret
```

`scripts/verify_secret_mount.sh` reproduces both cases against the running
engine (root:0600 unreadable, 10001:0400 readable) for an operator who wants to
see it rather than take it on trust. The compose file's header states the same
rule where an operator will read it.

*Verified by:* `tests/test_deployment_artifacts.py` asserts the artifacts are
consistent with this rule (no `uid`/`gid`/`mode` on file secrets; the requirement
is documented). The live bind-mount behaviour itself needs a Docker engine, so
it is checked by the script, not by pytest — and this document does not claim
pytest proved it.

## Not verified — do not claim these

- **No container was built or run in this environment.** Docker is not
  installed here. The Dockerfiles and compose file are checked statically
  (`tests/test_deployment_artifacts.py`), not by execution. A separate
  independent Docker host did confirm that the images build, that the real HTTP
  auth path works (401 without a token, 200 with one), and that the secret
  bind-mount permission rule in §10 holds; that evidence is external to this
  repository and is not reproduced by the suite here.
- **The `coolify` network name is asserted, not probed.** The compose file
  declares `coolify` as an external network named `coolify`, matching the
  network the Coolify deployment is created with. Whether that network exists
  (and whether the gateway is attached to it) is a property of the host, not of
  this repository, and is not verified by any test here.
- **Dependency hashes are not pinned.** `deploy/constraints.txt` pins exact
  versions but not hashes, so the plan to close that gap is
  `uv export --generate-hashes`. Reference: OWASP A06/A08 supply-chain guidance.
- **No live Google call, and no Google credential, was used.** Credential
  acquisition remains lazy and untouched; the tests never exercise it.
- **No TLS.** The endpoint speaks plain HTTP and must sit behind a
  TLS-terminating proxy on a private network. There is no rate limiting and no
  per-caller identity: everyone holding the shared token is one identity.
- **LiteLLM itself was not exercised.** The MCP client used is the SDK's own
  `ClientSession` over `streamable_http_client`, which is the same client class
  a gateway uses, but that is not a LiteLLM integration test. In particular, no
  test proves LiteLLM can resolve the alias and complete a handshake — that
  requires the live network.
- **No penetration test, and no hostile-network testing.** The auth tests cover
  the HTTP surface the SDK exposes, not every reachable code path, and nothing
  here tests behaviour against a hostile container on the `coolify` network.

## Residual risks

| Risk | Why it is accepted here | Mitigation available |
| --- | --- | --- |
| Shared token = single identity | Simplest thing that satisfies "LiteLLM connects with a bearer token"; no OAuth server in scope | Front with the platform's auth proxy; move to OAuth via the SDK's `auth_server_provider` |
| Both services on the shared `coolify` network | They must be reachable from the LiteLLM gateway, which lives on that network | Put each on its own network and attach only the gateway; the compose file is one edit away from this |
| Token in the process environment | Supported for simplicity | Prefer `MCP_HTTP_AUTH_TOKEN_FILE`; the compose file uses it |
| Secret files unreadable by uid 10001 | Compose `file:` secrets are bind mounts and cannot remap ownership; a root:0600 file fails closed at startup | `chown 10001:10001` + `chmod 0400` on each host secret; see §10 and `scripts/verify_secret_mount.sh` |
| SDK private attribute assignment (`_token_verifier`, `settings.auth`) | `mcp==2.3.0` has no public setter for auth settings on an already-constructed server | Pinned exact SDK version; `build_streamable_http_app` verifies the wiring and raises if it does not take effect |
| Dependency compromise | No hash pinning yet | `uv export --generate-hashes`, image rebuild on lockfile change |

## Reporting

If a check in this document does not match the code or a test, the test is
wrong or the code is; either way the claim above should be treated as
unverified until they agree again.