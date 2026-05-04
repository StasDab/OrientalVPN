"""init schema

Revision ID: 0001_init
Revises:
Create Date: 2026-05-04 00:00:00
"""

from alembic import op
import sqlalchemy as sa


revision = "0001_init"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("tg_id", sa.BigInteger(), nullable=False),
        sa.Column("username", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_users_tg_id", "users", ["tg_id"], unique=True)

    op.create_table(
        "subscriptions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("location_code", sa.String(length=32), nullable=False),
        sa.Column("subscription_url", sa.Text(), nullable=False),
        sa.Column("starts_at", sa.DateTime(), nullable=False),
        sa.Column("ends_at", sa.DateTime(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("external_user_id", sa.String(length=255), nullable=False),
        sa.Column("node_api_url", sa.String(length=500), nullable=False),
    )
    op.create_index("ix_subscriptions_user_id", "subscriptions", ["user_id"], unique=False)
    op.create_index("ix_subscriptions_ends_at", "subscriptions", ["ends_at"], unique=False)
    op.create_index(
        "ix_subscriptions_external_user_id",
        "subscriptions",
        ["external_user_id"],
        unique=False,
    )

    op.create_table(
        "payments",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("tg_charge_id", sa.String(length=255), nullable=False),
        sa.Column("provider_charge_id", sa.String(length=255), nullable=False),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(length=10), nullable=False),
        sa.Column("invoice_payload", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_payments_user_id", "payments", ["user_id"], unique=False)
    op.create_index("ix_payments_tg_charge_id", "payments", ["tg_charge_id"], unique=True)
    op.create_index(
        "ix_payments_provider_charge_id", "payments", ["provider_charge_id"], unique=False
    )
    op.create_index("ix_payments_created_at", "payments", ["created_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_payments_created_at", table_name="payments")
    op.drop_index("ix_payments_provider_charge_id", table_name="payments")
    op.drop_index("ix_payments_tg_charge_id", table_name="payments")
    op.drop_index("ix_payments_user_id", table_name="payments")
    op.drop_table("payments")

    op.drop_index("ix_subscriptions_external_user_id", table_name="subscriptions")
    op.drop_index("ix_subscriptions_ends_at", table_name="subscriptions")
    op.drop_index("ix_subscriptions_user_id", table_name="subscriptions")
    op.drop_table("subscriptions")

    op.drop_index("ix_users_tg_id", table_name="users")
    op.drop_table("users")
