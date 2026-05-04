from datetime import datetime, timedelta
import json

from sqlalchemy import Select, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Event, Payment, Subscription, User


async def get_or_create_user(session: AsyncSession, tg_id: int, username: str | None) -> User:
    query: Select[tuple[User]] = select(User).where(User.tg_id == tg_id)
    row = await session.execute(query)
    user = row.scalar_one_or_none()
    if user:
        return user
    user = User(tg_id=tg_id, username=username)
    session.add(user)
    await session.flush()
    return user


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
    now = datetime.utcnow()
    if current and current.ends_at > now:
        current.ends_at = current.ends_at + delta
        current.subscription_url = subscription_url
        current.location_code = location_code
        current.external_user_id = external_user_id
        current.node_api_url = node_api_url
        return current

    sub = Subscription(
        user_id=user_id,
        location_code=location_code,
        subscription_url=subscription_url,
        starts_at=now,
        ends_at=now + delta,
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


async def list_expired_active_subscriptions(session: AsyncSession) -> list[Subscription]:
    now = datetime.utcnow()
    row = await session.execute(
        select(Subscription).where(Subscription.status == "active", Subscription.ends_at <= now)
    )
    return list(row.scalars().all())


async def list_active_subscriptions_for_user(session: AsyncSession, user_id: int) -> list[Subscription]:
    now = datetime.utcnow()
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
    now = datetime.utcnow()
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
        .values(reminder_sent_at=datetime.utcnow())
    )


async def mark_trial_used(session: AsyncSession, user_id: int) -> None:
    await session.execute(update(User).where(User.id == user_id).values(trial_used=True))


async def create_event(session: AsyncSession, type_: str, payload: dict) -> Event:
    now = datetime.utcnow()
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
    values: dict = {"status": status, "updated_at": datetime.utcnow()}
    if retries is not None:
        values["retries"] = retries
    await session.execute(update(Event).where(Event.id == event_id).values(**values))


async def admin_stats_snapshot(session: AsyncSession) -> dict[str, int | float]:
    active_row = await session.execute(
        select(func.count()).select_from(Subscription).where(Subscription.status == "active")
    )
    active_subs = int(active_row.scalar_one() or 0)

    since = datetime.utcnow() - timedelta(days=30)
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
