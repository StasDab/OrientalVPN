"""promo_codes, referrals, promo_redemptions

Revision ID: 0005_promo_referral
Revises: 0004_user_balance
Create Date: 2026-05-08

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005_promo_referral"
down_revision = "0004_user_balance"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "promo_codes",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("discount_percent", sa.Integer(), nullable=True),
        sa.Column("discount_fixed_minor", sa.BigInteger(), nullable=True),
        sa.Column("bonus_days", sa.Integer(), nullable=True),
        sa.Column("max_uses", sa.Integer(), nullable=True),
        sa.Column("max_uses_per_user", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("uses_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code", name="uq_promo_codes_code"),
    )

    op.add_column(
        "users",
        sa.Column(
            "referred_by_user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column(
        "users",
        sa.Column(
            "active_promo_id",
            sa.Integer(),
            sa.ForeignKey("promo_codes.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index("ix_users_referred_by_user_id", "users", ["referred_by_user_id"])

    op.create_table(
        "promo_redemptions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column(
            "promo_id",
            sa.Integer(),
            sa.ForeignKey("promo_codes.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "payment_id",
            sa.Integer(),
            sa.ForeignKey("payments.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_promo_redemptions_promo_user", "promo_redemptions", ["promo_id", "user_id"])
    op.create_index("ix_promo_redemptions_payment_id", "promo_redemptions", ["payment_id"], unique=False)


def downgrade() -> None:
    op.drop_table("promo_redemptions")
    op.drop_index("ix_users_referred_by_user_id", table_name="users")
    op.drop_column("users", "active_promo_id")
    op.drop_column("users", "referred_by_user_id")
    op.drop_table("promo_codes")
