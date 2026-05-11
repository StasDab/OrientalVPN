"""reset subscription_devices: gate fingerprint is IP-only by default

Revision ID: 0006_sub_gate_ip_fp
Revises: 0005_promo_referral
Create Date: 2026-05-11

Старые fingerprint_sha256 считались как SHA256(IP + User-Agent). После перехода на
IP-only хеши не совпадают — без очистки все запросы выглядели бы как «новые устройства»
и упирались в лимит. Один раз очищаем таблицу; пользователи заново займут слоты по IP.

"""

from __future__ import annotations

from alembic import op

revision = "0006_sub_gate_ip_fp"
down_revision = "0005_promo_referral"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DELETE FROM subscription_devices")


def downgrade() -> None:
    pass
