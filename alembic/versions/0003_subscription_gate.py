"""subscription gate: upstream url, gate token, devices

Revision ID: 0003_sg
Revises: 0002_tr_ev
Create Date: 2026-05-07
"""

from alembic import op
import sqlalchemy as sa


revision = "0003_sg"
down_revision = "0002_tr_ev"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "subscriptions",
        sa.Column("upstream_subscription_url", sa.Text(), nullable=True),
    )
    op.add_column(
        "subscriptions",
        sa.Column("sub_gate_token", sa.String(length=36), nullable=True),
    )
    op.add_column(
        "subscriptions",
        sa.Column("max_devices", sa.Integer(), nullable=False, server_default="2"),
    )
    op.create_index(
        "uq_subscriptions_sub_gate_token",
        "subscriptions",
        ["sub_gate_token"],
        unique=True,
    )

    op.create_table(
        "subscription_devices",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("subscription_id", sa.Integer(), nullable=False),
        sa.Column("fingerprint_sha256", sa.String(length=64), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["subscription_id"], ["subscriptions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "subscription_id",
            "fingerprint_sha256",
            name="uq_subscription_device_fp",
        ),
    )
    op.create_index(
        "ix_subscription_devices_subscription_id",
        "subscription_devices",
        ["subscription_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_subscription_devices_subscription_id", table_name="subscription_devices")
    op.drop_table("subscription_devices")
    op.drop_index("uq_subscriptions_sub_gate_token", table_name="subscriptions")
    op.drop_column("subscriptions", "max_devices")
    op.drop_column("subscriptions", "sub_gate_token")
    op.drop_column("subscriptions", "upstream_subscription_url")
