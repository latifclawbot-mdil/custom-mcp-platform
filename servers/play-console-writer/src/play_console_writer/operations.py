"""The named non-financial write operations, and their tool specifications.

Every operation:

* calls exactly one allowlisted method id via :class:`GoogleWriteClient` — the
  verb and path live in ``client.METHOD_SPECS``, never here;
* validates its own arguments with the strict schema validator *and* with the
  field validators, before any network call;
* for a staged edit mutation, requires an edit id this server opened
  (:class:`EditRegistry`) and records the stage for audit;
* reads the affected resource back and returns the observed state, so a caller
  never has to trust a bare 200;
* never retries (the client sends writes once).

There is no operation that takes a path, a verb, a URL, a price, a grant, or a
user. The capability boundary in ``policy.py`` is enforced twice: the tool set
below is the whole surface, and the client refuses any method id outside the
allowlist.
"""
from __future__ import annotations

from typing import Any, Mapping

from .client import GoogleWriteClient
from .edit_registry import EditRegistry
from .errors import LocalError
from .settings import Settings
from .validation import (
    ValidationError,
    validate_edit_id,
    validate_enum,
    validate_https_url,
    validate_integer_id,
    validate_path_segment,
    validate_text_field,
    IMAGE_TYPES,
    DEOBFUSCATION_FILE_TYPES,
)

__all__ = ["WriteOperations", "TOOL_SPECS"]

_TRACKS = frozenset({
    "production", "beta", "alpha", "internal",
    "qa", "wear:production", "wear:beta", "wear:alpha", "wear:internal",
    "automotive:production", "automotive:beta", "automotive:alpha",
    "automotive:internal",
})

#: TrackRelease.status enum, exactly as published in the discovery document.
_RELEASE_STATUSES = frozenset({"draft", "inProgress", "halted", "completed"})

#: TrackConfig.formFactor enum. ``DEFAULT`` is the phone/tablet track; the other
#: two are the wear and automotive variants, matched by the ``wear:`` /
#: ``automotive:`` prefixes in ``_TRACKS``.
_FORM_FACTORS = frozenset({"DEFAULT", "WEAR", "AUTOMOTIVE"})

_LISTING_TEXT_MAX = 4000
_SHORT_DESC_MAX = 80
_TITLE_MAX = 30
_SAFETY_CSV_MAX = 200_000

#: Hard ceilings on decoded upload bytes. A base64 argument arrives inside a JSON
#: request, so an unbounded field is a memory-exhaustion lever; these caps are
#: checked before decoding and again on the decoded length. They are deliberately
#: modest: larger artifacts belong in a build pipeline, not in an MCP call.
_MAX_IMAGE_BYTES = 8 * 1024 * 1024        # 8 MiB
_MAX_ARTIFACT_BYTES = 64 * 1024 * 1024    # 64 MiB (AAB / APK)
_MAX_DEOBFUSCATION_BYTES = 32 * 1024 * 1024  # 32 MiB (mapping / symbols)


class WriteOperations:
    """Non-financial write operations bound to a client, settings and registry."""

    def __init__(self, *, client: GoogleWriteClient, settings: Settings,
                 registry: EditRegistry | None = None):
        self._client = client
        self._settings = settings
        self._edits = registry or EditRegistry()

    # -- helpers -----------------------------------------------------------

    def _pkg(self, package_name: str) -> str:
        return self._settings.require_allowed_package(package_name)

    def _edit(self, package: str, edit_id: str):
        """Validate and require an edit id this server opened."""
        validated = validate_edit_id(edit_id)
        return self._edits.require_owned(package, validated), validated

    def _staged(self, package: str, edit_id: str, method_id: str, result: Any) -> Any:
        self._edits.record_staged(package, edit_id, method_id)
        return result

    @staticmethod
    def _track(track) -> dict[str, Any]:
        return {
            "edit_id": track.edit_id,
            "package_name": track.package,
            "expires_in_seconds": track.seconds_remaining(_now()),
        }

    def _base(self, package: str) -> dict[str, Any]:
        return {"package_name": package}

    # -- read-back verification -------------------------------------------

    def _read_back(self, method_id: str, *, params: dict[str, Any]) -> tuple[Any, str | None]:
        """Attempt a verification GET; return ``(value, error_reason)``.

        A failed read-back is **not** an operation failure: the mutation was
        already sent and may have been applied. Returning the error here lets
        the caller report ``applied_unverified`` instead of raising, so a client
        does not read a tool error and re-issue a write that already happened.
        """
        try:
            return self._client.request(method_id, params=params), None
        except Exception as exc:  # noqa: BLE001 - verification is best-effort
            return None, str(exc)

    @staticmethod
    def _verification(observed: Any, *, error: str | None,
                      expected: Mapping[str, Any] | None = None,
                      container: str | None = None) -> tuple[dict[str, Any], bool]:
        """Build the ``verification`` block and report whether it verified.

        ``verified`` means the read-back ran *and* the observed resource matches
        what was written. ``matches_intent`` is only meaningful when a read-back
        was obtained; an error or a mismatch both leave the write applied but
        unproven, which is exactly the state a caller must not retry blindly.
        """
        if error is not None:
            return ({
                "verified": False,
                "matches_intent": None,
                "reason": "read_back_failed",
                "error": error,
                "guidance": (
                    "The write was sent and may have been applied, but the "
                    "read-back failed, so it is unverified. Do NOT blindly "
                    "re-issue this write — it may duplicate the change. Read the "
                    "resource (or open a new edit and inspect) before retrying."
                ),
            }, False)

        if expected:
            mismatches = _mismatches(observed, expected, container)
            if mismatches:
                return ({
                    "verified": False,
                    "matches_intent": False,
                    "reason": "read_back_differs",
                    "mismatches": mismatches,
                    "guidance": (
                        "The read-back succeeded but does not show the value "
                        "that was written. Do NOT re-issue this write; inspect "
                        "the resource and reconcile before acting again."
                    ),
                }, False)

        return ({
            "verified": True,
            "matches_intent": True,
        }, True)

    def _verified_result(self, package: str, *, base: dict[str, Any],
                         observed: Any, error: str | None,
                         resource_key: str | None = None,
                         expected: Mapping[str, Any] | None = None,
                         container: str | None = None,
                         extra: dict[str, Any] | None = None) -> dict[str, Any]:
        """Assemble an operation result carrying the F2 outcome contract."""
        verification, verified = self._verification(
            observed, error=error, expected=expected, container=container)
        result: dict[str, Any] = {
            **self._base(package),
            **base,
            "outcome": "applied" if verified else "applied_unverified",
            "verification": verification,
        }
        if resource_key is not None:
            result["read_back"] = {resource_key: observed}
        if extra:
            result.update(extra)
        return result

    # -- 1. edit session plumbing -----------------------------------------

    def edit_insert(self, package_name: str) -> dict[str, Any]:
        pkg = self._pkg(package_name)
        # Google permits one active edit per app; a second edits.insert discards
        # the first. Refuse before the request so staged work is never lost
        # silently (the registry already owns a live edit for this package).
        live = self._edits.live_edit_for(pkg)
        if live is not None:
            raise LocalError(
                f"package {pkg!r} already has a live edit ({live!r}) that is "
                "already tracked by this server. Opening another would discard "
                "it and every change staged into it. Reuse that edit id, or "
                "discard it with play_edit_delete first."
            )
        data = self._client.request("edits.insert", params={"packageName": pkg}, body={})
        edit_id = str((data or {}).get("id") or "")
        if not edit_id:
            raise LocalError("Google opened an edit but returned no edit id")
        track = self._edits.open_edit(pkg, edit_id)
        return {
            "package_name": pkg,
            "edit_id": edit_id,
            "expiry_time_seconds": (data or {}).get("expiryTimeSeconds"),
            "expires_in_seconds": track.seconds_remaining(_now()),
            "note": (
                "Staged changes are discarded by Google if this edit is never "
                "applied. This server exposes no commit, so nothing staged here "
                "is published through it. Treat this edit id as private: anyone "
                "holding it can stage into or commit it."
            ),
            "read_back": {"edit": data},
        }

    def edit_validate(self, package_name: str, edit_id: str) -> dict[str, Any]:
        pkg = self._pkg(package_name)
        track, edit = self._edit(pkg, edit_id)
        data = self._client.request(
            "edits.validate", params={"packageName": pkg, "editId": edit}
        )
        return {
            **self._base(pkg), "edit_id": edit,
            "validation": data, "staged_methods": self._edits.staged_methods(pkg, edit),
        }

    def edit_delete(self, package_name: str, edit_id: str) -> dict[str, Any]:
        pkg = self._pkg(package_name)
        track, edit = self._edit(pkg, edit_id)
        staged = self._edits.staged_methods(pkg, edit)
        self._client.request("edits.delete", params={"packageName": pkg, "editId": edit})
        self._edits.release(pkg, edit)
        return {
            **self._base(pkg), "edit_id": edit, "deleted": True,
            "discarded_staged_methods": staged,
        }

    # -- 2. store listings -------------------------------------------------

    def listing_update(self, package_name: str, edit_id: str, language: str,
                       title: str | None = None, short_description: str | None = None,
                       full_description: str | None = None,
                       video: str | None = None) -> dict[str, Any]:
        return self._write_listing(
            "edits.listings.update", package_name, edit_id, language,
            title, short_description, full_description, video,
        )

    def listing_patch(self, package_name: str, edit_id: str, language: str,
                      title: str | None = None, short_description: str | None = None,
                      full_description: str | None = None,
                      video: str | None = None) -> dict[str, Any]:
        return self._write_listing(
            "edits.listings.patch", package_name, edit_id, language,
            title, short_description, full_description, video,
        )

    def _write_listing(self, method_id: str, package_name: str, edit_id: str,
                       language: str, title: str | None, short_description: str | None,
                       full_description: str | None, video: str | None) -> dict[str, Any]:
        pkg = self._pkg(package_name)
        _, edit = self._edit(pkg, edit_id)
        lang = validate_path_segment(language, "language")
        body = _listing_body(title, short_description, full_description, video)
        if method_id == "edits.listings.update" and not body:
            raise ValidationError(
                "listing_update needs at least one field (title, "
                "short_description, full_description, video)"
            )
        if not body:
            raise ValidationError("listing_patch needs at least one field to change")
        data = self._client.request(
            method_id,
            params={"packageName": pkg, "editId": edit, "language": lang},
            body=body,
        )
        self._staged(pkg, edit, method_id, data)
        observed, error = self._read_back(
            "edits.listings.get",
            params={"packageName": pkg, "editId": edit, "language": lang},
        )
        return self._verified_result(
            pkg,
            base={"edit_id": edit, "language": lang, "result": data},
            observed=observed, error=error, resource_key="listing",
            expected=body,
        )

    def listing_delete(self, package_name: str, edit_id: str,
                       language: str) -> dict[str, Any]:
        pkg = self._pkg(package_name)
        _, edit = self._edit(pkg, edit_id)
        lang = validate_path_segment(language, "language")
        self._client.request(
            "edits.listings.delete",
            params={"packageName": pkg, "editId": edit, "language": lang},
        )
        self._staged(pkg, edit, "edits.listings.delete", None)
        return {
            **self._base(pkg), "edit_id": edit, "language": lang,
            "deleted": True,
            "read_back": {"listings": self._safe_listings(pkg, edit)},
        }

    def listings_delete_all(self, package_name: str, edit_id: str) -> dict[str, Any]:
        pkg = self._pkg(package_name)
        _, edit = self._edit(pkg, edit_id)
        self._client.request(
            "edits.listings.deleteall", params={"packageName": pkg, "editId": edit}
        )
        self._staged(pkg, edit, "edits.listings.deleteall", None)
        return {
            **self._base(pkg), "edit_id": edit, "deleted_all": True,
            "read_back": {"listings": self._safe_listings(pkg, edit)},
        }

    def _safe_listings(self, pkg: str, edit: str) -> Any:
        try:
            return self._client.request(
                "edits.listings.list", params={"packageName": pkg, "editId": edit}
            )
        except Exception as exc:  # noqa: BLE001 - verification is best-effort
            return {"verification_error": str(exc)}

    # -- 3. app details / contact info ------------------------------------

    def details_update(self, package_name: str, edit_id: str,
                       contact_email: str | None = None,
                       contact_phone: str | None = None,
                       contact_website: str | None = None,
                       default_language: str | None = None) -> dict[str, Any]:
        return self._write_details(
            "edits.details.update", package_name, edit_id,
            contact_email, contact_phone, contact_website, default_language,
        )

    def details_patch(self, package_name: str, edit_id: str,
                      contact_email: str | None = None,
                      contact_phone: str | None = None,
                      contact_website: str | None = None,
                      default_language: str | None = None) -> dict[str, Any]:
        return self._write_details(
            "edits.details.patch", package_name, edit_id,
            contact_email, contact_phone, contact_website, default_language,
        )

    def _write_details(self, method_id: str, package_name: str, edit_id: str,
                       contact_email: str | None, contact_phone: str | None,
                       contact_website: str | None, default_language: str | None) -> dict[str, Any]:
        pkg = self._pkg(package_name)
        _, edit = self._edit(pkg, edit_id)
        body: dict[str, Any] = {}
        if contact_email is not None:
            body["contactEmail"] = validate_text_field(
                contact_email, "contact_email", max_length=200, allow_empty=False)
            if "@" not in body["contactEmail"]:
                raise ValidationError("contact_email must contain '@'")
        if contact_phone is not None:
            body["contactPhone"] = validate_text_field(
                contact_phone, "contact_phone", max_length=40, allow_empty=False)
        if contact_website is not None:
            body["contactWebsite"] = validate_https_url(contact_website, "contact_website")
        if default_language is not None:
            body["defaultLanguage"] = validate_path_segment(default_language, "default_language")
        if method_id == "edits.details.update" and not body:
            raise ValidationError("details_update needs at least one field")
        if not body:
            raise ValidationError("details_patch needs at least one field to change")
        data = self._client.request(
            method_id, params={"packageName": pkg, "editId": edit}, body=body
        )
        self._staged(pkg, edit, method_id, data)
        observed, error = self._read_back(
            "edits.details.get", params={"packageName": pkg, "editId": edit})
        return self._verified_result(
            pkg,
            base={"edit_id": edit, "result": data},
            observed=observed, error=error, resource_key="details",
            expected=body,
        )

    # -- 4. testers --------------------------------------------------------

    def testers_update(self, package_name: str, edit_id: str, track: str,
                       google_groups: list[str]) -> dict[str, Any]:
        return self._write_testers(
            "edits.testers.update", package_name, edit_id, track, google_groups
        )

    def testers_patch(self, package_name: str, edit_id: str, track: str,
                      google_groups: list[str]) -> dict[str, Any]:
        return self._write_testers(
            "edits.testers.patch", package_name, edit_id, track, google_groups
        )

    def _write_testers(self, method_id: str, package_name: str, edit_id: str,
                       track: str, google_groups: list[str]) -> dict[str, Any]:
        pkg = self._pkg(package_name)
        _, edit = self._edit(pkg, edit_id)
        validated_track = validate_enum(track, _TRACKS, "track")
        if not isinstance(google_groups, list):
            raise ValidationError("google_groups must be a list of Google Group addresses")
        groups: list[str] = []
        for group in google_groups:
            text = validate_text_field(group, "google_groups[]", max_length=320,
                                       allow_empty=False)
            if "@" not in text:
                raise ValidationError("each google_groups entry must be an email address")
            groups.append(text)
        body = {"googleGroups": groups}
        data = self._client.request(
            method_id,
            params={"packageName": pkg, "editId": edit, "track": validated_track},
            body=body,
        )
        self._staged(pkg, edit, method_id, data)
        observed, error = self._read_back(
            "edits.testers.get",
            params={"packageName": pkg, "editId": edit, "track": validated_track},
        )
        return self._verified_result(
            pkg,
            base={"edit_id": edit, "track": validated_track, "result": data},
            observed=observed, error=error, resource_key="testers",
            expected=body,
            extra={
                "api_limitation": (
                    "The Testers resource does not support individual email "
                    "lists; only Google Groups are managed through this API."
                ),
            },
        )

    # -- 5. declarations ---------------------------------------------------

    def data_safety_update(self, package_name: str, safety_labels_csv: str) -> dict[str, Any]:
        pkg = self._pkg(package_name)
        csv_text = validate_text_field(
            safety_labels_csv, "safety_labels_csv",
            max_length=_SAFETY_CSV_MAX, allow_empty=False,
        )
        data = self._client.request(
            "applications.dataSafety",
            params={"packageName": pkg},
            body={"safetyLabels": csv_text},
        )
        return {
            **self._base(pkg), "result": data,
            "note": (
                "The Data safety declaration is written as a whole; there is no "
                "read-back endpoint for it in this API version."
            ),
        }

    def device_tier_config_create(self, package_name: str,
                                  config: dict[str, Any],
                                  allow_unknown_devices: bool = False) -> dict[str, Any]:
        pkg = self._pkg(package_name)
        if not isinstance(config, dict):
            raise ValidationError("config must be an object")
        if not config:
            raise ValidationError("config must describe at least one device tier/group")
        known = {"deviceTierSet", "deviceGroups", "userCountrySets"}
        unexpected = set(config) - known
        if unexpected:
            raise ValidationError(
                f"config contains unsupported keys: {sorted(unexpected)}; allowed: {sorted(known)}"
            )
        params: dict[str, Any] = {"packageName": pkg}
        if allow_unknown_devices:
            params["allowUnknownDevices"] = "true"
        data = self._client.request(
            "applications.deviceTierConfigs.create", params=params, body=config
        )
        config_id = (data or {}).get("deviceTierConfigId")
        observed: Any = data
        error: str | None = None
        if config_id is not None:
            observed, error = self._read_back(
                "applications.deviceTierConfigs.get",
                params={"packageName": pkg, "deviceTierConfigId": str(config_id)},
            )
        return self._verified_result(
            pkg,
            base={"result": data},
            observed=observed, error=error, resource_key="device_tier_config",
        )

    # -- 6. reviews --------------------------------------------------------

    def review_reply(self, package_name: str, review_id: str, reply_text: str) -> dict[str, Any]:
        pkg = self._pkg(package_name)
        rid = validate_path_segment(review_id, "review_id")
        text = validate_text_field(
            reply_text, "reply_text",
            max_length=self._settings.max_reply_length, allow_empty=False,
        )
        data = self._client.request(
            "reviews.reply",
            params={"packageName": pkg, "reviewId": rid},
            body={"replyText": text},
        )
        return {
            **self._base(pkg), "review_id": rid, "result": data,
            "api_limitation": (
                "Only reviews with comment text on the production track can be "
                "replied to; a review without a comment is refused upstream."
            ),
        }

    # -- 7. releases (release gate; default off) --------------------------

    def track_update(self, package_name: str, edit_id: str, track: str,
                     version_codes: list[int] | None = None,
                     status: str | None = None,
                     user_fraction: float | None = None,
                     release_name: str | None = None,
                     release_notes: dict[str, str] | None = None,
                     in_app_update_priority: int | None = None,
                     countries: list[str] | None = None,
                     include_rest_of_world: bool | None = None) -> dict[str, Any]:
        """Replace a track's release list inside an edit this server opened.

        This does **not** publish: it stages a release definition. It is still
        behind the release gate because it is the one operation that decides
        what would ship, and the edit it stages into is the one thing that must
        stay free of financial changes.
        """
        pkg = self._pkg(package_name)
        self._settings.require_release_gate_enabled("track_update")
        _, edit = self._edit(pkg, edit_id)
        validated_track = validate_enum(track, _TRACKS, "track")

        release: dict[str, Any] = {}
        if version_codes is not None:
            if not isinstance(version_codes, list) or not version_codes:
                raise ValidationError("version_codes must be a non-empty list")
            if len(version_codes) > 20:
                raise ValidationError("at most 20 version codes per release")
            release["versionCodes"] = [
                str(validate_integer_id(code, "version_codes[]")) for code in version_codes
            ]
        if status is not None:
            release["status"] = validate_enum(status, _RELEASE_STATUSES, "status")
        if user_fraction is not None:
            if isinstance(user_fraction, bool) or not isinstance(user_fraction, (int, float)):
                raise ValidationError("user_fraction must be a number")
            if not (0 < float(user_fraction) < 1):
                raise ValidationError(
                    "user_fraction must be strictly between 0 and 1 (exclusive)")
            release["userFraction"] = float(user_fraction)
        if release_name is not None:
            release["name"] = validate_text_field(
                release_name, "release_name", max_length=200, allow_empty=False)
        if release_notes is not None:
            if not isinstance(release_notes, dict) or not release_notes:
                raise ValidationError("release_notes must be a non-empty object")
            if len(release_notes) > 10:
                raise ValidationError("at most 10 release-note languages")
            notes = []
            for language, text in release_notes.items():
                notes.append({
                    "language": validate_path_segment(language, "release_notes language"),
                    "text": validate_text_field(
                        text, f"release_notes[{language}]", max_length=500,
                        allow_empty=False),
                })
            release["releaseNotes"] = notes
        if in_app_update_priority is not None:
            priority = validate_integer_id(in_app_update_priority, "in_app_update_priority")
            if priority > 5:
                raise ValidationError("in_app_update_priority must be between 0 and 5")
            release["inAppUpdatePriority"] = priority
        if countries is not None or include_rest_of_world is not None:
            targeting: dict[str, Any] = {}
            if countries is not None:
                if not isinstance(countries, list):
                    raise ValidationError("countries must be a list of two-letter codes")
                validated: list[str] = []
                for code in countries:
                    text = validate_text_field(
                        code, "countries[]", max_length=2, allow_empty=False)
                    if len(text) != 2 or not text.isalpha():
                        raise ValidationError(
                            "each countries entry must be a two-letter CLDR code")
                    validated.append(text.upper())
                targeting["countries"] = validated
            if include_rest_of_world is not None:
                targeting["includeRestOfWorld"] = bool(include_rest_of_world)
            release["countryTargeting"] = targeting

        if not release:
            raise ValidationError("track_update needs at least one release field")

        body = {"track": validated_track, "releases": [release]}
        data = self._client.request(
            "edits.tracks.update",
            params={"packageName": pkg, "editId": edit, "track": validated_track},
            body=body,
        )
        self._staged(pkg, edit, "edits.tracks.update", data)
        observed, error = self._read_back(
            "edits.tracks.list", params={"packageName": pkg, "editId": edit})
        return self._verified_result(
            pkg,
            base={"edit_id": edit, "track": validated_track, "result": data},
            observed=observed, error=error, resource_key="track",
            expected={"track": validated_track, "releases": [release]},
            container="tracks",
            extra={
                "not_published": (
                    "Staged only. This server exposes no commit: applying an "
                    "edit cannot be proven free of financial changes staged by "
                    "another actor (see play_describe_capability)."
                ),
            },
        )

    def track_create(self, package_name: str, edit_id: str, track: str,
                     form_factor: str = "DEFAULT") -> dict[str, Any]:
        """Create a NEW track inside an edit this server opened.

        ``edits.tracks.create`` is the only API path that can create a track:
        ``edits.tracks.update`` targets an existing track because its track id
        is a path parameter, and ``edits.tracks.patch`` is the same path as
        update. The body is a ``TrackConfig`` whose ``type`` is fixed to
        ``CLOSED_TESTING`` (Google documents no other supported value) and whose
        ``track`` field names the new track. Both enums are the whole safety
        property, so there is deliberately no free-form passthrough.
        """
        pkg = self._pkg(package_name)
        self._settings.require_release_gate_enabled("track_create")
        _, edit = self._edit(pkg, edit_id)
        validated_track = validate_enum(track, _TRACKS, "track")
        validated_form_factor = validate_enum(
            form_factor, _FORM_FACTORS, "form_factor")

        body = {
            "track": validated_track,
            "type": "CLOSED_TESTING",
            "formFactor": validated_form_factor,
        }
        data = self._client.request(
            "edits.tracks.create",
            params={"packageName": pkg, "editId": edit},
            body=body,
        )
        self._staged(pkg, edit, "edits.tracks.create", data)
        # A create returns the new Track; confirm it with the list read-back.
        observed, error = self._read_back(
            "edits.tracks.list", params={"packageName": pkg, "editId": edit})
        return self._verified_result(
            pkg,
            base={"edit_id": edit, "track": validated_track, "result": data},
            observed=observed, error=error, resource_key="tracks",
            expected={"track": validated_track},
            container="tracks",
            extra={
                "not_published": (
                    "A track definition was created in the edit. This server "
                    "exposes no commit, so nothing is published."
                ),
            },
        )

    def _safe_track(self, pkg: str, edit: str, track: str) -> Any:
        try:
            return self._client.request(
                "edits.tracks.list", params={"packageName": pkg, "editId": edit})
        except Exception as exc:  # noqa: BLE001 - verification is best-effort
            return {"verification_error": str(exc)}

    # NOTE: there is deliberately no edit_commit operation. Committing an edit
    # applies everything staged into it, the API cannot enumerate an edit's
    # contents, and an edit id is a shared handle, so the absence of a financial
    # change staged by another actor cannot be established. See
    # policy.DISABLED_METHODS and DISABLED_REASONS.

    # -- 8. binary uploads (opt-in) ---------------------------------------

    def image_upload(self, package_name: str, edit_id: str, language: str,
                     image_type: str, image_bytes_b64: str) -> dict[str, Any]:
        pkg = self._pkg(package_name)
        self._settings.require_artifact_upload_enabled("image_upload")
        _, edit = self._edit(pkg, edit_id)
        lang = validate_path_segment(language, "language")
        img_type = validate_enum(image_type, IMAGE_TYPES, "image_type")
        media = _decode_base64(image_bytes_b64, "image_bytes_b64", _MAX_IMAGE_BYTES)
        content_type = _sniff_image_type(media)
        if content_type == "application/octet-stream":
            raise ValidationError(
                "image_bytes_b64 is not a PNG or JPEG image (the only image "
                "formats Play accepts for store graphics)"
            )
        data = self._client.request(
            "edits.images.upload",
            params={"packageName": pkg, "editId": edit, "language": lang,
                    "imageType": img_type},
            media=media, media_content_type=content_type,
        )
        self._staged(pkg, edit, "edits.images.upload", data)
        return {
            **self._base(pkg), "edit_id": edit, "language": lang,
            "image_type": img_type, "result": data,
            "read_back": {
                "images": self._safe_images(pkg, edit, lang, img_type)
            },
        }

    def image_delete(self, package_name: str, edit_id: str, language: str,
                     image_type: str, image_id: str) -> dict[str, Any]:
        pkg = self._pkg(package_name)
        _, edit = self._edit(pkg, edit_id)
        lang = validate_path_segment(language, "language")
        img_type = validate_enum(image_type, IMAGE_TYPES, "image_type")
        img_id = validate_path_segment(image_id, "image_id")
        self._client.request(
            "edits.images.delete",
            params={"packageName": pkg, "editId": edit, "language": lang,
                    "imageType": img_type, "imageId": img_id},
        )
        self._staged(pkg, edit, "edits.images.delete", None)
        return {
            **self._base(pkg), "edit_id": edit, "language": lang,
            "image_type": img_type, "image_id": img_id, "deleted": True,
            "read_back": {"images": self._safe_images(pkg, edit, lang, img_type)},
        }

    def images_delete_all(self, package_name: str, edit_id: str, language: str,
                          image_type: str) -> dict[str, Any]:
        pkg = self._pkg(package_name)
        _, edit = self._edit(pkg, edit_id)
        lang = validate_path_segment(language, "language")
        img_type = validate_enum(image_type, IMAGE_TYPES, "image_type")
        data = self._client.request(
            "edits.images.deleteall",
            params={"packageName": pkg, "editId": edit, "language": lang,
                    "imageType": img_type},
        )
        self._staged(pkg, edit, "edits.images.deleteall", data)
        return {
            **self._base(pkg), "edit_id": edit, "language": lang,
            "image_type": img_type, "result": data,
            "read_back": {"images": self._safe_images(pkg, edit, lang, img_type)},
        }

    def _safe_images(self, pkg: str, edit: str, lang: str, img_type: str) -> Any:
        try:
            return self._client.request(
                "edits.images.list",
                params={"packageName": pkg, "editId": edit, "language": lang,
                        "imageType": img_type},
            )
        except Exception as exc:  # noqa: BLE001
            return {"verification_error": str(exc)}

    def bundle_upload(self, package_name: str, edit_id: str,
                      bundle_bytes_b64: str) -> dict[str, Any]:
        pkg = self._pkg(package_name)
        self._settings.require_artifact_upload_enabled("bundle_upload")
        _, edit = self._edit(pkg, edit_id)
        media = _decode_base64(bundle_bytes_b64, "bundle_bytes_b64", _MAX_ARTIFACT_BYTES)
        members = _require_zip(media, "bundle_bytes_b64", "AAB")
        if members is not None and "base/manifest/AndroidManifest.xml" not in members:
            raise ValidationError(
                "bundle_bytes_b64 is a zip but not an Android App Bundle: it is "
                "missing base/manifest/AndroidManifest.xml"
            )
        data = self._client.request(
            "edits.bundles.upload",
            params={"packageName": pkg, "editId": edit},
            media=media, media_content_type="application/octet-stream",
        )
        self._staged(pkg, edit, "edits.bundles.upload", data)
        return {
            **self._base(pkg), "edit_id": edit, "result": data,
            "read_back": {"bundles": self._safe_bundles(pkg, edit)},
            "warning": (
                "Uploading stages an artifact into the edit; it does not publish. "
                "This server exposes no commit, so the artifact is never applied."
            ),
        }

    def apk_upload(self, package_name: str, edit_id: str,
                   apk_bytes_b64: str) -> dict[str, Any]:
        pkg = self._pkg(package_name)
        self._settings.require_artifact_upload_enabled("apk_upload")
        _, edit = self._edit(pkg, edit_id)
        media = _decode_base64(apk_bytes_b64, "apk_bytes_b64", _MAX_ARTIFACT_BYTES)
        _require_zip(media, "apk_bytes_b64", "APK")
        data = self._client.request(
            "edits.apks.upload",
            params={"packageName": pkg, "editId": edit},
            media=media, media_content_type="application/vnd.android.package-archive",
        )
        self._staged(pkg, edit, "edits.apks.upload", data)
        return {
            **self._base(pkg), "edit_id": edit, "result": data,
            "read_back": {"apks": self._safe_apks(pkg, edit)},
            "warning": "Staged only; this server exposes no commit, so nothing is applied.",
        }

    def _safe_bundles(self, pkg: str, edit: str) -> Any:
        try:
            return self._client.request(
                "edits.bundles.list", params={"packageName": pkg, "editId": edit})
        except Exception as exc:  # noqa: BLE001
            return {"verification_error": str(exc)}

    def _safe_apks(self, pkg: str, edit: str) -> Any:
        try:
            return self._client.request(
                "edits.apks.list", params={"packageName": pkg, "editId": edit})
        except Exception as exc:  # noqa: BLE001
            return {"verification_error": str(exc)}

    def deobfuscation_file_upload(self, package_name: str, edit_id: str,
                                  apk_version_code: int, file_type: str,
                                  file_bytes_b64: str) -> dict[str, Any]:
        pkg = self._pkg(package_name)
        self._settings.require_artifact_upload_enabled("deobfuscation_file_upload")
        _, edit = self._edit(pkg, edit_id)
        version = validate_integer_id(apk_version_code, "apk_version_code")
        ftype = validate_enum(file_type, DEOBFUSCATION_FILE_TYPES, "file_type")
        media = _decode_base64(file_bytes_b64, "file_bytes_b64",
                               _MAX_DEOBFUSCATION_BYTES)
        data = self._client.request(
            "edits.deobfuscationfiles.upload",
            params={"packageName": pkg, "editId": edit,
                    "apkVersionCode": str(version), "deobfuscationFileType": ftype},
            media=media, media_content_type="application/octet-stream",
        )
        self._staged(pkg, edit, "edits.deobfuscationfiles.upload", data)
        return {
            **self._base(pkg), "edit_id": edit, "apk_version_code": version,
            "file_type": ftype, "result": data,
            "warning": "Staged into the edit; this server exposes no commit.",
        }


# ---------------------------------------------------------------------------
# Tool specifications: name -> (attr, description, JSON schema)
# ---------------------------------------------------------------------------

def _schema(props: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": required,
            "additionalProperties": False}


_PKG = {
    "type": "string",
    "description": "Android package name, e.g. com.example.app. Must be allowlisted.",
}
_EDIT = {
    "type": "string",
    "description": "Edit id returned by play_edit_insert (open a new edit first).",
}
_LANG = {
    "type": "string",
    "description": "BCP-47 language tag, e.g. en-US.",
}


def _build_tool_specs() -> dict[str, dict[str, Any]]:
    specs: dict[str, dict[str, Any]] = {
        "play_edit_insert": {
            "attr": "edit_insert",
            "method_id": "edits.insert",
            "description": (
                "Open a new Play edit session (POST .../edits). Returns an edit id "
                "used by every staged operation. Nothing is published by opening an "
                "edit, and this server cannot commit it."
            ),
            "schema": _schema({"package_name": _PKG}, ["package_name"]),
        },
        "play_edit_validate": {
            "attr": "edit_validate",
            "method_id": "edits.validate",
            "description": (
                "Validate the current contents of an edit "
                "(POST .../edits/{editId}:validate). Read-like: it reports errors "
                "without applying anything."
            ),
            "schema": _schema({"package_name": _PKG, "edit_id": _EDIT},
                              ["package_name", "edit_id"]),
        },
        "play_edit_delete": {
            "attr": "edit_delete",
            "method_id": "edits.delete",
            "description": (
                "Discard an edit session and every change staged into it "
                "(DELETE .../edits/{editId}). This throws away uncommitted work."
            ),
            "schema": _schema({"package_name": _PKG, "edit_id": _EDIT},
                              ["package_name", "edit_id"]),
        },
        "play_listing_update": {
            "attr": "listing_update",
            "method_id": "edits.listings.update",
            "description": (
                "Create or replace a localized store listing "
                "(PUT .../listings/{language}). Text and video only; no price field."
            ),
            "schema": _schema(
                {
                    "package_name": _PKG, "edit_id": _EDIT, "language": _LANG,
                    "title": {"type": "string", "maxLength": _TITLE_MAX},
                    "short_description": {"type": "string", "maxLength": _SHORT_DESC_MAX},
                    "full_description": {"type": "string", "maxLength": _LISTING_TEXT_MAX},
                    "video": {"type": "string", "description": "https promotional video URL."},
                },
                ["package_name", "edit_id", "language"],
            ),
        },
        "play_listing_patch": {
            "attr": "listing_patch",
            "method_id": "edits.listings.patch",
            "description": (
                "Patch fields of a localized store listing "
                "(PATCH .../listings/{language}). Only the fields you supply change."
            ),
            "schema": _schema(
                {
                    "package_name": _PKG, "edit_id": _EDIT, "language": _LANG,
                    "title": {"type": "string", "maxLength": _TITLE_MAX},
                    "short_description": {"type": "string", "maxLength": _SHORT_DESC_MAX},
                    "full_description": {"type": "string", "maxLength": _LISTING_TEXT_MAX},
                    "video": {"type": "string"},
                },
                ["package_name", "edit_id", "language"],
            ),
        },
        "play_listing_delete": {
            "attr": "listing_delete",
            "method_id": "edits.listings.delete",
            "description": (
                "Delete one localized store listing "
                "(DELETE .../listings/{language}). Destructive."
            ),
            "schema": _schema({"package_name": _PKG, "edit_id": _EDIT, "language": _LANG},
                              ["package_name", "edit_id", "language"]),
        },
        "play_listings_delete_all": {
            "attr": "listings_delete_all",
            "method_id": "edits.listings.deleteall",
            "description": (
                "Delete every localized store listing in the edit "
                "(DELETE .../listings). Destructive."
            ),
            "schema": _schema({"package_name": _PKG, "edit_id": _EDIT},
                              ["package_name", "edit_id"]),
        },
        "play_details_update": {
            "attr": "details_update",
            "method_id": "edits.details.update",
            "description": (
                "Replace app details / contact info (PUT .../details). Contact "
                "email, phone, website and default language only."
            ),
            "schema": _schema(
                {
                    "package_name": _PKG, "edit_id": _EDIT,
                    "contact_email": {"type": "string", "maxLength": 200},
                    "contact_phone": {"type": "string", "maxLength": 40},
                    "contact_website": {"type": "string"},
                    "default_language": {"type": "string"},
                },
                ["package_name", "edit_id"],
            ),
        },
        "play_details_patch": {
            "attr": "details_patch",
            "method_id": "edits.details.patch",
            "description": (
                "Patch app details / contact info (PATCH .../details). Only the "
                "fields you supply change."
            ),
            "schema": _schema(
                {
                    "package_name": _PKG, "edit_id": _EDIT,
                    "contact_email": {"type": "string", "maxLength": 200},
                    "contact_phone": {"type": "string", "maxLength": 40},
                    "contact_website": {"type": "string"},
                    "default_language": {"type": "string"},
                },
                ["package_name", "edit_id"],
            ),
        },
        "play_testers_update": {
            "attr": "testers_update",
            "method_id": "edits.testers.update",
            "description": (
                "Replace the tester Google Groups for a track "
                "(PUT .../testers/{track}). Limitation: the API manages Google "
                "Groups only, not individual tester emails."
            ),
            "schema": _schema(
                {
                    "package_name": _PKG, "edit_id": _EDIT,
                    "track": {"type": "string"},
                    "google_groups": {"type": "array", "items": {"type": "string"}},
                },
                ["package_name", "edit_id", "track", "google_groups"],
            ),
        },
        "play_testers_patch": {
            "attr": "testers_patch",
            "method_id": "edits.testers.patch",
            "description": (
                "Patch the tester Google Groups for a track "
                "(PATCH .../testers/{track}). Google Groups only."
            ),
            "schema": _schema(
                {
                    "package_name": _PKG, "edit_id": _EDIT,
                    "track": {"type": "string"},
                    "google_groups": {"type": "array", "items": {"type": "string"}},
                },
                ["package_name", "edit_id", "track", "google_groups"],
            ),
        },
        "play_data_safety_update": {
            "attr": "data_safety_update",
            "method_id": "applications.dataSafety",
            "description": (
                "Write the Data safety declaration (POST .../dataSafety). Takes the "
                "Data safety form CSV. No read-back endpoint exists for it."
            ),
            "schema": _schema(
                {
                    "package_name": _PKG,
                    "safety_labels_csv": {"type": "string", "maxLength": _SAFETY_CSV_MAX},
                },
                ["package_name", "safety_labels_csv"],
            ),
        },
        "play_device_tier_config_create": {
            "attr": "device_tier_config_create",
            "method_id": "applications.deviceTierConfigs.create",
            "description": (
                "Create a device tier config for an app "
                "(POST .../deviceTierConfigs). Accepts deviceTierSet, deviceGroups "
                "and userCountrySets only."
            ),
            "schema": _schema(
                {
                    "package_name": _PKG,
                    "config": {"type": "object", "additionalProperties": True},
                    "allow_unknown_devices": {"type": "boolean", "default": False},
                },
                ["package_name", "config"],
            ),
        },
        "play_review_reply": {
            "attr": "review_reply",
            "method_id": "reviews.reply",
            "description": (
                "Reply to a review, or update an existing reply "
                "(POST .../reviews/{reviewId}:reply). Text-only."
            ),
            "schema": _schema(
                {
                    "package_name": _PKG,
                    "review_id": {"type": "string"},
                    "reply_text": {"type": "string", "maxLength": 350},
                },
                ["package_name", "review_id", "reply_text"],
            ),
        },
        "play_track_update": {
            "attr": "track_update",
            "method_id": "edits.tracks.update",
            "description": (
                "Stage a track release definition (PUT .../tracks/{track}): version "
                "codes, status, staged-rollout fraction, release notes, country "
                "targeting. Does NOT publish; requires the release gate."
            ),
            "schema": _schema(
                {
                    "package_name": _PKG, "edit_id": _EDIT,
                    "track": {"type": "string", "enum": sorted(_TRACKS)},
                    "version_codes": {
                        "type": "array", "items": {"type": "integer", "minimum": 0},
                        "minItems": 1, "maxItems": 20,
                    },
                    "status": {"type": "string", "enum": sorted(_RELEASE_STATUSES)},
                    "user_fraction": {"type": "number", "minimum": 0, "maximum": 1},
                    "release_name": {"type": "string", "maxLength": 200},
                    "release_notes": {"type": "object", "additionalProperties": True},
                    "in_app_update_priority": {"type": "integer", "minimum": 0, "maximum": 5},
                    "countries": {
                        "type": "array", "items": {"type": "string", "minLength": 2,
                                                   "maxLength": 2},
                        "maxItems": 250,
                    },
                    "include_rest_of_world": {"type": "boolean"},
                },
                ["package_name", "edit_id", "track"],
            ),
        },
        "play_track_create": {
            "attr": "track_create",
            "method_id": "edits.tracks.create",
            "description": (
                "Create a NEW track in the edit (POST .../tracks with a "
                "TrackConfig). Only closed-testing tracks are supported, and "
                "only the fixed track ids are accepted; this is the only API "
                "path that can create a track. Requires the release gate. Does "
                "NOT publish."
            ),
            "schema": _schema(
                {
                    "package_name": _PKG, "edit_id": _EDIT,
                    "track": {"type": "string", "enum": sorted(_TRACKS)},
                    "form_factor": {
                        "type": "string", "enum": sorted(_FORM_FACTORS),
                        "default": "DEFAULT",
                    },
                },
                ["package_name", "edit_id", "track"],
            ),
        },
        "play_image_upload": {
            "attr": "image_upload",
            "method_id": "edits.images.upload",
            "description": (
                "Upload an image (icon/screenshots/feature graphic) into an edit "
                "(POST .../{imageType}). Requires PLAY_WRITER_ENABLE_ARTIFACT_UPLOAD. "
                "Staged only; this server cannot commit."
            ),
            "schema": _schema(
                {
                    "package_name": _PKG, "edit_id": _EDIT, "language": _LANG,
                    "image_type": {"type": "string", "enum": sorted(IMAGE_TYPES)},
                    "image_bytes_b64": {"type": "string"},
                },
                ["package_name", "edit_id", "language", "image_type", "image_bytes_b64"],
            ),
        },
        "play_image_delete": {
            "attr": "image_delete",
            "method_id": "edits.images.delete",
            "description": (
                "Delete one image from an edit "
                "(DELETE .../{imageType}/{imageId}). Destructive."
            ),
            "schema": _schema(
                {
                    "package_name": _PKG, "edit_id": _EDIT, "language": _LANG,
                    "image_type": {"type": "string", "enum": sorted(IMAGE_TYPES)},
                    "image_id": {"type": "string"},
                },
                ["package_name", "edit_id", "language", "image_type", "image_id"],
            ),
        },
        "play_images_delete_all": {
            "attr": "images_delete_all",
            "method_id": "edits.images.deleteall",
            "description": (
                "Delete every image of a language and image type "
                "(DELETE .../listings/{language}/{imageType}). Destructive."
            ),
            "schema": _schema(
                {
                    "package_name": _PKG, "edit_id": _EDIT, "language": _LANG,
                    "image_type": {"type": "string", "enum": sorted(IMAGE_TYPES)},
                },
                ["package_name", "edit_id", "language", "image_type"],
            ),
        },
        "play_bundle_upload": {
            "attr": "bundle_upload",
            "method_id": "edits.bundles.upload",
            "description": (
                "Stage an Android App Bundle (.aab) into an edit "
                "(POST .../bundles). Requires PLAY_WRITER_ENABLE_ARTIFACT_UPLOAD. "
                "Staged only; this server cannot commit and cannot set tracks."
            ),
            "schema": _schema(
                {
                    "package_name": _PKG, "edit_id": _EDIT,
                    "bundle_bytes_b64": {"type": "string"},
                },
                ["package_name", "edit_id", "bundle_bytes_b64"],
            ),
        },
        "play_apk_upload": {
            "attr": "apk_upload",
            "method_id": "edits.apks.upload",
            "description": (
                "Stage an APK into an edit (POST .../apks). Requires "
                "PLAY_WRITER_ENABLE_ARTIFACT_UPLOAD. Staged only; no commit."
            ),
            "schema": _schema(
                {
                    "package_name": _PKG, "edit_id": _EDIT,
                    "apk_bytes_b64": {"type": "string"},
                },
                ["package_name", "edit_id", "apk_bytes_b64"],
            ),
        },
        "play_deobfuscation_file_upload": {
            "attr": "deobfuscation_file_upload",
            "method_id": "edits.deobfuscationfiles.upload",
            "description": (
                "Upload a ProGuard/native deobfuscation file for an APK version "
                "(POST .../deobfuscationFiles/{type}). Requires "
                "PLAY_WRITER_ENABLE_ARTIFACT_UPLOAD. Staged only."
            ),
            "schema": _schema(
                {
                    "package_name": _PKG, "edit_id": _EDIT,
                    "apk_version_code": {"type": "integer", "minimum": 0},
                    "file_type": {"type": "string", "enum": sorted(DEOBFUSCATION_FILE_TYPES)},
                    "file_bytes_b64": {"type": "string"},
                },
                ["package_name", "edit_id", "apk_version_code", "file_type",
                 "file_bytes_b64"],
            ),
        },
    }
    return specs


TOOL_SPECS: dict[str, dict[str, Any]] = _build_tool_specs()


def _mismatches(observed: Any, expected: Mapping[str, Any],
                container: str | None = None) -> dict[str, Any]:
    """Return ``{field: {intended, observed}}`` for fields that do not match.

    ``expected`` holds the camelCase body keys that were written. ``container``
    names a wrapper list (e.g. ``tracks``) whose first element holds the
    comparison fields; if it is absent, a dict is compared directly and a list
    of wrappers is searched for one carrying the expected keys.
    """
    if not isinstance(expected, Mapping) or not expected:
        return {}

    candidates: list[Any]
    if container is not None:
        inner = observed.get(container) if isinstance(observed, Mapping) else None
        candidates = list(inner) if isinstance(inner, list) else []
    elif isinstance(observed, list):
        candidates = list(observed)
    elif isinstance(observed, Mapping):
        candidates = [observed]
    else:
        candidates = []

    # Pick the element that carries the most of the expected keys.
    best: Mapping[str, Any] | None = None
    best_score = -1
    for item in candidates:
        if not isinstance(item, Mapping):
            continue
        score = sum(1 for key in expected if key in item)
        if score > best_score:
            best, best_score = item, score
    if best is None:
        best = {}

    diffs: dict[str, Any] = {}
    for key, intended in expected.items():
        if key not in best:
            continue
        got = best.get(key)
        if got != intended:
            diffs[key] = {"intended": intended, "observed": got}
    return diffs


def _listing_body(title, short_description, full_description, video) -> dict[str, Any]:
    body: dict[str, Any] = {}
    if title is not None:
        body["title"] = validate_text_field(title, "title", max_length=_TITLE_MAX,
                                            allow_empty=False)
    if short_description is not None:
        body["shortDescription"] = validate_text_field(
            short_description, "short_description", max_length=_SHORT_DESC_MAX,
            allow_empty=False)
    if full_description is not None:
        body["fullDescription"] = validate_text_field(
            full_description, "full_description", max_length=_LISTING_TEXT_MAX,
            allow_empty=False)
    if video is not None:
        body["video"] = validate_https_url(video, "video")
    return body


def _decode_base64(value: str, field: str, max_bytes: int) -> bytes:
    """Decode strict base64, refusing anything above ``max_bytes``.

    The length is checked on the *encoded* string first (cheap, and rejects an
    oversized payload before allocating the decode buffer) and again on the
    decoded bytes, because base64 length is only an upper bound on payload size.
    """
    import base64

    if not isinstance(value, str) or not value:
        raise ValidationError(f"{field} must be a non-empty base64 string")
    # 4 base64 characters encode 3 bytes; allow a little slack for padding.
    if len(value) > (max_bytes // 3 + 1) * 4:
        raise ValidationError(
            f"{field} exceeds the {max_bytes}-byte upload limit for this operation"
        )
    try:
        decoded = base64.b64decode(value, validate=True)
    except (ValueError, TypeError) as exc:
        raise ValidationError(f"{field} is not valid base64") from exc
    if len(decoded) > max_bytes:
        raise ValidationError(
            f"{field} exceeds the {max_bytes}-byte upload limit for this operation"
        )
    return decoded


#: A zip archive begins with a local-file-header signature ``PK\x03\x04`` (an
#: empty archive with ``PK\x05\x06``, a spanned archive with ``PK\x07\x08``). The
#: bare ``PK`` prefix is too weak: ``PKjunk...`` is not an archive.
_ZIP_SIGNATURES = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")


def _zip_members(media: bytes) -> list[str] | None:
    """Return the archive's member names, or ``None`` if it is not a readable zip.

    ``None`` means "not a parseable zip", which is distinct from "an empty zip":
    a caller that sends a signature and junk is not sending an archive.
    """
    import io
    import zipfile

    if not media.startswith(_ZIP_SIGNATURES):
        return None
    try:
        with zipfile.ZipFile(io.BytesIO(media)) as archive:
            return archive.namelist()
    except (zipfile.BadZipFile, ValueError, OSError):
        return None


def _require_zip(media: bytes, field: str, label: str) -> list[str] | None:
    """Refuse a payload that is not a zip; return members if it parses as one."""
    if not media.startswith(_ZIP_SIGNATURES):
        raise ValidationError(
            f"{field} does not look like {'an AAB' if label == 'AAB' else 'an APK'} "
            "(expected a zip archive: no PK local-file-header signature)"
        )
    return _zip_members(media)


def _sniff_image_type(media: bytes) -> str:
    if media.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if media.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    return "application/octet-stream"


def _now() -> float:
    import time

    return time.time()
