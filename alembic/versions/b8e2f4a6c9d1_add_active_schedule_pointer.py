"""add active schedule pointer for effective version

Revision ID: b8e2f4a6c9d1
Revises: a3b8c5d7e2f9
Create Date: 2026-09-16
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "b8e2f4a6c9d1"
down_revision: str | None = "a3b8c5d7e2f9"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    op.add_column(
        "schedule_requests",
        sa.Column(
            "active_schedule_id",
            sa.String(length=36),
            sa.ForeignKey("schedules.id", ondelete="SET NULL", use_alter=True),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_schedule_requests_active_schedule_id",
        "schedule_requests",
        ["active_schedule_id"],
    )
    op.execute("""
        UPDATE schedule_requests AS request
        SET active_schedule_id = latest.id
        FROM schedules AS latest
        WHERE latest.request_id = request.id
          AND latest.version = (
              SELECT max(version)
              FROM schedules
              WHERE request_id = request.id
          )
    """)


def downgrade() -> None:
    op.drop_index(
        "ix_schedule_requests_active_schedule_id",
        table_name="schedule_requests",
    )
    op.drop_column("schedule_requests", "active_schedule_id")
