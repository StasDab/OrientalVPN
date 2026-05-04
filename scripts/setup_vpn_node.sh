#!/usr/bin/env bash
set -euo pipefail

# Usage:
#   sudo bash setup_vpn_node.sh <LOCATION_CODE>
# Example:
#   sudo bash setup_vpn_node.sh de

LOCATION_CODE="${1:-de}"

echo "[1/7] Update packages"
apt update -y
apt upgrade -y

echo "[2/7] Install base tools"
apt install -y curl git ufw fail2ban ca-certificates gnupg lsb-release

echo "[3/7] Install Docker"
if ! command -v docker >/dev/null 2>&1; then
  curl -fsSL https://get.docker.com | sh
fi
systemctl enable docker
systemctl start docker

echo "[4/7] Configure firewall"
ufw allow 22/tcp
ufw allow 80/tcp
ufw allow 443/tcp
ufw allow 8000/tcp
ufw --force enable

echo "[5/7] Enable fail2ban"
systemctl enable fail2ban
systemctl restart fail2ban

echo "[6/7] Install Marzban"
bash -c "$(curl -fsSL https://github.com/Gozargah/Marzban-scripts/raw/master/marzban.sh)"

echo "[7/7] Done"
echo "Node location: ${LOCATION_CODE}"
echo "Next actions:"
echo "  1) Open Marzban panel and create admin user."
echo "  2) Create VLESS inbound with tag loc-${LOCATION_CODE}."
echo "  3) Save PANEL_URL, PANEL_USERNAME, PANEL_PASSWORD for .env."
