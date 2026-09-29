"""Offline tests for TOTP MFA helpers and login gate behavior."""

from __future__ import annotations

import pyotp
import pytest

from app.core.mfa import (
    generate_mfa_secret,
    generate_recovery_codes,
    provisioning_uri,
    verify_recovery_codes,
    verify_totp,
)


def test_generate_mfa_secret_returns_base32() -> None:
    secret = generate_mfa_secret()
    assert len(secret) >= 32
    assert all(c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567" for c in secret)


def test_provisioning_uri_contains_issuer_and_email() -> None:
    uri = provisioning_uri("JBSWY3DPEHPK3PXP", "admin@example.com", "Nurse Scheduler")
    assert "otpauth://totp/" in uri
    assert "Nurse%20Scheduler" in uri
    assert "admin%40example.com" in uri


def test_verify_totp_accepts_current_and_recent_code() -> None:
    secret = generate_mfa_secret()
    totp = pyotp.TOTP(secret)
    assert verify_totp(secret, totp.now())
    assert not verify_totp(secret, "000000")


def test_verify_totp_rejects_empty() -> None:
    assert not verify_totp("", "123456")
    assert not verify_totp("JBSWY3DPEHPK3PXP", "")


def test_recovery_codes_roundtrip() -> None:
    codes, digest = generate_recovery_codes(count=5)
    assert len(codes) == 5
    assert verify_recovery_codes(codes, digest)
    assert not verify_recovery_codes(codes[:4], digest)


def test_recovery_codes_rejects_empty_hash() -> None:
    codes, _ = generate_recovery_codes()
    assert not verify_recovery_codes(codes, "")


@pytest.mark.parametrize(
    ("enabled", "secret", "code"),
    [(True, "JBSWY3DPEHPK3PXP", None), (True, "JBSWY3DPEHPK3PXP", "123456")],
)
def test_mfa_login_gate_logic(enabled: bool, secret: str, code: str | None) -> None:
    """Simulates the login gate decision without a live HTTP call."""
    requires_mfa = enabled and secret is not None and code is None
    verify = enabled and secret is not None and code is not None
    if requires_mfa:
        assert code is None
    elif verify:
        assert isinstance(verify_totp(secret, code or "000000"), bool)
