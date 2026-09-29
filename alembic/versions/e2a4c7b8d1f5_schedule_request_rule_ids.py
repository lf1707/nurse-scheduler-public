"""add optional rule-id filters to schedule_requests

A schedule request may now restrict which skill-mix and/or shift-sequence
rules are applied (null = all active rules). Adds two nullable JSON columns.
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "e2a4c7b8d1f5"
down_revision: str | None = "d8f3b2c1a9e0"


def upgrade() -> None:
    op.add_column(
        "schedule_requests",
        sa.Column("skill_mix_rule_ids", sa.JSON(), nullable=True),
    )
    op.add_column(
        "schedule_requests",
        sa.Column("shift_sequence_rule_ids", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("schedule_requests", "shift_sequence_rule_ids")
    op.drop_column("schedule_requests", "skill_mix_rule_ids")
