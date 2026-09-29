"""add per-tenant daily schedule request sequence

Revision ID: f4b6d9e2c7a8
Revises: e2a4c7b8d1f5
Create Date: 2026-08-25
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "f4b6d9e2c7a8"
down_revision: str | None = "e2a4c7b8d1f5"


def upgrade() -> None:
    op.add_column(
        "schedule_requests",
        sa.Column("request_date", sa.Date(), nullable=True),
    )
    op.add_column(
        "schedule_requests",
        sa.Column("daily_sequence", sa.Integer(), nullable=True),
    )
    # Backfill must see every tenant's history even though the application
    # role is RLS-enforced; the transaction-local super flag is removed on
    # transaction end.
    op.execute("SET LOCAL app.is_super = '1'")
    op.execute(
        """
        UPDATE schedule_requests AS request
        SET request_date = (request.created_at AT TIME ZONE 'UTC')::date,
            daily_sequence = numbered.sequence
        FROM (
            SELECT id,
                   row_number() OVER (
                       PARTITION BY (created_at AT TIME ZONE 'UTC')::date
                       ORDER BY created_at, id
                   ) AS sequence
            FROM schedule_requests
        ) AS numbered
        WHERE request.id = numbered.id
        """
    )
    op.alter_column("schedule_requests", "request_date", nullable=False)
    op.alter_column("schedule_requests", "daily_sequence", nullable=False)
    op.create_unique_constraint(
        "uq_schedule_requests_date_seq",
        "schedule_requests",
        ["request_date", "daily_sequence"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_schedule_requests_date_seq",
        "schedule_requests",
        type_="unique",
    )
    op.drop_column("schedule_requests", "daily_sequence")
    op.drop_column("schedule_requests", "request_date")
