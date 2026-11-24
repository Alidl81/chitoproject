"""Store the selected discipline on each project.

Revision ID: 0002_project_subject_area
Revises: 0001_initial
"""
from alembic import op
import sqlalchemy as sa

revision = "0002_project_subject_area"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "projects",
        sa.Column("subject_area", sa.String(length=40), nullable=False, server_default="computer"),
    )


def downgrade() -> None:
    op.drop_column("projects", "subject_area")
