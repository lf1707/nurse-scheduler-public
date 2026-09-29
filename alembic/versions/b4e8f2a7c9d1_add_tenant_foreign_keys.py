"""Add tenant foreign keys after cleaning orphaned tenant data.

Revision ID: b4e8f2a7c9d1
Revises: a9c7e5b3d1f8
Create Date: 2026-09-10
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "b4e8f2a7c9d1"
down_revision: str | None = "a9c7e5b3d1f8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


TABLES = (
    "assignments",
    "schedules",
    "schedule_requests",
    "nurse_preferences",
    "leaves",
    "contracts",
    "skill_mix_requirements",
    "skill_mix_rules",
    "shift_sequence_role_restrictions",
    "shift_sequence_steps",
    "shift_sequence_rules",
    "shift_templates",
    "day_group_days",
    "day_groups",
    "roles",
    "skills",
)


def upgrade() -> None:
    for table_name in TABLES:
        op.execute(
            f"DELETE FROM {table_name} WHERE NOT EXISTS ("
            "SELECT 1 FROM tenants WHERE tenants.id = "
            f"{table_name}.tenant_id) AND {table_name}.tenant_id IS NOT NULL"
        )
        op.create_foreign_key(
            f"fk_{table_name}_tenant_id",
            table_name,
            "tenants",
            ["tenant_id"],
            ["id"],
            ondelete="CASCADE",
        )


def downgrade() -> None:
    for table_name in reversed(TABLES):
        op.drop_constraint(
            f"fk_{table_name}_tenant_id", table_name, type_="foreignkey"
        )
