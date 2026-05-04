"""trial flag, subscription reminder, events queue

Revision ID: 0002_tr_ev
Revises: 0001_init
Create Date: 2026-05-04
"""

from alembic import op
import sqlalchemy as sa


revision = "0002_tr_ev"
down_revision = "0001_init"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("trial_used", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column(
        "subscriptions",
        sa.Column("reminder_sent_at", sa.DateTime(), nullable=True),
    )
    op.create_table(
        "events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("type", sa.String(length=64), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("retries", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_events_status_type", "events", ["status", "type"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_events_status_type", table_name="events")
    op.drop_table("events")
    op.drop_column("subscriptions", "reminder_sent_at")
    op.drop_column("users", "trial_used")
