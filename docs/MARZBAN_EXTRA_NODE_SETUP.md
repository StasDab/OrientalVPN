# Добавление дополнительного узла Marzban (отдельный VPS / новая страна)

Инструкция описывает связку **основная панель Marzban** + **Marzban Node** на втором (и следующих) серверах и обновление **`vpn_nodes.json`** бота OrientalVPN. На примере добавлялся узел **Germany**; шаги универсальны для любой новой локации.

---

## 1. Сервер под Node: ОС и доступ

- **Ubuntu 22.04/24.04**, доступ `root` или `sudo`.
- Публичный **IP** узла (или домен с `A`‑записью на этот IP).

```bash
sudo apt update && sudo apt upgrade -y
sudo timedatectl set-timezone Europe/Berlin   # или свой часовой пояс
```

---

## 2. Firewall (UFW)

Минимально:

```bash
sudo apt install -y ufw
sudo ufw allow OpenSSH
sudo ufw allow 443/tcp          # клиентский VLESS REALITY
sudo ufw allow 62050/tcp        # API Marzban-Node (SERVICE_PORT по умолчанию)
sudo ufw allow 62051/tcp        # XRAY API на узле — часто нужно панели
sudo ufw enable
sudo ufw status verbose
```

Порты **62050/62051** сверьте с `docker-compose.yml` на узле после установки; если меняли переменные окружения — откройте свои значения.

---

## 3. Docker

```bash
sudo apt install -y curl socat git ca-certificates
curl -fsSL https://get.docker.com | sudo sh
sudo systemctl enable --now docker
```

---

## 4. Установка Marzban Node (официальный скрипт)

Официально: [Marzban Node — документация](https://gozargah.github.io/marzban/en/docs/marzban-node).

```bash
sudo bash -c "$(curl -sL https://github.com/Gozargah/Marzban-scripts/raw/master/marzban-node.sh)" @ install
```

Скрипт спросит содержимое **Client Certificate** с панели (см. шаг 5).  
Переустановка с вводом другого сертификата:

```bash
sudo bash -c "$(curl -sL https://github.com/Gozargah/Marzban-scripts/raw/master/marzban-node.sh)" @ install
# на вопрос об override — y
```

---

## 5. Сертификат с панели (главный сервер ↔ узел)

На **веб-панели** Marzban: **Node Settings** → **Add New Marzban Node** (или аналог по локализации):

1. Кнопка **Show Certificate** / **Скачать сертификат** / иконка «глаз» — скопируйте **PEM** целиком от `-----BEGIN CERTIFICATE-----` до `-----END CERTIFICATE-----`.
2. На **узле** файл должен лежать по пути из `SSL_CLIENT_CERT_FILE` в `/opt/marzban-node/docker-compose.yml` — часто это:

   **`/var/lib/marzban-node/cert.pem`**

   (не путать с именем `ssl_client_cert.pem`, если в compose указано именно `cert.pem`).

```bash
sudo mkdir -p /var/lib/marzban-node
sudo nano /var/lib/marzban-node/cert.pem   # вставить PEM
sudo chmod 600 /var/lib/marzban-node/cert.pem
cd /opt/marzban-node
sudo docker compose down && sudo docker compose up -d
sudo docker compose logs -f --tail=30
```

В логах ожидается что-то вроде: **Node service running on :62050**, **Uvicorn running on https://0.0.0.0:62050**.

В панели у узла:

- **Address** — публичный IP узла (или домен).
- **Port / API Port** — как в compose (часто **62050** и **62051**).
- **Add Node** / **Переподключиться** при ошибке **Failed to connect to node's API**.

Если после смены сертификата на панели узел не подключается — снова скачайте PEM и **перезапишите** `cert.pem` на VPS, перезапустите compose.

---

## 6. Core: новый inbound (VLESS REALITY)

В **Core** панели добавьте inbound:

- **Протокол:** VLESS, **TCP**, **Reality**.
- **Тег** (tag) — совпадает с ботом, например **`loc-de`**.
- **Remark** — имя в подписке/Happ, удобно с флагом: **`🇩🇪 Germany`**.
- **Ключи Reality:** `xray x25519` (не SSH-ключи); отдельные **privateKey** / **shortIds** для каждой локации.
- **Привязка к хосту:** inbound должен обслуживаться **немецким Node** (не дублировать один и тот же порт 443 несколькими inbound на **одной** машине без отдельных IP).

После сохранения проверьте статус узла и отсутствие ошибок Xray в логах node.

---

## 7. Бот: `vpn_nodes.json`

На сервере с ботом (`/opt/myvpn` и т.д.) в **`vpn_nodes.json`** (или `VPN_NODES_JSON_FILE`) добавьте объект:

```json
{
  "location_code": "de",
  "inbound_tag": "loc-de",
  "link_matches": ["92.118.", "germany", "de"],
  "api_url": "https://panel.example.com:8443",
  "capacity": 1000,
  "current_load": 0,
  "is_healthy": true
}
```

- **`inbound_tag`** = тег в Core.  
- **`api_url`** = URL **той же** панели, что у остальных нод.  
- **`link_matches`** — уникальные подстроки из ссылок узла (IP, домен, часть remark), как у SE/FI/US.

Для подписи в Telegram при необходимости добавьте ключ в **`LOCATION_TITLES`** в `app/plans.py`.

Перезапуск бота:

```bash
sudo systemctl restart myvpn-bot
```

---

## 8. Пользователи и обновление подписки

### Кнопка «Обновить подписку» в боте

- **Не дублирует** ручное «обновить» в Happ по смыслу: она на стороне **Marzban** делает **PUT пользователя** с актуальным списком **inbounds** для панели (как при новой выдаче) и обновляет URL в БД бота (в т.ч. шлюз).
- **Обновление только в Happ** подтянет новые серверы **только если** в Marzban у `tg_*` уже прописаны все нужные inbound’ы. После добавления новой страны старые клиенты иногда остаются со **старым** списком inbounds до следующей синхронизации — тогда без кнопки/повторной выдачи новый узел в подписке **не появится**, даже если Happ обновляет каждый час.

Итого: держите кнопку для пользователей **после смены topology** или отключите осознанно, если синхронизируете всех админским скриптом/повторными оплатами.

### Иконки (флаги) в Happ

Happ берёт названия из **Remark / имени узла/inbound в Marzban**. Пропишите в remark, например: **`🇸🇪 Stockholm`**, **`🇩🇪 Germany`**. Emoji в тексте профиля бота задаётся в **`LOCATION_TITLES`** — для Happ нужна правка именно в панели.

### Автообновление раз в час в Happ для всех (через шлюз)

Если подписка идёт через **`SUBSCRIPTION_GATE_PUBLIC_BASE`** (URL вида `https://sub.orientalvpn.ru/.../sub/...`), бот может **добавлять в начало тела подписки** директивы Happ (см. [документ Happ «Управление приложением»](https://www.happ.su/main/ru/dev-docs/app-management)).

В **`app/.env`** (или корневом `.env`):

```env
SUBSCRIPTION_HAPP_PROFILE_UPDATE_HOURS=1
SUBSCRIPTION_HAPP_PROFILE_TITLE=OrientalVPN sub
```

- **`SUBSCRIPTION_HAPP_PROFILE_UPDATE_HOURS`** — интервал автообновления в **часах** (Happ: кратен 1 часу). `1` ≈ каждый час (точность зависит от ОС/Happ). `0` = не добавлять директиву.
- **`SUBSCRIPTION_HAPP_PROFILE_TITLE`** — до **25 символов** (ограничение Happ). Строку вроде `sub.orientalvpn.ru | автообновление` часто не влезает — используйте короткое имя, например **`OVPN sub`** или кириллицу покороче.

Перезапуск бота. Прямые URL Marzban **без шлюза** эти строки не получают.

Если Happ пишет **«There are no server links» / ошибка 39** после включения этих переменных:

- шлюз добавляет `#profile-*` **только** к ответу, который похож на **список URI** (`vless://`…). Подписка в формате **JSON/YAML/Clash** остаётся без вставки во избежание поломки парсера.
- после правок на сервере: `git pull`, `sudo systemctl restart myvpn-bot`, в Happ **удалить профиль под `sub.…` и добавить ссылку заново** или принудительное обновление подписки.
- имя **`profile-title`** применяется к **новым** запросам контента; если закэшировалось старое имя — пересоздайте подписку в Happ.

**Флаги в списке серверов Happ** зависят от **текста в ссылке/outbound** (часто имя после `#` в `vless://`… либо поле remark в панели Marzban для inbound). Если в «сырой» подписке имя без эмодзи, Happ может показать **глобус**. Проверка с ПК: `curl -sS "https://sub.…/sub/ВАШ_UUID" | head -5` — есть ли в конце URI фрагмент `#…` с флагом.

---

## 9. Безопасность

Не публикуйте **privateKey** Reality и клиентские PEM из панели в открытых чатах. При утечке — перевыпустите ключи и обновите подписки.

---

## 10. Краткий чеклист

| Шаг | Действие |
|-----|-----------|
| 1 | VPS: апдейты, UFW (22, 443, 62050, 62051) |
| 2 | Docker + `marzban-node.sh @ install`, cert в **`cert.pem`** как в compose |
| 3 | Панель: узел онлайн, inbound на node, уникальный **tag**, remark с флагом |
| 4 | `vpn_nodes.json` + рестарт бота |
| 5 | Пользователям: «Обновить подписку» при необходимости + Happ / опционально `SUBSCRIPTION_HAPP_PROFILE_UPDATE_HOURS` |
