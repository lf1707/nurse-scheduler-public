"""add user token version for session revocation

Revision ID: d4a7c1e9b2f6
Revises: c9d4f7a2b6e8
Create Date: 2026-08-27
"""

import sqlalchemy as sa

from alembic import op

revision: str = "d4a7c1e9b2f6"
down_revision: str | None = "c9d4f7a2b6e8"
branch_labels: None = None
depends_on: None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "token_version",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )


def downgrade() -> None:
    op.drop_column("users", "token_version")
