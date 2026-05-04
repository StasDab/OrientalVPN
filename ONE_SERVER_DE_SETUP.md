# Одна VPS (MVP), пример ниже был под DE — сейчас у тебя фактическая локация SE (Aeza Sweden)

## 1) Где взять SSH ключ на Windows
Открой PowerShell и выполни:

```powershell
Test-Path "$env:USERPROFILE\.ssh\id_ed25519.pub"
```

- Если вернуло `True`, ключ уже есть.
- Если `False`, создай:

```powershell
ssh-keygen -t ed25519 -C "myvpn" -f "$env:USERPROFILE\.ssh\id_ed25519"
```

Показать публичный ключ:

```powershell
Get-Content "$env:USERPROFILE\.ssh\id_ed25519.pub"
```

Скопируй эту строку и добавь в Vultr: `Account -> SSH Keys -> Add SSH Key`.

## 2) Если создавался сервер во Vultr (DE), а ты сейчас на Aeza (SE)
Ничего страшного: это только метка “локации” в маркетинге. Для бота мы используем код `se` и inbound-tag `loc-se`.
- Type: `Shared CPU`
- Location (пример Aeza Sweden): любая подходящая EU/SE зона тарифом
- Plan: `1 vCPU / 2 GB` (для теста)
- OS: `Ubuntu 22.04 LTS`
- SSH Key: твой `id_ed25519.pub`
- Hostname: `vpn-se-01`
- Label: `vpn-se-01`
- Connectivity: `Public IPv4 + IPv6`
- VPC Network: подключи созданную VPC
- Backups: можно выключить для теста

## 3) Установка Marzban через терминал
Подключись:

```bash
ssh root@<SERVER_IP>
```

Обнови пакеты и поставь Docker:

```bash
apt update && apt upgrade -y
curl -fsSL https://get.docker.com | sh
systemctl enable docker && systemctl start docker
```

Безопасность:

```bash
apt install -y ufw fail2ban curl
ufw allow 22/tcp
ufw allow 80/tcp
ufw allow 443/tcp
ufw allow 8000/tcp
ufw --force enable
systemctl enable fail2ban && systemctl restart fail2ban
```

Установка Marzban:

```bash
bash -c "$(curl -fsSL https://github.com/Gozargah/Marzban-scripts/raw/master/marzban.sh)"
```

## 4) Что сделать в панели Marzban после установки
- Создать admin пользователя.
- Создать inbound:
  - protocol: `VLESS`
  - tag: `loc-de`
- Сохранить данные:
  - `PANEL_URL`
  - `PANEL_USERNAME`
  - `PANEL_PASSWORD`

## 5) Как заполнить `.env` для одной ноды (`se`)
```env
VPN_PROVIDER=marzban
PANEL_URL=https://panel.example.com
PANEL_USERNAME=admin
PANEL_PASSWORD=strong_password
VPN_NODES_JSON=[{"location_code":"se","api_url":"https://panel.example.com","capacity":500,"current_load":10,"is_healthy":true}]
```

## 6) Запуск бота
```bash
docker compose -f infra/docker-compose.yml up -d
alembic upgrade head
python -m app.main
```
