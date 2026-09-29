"""Add TOTP MFA fields to users.

Revision ID: f2b8d4a6c1e7
Revises: e5a7b2c9d3f1
Create Date: 2026-09-06
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "f2b8d4a6c1e7"
down_revision: str | None = "e5a7b2c9d3f1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE users ADD COLUMN mfa_secret VARCHAR(64) NULL")
    op.execute(
        "ALTER TABLE users ADD COLUMN mfa_enabled BOOLEAN NOT NULL DEFAULT false"
    )
    op.execute("ALTER TABLE users ADD COLUMN mfa_recovery_hash VARCHAR(255) NULL")


def downgrade() -> None:
    op.execute("ALTER TABLE users DROP COLUMN mfa_recovery_hash")
    op.execute("ALTER TABLE users DROP COLUMN mfa_enabled")
    op.execute("ALTER TABLE users DROP COLUMN mfa_secret")
