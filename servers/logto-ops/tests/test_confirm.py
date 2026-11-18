"""Tests for the confirmation gate, written before the verifier existed."""
from __future__ import annotations

import pytest

from logto_ops.confirm import (
    ConfirmationError,
    ConfirmationVerifier,
    ENV_CONFIRM_SECRET,
    ENV_CONFIRM_SECRET_FILE,
    canonical_digest,
    load_confirm_secret,
    redacted_arguments,
)

SECRET = b"x" * 32
OPERATION = "create_m2m_application"
ARGS = {"name": "vlobel-scheduler", "type": "MachineToMachine", "description": "prod job runner"}


def verifier(ttl: int = 900) -> ConfirmationVerifier:
    return ConfirmationVerifier(secret=SECRET, ttl_seconds=ttl)


def test_a_write_without_a_token_is_refused():
    with pytest.raises(ConfirmationError) as excinfo:
        verifier().verify(OPERATION, ARGS, "")
    assert excinfo.value.code == "confirmation_required"


def test_mint_then_verify_authorises_the_same_call():
    v = verifier()
    token = v.mint(OPERATION, ARGS)
    v.verify(OPERATION, ARGS, token)  # must not raise


def test_changing_a_non_secret_argument_invalidates_the_token():
    v = verifier()
    token = v.mint(OPERATION, ARGS)
    changed = {**ARGS, "name": "somebody-elses-app"}
    with pytest.raises(ConfirmationError) as excinfo:
        v.verify(OPERATION, changed, token)
    assert excinfo.value.code == "confirmation_mismatch"


def test_changing_a_secret_argument_also_invalidates_the_token():
    # The secret is redacted from the plan shown to the caller, but it must
    # still be bound by the token: otherwise approval would cover "some
    # password" rather than the password that will be set.
    v = verifier()
    args = {"user_id": "abc", "password": "first-password"}
    token = v.mint("update_user_password", args)
    with pytest.raises(ConfirmationError):
        v.verify("update_user_password", {**args, "password": "other-password"}, token)


def test_a_token_for_one_operation_cannot_authorise_another():
    v = verifier()
    args = {"name": "same-arguments"}
    token = v.mint("create_role", args)
    with pytest.raises(ConfirmationError) as excinfo:
        v.verify("create_organization", args, token)
    assert excinfo.value.code == "confirmation_mismatch"


def test_a_token_signed_with_another_secret_is_rejected():
    token = ConfirmationVerifier(secret=b"y" * 32).mint(OPERATION, ARGS)
    with pytest.raises(ConfirmationError) as excinfo:
        verifier().verify(OPERATION, ARGS, token)
    assert excinfo.value.code == "confirmation_mismatch"


def test_an_expired_token_is_rejected():
    v = verifier(ttl=60)
    token = v.mint(OPERATION, ARGS)
    issued_at = int(token.split(".", 1)[0])
    with pytest.raises(ConfirmationError) as excinfo:
        v.verify(OPERATION, ARGS, token, now=issued_at + 61)
    assert excinfo.value.code == "confirmation_expired"


def test_a_token_is_still_valid_just_before_expiry():
    v = verifier(ttl=60)
    token = v.mint(OPERATION, ARGS)
    issued_at = int(token.split(".", 1)[0])
    v.verify(OPERATION, ARGS, token, now=issued_at + 59)


def test_a_token_claiming_to_be_from_the_future_is_rejected():
    v = verifier()
    token = v.mint(OPERATION, ARGS)
    issued_at = int(token.split(".", 1)[0])
    with pytest.raises(ConfirmationError) as excinfo:
        v.verify(OPERATION, ARGS, token, now=issued_at - 120)
    assert excinfo.value.code == "confirmation_clock_skew"


@pytest.mark.parametrize("token", ["nonsense", "123.", ".abc", "abc.def", "  "])
def test_malformed_tokens_are_rejected(token):
    with pytest.raises(ConfirmationError) as excinfo:
        verifier().verify(OPERATION, ARGS, token)
    assert excinfo.value.code in ("confirmation_malformed", "confirmation_required")


def test_digest_is_stable_across_key_order():
    a = canonical_digest(OPERATION, {"a": 1, "b": 2})
    b = canonical_digest(OPERATION, {"b": 2, "a": 1})
    assert a == b


def test_digest_ignores_the_confirmation_token_itself():
    token = verifier().mint(OPERATION, ARGS)
    assert canonical_digest(OPERATION, ARGS) == canonical_digest(
        OPERATION, {**ARGS, "confirmation_token": token}
    )


def test_redacted_arguments_hide_secrets_but_keep_the_shape():
    shown = redacted_arguments({**ARGS, "password": "hunter2"})
    assert shown["password"] == "<redacted>"
    assert shown["name"] == "vlobel-scheduler"
    assert "confirmation_token" not in shown


def test_redacted_arguments_keep_an_absent_secret_absent():
    shown = redacted_arguments({"user_id": "abc", "password": ""})
    assert shown["password"] == ""


def test_load_confirm_secret_fails_closed_when_unset():
    with pytest.raises(ConfirmationError) as excinfo:
        load_confirm_secret({})
    assert excinfo.value.code == "confirm_secret_missing"


def test_load_confirm_secret_rejects_a_short_key():
    with pytest.raises(ConfirmationError) as excinfo:
        load_confirm_secret({ENV_CONFIRM_SECRET: "short"})
    assert excinfo.value.code == "confirm_secret_too_short"


def test_load_confirm_secret_accepts_a_generated_key():
    from logto_ops.confirm import generate_confirm_secret

    key = generate_confirm_secret()
    assert load_confirm_secret({ENV_CONFIRM_SECRET: key}) == key.encode("utf-8")


def write_key(tmp_path, value="k" * 40, mode=0o600, name="confirm.key"):
    path = tmp_path / name
    path.write_text(value, encoding="utf-8")
    path.chmod(mode)
    return str(path)


def test_secret_can_come_from_a_file(tmp_path):
    path = write_key(tmp_path)
    assert load_confirm_secret({ENV_CONFIRM_SECRET_FILE: path}) == (b"k" * 40)


def test_the_file_wins_over_the_environment_value(tmp_path):
    path = write_key(tmp_path)
    assert load_confirm_secret(
        {ENV_CONFIRM_SECRET_FILE: path, ENV_CONFIRM_SECRET: "e" * 40}
    ) == (b"k" * 40)


def test_a_loose_key_file_is_refused_rather_than_falling_back(tmp_path):
    path = write_key(tmp_path, mode=0o644)
    with pytest.raises(ConfirmationError) as excinfo:
        load_confirm_secret({ENV_CONFIRM_SECRET_FILE: path, ENV_CONFIRM_SECRET: "e" * 40})
    assert excinfo.value.code == "confirm_secret_permissions"


def test_a_missing_key_file_is_refused_rather_than_falling_back(tmp_path):
    with pytest.raises(ConfirmationError) as excinfo:
        load_confirm_secret(
            {
                ENV_CONFIRM_SECRET_FILE: str(tmp_path / "absent"),
                ENV_CONFIRM_SECRET: "e" * 40,
            }
        )
    assert excinfo.value.code == "confirm_secret_unreadable"


def test_an_empty_key_file_is_refused(tmp_path):
    path = write_key(tmp_path, value="   ")
    with pytest.raises(ConfirmationError) as excinfo:
        load_confirm_secret({ENV_CONFIRM_SECRET_FILE: path})
    assert excinfo.value.code == "confirm_secret_empty"


def test_a_multi_line_key_file_is_refused(tmp_path):
    path = write_key(tmp_path, value="one\ntwo")
    with pytest.raises(ConfirmationError) as excinfo:
        load_confirm_secret({ENV_CONFIRM_SECRET_FILE: path})
    assert excinfo.value.code == "confirm_secret_malformed"


def test_a_short_key_from_a_file_is_refused(tmp_path):
    path = write_key(tmp_path, value="short")
    with pytest.raises(ConfirmationError) as excinfo:
        load_confirm_secret({ENV_CONFIRM_SECRET_FILE: path})
    assert excinfo.value.code == "confirm_secret_too_short"
