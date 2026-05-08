# MyVpn Telegram Bot (MVP Scaffold)

## Что внутри
- ТЗ: `TZ_BOT.md`
- Анализ VPS и локаций: `VPS_LOCATIONS_ANALYSIS.md`
- **Multihop РФ → EU** (белые списки / LTE): `MULTIHOP_RU_EU.md`
- **Два узла (EU + US), отдельные ключи REALITY:** `MARZBAN_REALITY_TWO_NODES.md`
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
- Пробный период (целые **часы** из `TRIAL_HOURS`, по умолчанию 3), один раз на Telegram (`users.trial_used`). Выдаётся один Marzban-пользователь `tg_*`; в конфиг добавляются **все VLESS inbound** с той же панели (см. `VPN_NODES_JSON`), чтобы одна ссылка подписки `/sub/` включала все доступные узлы сразу.
- Оплата и пробный — **без выбора локации** (все серверы в одной подписке). Отдельная ссылка на один узел — меню «Выбрать сервер» (на сервере в `VPN_NODES_JSON`: **`link_match`** или список **`link_matches`** — подстроки из `links` в API Marzban, уникальные для узла; см. `.env.example`).
- Если в панели Marzban «осталось времени» выглядит как минуты при `TRIAL_HOURS=3`, проверьте в API `GET /api/user/tg_<id>` поле **`expire`** (unix **секунды**, UTC). Часовой пояс сервера и панели должны быть согласованы.
- Фоновые задачи: отключение истёкших подписок, напоминание за `REMINDER_HOURS_BEFORE` часов, повторная выдача из очереди.

### Как проверить `expire` вручную
Имя пользователя в Marzban у бота: `tg_<ваш_telegram_id>` (цифры без `@`). Токен — **Admin / API token** из панели Marzban (тот же, что в `.env` бота для API).

На сервере с `curl`:

```bash
curl -sS -H "Authorization: Bearer YOUR_ADMIN_TOKEN" \
  "https://YOUR_PANEL_HOST:YOUR_PORT/api/user/tg_123456789"
```

В ответе смотрите поле **`expire`**: это конец действия подписки в **unix-секундах UTC** (`0` обычно значит «без срока»). Для пробного на 3 часа разница `expire − now` должна быть около **10800** секунд (с допуском на скос из настроек бота).

Текущее время в секундах: `date +%s` (Linux). Расшифровка: `date -u -d @EXPIRE` (GNU date) или любой калькулятор unix time.
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
1. Системный пользователь и каталог проекта (от **`myvpn`**, не от root):
   ```bash
   sudo useradd --system --user-group --create-home \
     --home-dir /var/lib/myvpn --shell /usr/sbin/nologin myvpn
   sudo git clone <repo> /opt/myvpn
   sudo chown -R myvpn:myvpn /opt/myvpn
   sudo -u myvpn python3.12 -m venv /opt/myvpn/.venv
   sudo -u myvpn /opt/myvpn/.venv/bin/pip install -r /opt/myvpn/requirements.txt
   ```
2. `docker compose -f infra/docker-compose.yml up -d` — Postgres и Redis на `localhost`.
3. `/opt/myvpn/app/.env` (владелец **`myvpn`**, права **600**): `DATABASE_URL` на `127.0.0.1`, остальное как в `.env.example`.
4. Миграции: `sudo -u myvpn /opt/myvpn/.venv/bin/python -m alembic upgrade head` из каталога `/opt/myvpn` или `sudo -u myvpn ./scripts/migrate.sh`.
5. `sudo cp infra/systemd/myvpn-bot.service.example /etc/systemd/system/myvpn-bot.service` → `sudo systemctl daemon-reload && sudo systemctl enable --now myvpn-bot`.

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

1. См. выше: пользователь **`myvpn`**, репозиторий в `/opt/myvpn`, **`chown -R myvpn:myvpn`**, venv и `pip` от **`sudo -u myvpn`**. `.env` в `/opt/myvpn/app/.env`, права **600**, владелец **myvpn**.
2. Поднимите только БД: `docker compose -f infra/docker-compose.yml up -d`
3. Миграции: `cd /opt/myvpn && sudo -u myvpn ./scripts/migrate.sh`
4. Unit: `sudo cp infra/systemd/myvpn-bot.service.example /etc/systemd/system/myvpn-bot.service`, затем `sudo systemctl daemon-reload && sudo systemctl enable --now myvpn-bot`

### Нужен ли отдельный firstVDS «Старт»?
**Не обязательно**, если текущий VPS тянет нагрузку и есть **~2 GB+ RAM**. **Имеет смысл**, если хотите изоляцию (падение VPN не роняет биллинг), мало памяти или планируете много пользователей бота.
