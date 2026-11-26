"""Store the checkout channel and verified gateway transaction."""

import sqlalchemy as sa

from alembic import op

revision = "0007_website_checkout"
down_revision = "0006_customer_intake_extras"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("payment_stages", sa.Column("website_checkout", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("payment_stages", sa.Column("gateway_authority", sa.String(36)))
    op.add_column("payment_stages", sa.Column("gateway_reference", sa.String(36)))
    op.create_index("ix_payment_stages_gateway_authority", "payment_stages", ["gateway_authority"], unique=True)
    op.create_index("ix_payment_stages_gateway_reference", "payment_stages", ["gateway_reference"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_payment_stages_gateway_reference", table_name="payment_stages")
    op.drop_index("ix_payment_stages_gateway_authority", table_name="payment_stages")
    op.drop_column("payment_stages", "gateway_reference")
    op.drop_column("payment_stages", "gateway_authority")
    op.drop_column("payment_stages", "website_checkout")
