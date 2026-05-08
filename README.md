# OrientalVPN — Telegram-бот (Marzban)

Бот выдаёт подписку VPN через Marzban API: пробный период, оплата (Telegram Payments или ЮKassa), **внутренний баланс** (пополнение произвольной суммой + списание при покупке тарифа), шлюз `/sub/{token}` с лимитом устройств.

## Структура проекта

| Путь | Назначение |
|------|------------|
| `app/main.py` | Точка входа: polling, роутеры, фоновые задачи, HTTP шлюз подписки |
| `app/config.py` | Настройки из `.env` (Pydantic Settings) |
| `app/db/` | SQLAlchemy-модели, репозитории, сессия |
| `app/handlers/` | Обработчики: пользователь, платежи, админ |
| `app/services/vpn_provider.py` | Marzban API (создание пользователя, expire, отключение) |
| `app/subscription_gate_http.py` | aiohttp: `GET /sub/{token}` → лимит устройств → прокси на Marzban |
| `app/plans.py` | Тарифы, разбор payload инвойса (`тариф:all:tg_id[:списание_с_баланса]`, `topup:tg_id:копейки`) |
| `app/payment_fulfillment.py` | Выдача VPN после оплаты, списание баланса с откатом при ошибке Marzban |
| `alembic/` | Миграции PostgreSQL |
| `infra/` | Docker Compose, пример systemd, пример nginx |
| `scripts/` | Утилиты обслуживания (см. ниже) |

## Быстрый старт (локально)

1. Скопируйте `.env.example` → `.env` и заполните `BOT_TOKEN`, `DATABASE_URL`, `REDIS_URL`, `PANEL_*`, `VPN_NODES_JSON`.
2. Поднимите Postgres/Redis, например: `docker compose -f infra/docker-compose.yml up -d`
3. `pip install -r requirements.txt`
4. `alembic upgrade head`
5. `python -m app.main`

## Миграции

Всегда из **корня** репозитория (рядом с `alembic.ini`):

```bash
alembic upgrade head
```

На VPS от пользователя `myvpn`:

```bash
cd /opt/myvpn && sudo -u myvpn ./scripts/migrate.sh
```

## Скрипты (`scripts/`)

| Скрипт | Назначение |
|--------|------------|
| `migrate.sh` | `alembic upgrade head` из корня репо |
| `reset_trial_for_tg.py <tg_id>` | Сброс `trial_used` и удаление подписок пользователя в **БД бота** (Marzban не трогает). Читает `DATABASE_URL` из `.env` или `app/.env` **в корне репо** (`/opt/myvpn`, не из `/opt/myvpn/app` только). |
| `reset_all_trials.py` | Сброс `trial_used` у всех пользователей (осторожно) |
| `fix_postgres_password.py` | Синхронизация пароля роли PostgreSQL с `DATABASE_URL` |
| `ensure_postgres_database.py` | Создание БД, если её нет |
| `replace_subscription_prefix.py` | Пакетная замена префикса в URL подписок (если нужно) |

Пример сброса пробного периода на сервере:

```bash
cd /opt/myvpn
/opt/myvpn/.venv/bin/python scripts/reset_trial_for_tg.py 731162352
```

## Баланс и оплата

- **Пополнить баланс** (профиль): пользователь вводит сумму в рублях, приходит инвойс Telegram Payments или ссылка ЮKassa. Зачисление — после успешной оплаты (`payments`, payload `topup:…`).
- **Купить**: тариф из `PLAN_MAP`; к оплате **сумма тарифа минус баланс** (но не меньше нуля). Часть может списаться **полностью с баланса** — тогда отдельный инвойс не создаётся.
- Списание с баланса при оплате тарифа выполняется в момент выдачи Marzban; при сбое API часть с баланса **возвращается**.

## Команды и меню бота

- Пользователь: `/start`, `/buy`, `/trial`, `/my`, `/profile`, `/help`, `/cancel` (отмена ввода суммы пополнения)
- Админ (если `tg_id` в `ADMIN_IDS`): `/stats`, `/servers`, `/add_days`, `/revoke`, `/broadcast`, `/cancel`

## Шлюз подписки и nginx

Публичная ссылка вида `https://sub.example.com/sub/<uuid>` должна проксироваться на порт шлюза (`SUBSCRIPTION_GATE_LISTEN_PORT`, по умолчанию 8095). Тестируйте **полный** URL с UUID из бота, не голый `/sub`.

**Важно для Happ / iOS:** на имени `sub.вашдомен` должен быть **валидный** TLS-сертификат (обычно Let's Encrypt через `certbot --nginx`). Иначе клиент пишет «сертификат недействителен» / незащищённое подключение и не обновляет подписку.

Пример только для шлюза подписок: `infra/nginx-subscription-gate.conf.example`. Панель Marzban: `infra/nginx-marzban-panel.conf.example`.

## Деплой на VPS (кратко)

- Пользователь **`myvpn`**, каталог **`/opt/myvpn`**, `chown -R myvpn:myvpn`
- Конфиг: **`/opt/myvpn/app/.env`** (права `600`)
- БД: `docker compose -f infra/docker-compose.yml up -d`
- Сервис: `infra/systemd/myvpn-bot.service.example` → `myvpn-bot.service`, `WorkingDirectory=/opt/myvpn`

## Multihop и несколько узлов REALITY

Если нужен **multihop** (клиент → VPS в РФ → EU с Marzban), заводите в подписке Marzban один публичный **Host** (релей в РФ) и держите API панели на EU — логика та же, что для одной панели с несколькими inbound.

Если **два независимых узла** (EU и US) в одной панели — у каждого inbound **свои** ключи REALITY; в `VPN_NODES_JSON` укажите разные `inbound_tag`, один `api_url` панели.

---

Подробности по переменным окружения — в **`.env.example`**.
