"""Tests for the effect-classification gate, written before the gate existed."""
from __future__ import annotations

import pytest

from logto_ops.gate import (
    DENIED_OPERATIONS,
    GateConfig,
    GateConfigError,
    READ_OPERATIONS,
    WRITE_OPERATIONS,
    assert_servable,
    classify,
    load_gate,
)


def test_delete_operations_are_denied_everywhere():
    # The user explicitly wants M2M creation but no deletes. A delete handler
    # must be unreachable regardless of mode.
    for operation in ("delete_application", "delete_user", "delete_role"):
        with pytest.raises(GateConfigError):
            classify(operation)


def test_unclassified_delete_prefix_is_denied_even_if_not_enumerated():
    # Future handlers get the prefix rule for free; they must not fall through
    # to "unclassified read".
    assert "delete_organization_role" in DENIED_OPERATIONS
    with pytest.raises(GateConfigError):
        classify("delete_something_new")


def test_creating_an_m2m_application_is_a_write():
    assert classify("create_m2m_application") == "write"


def test_creating_an_application_secret_is_a_write():
    assert classify("create_application_secret") == "write"


def test_listing_applications_is_a_read():
    assert classify("list_applications") == "read"


def test_unclassified_operation_is_an_error_not_a_read():
    with pytest.raises(GateConfigError):
        classify("frobnicate_tenant")


def test_every_declared_operation_is_classified():
    for operation in READ_OPERATIONS:
        assert classify(operation) == "read"
    for operation in WRITE_OPERATIONS:
        assert classify(operation) == "write"


def test_no_operation_is_both_a_read_and_a_write():
    assert READ_OPERATIONS.isdisjoint(WRITE_OPERATIONS)
    assert READ_OPERATIONS.isdisjoint(DENIED_OPERATIONS)
    assert WRITE_OPERATIONS.isdisjoint(DENIED_OPERATIONS)


def test_admin_mode_requires_an_explicit_unconfirmed_writes_flag():
    with pytest.raises(GateConfigError):
        GateConfig(mode="admin", allow_unconfirmed_writes=False, allow_deletes=False)
    config = GateConfig(mode="admin", allow_unconfirmed_writes=True, allow_deletes=False)
    assert config.serves_writes is True
    assert config.writes_need_confirmation is False


def test_apply_mode_requires_confirmation_for_writes():
    config = GateConfig(mode="apply", allow_unconfirmed_writes=False, allow_deletes=False)
    assert config.serves_writes is True
    assert config.writes_need_confirmation is True


def test_read_mode_serves_no_writes():
    config = GateConfig(mode="read", allow_unconfirmed_writes=False, allow_deletes=False)
    assert config.serves_writes is False


def test_deletes_cannot_be_switched_on():
    # The flag exists only so a stale deployment value fails loudly instead of
    # being quietly ignored while an operator believes deletes are enabled.
    with pytest.raises(GateConfigError):
        GateConfig(mode="apply", allow_unconfirmed_writes=False, allow_deletes=True)


def test_unknown_mode_is_refused():
    with pytest.raises(GateConfigError):
        GateConfig(mode="write", allow_unconfirmed_writes=True, allow_deletes=False)  # type: ignore[arg-type]


def test_load_gate_defaults_to_apply():
    config = load_gate({})
    assert config.mode == "apply"
    assert config.writes_need_confirmation is True


def test_load_gate_reads_mode_case_insensitively():
    assert load_gate({"LOGTO_MCP_MODE": " READ "}).mode == "read"
    assert load_gate({"LOGTO_MCP_MODE": "Admin", "LOGTO_MCP_ALLOW_UNCONFIRMED_WRITES": "TRUE"}).mode == "admin"


def test_read_mode_refuses_to_serve_a_write_operation():
    gate = load_gate({"LOGTO_MCP_MODE": "read"})
    with pytest.raises(GateConfigError):
        assert_servable(["list_applications", "create_m2m_application"], gate)


def test_apply_mode_serves_the_full_non_delete_surface():
    gate = load_gate({"LOGTO_MCP_MODE": "apply"})
    assert_servable(sorted(READ_OPERATIONS | WRITE_OPERATIONS), gate)
