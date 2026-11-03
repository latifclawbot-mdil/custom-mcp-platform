"""Capability report and edit-registry tests."""
from __future__ import annotations

import pytest

from play_console_writer import policy
from play_console_writer.capability import (
    API_LIMITATIONS,
    UNSUPPORTED_CAPABILITIES,
    capability_report,
)
from play_console_writer.edit_registry import EditRegistry
from play_console_writer.errors import LocalError


# --------------------------------------------------------------------------
# Capability report
# --------------------------------------------------------------------------

def test_report_never_claims_all_non_financial_actions():
    report = capability_report(release_gate_enabled=False, artifact_upload_enabled=False)
    assert "not 'all non-financial Play actions'" in report["claim"]
    assert report["implemented_write_methods_count"] == len(policy.ALLOWED_WRITE_METHODS)
    assert report["financial_methods_excluded_count"] == len(policy.FINANCIAL_METHODS)


def test_report_lists_gates():
    off = capability_report(release_gate_enabled=False, artifact_upload_enabled=False)
    assert off["gates"]["release_gate_enabled"] is False
    assert off["gates"]["artifact_upload_enabled"] is False
    on = capability_report(release_gate_enabled=True, artifact_upload_enabled=True)
    assert on["gates"]["release_gate_enabled"] is True


def test_report_carries_unsupported_capabilities_with_reasons():
    report = capability_report(release_gate_enabled=False, artifact_upload_enabled=False)
    caps = {c["capability"] for c in report["unsupported_capabilities"]}
    assert any("publish" in c for c in caps)
    assert any("tester" in c.lower() for c in caps)
    for entry in report["unsupported_capabilities"]:
        assert entry["reason"], entry


def test_report_lists_excluded_methods_with_reasons():
    report = capability_report(release_gate_enabled=False, artifact_upload_enabled=False)
    ids = {e["method_id"] for e in report["excluded_nonfinancial_methods"]}
    assert "appsigning.rotateAppSigningKey" in ids
    assert "appstoreappsreview.uploadapk" in ids


def test_api_limitations_mention_price_and_scope():
    text = " ".join(API_LIMITATIONS)
    assert "scope" in text
    assert "list my apps" in text


def test_no_unsupported_entry_is_empty():
    for entry in UNSUPPORTED_CAPABILITIES:
        assert entry["capability"].strip()
        assert entry["reason"].strip()


# --------------------------------------------------------------------------
# Edit registry
# --------------------------------------------------------------------------

def test_unknown_edit_is_refused():
    with pytest.raises(LocalError, match="was not opened by this server"):
        EditRegistry().require_owned("com.example.app", "edit-x")


def test_expired_edit_is_refused():
    clock = _Clock()
    registry = EditRegistry(ttl_seconds=100, clock=clock)
    registry.open_edit("com.example.app", "e1")
    clock.advance(101)
    with pytest.raises(LocalError, match="expired"):
        registry.require_owned("com.example.app", "e1")


def test_committed_edit_is_refused_thereafter():
    registry = EditRegistry()
    registry.open_edit("com.example.app", "e1")
    registry.mark_committed("com.example.app", "e1")
    with pytest.raises(LocalError, match="already committed"):
        registry.require_owned("com.example.app", "e1")


def test_staged_methods_are_recorded():
    registry = EditRegistry()
    registry.open_edit("com.example.app", "e1")
    registry.record_staged("com.example.app", "e1", "edits.listings.update")
    assert registry.staged_methods("com.example.app", "e1") == ["edits.listings.update"]


def test_risk_note_mentions_the_shared_handle_hazard():
    note = EditRegistry().risk_note("com.example.app", "e1")
    assert "SHARED handle" in note
    assert "no endpoint" in note.lower()


def test_registry_is_bounded_and_refuses_to_drop_a_live_edit():
    from play_console_writer.edit_registry import MAX_TRACKED_EDITS

    registry = EditRegistry(clock=_Clock())
    for index in range(MAX_TRACKED_EDITS):
        registry.open_edit("com.example.app", f"e{index}")

    # At capacity a new edit is refused rather than evicting one that may still
    # be mid-workflow: losing ownership silently would be the unsafe direction.
    with pytest.raises(LocalError, match="maximum"):
        registry.open_edit("com.example.app", "overflow")
    registry.require_owned("com.example.app", "e0")


class _Clock:
    def __init__(self) -> None:
        self._t = 0.0

    def advance(self, seconds: float) -> None:
        self._t += seconds

    def __call__(self) -> float:
        return self._t