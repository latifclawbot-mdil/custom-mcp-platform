# logto-ops security notes

This file states what the Logto ops server does and does not enforce, and how
each claim is checked. It is written the same way as `security.md`: a claim
without a test name is a guess.

## The credential is a tenant administrator

The Management API has one scope, `all`. There is no narrower token. The M2M
application this server authenticates as can therefore do anything the Management
API allows, including deleting every user — even though *this server* implements
no delete operation.

Consequences, stated plainly:

- The operation set is an interface restriction, not a capability restriction. A
  bug that forwards an arbitrary path would not be stopped by the token.
- Anyone who can reach the container with its bearer token, or who reads the M2M
  secret file, holds that authority directly and does not need this server.

*Mitigations in place:* the endpoint publishes no host port, answers only on the
private `coolify` network by alias, requires a bearer token compared in constant
time, and the M2M secret is a mounted file (0600) rather than an environment
value. The container runs read-only, non-root, with `cap_drop: ALL` and
`no-new-privileges`.

*Checked by:* `tests/test_logto_ops_deployment.py` (no published port, secrets
mounted as files, no credential in the image), `tests/test_http_protocol.py`
(401 before MCP dispatch, for the shared transport), and
`tests/test_logto_ops_http.py` (401 over the wire for this server).

## Deletes are refused in two independent places

1. `DENIED_OPERATIONS` classifies every known Logto delete as denied.
2. A `delete_` prefix rule catches a handler that was never declared.

`classify()` raises for anything in neither the read nor the write set, so an
operation added without classification cannot silently become a read.

*Checked by:* `tests/test_gate.py`
(`test_delete_operations_are_denied_everywhere`,
`test_unclassified_delete_prefix_is_denied_even_if_not_enumerated`,
`test_unclassified_operation_is_an_error_not_a_read`) and
`test_no_delete_tool_is_advertised` / `test_tools_list_shows_the_write_surface_and_no_deletes`.

## The confirmation gate is real, but it is not a human

Every write without a valid `confirmation_token` returns a plan and executes
nothing. The token is an HMAC over `(operation, canonical arguments, issue time)`
keyed by a server-held secret, so:

- it cannot be replayed against different arguments
  (`test_changing_a_non_secret_argument_invalidates_the_token`);
- a secret argument is bound by the digest even though it is redacted from the
  plan (`test_changing_a_secret_argument_also_invalidates_the_token`);
- it is scoped to one operation (`test_a_token_for_one_operation_cannot_authorise_another`);
- it expires (`test_an_expired_token_is_rejected`) and does not accept a future
  timestamp (`test_a_token_claiming_to_be_from_the_future_is_rejected`);
- a token from another server with another key is rejected
  (`test_a_token_signed_with_another_secret_is_rejected`).

**What this does not prove.** There is no server-side record that a human
approved anything. A model running inside a client that permits two consecutive
tool calls can plan and apply without anyone reading the plan. The gate turns an
irreversible single call into a reviewable two-step; whether a review happens is
a property of the *client*, not of this server. Do not describe this deployment
as human-approved without pointing at the client-side control that enforces it.

*Checked by:* `tests/test_confirm.py`, `tests/test_server.py`
(`test_write_without_a_token_returns_a_plan_and_does_not_execute`), and
`tests/test_logto_ops_http.py` (`test_gated_write_without_a_token_does_not_execute`,
`test_apply_with_a_changed_argument_is_refused`).

## Undeclared arguments are refused, not dropped

The MCP SDK builds its argument model from the handler signature and **silently
drops** keys it does not know. Left alone, `create_m2m_application(type=
"Traditional")` would have succeeded while creating a MachineToMachine app —
a caller-visible lie about what happened. The tool sets `extra="forbid"` on that
model and rebuilds the pydantic schema so the setting takes effect.

*Checked by:* `test_unknown_argument_is_refused_at_validation_and_never_reaches_logto`
and `test_every_advertised_schema_forbids_extra_properties` (server suite),
plus `test_schema_disallows_extra_properties_over_the_wire` (HTTP suite).

Note the honest limit: this closes the SDK's dropping behaviour for *declared*
tools. It is not a general input sanitiser for values that are accepted.

## Ambiguous writes are reported, never retried

A transport failure on a create raises `AmbiguousResult`; the tool returns
`{"code": "ambiguous"}` and tells the caller to read the target back. Nothing
retries automatically, because a second `POST /applications` after a timeout can
create a duplicate application.

*Checked by:* `test_timeout_on_a_create_is_reported_as_ambiguous` and
`test_timeout_on_a_read_is_a_plain_transport_error`.

## The client is deliberately small

- Tokens are fetched lazily and cached in memory only; no disk cache survives a
  restart (`test_token_is_fetched_once_and_reused`).
- Redirects are never followed — a 3xx is raised, because the redirect target
  would receive the bearer (`test_redirects_are_not_followed`).
- Response bodies are bounded (`test_oversized_response_is_refused`).
- Provider error bodies are classified and truncated, never echoed; a token or
  secret fragment cannot ride into the model's context
  (`test_provider_error_is_classified_without_echoing_the_body`,
  `test_token_endpoint_failure_reports_http_status_not_the_secret`).
- A 401 invalidates the token and retries exactly once
  (`test_repeated_401_does_not_loop`).

## Secret handling

- `LOGTO_M2M_SECRET_FILE` and `LOGTO_MCP_CONFIRM_SECRET_FILE` must be 0600 or
  stricter; a loose file is a startup error, not a warning, and there is no
  fallback to an environment value (`test_a_loose_key_file_is_refused_rather_than_falling_back`,
  `test_world_readable_secret_file_is_refused`).
- Secrets never appear in `repr()` of `Settings` or `AppConfig`
  (`test_secret_never_appears_in_repr`).
- The endpoint bearer and the M2M secret are absent from the server log in the
  HTTP suite (`test_gated_write_without_a_token_does_not_execute` asserts both).

## What is not covered

- **No rate limiting.** A caller with the endpoint token can issue writes as fast
  as the two-phase gate allows. LiteLLM-level limits are the appropriate place,
  and are not configured here.
- **No per-caller identity.** One shared bearer token means every caller is the
  same principal; the audit Logto records will not distinguish them.
- **No live-provider write has been performed.** Every test uses a fake
  Management API or a fake client. The request *shapes* are asserted; that Logto
  accepts them end to end is unverified until an approved write is made.
- **Suspend replaces delete, and is reversible.** `set_user_suspended` is a real
  effect on real users. It is gated like every other write, but it is not a
  no-op and should not be treated as one.
