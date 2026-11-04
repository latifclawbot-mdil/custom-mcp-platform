"""Capability description: what this server does, and what it deliberately does not.

Two supporting lists make the boundary legible to a caller rather than implied:

* :data:`UNSUPPORTED_CAPABILITIES` — user-facing Play Console actions that this
  server (or the API itself) cannot perform, each with a reason. The
  ``play_describe_capability`` tool returns this verbatim, so an agent can tell
  "not implemented here" apart from "not possible anywhere".
* :data:`API_LIMITATIONS` — things the Android Publisher API does not expose at
  all, so no server could promise them.
"""
from __future__ import annotations

from typing import Any

from .policy import (
    ALLOWED_WRITE_METHODS,
    DISABLED_REASONS,
    EXCLUDED_METHODS,
    EXCLUSION_NOTES,
    FINANCIAL_METHODS,
)

__all__ = [
    "UNSUPPORTED_CAPABILITIES",
    "API_LIMITATIONS",
    "FINANCIAL_EXCLUSION_SUMMARY",
    "capability_report",
]

#: Actions a caller might expect that are NOT exposed, and why.
UNSUPPORTED_CAPABILITIES: tuple[dict[str, str], ...] = (
    {
        "capability": "publish an edit (apply staged changes to production)",
        "reason": (
            "Not supported at all: commit is disabled, not merely gated. The "
            "Android Publisher API exposes no endpoint that lists what is "
            "staged in an edit, and an edit id is a shared handle, so this "
            "server cannot establish that a commit would not apply a financial "
            "change staged by another actor. Under the financial exclusion the "
            "only fail-closed choice is to expose no commit."
        ),
    },
    {
        "capability": "patch a track definition (edits.tracks.patch)",
        "reason": (
            "edits.tracks.patch is PATCH to the same path as edits.tracks.update "
            "with the same Track body; Google documents no behavioural difference "
            "beyond the verb, so it is redundant. Creating a track IS supported "
            "(play_track_create, edits.tracks.create), which tracks.update cannot "
            "do because its track id is a path parameter."
        ),
    },
    {
        "capability": "send changes without review / alter review behaviour",
        "reason": (
            "The commit query parameters changesNotSentForReview and "
            "changesInReviewBehavior are never sent; Google's default applies."
        ),
    },
    {
        "capability": "reply to a review with an image or a rating change",
        "reason": "reviews.reply is text-only by API design.",
    },
    {
        "capability": "manage tester email lists",
        "reason": (
            "The Testers resource does not support email lists; only Google "
            "Groups are manageable through the API."
        ),
    },
    {
        "capability": (
            "edit content rating, target audience, ads, privacy policy, or "
            "'needs attention' declarations"
        ),
        "reason": (
            "No resource in Android Publisher v3 covers these. Only the Data "
            "safety declaration has an endpoint (applications.dataSafety)."
        ),
    },
    {
        "capability": "set the base app price or free/paid state",
        "reason": (
            "Not in the API at all; base app pricing is Play Console only. A "
            "server with no price method physically cannot change it."
        ),
    },
    {
        "capability": "read installs, ratings, crashes, or revenue reports",
        "reason": (
            "Not in this API. Bulk reports are monthly CSV in Cloud Storage "
            "(gs://pubsite_prod_*), read with the Cloud Storage API."
        ),
    },
    {
        "capability": "manage signing enrollment / rotation (Google-managed)",
        "reason": (
            "Only self-hosted Cloud KMS enrollment/rotation are API-reachable, "
            "and both are excluded because they change the app signing key."
        ),
    },
    {
        "capability": "grant a user access, or change app-level permissions",
        "reason": (
            "users.* and grants.* are excluded: the request body can carry "
            "financial permissions, so they are a privilege-escalation path to "
            "exactly the capability this server excludes."
        ),
    },
    {
        "capability": "upload to the internal app-sharing channel",
        "reason": (
            "internalappsharingartifacts.* is an internal-only artifact channel, "
            'gated by the Play Console permission "Release apps to testing '
            'tracks" (which cannot publish to production). Excluded on '
            "channel-scope grounds; it is the lowest-risk write surface in the "
            "API, not an ungated one."
        ),
    },
    {
        "capability": "remediation campaigns (halt or recover a release)",
        "reason": "apprecovery.* can push a release to production; excluded.",
    },
    {
        "capability": "third-party app-store review publishing",
        "reason": "appstoreappsreview.* is a separate, un-reviewed surface; excluded.",
    },
    {
        "capability": "expansion files and externally-hosted APKs",
        "reason": (
            "edits.expansionfiles.* and edits.apks.addexternallyhosted are "
            "legacy artifact routes: OBB files supplement the APK rather than "
            "replacing it, externally-hosted APKs are Managed-Play-only, and "
            "both are superseded by App Bundle + Play Asset Delivery. Excluded "
            "on legacy-scope grounds."
        ),
    },
    {
        "capability": "list the apps in the account",
        "reason": (
            "The API has no 'list my apps' endpoint and every path is "
            "package-scoped. This server's package allowlist is therefore the "
            "only enumeration control and is mandatory."
        ),
    },
    {
        "capability": "any operation not named by a tool in this server",
        "reason": (
            "There is no generic request tool. A method that is not an explicit, "
            "named operation cannot be reached, even with a valid token."
        ),
    },
)

#: Things the API itself does not provide; no server can promise them.
API_LIMITATIONS: tuple[str, ...] = (
    "No 'list my apps' endpoint; all paths are package-scoped.",
    "No per-method permission mapping is published by Google, so no server can "
    "claim its tool set equals 'what the account's permissions allow'.",
    "A single OAuth scope (androidpublisher) covers every method, including "
    "refunds and price setters. The scope is not a financial boundary; the "
    "boundary is the server allowlist plus the Play Console permissions.",
    "Uncommitted edits are discarded ~7 days after insert, and concurrent edits "
    "on one package race (the loser's staged changes are lost).",
    "Only reviews with comment text on the production track can be replied to.",
    "The Data safety declaration has no read-back endpoint.",
    "Deleting an in-app product or subscription is irreversible through the API; "
    "there is no 'deactivate' and no restore.",
)

#: One-line statement of the financial boundary, for the capability report.
FINANCIAL_EXCLUSION_SUMMARY = (
    "Every financial method is excluded by construction: money movement "
    "(refunds), price/tax setters, paid-entitlement revocation "
    "(acknowledge/consume/cancel/defer), methods that turn paid sales on or off "
    "(activate/deactivate/archive/*States), deletion of revenue-bearing products, "
    "user/grant mutations that can carry financial permissions, and "
    "external-transaction bookkeeping. There is no tool, parameter, or path by "
    "which this server can reach any of them."
)


def capability_report(*, release_gate_enabled: bool,
                      artifact_upload_enabled: bool) -> dict[str, Any]:
    """Machine-readable description of the write surface, gates and gaps.

    ``implemented_write_methods`` is the method-level truth; ``enabled_now``
    reports the gates so a caller knows which of them will actually be sent
    with this configuration. The report never claims "all non-financial
    actions" — it names exactly what is reachable.
    """
    return {
        "server": "play-console-writer",
        "financial_exclusion": FINANCIAL_EXCLUSION_SUMMARY,
        "financial_methods_excluded_count": len(FINANCIAL_METHODS),
        "implemented_write_methods_count": len(ALLOWED_WRITE_METHODS),
        "implemented_write_methods": sorted(ALLOWED_WRITE_METHODS),
        #: Methods the API offers but this server refuses on safety grounds,
        #: each with the reason. Distinct from an out-of-scope exclusion.
        "disabled_methods": [
            {"method_id": mid, "reason": reason}
            for mid, reason in sorted(DISABLED_REASONS.items())
        ],
        #: Commit is not available, and the report says so as a boolean rather
        #: than leaving a caller to infer it from the allowlist.
        "commit_available": False,
        "gates": {
            "release_gate_enabled": release_gate_enabled,
            "artifact_upload_enabled": artifact_upload_enabled,
            "note": (
                "A disabled gate refuses the operation with a message naming the "
                "environment variable, before any network call."
            ),
        },
        "excluded_nonfinancial_methods_count": len(EXCLUDED_METHODS),
        "excluded_nonfinancial_methods": [
            {"method_id": mid, "reason": EXCLUSION_NOTES.get(mid, "excluded")}
            for mid in sorted(EXCLUDED_METHODS)
        ],
        "unsupported_capabilities": list(UNSUPPORTED_CAPABILITIES),
        "api_limitations": list(API_LIMITATIONS),
        "claim": (
            "This is the complete, explicit set this server implements. It is "
            "not 'all non-financial Play actions': publishing is disabled "
            "outright (commit cannot be proven free of financial changes), and "
            "access control, signing, recovery campaigns, internal app sharing, "
            "expansion files and third-party store review are excluded by "
            "design. Several Play Console capabilities have no API at all."
        ),
    }