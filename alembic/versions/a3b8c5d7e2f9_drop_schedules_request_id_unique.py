"""drop unique constraint on schedules.request_id for B3-11 multi-version

Revision ID: a3b8c5d7e2f9
Revises: f4b6d9e2c7a8
Create Date: 2026-09-15
"""

from __future__ import annotations

from alembic import op

revision: str = "a3b8c5d7e2f9"
down_revision: str | None = "c7d9e3f5a2b4"


def upgrade() -> None:
    op.drop_constraint(
        "schedules_request_id_key", "schedules", type_="unique"
    )


def downgrade() -> None:
    op.create_unique_constraint(
        "schedules_request_id_key", "schedules", ["request_id"]
    )
