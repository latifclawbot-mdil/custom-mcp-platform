"""The policy layer: what this server is allowed to write, and what it is not.

This module is the single source of truth for the capability boundary. It is
deliberately data, not code paths: tests iterate over it, the capability report
is generated from it, and the client refuses anything absent from it *before*
any network call.

Four sets matter:

``ALLOWED_WRITE_METHODS``
    The discovery-document method ids this server will issue. Every entry has a
    matching named operation with its own path template and typed schema. This
    is the *only* place a verb is permitted.

``FINANCIAL_METHODS``
    Method ids that move money, set price/tax, revoke or alter a paid
    entitlement, delete or deactivate a revenue-bearing product, record or
    reverse an externally-settled payment, or grant a permission that reaches
    any of those. None is in the allowlist. The set is explicit so the boundary
    is testable (allowlist ∩ financial = ∅) rather than an argument in a
    document, and it is the single place the financial boundary is stated — a
    financial method is never also listed as "excluded".

``EXCLUDED_METHODS``
    Non-financial methods that are nonetheless out of scope, each with a reason:
    signing, recovery campaigns, expansion files, internal app sharing, the
    third-party store-review surface, system APK variants, the redundant track
    patch verb, and ``edits.commit`` (disabled: see ``DISABLED_METHODS``). These
    are named individually rather than waved away as "not implemented".

``DISABLED_METHODS``
    Non-financial methods this server deliberately refuses even though the API
    and the account may permit them, because they cannot be made safe under the
    financial exclusion. Currently only ``edits.commit``: the API exposes no
    endpoint that lists what is staged in an edit, and an edit id is a shared
    handle, so the absence of a financial change staged by another actor cannot
    be established. ``is_disabled`` is the runtime check; the client refuses the
    method id outright.

``READ_BACK_METHODS``
    Verification GETs. They can never be turned into a mutation: the client
    derives the verb from the spec, and a ``read`` spec is only ever sent with
    GET.
"""
from __future__ import annotations

from typing import Mapping

__all__ = [
    "ALLOWED_WRITE_METHODS",
    "FINANCIAL_METHODS",
    "EXCLUDED_METHODS",
    "EXCLUSION_NOTES",
    "DISABLED_METHODS",
    "DISABLED_REASONS",
    "READ_BACK_METHODS",
    "RELEASE_GATE_METHODS",
    "ARTIFACT_UPLOAD_METHODS",
    "is_allowed",
    "is_financial",
    "is_disabled",
    "exclusion_reason",
]

# ---------------------------------------------------------------------------
# Allowlist: the non-financial writes this server implements, by discovery id.
# ---------------------------------------------------------------------------
ALLOWED_WRITE_METHODS: frozenset[str] = frozenset({
    # Edit-session plumbing (no state change alone: open / validate / drop)
    "edits.insert",
    "edits.validate",
    "edits.delete",
    # Store listing and localization
    "edits.listings.update",
    "edits.listings.patch",
    "edits.listings.delete",
    "edits.listings.deleteall",
    # Store graphics
    "edits.images.upload",
    "edits.images.delete",
    "edits.images.deleteall",
    # App details / contact info
    "edits.details.update",
    "edits.details.patch",
    # Declarations
    "applications.dataSafety",
    "applications.deviceTierConfigs.create",
    # Testers
    "edits.testers.update",
    "edits.testers.patch",
    # Diagnostics
    "edits.deobfuscationfiles.upload",
    # Reviews (text-only reply)
    "reviews.reply",
    # Artifact staging (upload alone publishes nothing; gated, and the commit
    # gate is separate and default-off)
    "edits.bundles.upload",
    "edits.apks.upload",
    # Releases. All behind the release gate (default off) and accepting only an
    # edit id this server opened. See ``RELEASE_GATE_METHODS``.
    #   * tracks.update replaces a track's release list (PUT .../tracks/{track});
    #   * tracks.create creates a NEW closed-testing track (POST .../tracks),
    #     which tracks.update cannot do because its track id is a path parameter.
    #     It is the only API path that can create a track, so a release manager
    #     who cannot call it cannot open a closed test through the API at all.
    # commit is NOT here: it is disabled (see DISABLED_METHODS).
    "edits.tracks.update",
    "edits.tracks.create",
})

#: Methods that make a change visible to users (or that stage the release list
#: which a commit would apply). Require both the release gate and an edit this
#: server opened.
RELEASE_GATE_METHODS: frozenset[str] = frozenset({
    "edits.tracks.update",
    "edits.tracks.create",
})

#: Binary upload methods, behind ``PLAY_WRITER_ENABLE_ARTIFACT_UPLOAD``.
ARTIFACT_UPLOAD_METHODS: frozenset[str] = frozenset({
    "edits.images.upload",
    "edits.bundles.upload",
    "edits.apks.upload",
    "edits.deobfuscationfiles.upload",
})

# ---------------------------------------------------------------------------
# Financial methods: never reachable. Explicit so the exclusion is testable.
# ---------------------------------------------------------------------------
FINANCIAL_METHODS: frozenset[str] = frozenset({
    # Money movement / reversal
    "orders.refund",
    "orders.reviewrefund",
    "externaltransactions.refundexternaltransaction",
    # Records an externally-settled payment. The ExternalTransaction body
    # carries originalPreTaxAmount, originalTaxAmount, currentPreTaxAmount,
    # currentTaxAmount and userTaxAddress — money metadata, so it is financial
    # bookkeeping and belongs here, not in the non-financial exclusion list.
    "externaltransactions.createexternaltransaction",
    # Price and tax setters
    "inappproducts.insert",
    "inappproducts.update",
    "inappproducts.patch",
    "inappproducts.batchUpdate",
    "monetization.convertRegionPrices",
    "monetization.subscriptions.create",
    "monetization.subscriptions.patch",
    "monetization.subscriptions.batchUpdate",
    "monetization.subscriptions.basePlans.migratePrices",
    "monetization.subscriptions.basePlans.batchMigratePrices",
    "monetization.subscriptions.basePlans.offers.create",
    "monetization.subscriptions.basePlans.offers.patch",
    "monetization.subscriptions.basePlans.offers.batchUpdate",
    "monetization.onetimeproducts.patch",
    "monetization.onetimeproducts.batchUpdate",
    "monetization.onetimeproducts.purchaseOptions.offers.batchUpdate",
    # Product deletion: destroys a revenue-bearing catalog entry, and the same
    # endpoint deletes subscriptions. There is no 'deactivate' and no restore,
    # so this is treated as financial despite carrying no price payload.
    "inappproducts.delete",
    "inappproducts.batchDelete",
    # Paid-entitlement revocation / acknowledgement
    "purchases.subscriptionsv2.cancel",
    "purchases.subscriptionsv2.revoke",
    "purchases.subscriptionsv2.defer",
    "purchases.subscriptions.cancel",
    "purchases.subscriptions.defer",
    "purchases.subscriptions.acknowledge",
    "purchases.products.acknowledge",
    "purchases.products.consume",
    # Deactivations that turn paid sales off, or terminate a billing agreement.
    # These bodies carry no price fields; a payload-only classifier misses them.
    "monetization.subscriptions.archive",
    "monetization.subscriptions.delete",
    "monetization.subscriptions.basePlans.delete",
    "monetization.subscriptions.basePlans.deactivate",
    "monetization.subscriptions.basePlans.activate",
    "monetization.subscriptions.basePlans.batchUpdateStates",
    "monetization.subscriptions.basePlans.offers.delete",
    "monetization.subscriptions.basePlans.offers.activate",
    "monetization.subscriptions.basePlans.offers.deactivate",
    "monetization.subscriptions.basePlans.offers.batchUpdateStates",
    "monetization.onetimeproducts.delete",
    "monetization.onetimeproducts.batchDelete",
    "monetization.onetimeproducts.purchaseOptions.batchDelete",
    "monetization.onetimeproducts.purchaseOptions.batchUpdateStates",
    "monetization.onetimeproducts.purchaseOptions.offers.batchDelete",
    "monetization.onetimeproducts.purchaseOptions.offers.deactivate",
    "monetization.onetimeproducts.purchaseOptions.offers.cancel",
    "monetization.onetimeproducts.purchaseOptions.offers.activate",
    "monetization.onetimeproducts.purchaseOptions.offers.batchUpdateStates",
    # Access control whose body can carry financial permissions: the escalation
    # path to everything above.
    "users.create",
    "users.patch",
    "users.delete",
    "grants.create",
    "grants.patch",
    "grants.delete",
})

# ---------------------------------------------------------------------------
# Excluded non-financial methods, each with the reason it is out of scope.
# ---------------------------------------------------------------------------
EXCLUDED_METHODS: frozenset[str] = frozenset({
    # Redundant duplicate of edits.tracks.update: same path, same Track body,
    # same effect. Excluded so there is exactly one release-list write path.
    # (tracks.create is NOT excluded — it is the only way to create a track.)
    "edits.tracks.patch",
    # Disabled, not merely excluded: see DISABLED_METHODS.
    "edits.commit",
    # Signing: changes the key that authenticates the shipped app.
    "appsigning.enrollApp",
    "appsigning.rotateAppSigningKey",
    # Internal app-sharing artifacts. Gated by the Play Console permission
    # "Release apps to testing tracks" (which cannot publish to production) and
    # unreachable for a service account without it; excluded on channel-scope
    # grounds, not because it is ungated.
    "internalappsharingartifacts.uploadapk",
    "internalappsharingartifacts.uploadbundle",
    # Legacy artifact routes. Expansion files (OBB) supplement the APK rather
    # than replacing it, and Play Asset Delivery supersedes them for >200 MB.
    "edits.apks.addexternallyhosted",
    "edits.expansionfiles.upload",
    "edits.expansionfiles.update",
    "edits.expansionfiles.patch",
    # Remediation campaigns can push a release to production.
    "apprecovery.create",
    "apprecovery.deploy",
    "apprecovery.addTargeting",
    "apprecovery.cancel",
    # Separate third-party store-review publishing surface.
    "appstoreappsreview.createappstorehostedapp",
    "appstoreappsreview.updateappstorehostedapp",
    "appstoreappsreview.updateappstorehostedapppublishstatus",
    "appstoreappsreview.uploadapk",
    "appstoreappsreview.uploadimage",
    "appstoreappsreview.uploadappstoreapppolicydeclarationfile",
    # OEM / device-specific APK variant builder.
    "systemapks.variants.create",
    # POST-verb batch reads: they mutate nothing, but this server exposes no
    # read tools at all, so they are out of scope like any other read.
    "monetization.subscriptions.basePlans.offers.batchGet",
    "monetization.onetimeproducts.purchaseOptions.offers.batchGet",
})

#: Human-readable reason per excluded method. Used by documentation and by the
#: ``play_describe_capability`` tool.
EXCLUSION_NOTES: Mapping[str, str] = {
    "edits.tracks.patch": (
        "PATCH to the same path as edits.tracks.update with the same Track "
        "body; Google documents no behavioural difference beyond the verb, so "
        "it is redundant. One release-list write path is easier to audit."
    ),
    "edits.commit": (
        "Disabled: pending publishes cannot be proven free of financial "
        "changes. The Android Publisher API exposes no endpoint that lists "
        "what is staged in an edit, and an edit id is a shared handle, so a "
        "commit could apply a price or entitlement change staged by another "
        "actor that this server cannot see. With the financial exclusion in "
        "force the only fail-closed choice is to expose no commit at all."
    ),
    "appsigning.enrollApp": "Changes the key that authenticates the shipped app.",
    "appsigning.rotateAppSigningKey": "Changes the app signing key.",
    "internalappsharingartifacts.uploadapk": (
        "Internal-only artifact channel, gated by the Play Console permission "
        '"Release apps to testing tracks" (which cannot publish to production). '
        "Excluded on channel-scope grounds; it is the lowest-risk write surface "
        "in the API, not an ungated one."
    ),
    "internalappsharingartifacts.uploadbundle": (
        "Internal-only artifact channel, gated by the Play Console permission "
        '"Release apps to testing tracks" (which cannot publish to production). '
        "Excluded on channel-scope grounds; it is the lowest-risk write surface "
        "in the API, not an ungated one."
    ),
    "edits.apks.addexternallyhosted": (
        "Legacy route for a self-hosted APK; only available to Managed Play "
        "organizations configured to restrict distribution. Superseded by App "
        "Bundle + Play Asset Delivery."
    ),
    "edits.expansionfiles.upload": (
        "Legacy OBB route. Expansion files supplement the APK; they are "
        "superseded by App Bundle + Play Asset Delivery for >200 MB."
    ),
    "edits.expansionfiles.update": (
        "Legacy OBB route, superseded by App Bundle + Play Asset Delivery."
    ),
    "edits.expansionfiles.patch": (
        "Legacy OBB route, superseded by App Bundle + Play Asset Delivery."
    ),
    "apprecovery.create": "Remediation campaign; can lead to a production push.",
    "apprecovery.deploy": "Can push a remediation release to production.",
    "apprecovery.addTargeting": "Remediation campaign targeting.",
    "apprecovery.cancel": "Reverses a remediation campaign.",
    "appstoreappsreview.createappstorehostedapp": (
        "Separate third-party store-review publishing surface."
    ),
    "appstoreappsreview.updateappstorehostedapp": (
        "Separate third-party store-review publishing surface."
    ),
    "appstoreappsreview.updateappstorehostedapppublishstatus": (
        "Publishes publish status to a third-party store front."
    ),
    "appstoreappsreview.uploadapk": "Publishes an artifact to a store front.",
    "appstoreappsreview.uploadimage": "Publishes an image to a store front.",
    "appstoreappsreview.uploadappstoreapppolicydeclarationfile": (
        "Publishes a policy declaration to a store front."
    ),
    "systemapks.variants.create": "Builds an OEM/device-specific APK variant.",
    "monetization.subscriptions.basePlans.offers.batchGet": (
        "POST-verb batch read (no mutation). This server exposes no read tools; "
        "use the read-only Play Console server."
    ),
    "monetization.onetimeproducts.purchaseOptions.offers.batchGet": (
        "POST-verb batch read (no mutation). This server exposes no read tools; "
        "use the read-only Play Console server."
    ),
}

#: Read methods this server uses to verify a write (read-back). They are GETs.
READ_BACK_METHODS: frozenset[str] = frozenset({
    "edits.get",
    "edits.listings.get",
    "edits.listings.list",
    "edits.details.get",
    "edits.testers.get",
    "edits.images.list",
    "edits.tracks.list",
    "edits.bundles.list",
    "edits.apks.list",
    "applications.deviceTierConfigs.get",
    "applications.deviceTierConfigs.list",
})

# ---------------------------------------------------------------------------
# Disabled methods: named individually, refused with a reason, unreachable.
# ---------------------------------------------------------------------------
#: Non-financial methods this server deliberately refuses even though the API
#: and the account may permit them. Each is also in :data:`EXCLUDED_METHODS` so
#: the "every write is in exactly one bucket" invariant still holds, but unlike
#: a scope exclusion the *reason* here is a safety property, not a design choice.
#:
#: ``edits.commit`` is the only member. It cannot be made fail-closed under the
#: financial exclusion: committing an edit applies everything staged into it,
#: the API has no "list what is staged" endpoint, and an edit id is a shared
#: handle, so this server cannot establish that a financial change staged by
#: another actor is absent.
DISABLED_METHODS: frozenset[str] = frozenset({"edits.commit"})

#: Human-readable reason per disabled method, used by the capability report.
DISABLED_REASONS: Mapping[str, str] = {
    "edits.commit": (
        "Disabled under the financial exclusion: the Android Publisher API "
        "exposes no endpoint that lists what is staged in an edit, and an edit "
        "id is a shared handle, so a commit could apply a financial change "
        "(a price, tax, base plan or offer) staged by another actor that this "
        "server cannot see. The absence of a financial side effect cannot be "
        "established, so no commit is exposed."
    ),
}


def is_allowed(method_id: str) -> bool:
    """True if ``method_id`` is an implemented write for this server."""
    return method_id in ALLOWED_WRITE_METHODS


def is_financial(method_id: str) -> bool:
    """True if ``method_id`` is classified financial (never reachable)."""
    return method_id in FINANCIAL_METHODS


def is_disabled(method_id: str) -> bool:
    """True if ``method_id`` is deliberately refused even though non-financial."""
    return method_id in DISABLED_METHODS


def exclusion_reason(method_id: str) -> str:
    """Return the recorded reason a method is out of scope, or a default."""
    return EXCLUSION_NOTES.get(
        method_id,
        "Excluded from this server's non-financial scope.",
    )


# Invariants, checked at import so a careless edit fails fast rather than
# shipping a price setter or an unreachable tool.
_overlap = ALLOWED_WRITE_METHODS & FINANCIAL_METHODS
if _overlap:  # pragma: no cover - guard
    raise RuntimeError(
        f"policy violation: financial methods in the write allowlist: {sorted(_overlap)}"
    )
_double_listed = ALLOWED_WRITE_METHODS & EXCLUDED_METHODS
if _double_listed:  # pragma: no cover - guard
    raise RuntimeError(
        f"policy violation: methods both allowed and excluded: {sorted(_double_listed)}"
    )
if not (RELEASE_GATE_METHODS | ARTIFACT_UPLOAD_METHODS) <= ALLOWED_WRITE_METHODS:
    raise RuntimeError("policy violation: gated methods outside the allowlist")  # pragma: no cover