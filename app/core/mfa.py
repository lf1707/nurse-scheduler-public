"""TOTP MFA helpers — secret generation, QR provisioning, and verification."""

from __future__ import annotations

import hashlib
import hmac
import io
import secrets

import pyotp
import qrcode
import qrcode.image.svg

RECOVERY_CODE_LENGTH = 10


def generate_mfa_secret() -> str:
    """Return a base32 TOTP secret (160-bit entropy)."""
    return pyotp.random_base32()


def provisioning_uri(secret: str, email: str, issuer: str) -> str:
    """Return the otpauth:// URI encoded into the enrollment QR code."""
    totp = pyotp.TOTP(secret)
    return totp.provisioning_uri(name=email, issuer_name=issuer)


def qr_code_svg(provisioning_uri_string: str) -> str:
    """Return an inline SVG string for a provisioning URI QR code."""
    qr = qrcode.QRCode()
    qr.add_data(provisioning_uri_string)
    qr.make(fit=True)
    buffer = io.BytesIO()
    image = qr.make_image(image_factory=qrcode.image.svg.SvgPathImage)
    image.save(buffer)
    return buffer.getvalue().decode("utf-8")


def verify_totp(secret: str, code: str) -> bool:
    """True when code is valid within the standard ±1 time step window."""
    if not secret or not code:
        return False
    return pyotp.TOTP(secret).verify(code.strip().replace(" ", ""), valid_window=1)


def generate_recovery_codes(count: int = 5) -> tuple[list[str], str]:
    """Generate recovery codes and return (plaintext_codes, sha256_hash)."""
    codes = [secrets.token_hex(RECOVERY_CODE_LENGTH // 2) for _ in range(count)]
    digest = hashlib.sha256("\n".join(sorted(codes)).encode()).hexdigest()
    return codes, digest


def verify_recovery_codes(codes: list[str], stored_hash: str) -> bool:
    """True when the sorted joined codes match the persisted hash."""
    if not stored_hash:
        return False
    joined = "\n".join(sorted(code.strip().lower() for code in codes if code.strip()))
    return hmac.compare_digest(
        hashlib.sha256(joined.encode()).hexdigest(), stored_hash
    )
