"""allow skill mix requirements to apply to any role

Revision ID: a9c4e7f1b3d8
Revises: c2a5e7b9d3f1
Create Date: 2026-09-18
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "a9c4e7f1b3d8"
down_revision: str | None = "c2a5e7b9d3f1"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    op.alter_column(
        "skill_mix_requirements",
        "role_id",
        existing_type=sa.String(36),
        nullable=True,
    )
    op.create_index(
        "uq_skillmix_any_role_skill",
        "skill_mix_requirements",
        ["skill_mix_rule_id", sa.text("COALESCE(skill_id, ''::text)")],
        unique=True,
        postgresql_where=sa.text("role_id IS NULL"),
    )


def downgrade() -> None:
    op.execute(
        "DELETE FROM skill_mix_requirements WHERE role_id IS NULL"
    )
    op.drop_index(
        "uq_skillmix_any_role_skill", table_name="skill_mix_requirements"
    )
    op.alter_column(
        "skill_mix_requirements",
        "role_id",
        existing_type=sa.String(36),
        nullable=False,
    )
