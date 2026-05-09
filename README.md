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

### Баннер на /start

- Через URL: задайте `START_BANNER_URL=https://.../banner.png`.
- Через локальный файл: задайте `START_BANNER_PATH=/opt/myvpn/assets/start-banner.png`.
- В репозитории есть готовый файл: `assets/start-banner.png`.

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

Все сценарии ниже читают **`DATABASE_URL`** из **корня репозитория** (файлы `/opt/myvpn/.env` и при наличии `/opt/myvpn/app/.env`). Конфиг только в `app/.env` допустим — тогда положите переменные там. **`venv`** обычно лежит в **`/opt/myvpn/.venv`**, каталог **`/opt/myvpn/app/`** сам по себе не содержит `scripts/` и `.venv` — команды выполнять после **`cd /opt/myvpn`**. В Telegram у админа есть **`/wipe_all_subs YES`** и **`/reset_trials_all YES`** (с отключением Marzban по каждой подписке при массовом удалении через бота). Скрипты Marzban **не вызывают** — для массовой очистки с панелью удобнее бот.

| Скрипт | Что делает |
|--------|------------|
| `migrate.sh` | `alembic upgrade head` из корня репо |
| `reset_all_trials.py` | **`--dry-run`** / **`--apply`**: сброс `trial_used = false` у **всех** пользователей. Опционально **`--apply --wipe-subs`**: то же + `DELETE FROM subscriptions` у всех. |
| `reset_trial_for_tg.py <tg_id>` | Сброс пробного **одного** пользователя: `trial_used = false` и удаление его строк `subscriptions` в БД бота. |
| `wipe_all_subscriptions.py` | Только подписки: **`--dry-run`** или **`--apply`** — `DELETE FROM subscriptions` для **всех**; `trial_used` **не** меняется. |
| `wipe_subscriptions_for_tg.py <tg_id>` | Только подписки **одного** `tg_id`; `trial_used` **не** меняется. Marzban отдельно при необходимости. |
| `fix_postgres_password.py` | Синхронизация пароля роли PostgreSQL с `DATABASE_URL` |
| `ensure_postgres_database.py` | Создание БД, если её нет |
| `replace_subscription_prefix.py` | Пакетная замена префикса в URL подписок в таблице |

### Примеры на VPS

Неверный путь `No such file or directory` для `.venv/bin/python` чаще всего из‑за каталога: вы в **`/opt/myvpn/app`**, а интерпретатор — **`/opt/myvpn/.venv/bin/python`**.

```bash
cd /opt/myvpn
sudo -u myvpn -H bash -lc 'cd /opt/myvpn && .venv/bin/python scripts/reset_all_trials.py --dry-run'
sudo -u myvpn -H bash -lc 'cd /opt/myvpn && .venv/bin/python scripts/reset_all_trials.py --apply'
sudo -u myvpn -H bash -lc 'cd /opt/myvpn && .venv/bin/python scripts/reset_all_trials.py --apply --wipe-subs'
sudo -u myvpn -H bash -lc 'cd /opt/myvpn && .venv/bin/python scripts/reset_trial_for_tg.py 731162352'
sudo -u myvpn -H bash -lc 'cd /opt/myvpn && .venv/bin/python scripts/wipe_all_subscriptions.py --dry-run'
sudo -u myvpn -H bash -lc 'cd /opt/myvpn && .venv/bin/python scripts/wipe_all_subscriptions.py --apply'
sudo -u myvpn -H bash -lc 'cd /opt/myvpn && .venv/bin/python scripts/wipe_subscriptions_for_tg.py 731162352'
```

## Баланс и оплата

- **Пополнить баланс** (профиль): пользователь вводит сумму в рублях, приходит инвойс Telegram Payments или ссылка ЮKassa. Зачисление — после успешной оплаты (`payments`, payload `topup:…`).
- **Купить**: тариф из `PLAN_MAP`; к оплате **сумма тарифа минус баланс** (но не меньше нуля). Часть может списаться **полностью с баланса** — тогда отдельный инвойс не создаётся.
- Списание с баланса при оплате тарифа выполняется в момент выдачи Marzban; при сбое API часть с баланса **возвращается**.

## Команды и меню бота

- Пользователь: `/start`, `/buy`, `/trial`, `/my`, `/profile`, `/help`, `/cancel` (отмена ввода суммы пополнения)
- Админ (если `tg_id` в `ADMIN_IDS`): `/stats`, `/servers`, `/promo_*`, `/give_sub`, `/add_days`, `/revoke`, `/wipe_subs`, **`/wipe_all_subs`**, **`/reset_trials_all`**, `/broadcast`, `/cancel`; в меню — **Промокоды** и **Подписки / сброс** (`/wipe_all_subs YES`, `/reset_trials_all YES`, при необходимости третьим словом **`WIPE_SUBS`**).

## Шлюз подписки и nginx

Публичная ссылка вида `https://sub.example.com/sub/<uuid>` должна проксироваться на порт шлюза (`SUBSCRIPTION_GATE_LISTEN_PORT`, по умолчанию 8095). Тестируйте **полный** URL с UUID из бота, не голый `/sub`.

**Важно для Happ / iOS:** на имени `sub.вашдомен` должен быть **валидный** TLS-сертификат (обычно Let's Encrypt через `certbot --nginx`). Иначе клиент пишет «сертификат недействителен» / незащищённое подключение и не обновляет подписку.

Пример только для шлюза подписок: `infra/nginx-subscription-gate.conf.example`. Панель Marzban: `infra/nginx-marzban-panel.conf.example`.

## Пошагово: `sub.orientalvpn.ru`, TLS, бот, nginx (исправить 404/500/сертификат)

Имя поддомена ниже — **sub.orientalvpn.ru** (в DNS и в конфиге должно совпадать буква в букву).

### Шаг 1. DNS

У регистратора домена создайте запись **A** (и при желании **AAAA** для IPv6):

- `sub.orientalvpn.ru` → **публичный IP** того сервера, где стоит **nginx** (тот же VPS, где слушает 443, или отдельный прокси — тогда IP прокси).

Проверка с ноутбука:

```bash
nslookup sub.orientalvpn.ru
```

### Шаг 2. Миграции БД и переменные бота

На сервере, от **`myvpn`**, из **корня** репозитория `/opt/myvpn`:

```bash
sudo -u myvpn -H bash -lc 'cd /opt/myvpn && git pull && .venv/bin/python -m alembic upgrade head'
```

В файле **`/opt/myvpn/app/.env`** должны быть (проверьте и перезапустите бота после правок):

```env
SUBSCRIPTION_GATE_PUBLIC_BASE=https://sub.orientalvpn.ru
SUBSCRIPTION_GATE_LISTEN_HOST=0.0.0.0
SUBSCRIPTION_GATE_LISTEN_PORT=8095
DATABASE_URL=...
```

Убедиться, что строка реально читается:

```bash
sudo -u myvpn -H bash -lc 'grep -E "^SUBSCRIPTION_GATE_" /opt/myvpn/app/.env'
```

Если **`SUBSCRIPTION_GATE_PUBLIC_BASE` пустой** — шлюз **не поднимется**, ссылки в боте будут без шлюза или со старым доменом.

Перезапуск:

```bash
sudo systemctl restart myvpn-bot
sudo systemctl status myvpn-bot --no-pager
```

В логах при старте должно быть что-то вроде **subscription gate listening 0.0.0.0:8095** (если база для шлюза задана).

```bash
sudo journalctl -u myvpn-bot -n 80 --no-pager
```

Проверка порта:

```bash
ss -tlnp | grep 8095
```

### Шаг 3. Локальный тест шлюза (обойти nginx)

Подставьте UUID из ссылки бота:

```bash
curl -sS -D- -o /tmp/sub.txt "http://127.0.0.1:8095/sub/ВАШ-UUID" | head -20
head -5 /tmp/sub.txt
```

Ожидание: **200** и тело похоже на список прокси / base64-подписку. **404** «Подписка не найдена» — в БД нет строки с таким `sub_gate_token` или пустой `upstream_subscription_url` (нужна новая выдача подписки из бота после включения шлюза или проверка миграций). **500** — смотрите логи: `journalctl -u myvpn-bot -n 100 --no-pager` (БД/upstream); если в логе было `charset must not be in content_type` — обновите бота с последнего `main` и перезапустите сервис.

### Шаг 4. Nginx: proxy_pass на шлюз

**Если на VPS порт 443 занят Xray (REALITY)** (`ss -tlnp | grep 443` показывает `xray`): nginx **не сможет** слушать 443. Используйте пример **`infra/nginx-subscription-gate.conf.example`** — там HTTPS на **9443** и тот же прокси на 8095. В `.env` бота:

```env
SUBSCRIPTION_GATE_PUBLIC_BASE=https://sub.orientalvpn.ru:9443
```

Откройте порт в фаерволе: `sudo ufw allow 9443/tcp`. После смены BASE пользователям нужны **новые** ссылки из бота (или замена URL в БД).

Установка:

```bash
sudo cp /opt/myvpn/infra/nginx-subscription-gate.conf.example /etc/nginx/sites-available/sub.orientalvpn.ru
sudo sed -i 's/sub\.example\.com/sub.orientalvpn.ru/g' /etc/nginx/sites-available/sub.orientalvpn.ru
sudo ln -sf /etc/nginx/sites-available/sub.orientalvpn.ru /etc/nginx/sites-enabled/
```

Сертификат (если ещё нет), через webroot на порту 80:

```bash
sudo certbot certonly --webroot -w /var/www/html -d sub.orientalvpn.ru
```

Если файлов `/etc/letsencrypt/options-ssl-nginx.conf` или `ssl-dhparams.pem` нет — установите пакет `python3-certbot-nginx` или временно закомментируйте строки `include ...` и `ssl_dhparam` в конфиге и добавьте минимальные ssl-директивы вручную.

Проверка и перезагрузка:

```bash
sudo nginx -t && sudo systemctl reload nginx
curl -sS -o /dev/null -w "%{http_code}\n" "https://sub.orientalvpn.ru:9443/sub/ВАШ-UUID"
```

**Если 443 свободен для nginx** — можно оставить только блок `:80` из примера и после `certbot --nginx -d sub...` использовать классический `https://sub.../sub/...` **без порта** (старый сценарий в отдельном сервере без Xray на 443).

В блоке `location /` должен быть **`proxy_pass http://127.0.0.1:8095;`**.

### Шаг 5. Happ / телефон

Вставьте полную ссылку из бота — с **портом**, если выбрали вариант с 9443, например  
`https://sub.orientalvpn.ru:9443/sub/<uuid>`.

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
