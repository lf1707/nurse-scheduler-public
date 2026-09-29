"""add optional skill_id to skill_mix_requirements

A skill-mix requirement may now optionally filter by skill: ``count`` nurses
with ``role_id`` who also hold ``skill_id``. Adds the nullable column and
replaces the (rule, role) unique constraint with (rule, role, skill) so the
same role can appear with different skills.
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d8f3b2c1a9e0"
down_revision: str | None = "c7e9a1b2d3f4"


def upgrade() -> None:
    op.add_column(
        "skill_mix_requirements",
        sa.Column(
            "skill_id",
            sa.String(36),
            sa.ForeignKey("skills.id", ondelete="CASCADE"),
            nullable=True,
        ),
    )
    op.drop_constraint("uq_skillmix_role", "skill_mix_requirements", type_="unique")
    op.create_unique_constraint(
        "uq_skillmix_role_skill",
        "skill_mix_requirements",
        ["skill_mix_rule_id", "role_id", "skill_id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_skillmix_role_skill", "skill_mix_requirements", type_="unique"
    )
    op.create_unique_constraint(
        "uq_skillmix_role", "skill_mix_requirements", ["skill_mix_rule_id", "role_id"]
    )
    op.drop_column("skill_mix_requirements", "skill_id")
