from datetime import datetime, timedelta
import json

from sqlalchemy import Select, delete, func, select, update

from app.config import settings
from app.datetime_util import utc_now_naive
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Event, Payment, PromoCode, PromoRedemption, Subscription, User
from app.subscription_urls import build_subscription_urls


def normalize_promo_code(raw: str) -> str:
    return (raw or "").strip().upper()


async def get_or_create_user(
    session: AsyncSession,
    tg_id: int,
    username: str | None,
    *,
    referrer_tg_id: int | None = None,
) -> User:
    query: Select[tuple[User]] = select(User).where(User.tg_id == tg_id)
    row = await session.execute(query)
    user = row.scalar_one_or_none()
    if user:
        return user
    ref_internal: int | None = None
    if referrer_tg_id is not None and referrer_tg_id != tg_id:
        rr = await session.execute(select(User).where(User.tg_id == referrer_tg_id))
        ref_user = rr.scalar_one_or_none()
        if ref_user:
            ref_internal = ref_user.id
    user = User(tg_id=tg_id, username=username, referred_by_user_id=ref_internal)
    session.add(user)
    await session.flush()
    return user


async def get_promo_by_id(session: AsyncSession, promo_id: int) -> PromoCode | None:
    row = await session.execute(select(PromoCode).where(PromoCode.id == promo_id))
    return row.scalar_one_or_none()


async def get_promo_by_code(session: AsyncSession, code: str) -> PromoCode | None:
    c = normalize_promo_code(code)
    if not c:
        return None
    row = await session.execute(select(PromoCode).where(PromoCode.code == c))
    return row.scalar_one_or_none()


def promo_discount_on_amount(plan_amount_minor: int, promo: PromoCode) -> tuple[int, int]:
    """Возвращает (итогова_цена_коп, скидка_коп) для промо percent/fixed."""
    amt = max(0, int(plan_amount_minor))
    if promo.kind == "percent":
        pct = int(promo.discount_percent or 0)
        pct = max(0, min(100, pct))
        discount = amt * pct // 100 if pct else 0
    elif promo.kind == "fixed":
        discount = int(promo.discount_fixed_minor or 0)
        discount = max(0, min(amt, discount))
    else:
        return amt, 0
    effective = max(0, amt - discount)
    return effective, amt - effective


async def count_promo_redemptions_for_user(session: AsyncSession, promo_id: int, user_id: int) -> int:
    row = await session.execute(
        select(func.count())
        .select_from(PromoRedemption)
        .where(PromoRedemption.promo_id == promo_id, PromoRedemption.user_id == user_id)
    )
    return int(row.scalar_one() or 0)


def promo_quota_sync_checks(promo: PromoCode) -> str | None:
    """Проверки лимита/срока без обращений к счётчикам пользователя."""
    now = utc_now_naive()
    if not promo.is_active:
        return "Промокод отключён."
    if promo.expires_at is not None and promo.expires_at <= now:
        return "Срок промокода истёк."
    if promo.max_uses is not None and promo.uses_count >= promo.max_uses:
        return "Лимит активаций исчерпан."
    return None


async def validate_promo_for_apply(session: AsyncSession, promo: PromoCode, user_id: int) -> str | None:
    err = promo_quota_sync_checks(promo)
    if err:
        return err
    used = await count_promo_redemptions_for_user(session, promo.id, user_id)
    if used >= promo.max_uses_per_user:
        return "Вы уже использовали этот промокод максимально допустимое число раз."
    return None


async def validate_discount_promo_for_checkout(session: AsyncSession, promo: PromoCode, user_id: int) -> str | None:
    if promo.kind not in ("percent", "fixed"):
        return "Этот промокод не даёт скидку при оплате."
    return await validate_promo_for_apply(session, promo, user_id)


async def set_user_active_promo(session: AsyncSession, user_id: int, promo_id: int | None) -> None:
    await session.execute(update(User).where(User.id == user_id).values(active_promo_id=promo_id))


async def create_promo_code(
    session: AsyncSession,
    *,
    code: str,
    kind: str,
    discount_percent: int | None = None,
    discount_fixed_minor: int | None = None,
    bonus_days: int | None = None,
    max_uses: int | None = None,
    max_uses_per_user: int = 1,
    expires_at: datetime | None = None,
) -> PromoCode:
    c = normalize_promo_code(code)
    row = PromoCode(
        code=c,
        kind=kind,
        discount_percent=discount_percent,
        discount_fixed_minor=discount_fixed_minor,
        bonus_days=bonus_days,
        max_uses=max_uses,
        max_uses_per_user=max_uses_per_user,
        expires_at=expires_at,
    )
    session.add(row)
    await session.flush()
    return row


async def deactivate_promo(session: AsyncSession, code: str) -> bool:
    c = normalize_promo_code(code)
    res = await session.execute(update(PromoCode).where(PromoCode.code == c).values(is_active=False))
    return (res.rowcount or 0) > 0  # type: ignore[union-attr]


async def list_promo_codes_admin(session: AsyncSession, limit: int = 30) -> list[PromoCode]:
    row = await session.execute(select(PromoCode).order_by(PromoCode.id.desc()).limit(limit))
    return list(row.scalars().all())


async def count_users_with_referrer(session: AsyncSession) -> int:
    row = await session.execute(
        select(func.count()).select_from(User).where(User.referred_by_user_id.is_not(None))
    )
    return int(row.scalar_one() or 0)


async def count_active_subscriptions_by_node_url(session: AsyncSession) -> dict[str, int]:
    stmt = (
        select(Subscription.node_api_url, func.count())
        .where(Subscription.status == "active")
        .group_by(Subscription.node_api_url)
    )
    rows = await session.execute(stmt)
    return {str(u or ""): int(n or 0) for u, n in rows.all()}


async def count_referrals_registered(session: AsyncSession, referrer_internal_id: int) -> int:
    row = await session.execute(
        select(func.count()).select_from(User).where(User.referred_by_user_id == referrer_internal_id)
    )
    return int(row.scalar_one() or 0)


async def count_referrals_with_tariff_payment(session: AsyncSession, referrer_internal_id: int) -> int:
    """
    Приглашённые пользователи, у которых есть хотя бы одна успешная оплата тарифного инвойса
    (не пополнение баланса по payload topup:).
    """
    row = await session.execute(
        select(func.count(func.distinct(Payment.user_id)))
        .select_from(Payment)
        .join(User, User.id == Payment.user_id)
        .where(
            User.referred_by_user_id == referrer_internal_id,
            Payment.status == "paid",
            ~Payment.invoice_payload.startswith("topup:"),
        )
    )
    return int(row.scalar_one() or 0)


async def maybe_finalize_plan_promo(
    session: AsyncSession,
    *,
    promo_id: int | None,
    user_internal_id: int,
    payment_id: int,
) -> None:
    if not promo_id:
        return
    existing = await session.execute(
        select(PromoRedemption.id).where(PromoRedemption.payment_id == payment_id)
    )
    if existing.scalar_one_or_none() is not None:
        return
    promo = await get_promo_by_id(session, promo_id)
    if not promo or promo.kind not in ("percent", "fixed"):
        return
    session.add(PromoRedemption(promo_id=promo_id, user_id=user_internal_id, payment_id=payment_id))
    await session.execute(update(PromoCode).where(PromoCode.id == promo_id).values(uses_count=PromoCode.uses_count + 1))
    await session.execute(
        update(User)
        .where(User.id == user_internal_id, User.active_promo_id == promo_id)
        .values(active_promo_id=None)
    )


async def record_free_days_promo_redemption(session: AsyncSession, promo_id: int, user_id: int) -> None:
    session.add(PromoRedemption(promo_id=promo_id, user_id=user_id, payment_id=None))
    await session.execute(update(PromoCode).where(PromoCode.id == promo_id).values(uses_count=PromoCode.uses_count + 1))


async def credit_referrer_from_purchase(session: AsyncSession, buyer_internal_id: int, purchase_minor: int) -> None:
    if purchase_minor <= 0:
        return
    bps = int(settings.referral_commission_bps)
    bonus = purchase_minor * bps // 10000
    if bonus <= 0:
        return
    row = await session.execute(select(User).where(User.id == buyer_internal_id))
    buyer = row.scalar_one_or_none()
    if not buyer or not buyer.referred_by_user_id:
        return
    ref_id = buyer.referred_by_user_id
    rr = await session.execute(select(User).where(User.id == ref_id))
    ref_u = rr.scalar_one_or_none()
    if not ref_u or ref_u.id == buyer.id:
        return
    await add_user_balance_minor(session, ref_u.id, bonus)


async def get_user_by_tg_id(session: AsyncSession, tg_id: int) -> User | None:
    row = await session.execute(select(User).where(User.tg_id == tg_id))
    return row.scalar_one_or_none()


async def payment_exists(session: AsyncSession, tg_charge_id: str) -> bool:
    row = await session.execute(select(Payment.id).where(Payment.tg_charge_id == tg_charge_id))
    return row.scalar_one_or_none() is not None


async def get_payment_by_tg_charge(session: AsyncSession, tg_charge_id: str) -> Payment | None:
    row = await session.execute(select(Payment).where(Payment.tg_charge_id == tg_charge_id))
    return row.scalar_one_or_none()


async def create_payment(
    session: AsyncSession,
    user_id: int,
    tg_charge_id: str,
    provider_charge_id: str,
    amount_minor: int,
    currency: str,
    invoice_payload: str,
    *,
    status: str = "paid",
) -> Payment:
    payment = Payment(
        user_id=user_id,
        tg_charge_id=tg_charge_id,
        provider_charge_id=provider_charge_id,
        amount_minor=amount_minor,
        currency=currency,
        invoice_payload=invoice_payload,
        status=status,
    )
    session.add(payment)
    await session.flush()
    return payment


async def update_payment_status(session: AsyncSession, payment_id: int, status: str) -> None:
    await session.execute(update(Payment).where(Payment.id == payment_id).values(status=status))


async def list_pending_yookassa_payments(session: AsyncSession, limit: int = 50) -> list[Payment]:
    row = await session.execute(
        select(Payment)
        .where(Payment.status == "pending_yookassa")
        .order_by(Payment.id.desc())
        .limit(limit)
    )
    return list(row.scalars().all())


async def create_or_extend_subscription(
    session: AsyncSession,
    user_id: int,
    external_user_id: str,
    subscription_url: str,
    location_code: str,
    node_api_url: str,
    duration_days: int | None = None,
    *,
    duration_hours: int | None = None,
    panel_ends_at: datetime | None = None,
) -> Subscription:
    if duration_hours is not None:
        delta = timedelta(hours=duration_hours)
    else:
        delta = timedelta(days=duration_days or 30)

    row = await session.execute(
        select(Subscription)
        .where(Subscription.user_id == user_id, Subscription.status == "active")
        .order_by(Subscription.ends_at.desc())
    )
    current = row.scalars().first()
    now = utc_now_naive()

    public_url, upstream, gate_token, max_d = build_subscription_urls(
        subscription_url,
        existing_gate_token=current.sub_gate_token if current else None,
    )

    if current and current.ends_at > now:
        current.ends_at = current.ends_at + delta
        current.subscription_url = public_url
        current.upstream_subscription_url = upstream
        current.sub_gate_token = gate_token
        current.max_devices = max_d
        current.location_code = location_code
        current.external_user_id = external_user_id
        current.node_api_url = node_api_url
        return current

    new_end = panel_ends_at if panel_ends_at is not None else (now + delta)
    if new_end <= now:
        new_end = now + delta

    sub = Subscription(
        user_id=user_id,
        location_code=location_code,
        subscription_url=public_url,
        upstream_subscription_url=upstream,
        sub_gate_token=gate_token,
        max_devices=max_d,
        starts_at=now,
        ends_at=new_end,
        status="active",
        external_user_id=external_user_id,
        node_api_url=node_api_url,
    )
    session.add(sub)
    await session.flush()
    return sub


async def extend_subscription_days(
    session: AsyncSession,
    user_id: int,
    extra_days: int,
) -> Subscription | None:
    row = await session.execute(
        select(Subscription)
        .where(Subscription.user_id == user_id, Subscription.status == "active")
        .order_by(Subscription.ends_at.desc())
    )
    sub = row.scalars().first()
    if not sub:
        return None
    sub.ends_at = sub.ends_at + timedelta(days=extra_days)
    return sub


async def revoke_user_subscriptions(session: AsyncSession, user_id: int) -> list[Subscription]:
    row = await session.execute(
        select(Subscription).where(Subscription.user_id == user_id, Subscription.status == "active")
    )
    subs = list(row.scalars().all())
    for s in subs:
        s.status = "revoked"
    return subs


async def list_all_subscription_rows_for_user(session: AsyncSession, user_id: int) -> list[Subscription]:
    row = await session.execute(select(Subscription).where(Subscription.user_id == user_id))
    return list(row.scalars().all())


async def delete_subscriptions_for_user(session: AsyncSession, user_id: int) -> int:
    r = await session.execute(delete(Subscription).where(Subscription.user_id == user_id))
    return int(r.rowcount or 0)  # type: ignore[arg-type]


async def delete_all_subscription_rows(session: AsyncSession) -> int:
    r = await session.execute(delete(Subscription))
    return int(r.rowcount or 0)  # type: ignore[arg-type]


async def hard_delete_promo_by_code(session: AsyncSession, code: str) -> bool:
    c = normalize_promo_code(code)
    if not c:
        return False
    res = await session.execute(delete(PromoCode).where(PromoCode.code == c))
    return int(res.rowcount or 0) > 0  # type: ignore[arg-type]


async def delete_all_promo_codes(session: AsyncSession) -> int:
    res = await session.execute(delete(PromoCode))
    return int(res.rowcount or 0)  # type: ignore[arg-type]


async def list_expired_active_subscriptions(session: AsyncSession) -> list[Subscription]:
    now = utc_now_naive()
    row = await session.execute(
        select(Subscription).where(Subscription.status == "active", Subscription.ends_at <= now)
    )
    return list(row.scalars().all())


async def list_active_subscriptions_for_user(session: AsyncSession, user_id: int) -> list[Subscription]:
    now = utc_now_naive()
    row = await session.execute(
        select(Subscription)
        .where(Subscription.user_id == user_id, Subscription.status == "active")
        .order_by(Subscription.ends_at.desc())
    )
    return [s for s in row.scalars().all() if s.ends_at > now]


async def list_subscriptions_needing_reminder(
    session: AsyncSession,
    *,
    hours_before: int,
    window_minutes: int = 30,
) -> list[Subscription]:
    now = utc_now_naive()
    target = now + timedelta(hours=hours_before)
    low = target - timedelta(minutes=window_minutes)
    high = target + timedelta(minutes=window_minutes)
    row = await session.execute(
        select(Subscription).where(
            Subscription.status == "active",
            Subscription.ends_at > now,
            Subscription.ends_at >= low,
            Subscription.ends_at <= high,
            Subscription.reminder_sent_at.is_(None),
        )
    )
    return list(row.scalars().all())


async def mark_reminder_sent(session: AsyncSession, subscription_id: int) -> None:
    await session.execute(
        update(Subscription)
        .where(Subscription.id == subscription_id)
        .values(reminder_sent_at=utc_now_naive())
    )


async def mark_trial_used(session: AsyncSession, user_id: int) -> None:
    await session.execute(update(User).where(User.id == user_id).values(trial_used=True))


async def try_deduct_user_balance_minor(session: AsyncSession, user_id: int, amount: int) -> bool:
    """Атомарное списание. amount в копейках. Возвращает False, если не хватило баланса."""
    if amount <= 0:
        return True
    res = await session.execute(
        update(User)
        .where(User.id == user_id, User.balance_minor >= amount)
        .values(balance_minor=User.balance_minor - amount)
    )
    return res.rowcount == 1  # type: ignore[union-attr]


async def add_user_balance_minor(session: AsyncSession, user_id: int, amount: int) -> None:
    if amount == 0:
        return
    await session.execute(
        update(User).where(User.id == user_id).values(balance_minor=User.balance_minor + amount)
    )


async def create_event(session: AsyncSession, type_: str, payload: dict) -> Event:
    now = utc_now_naive()
    ev = Event(
        type=type_,
        payload=json.dumps(payload, ensure_ascii=False),
        status="pending",
        retries=0,
        created_at=now,
        updated_at=now,
    )
    session.add(ev)
    await session.flush()
    return ev


async def list_pending_provision_events(session: AsyncSession, limit: int = 25) -> list[Event]:
    row = await session.execute(
        select(Event)
        .where(Event.status == "pending", Event.type == "provision_payment")
        .order_by(Event.id.asc())
        .limit(limit)
    )
    return list(row.scalars().all())


async def touch_event(session: AsyncSession, event_id: int, *, status: str, retries: int | None = None) -> None:
    values: dict = {"status": status, "updated_at": utc_now_naive()}
    if retries is not None:
        values["retries"] = retries
    await session.execute(update(Event).where(Event.id == event_id).values(**values))


async def admin_stats_snapshot(session: AsyncSession) -> dict[str, int | float]:
    active_row = await session.execute(
        select(func.count()).select_from(Subscription).where(Subscription.status == "active")
    )
    active_subs = int(active_row.scalar_one() or 0)

    since = utc_now_naive() - timedelta(days=30)
    pay_row = await session.execute(
        select(func.count(), func.coalesce(func.sum(Payment.amount_minor), 0))
        .select_from(Payment)
        .where(Payment.created_at >= since, Payment.status == "paid")
    )
    cnt, total_minor = pay_row.one()
    payments_30d = int(cnt or 0)
    volume_minor = int(total_minor or 0)

    pending_row = await session.execute(
        select(func.count()).select_from(Payment).where(Payment.status == "pending_provision")
    )
    pending_pay = int(pending_row.scalar_one() or 0)

    return {
        "active_subscriptions": active_subs,
        "payments_30d_count": payments_30d,
        "payments_30d_volume_minor": volume_minor,
        "pending_provision_payments": pending_pay,
    }


async def list_all_user_tg_ids(session: AsyncSession) -> list[int]:
    row = await session.execute(select(User.tg_id))
    return [int(r[0]) for r in row.all()]
