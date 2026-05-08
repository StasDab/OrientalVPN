from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class PromoCode(Base):
    __tablename__ = "promo_codes"
    __table_args__ = (UniqueConstraint("code", name="uq_promo_codes_code"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    discount_percent: Mapped[int | None] = mapped_column(Integer, nullable=True)
    discount_fixed_minor: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    bonus_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_uses: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_uses_per_user: Mapped[int] = mapped_column(Integer, default=1)
    uses_count: Mapped[int] = mapped_column(Integer, default=0)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class PromoRedemption(Base):
    __tablename__ = "promo_redemptions"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    promo_id: Mapped[int] = mapped_column(ForeignKey("promo_codes.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    payment_id: Mapped[int | None] = mapped_column(
        ForeignKey("payments.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    tg_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    trial_used: Mapped[bool] = mapped_column(Boolean, default=False)
    # Баланс в копейках (как Telegram total_amount / payments.amount_minor).
    balance_minor: Mapped[int] = mapped_column(BigInteger, default=0)
    referred_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    active_promo_id: Mapped[int | None] = mapped_column(
        ForeignKey("promo_codes.id", ondelete="SET NULL"),
        nullable=True,
    )


class Subscription(Base):
    __tablename__ = "subscriptions"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    location_code: Mapped[str] = mapped_column(String(32))
    # Публичная ссылка (шлюз /sub/{token} или прямой Marzban).
    subscription_url: Mapped[str] = mapped_column(Text)
    # Реальная ссылка Marzban /sub/... — только если включён шлюз.
    upstream_subscription_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    sub_gate_token: Mapped[str | None] = mapped_column(String(36), nullable=True, unique=True)
    max_devices: Mapped[int] = mapped_column(Integer, default=2)
    starts_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    ends_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    status: Mapped[str] = mapped_column(String(32), default="active")
    external_user_id: Mapped[str] = mapped_column(String(255), index=True)
    node_api_url: Mapped[str] = mapped_column(String(500))
    reminder_sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class SubscriptionDevice(Base):
    __tablename__ = "subscription_devices"
    __table_args__ = (
        UniqueConstraint("subscription_id", "fingerprint_sha256", name="uq_subscription_device_fp"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    subscription_id: Mapped[int] = mapped_column(ForeignKey("subscriptions.id", ondelete="CASCADE"), index=True)
    fingerprint_sha256: Mapped[str] = mapped_column(String(64))
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Payment(Base):
    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    tg_charge_id: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    provider_charge_id: Mapped[str] = mapped_column(String(255), index=True)
    amount_minor: Mapped[int] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(String(10), default="RUB")
    invoice_payload: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(32), default="paid")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)


class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    type: Mapped[str] = mapped_column(String(64), index=True)
    payload: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    retries: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
