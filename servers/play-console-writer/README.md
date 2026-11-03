# play-console-writer MCP server

An MCP (stdio) server for **non-financial Google Play Console writes**, built as
a separate, isolated server alongside the existing read-only `play_console`.

It exposes a fixed set of **named** write operations. There is no generic
`request(method, path, body)` tool, no caller-supplied URL, and no financial
method anywhere in the registry.

- Package: `play_console_writer` (`servers/play-console-writer`)
- Entry point: `python -m play_console_writer` (or `play-console-writer-mcp`)
- Upstream: `https://androidpublisher.googleapis.com/androidpublisher/v3/`
- Tools: **23** — 22 write operations + 1 capability report

---

## 1. What it does *not* do (read this first)

The claim is **not** "all non-financial Play actions". It is: *these 22 write
methods, explicitly named, with everything else refused.*

**Disabled outright** (the API allows it; this server refuses it): `edits.commit`.
Committing an edit applies everything staged into it, the API exposes no endpoint
that lists what is staged, and an edit id is a shared handle — so the absence of a
financial change staged by another actor cannot be established. Rather than gate a
method it cannot make fail-closed, this server exposes no commit at all. See
`policy.DISABLED_METHODS`; `play_describe_capability` reports it under
`disabled_methods` with `commit_available: false`.

**Excluded because it is financial or reaches finance** (never reachable — no
tool, parameter, or path):

- money movement: `orders.refund`, `orders.reviewrefund`,
  `externaltransactions.refundexternaltransaction`
- external-transaction bookkeeping: `externaltransactions.createexternaltransaction`
  (records an externally-settled payment; its body carries pre/post-tax amounts
  and a user tax address). Financial, not a mere scope exclusion.
- price / tax setters: every `inappproducts.*` insert/update/patch/batchUpdate,
  `monetization.convertRegionPrices`, `monetization.subscriptions.*` create/patch/
  batchUpdate, `basePlans.migratePrices` / `batchMigratePrices`, offer
  create/patch/batchUpdate, `monetization.onetimeproducts.*` patch/batchUpdate
- paid-entitlement revocation: `purchases.*` acknowledge / consume / cancel / defer / revoke
- turning paid sales on or off: `activate` / `deactivate` / `archive` / `delete` /
  `*States` on subscriptions, base plans, offers, purchase options. These bodies
  carry **no price fields** — a payload-only classifier would miss them, which is
  why the boundary is an explicit method list, not a schema scan.
- **product deletion** (`inappproducts.delete`, `inappproducts.batchDelete`):
  destroys a revenue-bearing catalog entry, and the same endpoint deletes
  subscriptions. There is no deactivate and no restore, so it is treated as
  financial despite carrying no price payload.
- **access control** (`users.*`, `grants.*`): the body can grant *financial*
  permissions to another account — privilege escalation to exactly the excluded
  set.

**Excluded despite being non-financial** (each named in
`policy.EXCLUDED_METHODS`, with a reason surfaced by
`play_describe_capability`):

| Group | Methods | Why |
| --- | --- | --- |
| signing | `appsigning.enrollApp`, `rotateAppSigningKey` | changes the key that authenticates the shipped app |
| internal app sharing | `internalappsharingartifacts.*` | internal-only channel, gated by "Release apps to testing tracks" (which cannot publish to production); excluded on scope, not because it is ungated |
| expansion files | `edits.expansionfiles.*`, `edits.apks.addexternallyhosted` | legacy OBB / self-hosted APK routes; OBB supplements the APK and both are superseded by App Bundle + Play Asset Delivery |
| remediation | `apprecovery.create/deploy/addTargeting/cancel` | `deploy` can push a release to production |
| store review | `appstoreappsreview.*` | separate, un-reviewed publishing surface |
| OEM variants | `systemapks.variants.create` | builds an OEM/device-specific APK variant |
| duplicate track verb | `edits.tracks.patch` | PATCH to the same path/body as `edits.tracks.update`; redundant. `edits.tracks.create` IS supported: it is the only API path that can create a track |
| commit | `edits.commit` | disabled: staged contents cannot be enumerated, so a financial change cannot be ruled out (see above) |
| batch reads | `*.offers.batchGet` | POST-verb reads; this server has no read tools |

**Not possible in the API at all** (so no server can promise them): content
rating, target audience, ads declaration, privacy policy, "needs attention"
status, base app price / free-paid state, installs / ratings / crashes / revenue
reports, a "list my apps" endpoint, tester email lists (Google Groups only), and
Google-managed signing enrollment.

**Release gate, and no publish path.** `play_track_update` (replace a track's
release list) and `play_track_create` (create a new track) exist, but both are
behind `PLAY_WRITER_ENABLE_RELEASE_GATE`, which defaults to **off**. There is no
`play_edit_commit` tool and no commit method: applying an edit cannot be proven
free of financial changes staged by another actor, so this server never applies
one. With the default configuration it can neither stage a release nor publish.

---

## 2. Tools

### Edit session

| Tool | Method | Notes |
| --- | --- | --- |
| `play_edit_insert` | `edits.insert` POST | opens an edit; nothing is published |
| `play_edit_validate` | `edits.validate` POST | reports errors, applies nothing |
| `play_edit_delete` | `edits.delete` DELETE | discards the edit and everything staged |

### Store listing and graphics

| Tool | Method |
| --- | --- |
| `play_listing_update` | `edits.listings.update` PUT |
| `play_listing_patch` | `edits.listings.patch` PATCH |
| `play_listing_delete` | `edits.listings.delete` DELETE |
| `play_listings_delete_all` | `edits.listings.deleteall` DELETE |
| `play_image_upload` | `edits.images.upload` POST (media) |
| `play_image_delete` | `edits.images.delete` DELETE |
| `play_images_delete_all` | `edits.images.deleteall` DELETE |

`Listing` carries `title`, `shortDescription`, `fullDescription`, `video`,
`language` — no price field exists to set.

### App details, testers, declarations

| Tool | Method |
| --- | --- |
| `play_details_update` | `edits.details.update` PUT |
| `play_details_patch` | `edits.details.patch` PATCH |
| `play_testers_update` | `edits.testers.update` PUT (Google Groups only) |
| `play_testers_patch` | `edits.testers.patch` PATCH |
| `play_data_safety_update` | `applications.dataSafety` POST |
| `play_device_tier_config_create` | `applications.deviceTierConfigs.create` POST |

### Reviews

| Tool | Method |
| --- | --- |
| `play_review_reply` | `reviews.reply` POST (`:reply`), text-only |

### Artifacts (opt-in, default off)

Require `PLAY_WRITER_ENABLE_ARTIFACT_UPLOAD=true`. Uploading stages only.

| Tool | Method |
| --- | --- |
| `play_bundle_upload` | `edits.bundles.upload` POST (media, AAB) |
| `play_apk_upload` | `edits.apks.upload` POST (media, APK) |
| `play_deobfuscation_file_upload` | `edits.deobfuscationfiles.upload` POST (media) |

### Release gate (opt-in, default off)

Require `PLAY_WRITER_ENABLE_RELEASE_GATE=true`.

| Tool | Method | Notes |
| --- | --- | --- |
| `play_track_update` | `edits.tracks.update` PUT | stages version codes, status, staged-rollout fraction, release notes, country targeting |
| `play_track_create` | `edits.tracks.create` POST | creates a NEW closed-testing track from a `TrackConfig`; the only API path that can create a track. Fixed `type: CLOSED_TESTING` and `form_factor` enum, no passthrough |

There is deliberately **no** `play_edit_commit`. See the disabled-method section
above.

### Capability

| Tool | Notes |
| --- | --- |
| `play_describe_capability` | read-only; returns every implemented method, every excluded method with its reason, the gate states, and the unsupported list |

---

## 3. Controls, and where the guarantee actually lives

1. **Explicit method allowlist** (`policy.ALLOWED_WRITE_METHODS`). The client
   refuses any method id outside it *before* any network call. A caller names a
   method id; the verb and path come from a frozen registry, never from input.
2. **Typed schema per tool**, `additionalProperties: false`, enforced at the
   **transport layer** by middleware. This matters: the MCP SDK builds its
   argument model from the handler signature and *silently drops* unknown keys,
   so without the guard a caller could pass `price` and get a successful call.
   The guard refuses it before dispatch.
3. **Mandatory package allowlist** (`PLAY_WRITER_ALLOWED_PACKAGES`), fail-closed
   at startup and at every call. There is no "list my apps" endpoint, so this is
   the only enumeration control.
4. **Fixed host.** `GoogleWriteClient` validates its base URL: only
   `androidpublisher.googleapis.com` over HTTPS, or a loopback address for
   tests. A static test token is only legal together with a loopback base.
5. **Edit ownership.** Every staged mutation, `play_track_update` and
   `play_track_create` must name an edit id this server opened
   (`edit_registry.EditRegistry`). An edit id from elsewhere is refused, and a
   second `edits.insert` for a package with a live edit is refused before the
   request — see the shared-edit hazard below.
6. **No write retries.** Writes are sent once. A transport failure raises
   `WriteOutcomeUnknown`, which says plainly that the change may have been
   applied rather than guessing "it failed". Only verification GETs retry.
7. **No arbitrary query parameters.** Each method declares the query parameters
   it accepts (only `deviceTierConfigs.create` declares `allowUnknownDevices`);
   anything else is a policy error, so `changesNotSentForReview` cannot be set.
8. **Read-back after every staged write** where the API offers one; the tool
   result carries `outcome` (`applied` / `applied_unverified`) plus a
   `verification` block, so a 200 alone is never the evidence — and a failed
   read-back is reported as unverified, not as a tool error that invites a
   duplicate retry.

### The shared-edit hazard (why commit is disabled)

An edit id is a **shared handle**. Whoever commits it applies everything staged
into it, including a price someone else staged. Android Publisher exposes no
endpoint that lists what is staged in an edit, so this server **cannot prove** an
edit it opened stayed financial-free.

What it does about it:

- refuses to stage into an edit it did not open (control 5), and refuses a second
  live edit for the same package;
- exposes **no commit at all** — the one guarantee that survives the hazard;
- states the reason in `play_describe_capability` (`disabled_methods`,
  `commit_available: false`).

### Second layer: Play Console permissions

There is exactly **one** OAuth scope for this API
(`.../auth/androidpublisher`), and it covers refunds and price setters too. The
scope is therefore *not* a financial boundary. Grant the service account only
`View app information`, `Manage store presence`, `Manage testing tracks` — and
**no** `View financial data`, `Manage orders and subscriptions`, or financial
reports permission. That is the boundary Google enforces even if this server is
bypassed with the same credential.

---

## 4. Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `PLAY_WRITER_ALLOWED_PACKAGES` | — (required) | comma-separated package allowlist; missing = refuse to start |
| `PLAY_WRITER_CREDENTIALS_FILE` | ADC | service-account JSON path |
| `PLAY_WRITER_SUBJECT` | — | domain-wide delegation subject (requires a credentials file) |
| `PLAY_WRITER_REQUEST_TIMEOUT` | `20` | seconds, 0.1–300 |
| `PLAY_WRITER_MAX_REPLY_LENGTH` | `350` | cap on `reviews.reply` text |
| `PLAY_WRITER_ENABLE_RELEASE_GATE` | `false` | enables `play_track_update` and `play_track_create` |
| `PLAY_WRITER_ENABLE_ARTIFACT_UPLOAD` | `false` | enables the three binary upload tools |
| `PLAY_WRITER_TEST_LOOPBACK_BASE` | — | test only; must be loopback |
| `PLAY_WRITER_TEST_STATIC_TOKEN` | — | test only; requires the loopback base |

---

## 5. Tests

```bash
cd servers/play-console-writer
PYTHONPATH=src:. python -m pytest -q
```

The suite is hermetic: no Google call is made. `tests/fixtures/androidpublisher_v3_methods.json`
is a snapshot of the live discovery document (145 methods, revision 20261102),
and the policy tests assert that every allowlisted method exists in it with the
**same verb and path**, that no financial method is reachable, and that every
write verb in the API falls in exactly one bucket (allowed / financial /
excluded).

`tests/test_stdio_e2e.py` drives a real `python -m play_console_writer`
subprocess over the MCP stdio protocol against an in-process fake upstream:
genuine `initialize`, `tools/list` and `tools/call`.
