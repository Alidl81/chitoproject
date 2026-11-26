"""Add project archive, assigned deadlines and reviewed deliveries."""

import sqlalchemy as sa

from alembic import op

revision = "0005_project_operations"
down_revision = "0004_conversation_read"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("projects", sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("projects", sa.Column("work_due_at", sa.DateTime(timezone=True), nullable=True))
    op.create_table(
        "project_deliveries",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("submitted_by", sa.Integer, sa.ForeignKey("admin_users.id", ondelete="SET NULL")),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_project_deliveries_project_id", "project_deliveries", ["project_id"])
    op.create_table(
        "project_delivery_files",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("delivery_id", sa.String(36), sa.ForeignKey("project_deliveries.id", ondelete="CASCADE"), nullable=False),
        sa.Column("stored_key", sa.String(64), unique=True, nullable=False),
        sa.Column("original_name", sa.String(200), nullable=False),
        sa.Column("mime_type", sa.String(120), nullable=False),
        sa.Column("size_bytes", sa.BigInteger, nullable=False),
        sa.Column("source_platform", sa.String(16), nullable=False),
        sa.Column("source_file_id", sa.Text, nullable=False),
    )
    op.create_index("ix_project_delivery_files_delivery_id", "project_delivery_files", ["delivery_id"])


def downgrade() -> None:
    op.drop_table("project_delivery_files")
    op.drop_table("project_deliveries")
    op.drop_column("projects", "work_due_at")
    op.drop_column("projects", "archived_at")
