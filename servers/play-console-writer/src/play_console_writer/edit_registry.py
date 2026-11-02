"""Server-side tracking of Play edit sessions, and the shared-edit hazard.

Android Publisher's ``edits`` resource is a two-phase transaction: ``edits.insert``
opens a session, per-resource calls stage changes, and ``edits.commit`` applies
them atomically.

An **edit id is a shared handle**. Whoever commits it applies everything staged
into it — including financial changes (a price, a base plan, an offer) staged by
some other actor that holds the same id. So "our code never sets a price" is not
enough on its own: a commit of a shared edit could carry a price someone else
staged. Three controls close that, and this module implements the first:

1. **Ownership.** Every edit this server opens is recorded here. Every staged
   mutation, ``edits.tracks.update`` and ``edits.commit`` must name an edit id in
   this registry. An edit id the server did not open is refused *before any
   network call*, so it cannot be used to stage into — or commit — a session
   whose other contents it cannot account for.
2. **Configuration gate.** ``edits.commit`` additionally requires
   ``PLAY_WRITER_ENABLE_RELEASE_GATE``; it is off by default.
3. **Disclosure of the residual gap.** The API has no "list what is staged in
   this edit" endpoint, so this server cannot prove an edit it opened stayed
   financial-free. :meth:`EditRegistry.risk_note` and the commit result say so
   explicitly rather than implying a guarantee that does not exist.

The registry is in-memory and per-process on purpose: it is a safety interlock,
not durable storage. A restarted server forgets its edits and refuses to touch
them until the caller opens a new one — the fail-closed direction.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

from .errors import LocalError

__all__ = ["EditRegistry", "TrackedEdit", "EDIT_TTL_SECONDS"]

#: Google discards an uncommitted edit ~7 days after insert. Track a slightly
#: shorter window so this server stops offering an edit before Google does.
EDIT_TTL_SECONDS = 6 * 24 * 60 * 60

#: Upper bound on tracked edits, so a caller cannot grow this map without limit.
MAX_TRACKED_EDITS = 64
MAX_STAGED_PER_EDIT = 256

_DISCLOSURE = (
    "Edit {edit_id} for {package} is a SHARED handle: committing it applies "
    "everything staged into it, including changes staged by any other actor "
    "holding the id. Android Publisher exposes no endpoint that lists what is "
    "staged, so this server cannot verify the edit stayed free of financial "
    "changes. Do not share this edit id, and commit only through this server."
)


@dataclass
class TrackedEdit:
    """One edit session this server opened."""

    package: str
    edit_id: str
    opened_at: float
    expiry_seconds: int = EDIT_TTL_SECONDS
    #: Method ids staged into this edit by this server, for audit.
    staged: list[str] = field(default_factory=list)
    committed: bool = False

    def expired(self, now: float) -> bool:
        return now >= self.opened_at + self.expiry_seconds

    def seconds_remaining(self, now: float) -> int:
        return max(0, int(self.opened_at + self.expiry_seconds - now))


class EditRegistry:
    """Thread-safe record of the edit sessions this server opened."""

    def __init__(self, *, ttl_seconds: int = EDIT_TTL_SECONDS, clock=time.time) -> None:
        self._ttl = ttl_seconds
        self._clock = clock
        self._edits: dict[tuple[str, str], TrackedEdit] = {}
        self._lock = threading.Lock()

    def open_edit(self, package: str, edit_id: str) -> TrackedEdit:
        track = TrackedEdit(
            package=package, edit_id=edit_id, opened_at=self._clock(),
            expiry_seconds=self._ttl,
        )
        with self._lock:
            if len(self._edits) >= MAX_TRACKED_EDITS:
                self._evict_oldest_locked()
            self._edits[(package, edit_id)] = track
        return track

    def require_owned(self, package: str, edit_id: str) -> TrackedEdit:
        """Return the tracked edit, or refuse before any network call.

        Refusing an unknown or expired edit id is the interlock that stops this
        server from staging into — or committing — a session whose other
        contents it cannot account for.
        """
        key = (package, edit_id)
        with self._lock:
            track = self._edits.get(key)
        if track is None:
            raise LocalError(
                f"edit {edit_id!r} for {package!r} was not opened by this server "
                "session; open one with play_edit_insert first. Refusing to touch "
                "an edit this server does not track (it may hold changes staged "
                "by another actor, including financial ones)."
            )
        if track.committed:
            raise LocalError(
                f"edit {edit_id!r} was already committed and is no longer valid; "
                "open a new edit."
            )
        if track.expired(self._clock()):
            with self._lock:
                self._edits.pop(key, None)
            raise LocalError(
                f"edit {edit_id!r} has expired (uncommitted edits are discarded "
                "by Google); open a new edit."
            )
        return track

    def record_staged(self, package: str, edit_id: str, method_id: str) -> None:
        with self._lock:
            track = self._edits.get((package, edit_id))
            if track is not None and len(track.staged) < MAX_STAGED_PER_EDIT:
                track.staged.append(method_id)

    def mark_committed(self, package: str, edit_id: str) -> None:
        """Mark an edit applied. It stays tracked so reuse is refused clearly."""
        with self._lock:
            track = self._edits.get((package, edit_id))
            if track is not None:
                track.committed = True

    def release(self, package: str, edit_id: str) -> None:
        """Forget an edit (after ``edits.delete``)."""
        with self._lock:
            self._edits.pop((package, edit_id), None)

    def risk_note(self, package: str, edit_id: str) -> str:
        """Explicit disclosure of the shared-edit-id hazard."""
        return _DISCLOSURE.format(edit_id=edit_id, package=package)

    def staged_methods(self, package: str, edit_id: str) -> list[str]:
        with self._lock:
            track = self._edits.get((package, edit_id))
            return list(track.staged) if track else []

    def _evict_oldest_locked(self) -> None:
        if not self._edits:
            return
        oldest = min(self._edits, key=lambda key: self._edits[key].opened_at)
        self._edits.pop(oldest, None)