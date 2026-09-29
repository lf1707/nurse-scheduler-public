"""Offline unit tests for password-strength enforcement.

Covers the weak-password check added on top of the existing argon2 hashing.
"""

from __future__ import annotations

from typing import cast

import pytest
from pydantic import ValidationError

from app.core.security import validate_password_strength
from app.schemas import (
    PasswordChange,
    TenantApplicationSetupRequest,
    UserCreate,
    UserUpdate,
)


@pytest.mark.parametrize(
    "password",
    [
        "Passw0rd!",
        "Ab1!def2",
        "Str0ng#Pass",
        "correct horse battery staple",
    ],
)
def test_password_strength_accepts_valid(password: str) -> None:
    validate_password_strength(password)


@pytest.mark.parametrize(
    "password",
    [
        "12345678",  # too short on classes
        "password",  # common
        "123456789",  # common
        "abc123",  # common
        "short",  # too short
        "x" * 129,  # too long
        "aaaaaaaa",  # one character class only
        "11111111",  # one character class only
        "aaaaaaaaaaaaaaaa",  # one character class only
        "ABCD!",  # one character class only
    ],
)
def test_password_strength_rejects_weak(password: str) -> None:
    with pytest.raises(ValueError):
        validate_password_strength(password)


def test_password_strength_requires_two_of_four_classes() -> None:
    with pytest.raises(ValueError):
        validate_password_strength("abcdefghijklmnopqrstuvwxyz")  # lowercase only
    with pytest.raises(ValueError):
        validate_password_strength("ABCDEFGHIJKLMNOPQR")  # uppercase only
    with pytest.raises(ValueError):
        validate_password_strength("123456789012")  # digits only


def test_password_strength_rejects_non_string() -> None:
    with pytest.raises(ValueError):
        validate_password_strength(cast(str, 12345678))


def test_user_create_rejects_weak_password() -> None:
    with pytest.raises(ValidationError):
        UserCreate(
            email="user@example.com",
            password="12345678",
            first_name="A",
            last_name="B",
        )


def test_user_create_accepts_strong_password() -> None:
    user = UserCreate(
        email="user@example.com",
        password="Passw0rd!",
        first_name="A",
        last_name="B",
    )
    assert user.password == "Passw0rd!"


def test_user_update_rejects_weak_password() -> None:
    with pytest.raises(ValidationError):
        UserUpdate(password="password", first_name="A", last_name="B")


def test_user_update_allows_none_password() -> None:
    update = UserUpdate(password=None, first_name="New", last_name="Name")
    assert update.password is None


def test_password_change_rejects_weak_new_password() -> None:
    with pytest.raises(ValidationError):
        PasswordChange(old_password="OldPass1!", new_password="12345678")


def test_tenant_application_setup_rejects_weak_password() -> None:
    with pytest.raises(ValidationError):
        TenantApplicationSetupRequest(
            token="a" * 32,
            new_password="password",
        )
