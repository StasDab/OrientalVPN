# MyVpn Telegram Bot (MVP Scaffold)

## Что внутри
- ТЗ: `TZ_BOT.md`
- Анализ VPS и локаций: `VPS_LOCATIONS_ANALYSIS.md`
- Инфраструктурный шаблон: `infra/docker-compose.yml`
- Каркас бота: `app/`

## Быстрый старт
1. Создайте `.env` на основе `.env.example`.
2. Поднимите инфраструктуру:
   - `docker compose -f infra/docker-compose.yml up -d`
3. Установите зависимости:
   - `pip install -r requirements.txt`
4. Примените миграции:
   - `alembic upgrade head`
5. Запустите бота:
   - `python -m app.main`

## Что реализовано
- Команды и меню: `/start`, `/buy`, `/trial`, `/my`, `/help`; кнопки «Купить», пробный период, подписки, помощь.
- Тарифы и локации **DE / NL / SE** через inline-кнопки; инвойс Telegram Payments.
- `pre_checkout_query` с проверкой суммы, валюты, payload и плательщика.
- `successful_payment`: запись платежа, выдача через Marzban, идемпотентность; при недоступной ноде или ошибке API — очередь `events` и статус `pending_provision`.
- Пробный период (часы из `TRIAL_HOURS`), один раз на пользователя (`users.trial_used`).
- Фоновые задачи: отключение истёкших подписок, напоминание за `REMINDER_HOURS_BEFORE` часов, повторная выдача из очереди.
- Админ: `/stats`, `/servers`, `/add_days <tg_id> <days>`, `/revoke <tg_id>`, `/broadcast` (подтверждение кнопками), `/cancel`.
- `MarzbanAdapter`: токен, создание/продление пользователя, `set_expire`, отключение.

## Что нужно доделать перед продом
- Добавить CI-проверку миграций Alembic.
- Метрики, алерты, аудит действий админа.
- Уточнить поля payload под вашу версию Marzban API (inbounds-теги `loc-de` и т.д. должны совпадать с панелью).

## Деплой на VPS

### FileZilla
SFTP подходит, чтобы **скопировать** проект, но недостаточно само по себе: нужны зависимости, миграции и автозапуск (см. ниже). Предпочтительно клонировать репозиторий по **SSH** (`git clone`), а не заливать вручную.

### firstVDS «готовые тарифы» (Москва) — что взять под **приложение + БД**
На одном VPS крутятся бот, PostgreSQL и Redis (или только бот, если БД в Docker на том же хосте).

| Тариф | RAM | Вердикт |
|--------|-----|--------|
| **Прогрев** | 1 GB | **Не рекомендуется**: Postgres + бот + фоновые задачи упрутся в память, канал 100 Mbit узковат. |
| **Старт** | 2 GB, 1 Gbit | **Минимальный разумный выбор** для старта и небольшой нагрузки. |
| **Разгон** | 4 GB, 2 vCPU | **Запас по комфорту** если много пользователей/рассылок или хотите меньше риска по OOM. |

VPN-ноды (Marzban) лучше держать **отдельными** VPS с учётом трафика и политики провайдера; тариф под бота не обязан тянуть весь туннель.

### Вариант A: всё в Docker (проще обновлять)
Из **корня** репозитория, с заполненным `.env` (в т.ч. `POSTGRES_*` из `.env.example`):

```bash
docker compose -f infra/docker-compose.stack.yml --env-file .env up -d --build
```

Поднимаются Postgres, Redis и контейнер бота (перед стартом внутри контейнера выполняется `alembic upgrade head`). Порты БД наружу не пробрасываются — только внутренняя сеть compose. Пароль `POSTGRES_PASSWORD` без символов `@ : / ? #` (или используйте URL-encoding в `DATABASE_URL`, если задаёте URL вручную).

### Вариант B: только БД в Docker, бот на хосте + systemd
1. `docker compose -f infra/docker-compose.yml up -d` — Postgres и Redis на `localhost`.
2. Виртуальное окружение, `pip install -r requirements.txt`, `.env` с `DATABASE_URL` на `127.0.0.1`.
3. Скопировать и настроить `infra/systemd/myvpn-bot.service.example` → `/etc/systemd/system/myvpn-bot.service`, затем `systemctl enable --now myvpn-bot`.

Файлы: `Dockerfile`, `infra/docker-compose.stack.yml`, `infra/systemd/myvpn-bot.service.example`.

### Закрепить Marzban с доменом (на сервере панели)
В `/opt/marzban/.env` задайте подписки с вашим доменом и перезапустите панель:

```bash
# в файле:
# XRAY_SUBSCRIPTION_URL_PREFIX=https://panel.example.com
marzban restart
```

### Бот на том же VPS, что Marzban (без отдельного firstVDS)
Имеет смысл, если на машине **≥ ~2 GB RAM** свободно под Postgres+Redis+бот (см. `free -h`). Marzban и Xray уже занимают память — на **1 GB** лучше вынести бота+БД на отдельный VPS (**firstVDS «Старт»** и т.п.).

На сервере с уже работающим nginx/Marzban:

1. Скопируйте проект (например `/opt/myvpn/app`), создайте `.env` из `.env.example`: `PANEL_URL`, учётка API Marzban, `VPN_NODES_JSON`, `BOT_TOKEN`, `ADMIN_IDS`, пароли БД.
2. Поднимите только БД: `docker compose -f infra/docker-compose.yml up -d`
3. Python 3.12+: `python3 -m venv /opt/myvpn/.venv && /opt/myvpn/.venv/bin/pip install -r /opt/myvpn/app/requirements.txt`
4. `cd /opt/myvpn/app && /opt/myvpn/.venv/bin/alembic upgrade head`
5. Unit systemd: скопируйте `infra/systemd/myvpn-bot.service.example` → `/etc/systemd/system/myvpn-bot.service`, поправьте пути на `/opt/myvpn/app` и venv, `EnvironmentFile=/opt/myvpn/app/.env`, затем `systemctl daemon-reload && systemctl enable --now myvpn-bot`

### Нужен ли отдельный firstVDS «Старт»?
**Не обязательно**, если текущий VPS тянет нагрузку и есть **~2 GB+ RAM**. **Имеет смысл**, если хотите изоляцию (падение VPN не роняет биллинг), мало памяти или планируете много пользователей бота.
