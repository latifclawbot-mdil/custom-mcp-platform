"""Bounded HTTP transport for the Android Publisher API v3 — writes included.

The single most important property of this module: **there is no generic
request method**. A caller names a *method id* from the frozen registry in
:data:`METHOD_SPECS`; the verb, the path template, and the payload kind all come
from that registry entry. A caller cannot supply a URL, a path, or a verb, so
there is no way to reach a financial endpoint even with a valid token.

Write semantics enforced here:

* a write is sent **once**. There is no retry for POST/PUT/PATCH/DELETE, because
  none of these operations is safely repeatable without the caller's knowledge
  (a retried ``edits.insert`` opens a second edit; a retried upload stages a
  duplicate artifact).
* a write that fails at the transport layer (timeout, connection reset) raises
  :class:`WriteOutcomeUnknown`, which states plainly that the change may have
  been applied. Guessing "it failed" would be a lie.
* reads used for verification are retried on 429/5xx inside one total time
  budget, exactly as the read-only server does.
"""
from __future__ import annotations

import random
import re
import time
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import urlparse

import httpx

from .auth import CredentialConfig
from .errors import LocalError, PolicyError, UpstreamError, WriteOutcomeUnknown, redact_upstream_error
from .policy import ALLOWED_WRITE_METHODS, DISABLED_METHODS, READ_BACK_METHODS
from .settings import Settings

__all__ = ["GoogleWriteClient", "MethodSpec", "METHOD_SPECS", "API_ROOT", "PATH_ROOT"]

API_ROOT = "https://androidpublisher.googleapis.com"
PATH_ROOT = "/androidpublisher/v3/"
ALLOWED_HOSTS = frozenset({"androidpublisher.googleapis.com"})
#: Loopback hosts are permitted only so tests can point the client at an
#: in-process fake. Production construction never passes an override, and the
#: override is validated here rather than trusted.
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})

_PLACEHOLDER = re.compile(r"\{(\+?)([A-Za-z_][A-Za-z0-9_]*)\}")
_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
_MAX_ATTEMPTS = 3


@dataclass(frozen=True)
class MethodSpec:
    """One upstream method this server may issue.

    ``kind`` is ``"read"``, ``"write"`` or ``"media"``. Only ``"write"`` and
    ``"media"`` entries appear in :data:`ALLOWED_WRITE_METHODS`; a ``"read"``
    entry is a verification GET and can never be turned into a mutation.

    ``query_params`` is the complete set of query parameters the method accepts.
    Anything else is a policy error (see :meth:`GoogleWriteClient.request`), so
    a caller cannot smuggle an upstream option such as
    ``changesNotSentForReview`` into a request.
    """

    method_id: str
    verb: str
    path_template: str
    kind: str
    idempotent: bool
    query_params: frozenset[str] = frozenset()


def _spec(method_id: str, verb: str, path: str, kind: str, idempotent: bool,
          query_params: frozenset[str] = frozenset()) -> MethodSpec:
    return MethodSpec(method_id, verb, path, kind, idempotent, query_params)


#: Frozen registry. Every entry is a real Android Publisher v3 method whose
#: verb and path were copied from the discovery document (revision 20261102).
METHOD_SPECS: Mapping[str, MethodSpec] = {
    # -- edit session plumbing ------------------------------------------------
    "edits.insert": _spec(
        "edits.insert", "POST", "applications/{packageName}/edits", "write", False),
    "edits.validate": _spec(
        "edits.validate", "POST", "applications/{packageName}/edits/{editId}:validate",
        "write", True),
    "edits.delete": _spec(
        "edits.delete", "DELETE", "applications/{packageName}/edits/{editId}",
        "write", True),
    # -- listings -------------------------------------------------------------
    "edits.listings.update": _spec(
        "edits.listings.update", "PUT",
        "applications/{packageName}/edits/{editId}/listings/{language}", "write", True),
    "edits.listings.patch": _spec(
        "edits.listings.patch", "PATCH",
        "applications/{packageName}/edits/{editId}/listings/{language}", "write", True),
    "edits.listings.delete": _spec(
        "edits.listings.delete", "DELETE",
        "applications/{packageName}/edits/{editId}/listings/{language}", "write", True),
    "edits.listings.deleteall": _spec(
        "edits.listings.deleteall", "DELETE",
        "applications/{packageName}/edits/{editId}/listings", "write", True),
    # -- images ---------------------------------------------------------------
    "edits.images.upload": _spec(
        "edits.images.upload", "POST",
        "applications/{packageName}/edits/{editId}/listings/{language}/{imageType}",
        "media", False),
    "edits.images.delete": _spec(
        "edits.images.delete", "DELETE",
        "applications/{packageName}/edits/{editId}/listings/{language}/{imageType}/{imageId}",
        "write", True),
    "edits.images.deleteall": _spec(
        "edits.images.deleteall", "DELETE",
        "applications/{packageName}/edits/{editId}/listings/{language}/{imageType}",
        "write", True),
    # -- details --------------------------------------------------------------
    "edits.details.update": _spec(
        "edits.details.update", "PUT",
        "applications/{packageName}/edits/{editId}/details", "write", True),
    "edits.details.patch": _spec(
        "edits.details.patch", "PATCH",
        "applications/{packageName}/edits/{editId}/details", "write", True),
    # -- declarations ---------------------------------------------------------
    "applications.dataSafety": _spec(
        "applications.dataSafety", "POST",
        "applications/{packageName}/dataSafety", "write", False),
    "applications.deviceTierConfigs.create": _spec(
        "applications.deviceTierConfigs.create", "POST",
        "applications/{packageName}/deviceTierConfigs", "write", False,
        query_params=frozenset({"allowUnknownDevices"})),
    # -- testers --------------------------------------------------------------
    "edits.testers.update": _spec(
        "edits.testers.update", "PUT",
        "applications/{packageName}/edits/{editId}/testers/{track}", "write", True),
    "edits.testers.patch": _spec(
        "edits.testers.patch", "PATCH",
        "applications/{packageName}/edits/{editId}/testers/{track}", "write", True),
    # -- diagnostics ----------------------------------------------------------
    "edits.deobfuscationfiles.upload": _spec(
        "edits.deobfuscationfiles.upload", "POST",
        "applications/{packageName}/edits/{editId}/apks/{apkVersionCode}"
        "/deobfuscationFiles/{deobfuscationFileType}", "media", False),
    # -- artifacts (staging only; commit is not exposed) ----------------------
    "edits.bundles.upload": _spec(
        "edits.bundles.upload", "POST",
        "applications/{packageName}/edits/{editId}/bundles", "media", False),
    "edits.apks.upload": _spec(
        "edits.apks.upload", "POST",
        "applications/{packageName}/edits/{editId}/apks", "media", False),
    # -- release gate (default off; see policy.RELEASE_GATE_METHODS) ----------
    "edits.tracks.update": _spec(
        "edits.tracks.update", "PUT",
        "applications/{packageName}/edits/{editId}/tracks/{track}", "write", True),
    # Create a NEW track (TrackConfig body, no {track} path segment). The only
    # API path that can open a closed-testing track; tracks.update cannot,
    # because its track id is a path parameter and the track must already exist.
    "edits.tracks.create": _spec(
        "edits.tracks.create", "POST",
        "applications/{packageName}/edits/{editId}/tracks", "write", False),
    # NOTE: edits.commit has no spec on purpose. It is in policy.DISABLED_METHODS
    # and must stay unreachable even by method id.
    # -- reviews --------------------------------------------------------------
    "reviews.reply": _spec(
        "reviews.reply", "POST",
        "applications/{packageName}/reviews/{reviewId}:reply", "write", True),

    # -- verification reads ---------------------------------------------------
    "edits.get": _spec(
        "edits.get", "GET", "applications/{packageName}/edits/{editId}", "read", True),
    "edits.listings.get": _spec(
        "edits.listings.get", "GET",
        "applications/{packageName}/edits/{editId}/listings/{language}", "read", True),
    "edits.listings.list": _spec(
        "edits.listings.list", "GET",
        "applications/{packageName}/edits/{editId}/listings", "read", True),
    "edits.details.get": _spec(
        "edits.details.get", "GET",
        "applications/{packageName}/edits/{editId}/details", "read", True),
    "edits.testers.get": _spec(
        "edits.testers.get", "GET",
        "applications/{packageName}/edits/{editId}/testers/{track}", "read", True),
    "edits.tracks.list": _spec(
        "edits.tracks.list", "GET",
        "applications/{packageName}/edits/{editId}/tracks", "read", True),
    "edits.images.list": _spec(
        "edits.images.list", "GET",
        "applications/{packageName}/edits/{editId}/listings/{language}/{imageType}",
        "read", True),
    "edits.bundles.list": _spec(
        "edits.bundles.list", "GET",
        "applications/{packageName}/edits/{editId}/bundles", "read", True),
    "edits.apks.list": _spec(
        "edits.apks.list", "GET",
        "applications/{packageName}/edits/{editId}/apks", "read", True),
    "applications.deviceTierConfigs.get": _spec(
        "applications.deviceTierConfigs.get", "GET",
        "applications/{packageName}/deviceTierConfigs/{deviceTierConfigId}", "read", True),
    "applications.deviceTierConfigs.list": _spec(
        "applications.deviceTierConfigs.list", "GET",
        "applications/{packageName}/deviceTierConfigs", "read", True),
}

# Import-time consistency: every allowlisted write must have a spec, and every
# spec is either an allowlisted write/media call or a declared read-back GET.
_missing_specs = ALLOWED_WRITE_METHODS - set(METHOD_SPECS)
if _missing_specs:  # pragma: no cover - guard
    raise RuntimeError(f"policy violation: allowlisted methods with no spec: {sorted(_missing_specs)}")
_bad_writes = {
    mid for mid, spec in METHOD_SPECS.items()
    if spec.kind != "read" and mid not in ALLOWED_WRITE_METHODS
}
if _bad_writes:  # pragma: no cover - guard
    raise RuntimeError(f"policy violation: write specs outside the allowlist: {sorted(_bad_writes)}")
_unknown_reads = {
    mid for mid, spec in METHOD_SPECS.items()
    if spec.kind == "read" and mid not in READ_BACK_METHODS
}
if _unknown_reads:  # pragma: no cover - guard
    raise RuntimeError(f"policy violation: undeclared read-back methods: {sorted(_unknown_reads)}")


class GoogleWriteClient:
    """Named-method transport with a fixed host and no write retries."""

    def __init__(
        self,
        *,
        settings: Settings,
        credentials: Any | None = None,
        credential_config: CredentialConfig | None = None,
        transport: httpx.BaseTransport | None = None,
        api_root: str = API_ROOT,
        sleep=time.sleep,
        clock=time.monotonic,
    ) -> None:
        self._settings = settings
        self._credentials = credentials
        self._credential_config = credential_config
        self._api_root = self._validated_root(api_root)
        self._sleep = sleep
        self._clock = clock
        self._last_request: dict[str, Any] | None = None
        #: The most recent *non-read* request, kept so a tool's audit echo names
        #: the mutation rather than the read-back GET that followed it.
        self._last_write_request: dict[str, Any] | None = None
        self._client = httpx.Client(
            base_url=self._api_root,
            transport=transport,
            timeout=settings.request_timeout,
            headers={"accept": "application/json"},
        )

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "GoogleWriteClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @staticmethod
    def _validated_root(api_root: str) -> str:
        """Refuse anything but the real Google host (or a loopback test fake)."""
        root = (api_root or "").rstrip("/")
        parsed = urlparse(root)
        host = (parsed.hostname or "").lower()
        if parsed.scheme not in {"https", "http"}:
            raise LocalError("API root must be an http(s) URL")
        if host in LOOPBACK_HOSTS:
            return root
        if parsed.scheme != "https":
            raise LocalError("the Android Publisher API must be reached over https")
        if host not in ALLOWED_HOSTS:
            raise LocalError(f"refusing to talk to host {host!r}; only the fixed Google host is allowed")
        if parsed.path not in {"", "/"}:
            raise LocalError("the API root must not contain a path")
        return root

    # -- request -----------------------------------------------------------

    def request(
        self,
        method_id: str,
        *,
        params: Mapping[str, Any] | None = None,
        body: Any | None = None,
        media: bytes | None = None,
        media_content_type: str | None = None,
        timeout: float | None = None,
        verification: bool = False,
    ) -> Any:
        """Issue the named method against its fixed path.

        ``params`` supplies both path placeholders and query parameters; a key
        consumed by a placeholder is removed from the query string. There is no
        way to supply a path or a verb.

        ``verification=True`` marks an auxiliary call made while serving a
        different operation (the pre-commit validation, for instance) so it does
        not replace the audit echo that names the caller's actual mutation.
        """
        spec = METHOD_SPECS.get(method_id)
        if spec is None:
            if method_id in DISABLED_METHODS:
                raise PolicyError(
                    f"method {method_id!r} is disabled under this server's "
                    "financial exclusion and will not be sent: the request "
                    "could apply a financial change this server cannot account "
                    "for. This server has no generic request capability."
                )
            raise PolicyError(
                f"method {method_id!r} is not implemented by this server and will "
                "not be sent. This server has no generic request capability."
            )
        if spec.kind != "read" and method_id not in ALLOWED_WRITE_METHODS:
            # Defence in depth: the registry is immutable, but a future edit
            # that adds a write spec without allowlisting it must fail closed.
            raise PolicyError(f"method {method_id!r} is not in the write allowlist")

        params = dict(params or {})
        path = self._render_path(spec.path_template, params)

        # Everything left after placeholder substitution is a query parameter.
        # Only the ones the method actually declares may be sent: an undeclared
        # key (e.g. changesNotSentForReview) is a policy error, so a caller
        # cannot alter upstream behaviour through an option this server never
        # documented. ``uploadType`` is set by this server for media, not by the
        # caller, and is therefore not a caller-supplied query parameter.
        caller_query = {k: v for k, v in params.items() if v is not None and v != ""}
        undeclared = set(caller_query) - set(spec.query_params)
        if undeclared:
            raise PolicyError(
                f"{method_id!r} does not accept query parameter(s) "
                f"{sorted(undeclared)}; allowed: {sorted(spec.query_params) or 'none'}"
            )
        query = {key: _stringify(value) for key, value in caller_query.items()}
        if spec.kind == "media":
            query["uploadType"] = "media"

        return self._send(spec, path, query, body=body, media=media,
                          media_content_type=media_content_type, timeout=timeout,
                          verification=verification)

    # -- internals ---------------------------------------------------------

    def _render_path(self, template: str, params: dict[str, Any]) -> str:
        """Substitute placeholders and forbid escaping the API root."""

        def replace(match: re.Match[str]) -> str:
            name = match.group(2)
            if name not in params:
                raise LocalError(f"missing path parameter: {name}")
            raw = params.pop(name)
            if raw is None or raw == "":
                raise LocalError(f"path parameter {name} must not be empty")
            text = _stringify(raw)
            if "/" in text or "?" in text or "#" in text or "%" in text or "\\" in text:
                raise LocalError(f"path parameter {name} must be a single segment")
            if ".." in text or (":" in text and not (
                name == "track" and re.fullmatch(r"(?:wear|automotive):(production|beta|alpha|internal)", text)
            )):
                raise LocalError(f"path parameter {name} contains a forbidden character")
            return text

        rendered = _PLACEHOLDER.sub(replace, template)
        path = PATH_ROOT + rendered
        if not path.startswith(PATH_ROOT):
            raise LocalError(f"refusing request outside {PATH_ROOT}")
        if ".." in path:
            raise LocalError("refusing request containing a parent-directory segment")
        return path

    def _send(
        self,
        spec: MethodSpec,
        path: str,
        query: dict[str, str],
        *,
        body: Any | None,
        media: bytes | None,
        media_content_type: str | None,
        timeout: float | None,
        verification: bool = False,
    ) -> Any:
        headers = {"authorization": f"Bearer {self._token()}"}
        if media is not None:
            headers["content-type"] = media_content_type or "application/octet-stream"

        effective_timeout = timeout or self._settings.request_timeout
        self._last_request = {
            "method_id": spec.method_id,
            "verb": spec.verb,
            "root": self._api_root,
            "path": path,
            "query": dict(query),
            "has_body": body is not None,
            "body_bytes": len(media) if media is not None else None,
        }
        if spec.kind != "read" and not verification:
            self._last_write_request = dict(self._last_request)

        if spec.kind == "read":
            return self._send_read(spec, path, query, headers, effective_timeout)
        return self._send_write(
            spec, path, query, headers, effective_timeout, body=body, media=media
        )

    def _send_read(
        self, spec: MethodSpec, path: str, query: dict[str, str],
        headers: dict[str, str], effective_timeout: float,
    ) -> Any:
        deadline = self._clock() + effective_timeout
        last: UpstreamError | None = None
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            attempt_timeout = effective_timeout
            if attempt > 1:
                remaining = deadline - self._clock()
                if remaining <= 0:
                    if last is not None:
                        raise last
                    raise UpstreamError(
                        status=504,
                        reason="upstream timeout",
                        url=f"{self._api_root}{path}",
                        body=f"request budget of {effective_timeout}s exhausted",
                    )
                attempt_timeout = min(effective_timeout, remaining)
            try:
                response = self._client.get(
                    path, params=query, headers=headers, timeout=attempt_timeout
                )
            except httpx.TimeoutException as exc:
                last = UpstreamError(
                    status=504,
                    reason="upstream timeout",
                    url=f"{self._api_root}{path}",
                    body=f"{type(exc).__name__}: request exceeded "
                         f"{attempt_timeout}s (total budget {effective_timeout}s)",
                )
                if attempt < _MAX_ATTEMPTS:
                    self._backoff(attempt)
                    continue
                raise last from None
            except httpx.HTTPError as exc:
                raise UpstreamError(
                    status=502,
                    reason="transport error",
                    url=f"{self._api_root}{path}",
                    body=type(exc).__name__,
                ) from None

            if response.status_code < 300:
                return _json_or_empty(response)

            error = _response_error(response, self._api_root)
            if response.status_code in _RETRY_STATUSES and attempt < _MAX_ATTEMPTS:
                last = error
                self._backoff(attempt)
                continue
            raise error

        assert last is not None  # pragma: no cover - loop always raises
        raise last

    def _send_write(
        self, spec: MethodSpec, path: str, query: dict[str, str],
        headers: dict[str, str], effective_timeout: float,
        *, body: Any | None, media: bytes | None,
    ) -> Any:
        """Send a write exactly once. No retry, ever."""
        kwargs: dict[str, Any] = {
            "params": query,
            "headers": headers,
            "timeout": effective_timeout,
        }
        if media is not None:
            kwargs["content"] = media
        elif body is not None:
            kwargs["json"] = body

        try:
            response = self._client.request(spec.verb, path, **kwargs)
        except (httpx.TimeoutException, httpx.HTTPError) as exc:
            raise WriteOutcomeUnknown(
                status=504 if isinstance(exc, httpx.TimeoutException) else 502,
                reason="transport error",
                url=f"{self._api_root}{path}",
                body=type(exc).__name__,
            ) from None

        if response.status_code < 300:
            return _json_or_empty(response)
        raise _response_error(response, self._api_root)

    def _backoff(self, attempt: int) -> None:
        self._sleep(min(0.25 * (2 ** (attempt - 1)), 2.0) + random.uniform(0, 0.05))

    def _token(self) -> str:
        """Return an access token, materialising credentials on first use."""
        creds = self._credentials
        if creds is None:
            if self._credential_config is None:
                raise LocalError(
                    "no Google credentials configured; the server cannot "
                    "authenticate to the Play API"
                )
            creds = self._credential_config.resolve()
            self._credentials = creds

        token = getattr(creds, "token", None)
        if token is None or getattr(creds, "valid", True) is False:
            import google.auth.transport.requests

            request = google.auth.transport.requests.Request()
            try:
                creds.refresh(request)
            except Exception as exc:  # noqa: BLE001
                raise _refresh_error(exc) from None
            token = getattr(creds, "token", None)

        if not token:
            raise LocalError("Google credentials produced an empty access token")
        return str(token)


def _refresh_error(exc: Exception) -> UpstreamError:
    from .auth import CredentialConfig

    return UpstreamError(
        status=401,
        reason="credential refresh failed",
        body=CredentialConfig._describe(exc),
    )


def _stringify(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple)):
        return ",".join(_stringify(item) for item in value)
    return str(value)


def _json_or_empty(response: httpx.Response) -> Any:
    if not response.content:
        return {}
    try:
        return response.json()
    except ValueError:
        raise UpstreamError(
            status=502,
            reason="upstream returned a non-JSON body",
            url=str(response.request.url),
            body=response.text,
        ) from None


def _response_error(response: httpx.Response, api_root: str) -> UpstreamError:
    url = str(response.request.url) if response.request is not None else api_root
    return redact_upstream_error(
        status=response.status_code,
        body=response.text,
        url=url,
        headers=dict(response.headers),
    )