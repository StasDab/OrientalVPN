# Быстрая установка серверов

## 1) VPN node (DE/NL/US)
На каждом VPN сервере:

```bash
ssh root@<SERVER_IP>
curl -fsSL https://raw.githubusercontent.com/<your-user>/<your-repo>/main/scripts/setup_vpn_node.sh -o setup_vpn_node.sh
sudo bash setup_vpn_node.sh de
```

Для NL/US поменяйте последний аргумент на `nl` / `us`.

## 2) Core node (бот + БД + Redis)
```bash
ssh root@<CORE_IP>
curl -fsSL https://raw.githubusercontent.com/<your-user>/<your-repo>/main/scripts/setup_core_node.sh -o setup_core_node.sh
sudo bash setup_core_node.sh
```

## 3) После установки
- В каждой панели Marzban создать inbound tag: `loc-de`, `loc-nl`, `loc-us`.
- Заполнить `.env` на core-ноде.
- Применить миграции: `alembic upgrade head`.
- Запустить бота: `python -m app.main`.
