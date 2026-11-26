"""Add personal and shared chat archives and broadcast history."""

import sqlalchemy as sa

from alembic import op

revision = "0003_conversation_controls"
down_revision = "0002_project_subject_area"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("conversations", sa.Column("panel_hidden_at", sa.DateTime(timezone=True), nullable=True))
    op.create_table(
        "conversation_hidden",
        sa.Column("admin_id", sa.Integer(), sa.ForeignKey("admin_users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("conversation_id", sa.String(36), sa.ForeignKey("conversations.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("hidden_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "broadcasts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("admin_id", sa.Integer(), sa.ForeignKey("admin_users.id", ondelete="SET NULL")),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("audience", sa.String(16), nullable=False),
        sa.Column("recipient_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("broadcasts")
    op.drop_table("conversation_hidden")
    op.drop_column("conversations", "panel_hidden_at")
