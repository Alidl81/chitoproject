"""Store the last read time per colleague and conversation."""

import sqlalchemy as sa

from alembic import op

revision = "0004_conversation_read"
down_revision = "0003_conversation_controls"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "conversation_read",
        sa.Column("admin_id", sa.Integer(), sa.ForeignKey("admin_users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("conversation_id", sa.String(36), sa.ForeignKey("conversations.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("conversation_read")
