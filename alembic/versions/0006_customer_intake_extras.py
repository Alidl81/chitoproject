"""Store optional deliverables and versioned manual payment stages."""

import sqlalchemy as sa

from alembic import op

revision = "0006_customer_intake_extras"
down_revision = "0005_project_operations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("projects", sa.Column("requested_extras", sa.JSON(), nullable=False, server_default="[]"))
    op.add_column("projects", sa.Column("staged_payment", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.create_table(
        "payment_stages",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("delivery_id", sa.String(36), sa.ForeignKey("project_deliveries.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("stage_count", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(140), nullable=False),
        sa.Column("amount_toman", sa.BigInteger(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("revision_requests", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("preview_key", sa.String(32), nullable=False),
        sa.Column("archive_key", sa.String(32), nullable=False),
        sa.Column("deadline", sa.DateTime(timezone=True)),
        sa.Column("receipt_key", sa.String(32)),
        sa.Column("bank_reference", sa.String(100), unique=True),
        sa.Column("verified_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("project_id", "number", name="uq_payment_stage_number"),
    )
    op.create_index("ix_payment_stages_project_id", "payment_stages", ["project_id"])


def downgrade() -> None:
    op.drop_table("payment_stages")
    op.drop_column("projects", "staged_payment")
    op.drop_column("projects", "requested_extras")
