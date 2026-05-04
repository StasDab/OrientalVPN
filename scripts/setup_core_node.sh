#!/usr/bin/env bash
set -euo pipefail

# Usage:
#   sudo bash setup_core_node.sh

echo "[1/6] Update packages"
apt update -y
apt upgrade -y

echo "[2/6] Install base tools"
apt install -y curl git ufw fail2ban python3 python3-pip python3-venv

echo "[3/6] Install Docker"
if ! command -v docker >/dev/null 2>&1; then
  curl -fsSL https://get.docker.com | sh
fi
systemctl enable docker
systemctl start docker

echo "[4/6] Configure firewall"
ufw allow 22/tcp
ufw allow 5432/tcp
ufw allow 6379/tcp
ufw --force enable

echo "[5/6] Enable fail2ban"
systemctl enable fail2ban
systemctl restart fail2ban

echo "[6/6] Done"
echo "Next actions:"
echo "  1) Clone MyVpn repo to this server."
echo "  2) Start postgres/redis: docker compose -f infra/docker-compose.yml up -d"
echo "  3) Fill .env and run: alembic upgrade head"
echo "  4) Start bot: python -m app.main"
