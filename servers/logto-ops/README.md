# logto-ops MCP

A write-capable MCP server for the Logto Management API, served over
authenticated MCP Streamable HTTP and registered in LiteLLM.

It exists so that creating an M2M application, minting its client secret,
assigning roles and scopes, or reading audit logs does not require clicking
through the Admin Console — and so that an agent can do it under review rather
than unsupervised.

**No delete operation is implemented.** Not user, not application, not role, not
organization. Delete handlers are refused at classification time, and a
`delete_*` prefix rule catches a future one that forgets to declare itself.

## What it exposes

| Group | Read | Write |
|---|---|---|
| Applications | list, get, roles, secret **names** | create (M2M only), create secret, assign roles, update |
| Roles / scopes | list, get, role scopes, role applications | create, update, assign scopes |
| Users | list, get, roles | create, update, suspend/unsuspend, set password, assign roles |
| Connectors | list, get | update, refresh token |
| Sign-in experience | get | update named fields |
| Organizations | list, get, org roles | create, update, add members |
| Resources | list, get, scopes | — (API design lives in code, not ops) |
| Audit | list logs, get log | — |

Application creation is **M2M-only**. There is no `type` argument: a caller
cannot ask for `Traditional` and get something else, because the argument does
not exist and undeclared arguments are refused rather than dropped.

## Two-phase writes

MCP has no approval primitive, so the server enforces one:

```
call create_m2m_application {"name": "vlobel-scheduler"}
  → isError, and a plan:
      plan.operation, plan.arguments (secrets redacted),
      plan.digest, confirmation_token, next_step

call create_m2m_application {"name": "vlobel-scheduler",
                            "confirmation_token": "<token>"}
  → executes
```

The token is an HMAC over the canonical argument digest plus an issue time and a
server-held key. It is stateless — nothing to leak, nothing to expire in a
database — and it binds the **arguments**, so changing the name, the target or a
secret between plan and apply invalidates it. Default TTL is 900 s
(`LOGTO_MCP_CONFIRM_TTL_SECONDS`).

### What this does and does not buy

It converts an irreversible call into a reviewed two-step whose plan appears in
the transcript. It is **not** proof that a human looked: a model whose client
lets it call a tool twice can produce both halves itself. Only a client-side
approval prompt makes the human part real. Treat the token as
"the action was described before it ran", not "a person consented".

## Modes

| `LOGTO_MCP_MODE` | Effect |
|---|---|
| `read` | every write refused with `write_not_permitted`; the server refuses to start if its registry contains writes |
| `apply` *(default)* | writes require a confirmation token |
| `admin` | writes run unconfirmed; **also** requires `LOGTO_MCP_ALLOW_UNCONFIRMED_WRITES=true`, so a mode typo cannot reach it |

`LOGTO_MCP_ALLOW_DELETES=true` is a startup error. The flag exists only so a
stale deployment value fails loudly instead of being ignored while an operator
believes deletes are on.

## Configuration

All secrets are files, not environment values.

| Variable | Meaning |
|---|---|
| `LOGTO_BASE_URL` | Logto core host, https only (e.g. `https://auth.example.org`) |
| `LOGTO_M2M_APP_ID` | M2M application id |
| `LOGTO_M2M_SECRET_FILE` | file containing that app's client secret (mode 0600) |
| `LOGTO_CF_ACCESS_CLIENT_ID` | optional; Cloudflare Access service token id |
| `LOGTO_CF_ACCESS_SECRET_FILE` | optional; its secret file (mode 0600) |
| `LOGTO_API_INDICATOR` | default `https://default.logto.app/api` — the OSS Management API audience |
| `LOGTO_MCP_MODE` | `read` \| `apply` \| `admin` |
| `LOGTO_MCP_CONFIRM_SECRET_FILE` | file with a 32+ byte HMAC key |
| `LOGTO_MCP_CONFIRM_TTL_SECONDS` | default 900, bounded 30–86400 |
| `LOGTO_MAX_PAGE_SIZE` | default 100; caller values are clamped, never widened |
| `LOGTO_REQUEST_TIMEOUT` / `LOGTO_HTTP_TIMEOUT` | seconds |
| `MCP_HTTP_*` | the shared transport: token file, host allowlist, port |

When the Logto core host sits behind Cloudflare Access, a deployment needs the
`LOGTO_CF_ACCESS_*` pair as well. Setting only one of the two is a startup
error; setting neither sends no Access headers.

## Local run

```bash
export LOGTO_BASE_URL=https://auth.example.org
export LOGTO_M2M_APP_ID=<id>
export LOGTO_M2M_SECRET_FILE=/path/to/m2m.secret     # chmod 600
export LOGTO_MCP_CONFIRM_SECRET_FILE=/path/to/confirm.key
export LOGTO_MCP_MODE=apply
export MCP_HTTP_AUTH_TOKEN_FILE=/path/to/endpoint.token
export MCP_HTTP_ALLOWED_HOSTS=127.0.0.1
export MCP_HTTP_PORT=8099

PYTHONPATH="packages/mcp_http_transport/src:servers/logto-ops/src" \
  .venv/bin/python -m logto_ops.http_main
```

## Tests

```bash
make test-logto     # this server's suite
make test           # whole platform, including the HTTP end-to-end tests
```

The suite covers the gate classification, confirmation minting/verification and
its failure modes, the client's bounds and redirect refusal, every handler's
request construction, and six end-to-end tests in which a real subprocess serves
over HTTP against a loopback fake Logto.

## Known gaps

- The Management API has exactly one scope (`all`). Least privilege here is the
  operation set plus the mode, not the token.
- Creating a **new** M2M application is `POST /applications` plus
  `POST /applications/{id}/secrets`; the secret value is returned once and is
  the only copy. Nothing stores it for you.
- Scope assignment is additive and cannot be reversed through this API. The
  handler says so in its result rather than pretending otherwise.
- Suspend is not delete: a suspended user still exists and can be unsuspended by
  the same tool.
