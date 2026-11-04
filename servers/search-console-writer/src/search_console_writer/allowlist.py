"""Fail-closed property allowlist for the write server.

Identical matching rules to the read-only server's allowlist — exact match after
a narrow normalisation — but a *separate* configuration variable
(``GSC_WRITE_ALLOWED_SITES``) and a separate instance. That separation matters:
a deployment can grant the read server visibility into properties the write
server is not permitted to mutate, and the two allowlists never share state.
"""

from __future__ import annotations

from typing import Iterable, Mapping, Optional

from .errors import EmptyAllowlistError

DEFAULT_SITES_ENV = "GSC_WRITE_ALLOWED_SITES"


def normalize_property(value: str) -> str:
    """Normalize a Search Console property identifier for exact comparison.

    - ``sc-domain:`` (Domain) properties: the whole prefix and the host are
      lowercased (hosts are case-insensitive).
    - ``http(s)://`` (URL-prefix) properties: a trailing ``/`` is added if
      missing; the rest of the string is preserved byte-for-byte, because URL
      paths are case-sensitive.

    Any other scheme is returned stripped but otherwise untouched, so an
    unexpected format matches only itself (fail-closed rather than fail-open).
    """
    s = value.strip()
    if not s:
        raise EmptyAllowlistError("property identifier is empty")
    lowered = s.lower()
    if lowered.startswith("sc-domain:"):
        rest = s[len("sc-domain:") :]
        return "sc-domain:" + rest.lower()
    if lowered.startswith("http://") or lowered.startswith("https://"):
        return s if s.endswith("/") else s + "/"
    return s


class PropertyAllowlist:
    """An immutable set of exact property identifiers the write server may mutate."""

    __slots__ = ("_entries", "_lookup")

    def __init__(self, entries: Iterable[str]) -> None:
        normalized: list[str] = []
        for raw in entries:
            if not isinstance(raw, str) or not raw.strip():
                continue
            normalized.append(normalize_property(raw))
        if not normalized:
            raise EmptyAllowlistError(
                f"no writable Search Console properties configured; set "
                f"{DEFAULT_SITES_ENV} to a comma-separated list of exact "
                f"property identifiers (e.g. 'sc-domain:example.com,"
                f"https://www.example.com/'). Refusing to start with an empty "
                f"allowlist."
            )
        seen: dict[str, None] = {}
        for item in normalized:
            seen.setdefault(item, None)
        self._entries: tuple[str, ...] = tuple(seen)
        self._lookup = frozenset(self._entries)

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "PropertyAllowlist":
        import os

        source = os.environ if env is None else env
        raw = source.get(DEFAULT_SITES_ENV, "")
        return cls(raw.split(","))

    @property
    def entries(self) -> tuple[str, ...]:
        """The normalized, de-duplicated allowed properties, in declared order."""
        return self._entries

    def contains(self, candidate: object) -> bool:
        """Whether ``candidate`` is exactly an allowed property.

        Non-strings are denied rather than coerced.
        """
        if not isinstance(candidate, str):
            return False
        try:
            normalized = normalize_property(candidate)
        except EmptyAllowlistError:
            return False
        return normalized in self._lookup

    def require(self, candidate: object) -> str:
        """Return the normalized property, or raise ``SiteNotAllowedError``.

        The returned value — not the caller's raw string — is what later code
        must use to build request URLs.
        """
        from .errors import SiteNotAllowedError

        if not isinstance(candidate, str) or not candidate.strip():
            raise SiteNotAllowedError("site_url is required")
        try:
            normalized = normalize_property(candidate)
        except EmptyAllowlistError:
            raise SiteNotAllowedError("site_url is empty") from None
        if normalized not in self._lookup:
            raise SiteNotAllowedError(
                "property is not in the configured write allowlist",
                details={"reason": "not_allowlisted"},
            )
        return normalized

    def __contains__(self, candidate: object) -> bool:
        return self.contains(candidate)

    def __len__(self) -> int:
        return len(self._entries)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"PropertyAllowlist({len(self._entries)} entries)"