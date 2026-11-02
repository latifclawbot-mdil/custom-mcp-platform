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
    entitlement, delete or deactivate a revenue-bearing product, or grant a
    permission that reaches any of those. None is in the allowlist. The set is
    explicit so the boundary is testable (allowlist ∩ financial = ∅) rather
    than an argument in a document.

``EXCLUDED_METHODS``
    Non-financial methods that are nonetheless out of scope, each with a reason:
    signing, recovery campaigns, expansion files, internal app sharing, the
    third-party store-review surface, system APK variants, and the redundant
    track create/patch duplicates. These are named individually rather than
    waved away as "not implemented".

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
    "READ_BACK_METHODS",
    "RELEASE_GATE_METHODS",
    "ARTIFACT_UPLOAD_METHODS",
    "is_allowed",
    "is_financial",
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
    # Releases. Both are behind the release gate (default off) and accept only
    # an edit id this server opened. See ``RELEASE_GATE_METHODS``.
    "edits.tracks.update",
    "edits.commit",
})

#: Methods that make a change visible to users (or that stage the release list
#: which a commit would apply). Require both the release gate and an edit this
#: server opened.
RELEASE_GATE_METHODS: frozenset[str] = frozenset({
    "edits.tracks.update",
    "edits.commit",
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
    # Redundant duplicates of edits.tracks.update (the create-or-replace call
    # this server implements). Excluded so there is exactly one release path.
    "edits.tracks.create",
    "edits.tracks.patch",
    # Financial bookkeeping: records an externally-settled payment.
    "externaltransactions.createexternaltransaction",
    # Signing: changes the key that authenticates the shipped app.
    "appsigning.enrollApp",
    "appsigning.rotateAppSigningKey",
    # Artifact channel with no gate beyond the API scope.
    "internalappsharingartifacts.uploadapk",
    "internalappsharingartifacts.uploadbundle",
    # Expansion files / externally-hosted apk: fork the shipped artifact set.
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
    "edits.tracks.create": (
        "Redundant with edits.tracks.update, which creates or replaces a track's "
        "release list; one release path is easier to audit."
    ),
    "edits.tracks.patch": (
        "Redundant with edits.tracks.update; one release path is easier to audit."
    ),
    "externaltransactions.createexternaltransaction": (
        "Financial bookkeeping: records an externally-settled payment amount."
    ),
    "appsigning.enrollApp": "Changes the key that authenticates the shipped app.",
    "appsigning.rotateAppSigningKey": "Changes the app signing key.",
    "internalappsharingartifacts.uploadapk": (
        "Artifact channel with no gate beyond the API scope."
    ),
    "internalappsharingartifacts.uploadbundle": (
        "Artifact channel with no gate beyond the API scope."
    ),
    "edits.apks.addexternallyhosted": "Forks the shipped artifact set.",
    "edits.expansionfiles.upload": "Forks the shipped artifact set.",
    "edits.expansionfiles.update": "Forks the shipped artifact set.",
    "edits.expansionfiles.patch": "Forks the shipped artifact set.",
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


def is_allowed(method_id: str) -> bool:
    """True if ``method_id`` is an implemented write for this server."""
    return method_id in ALLOWED_WRITE_METHODS


def is_financial(method_id: str) -> bool:
    """True if ``method_id`` is classified financial (never reachable)."""
    return method_id in FINANCIAL_METHODS


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