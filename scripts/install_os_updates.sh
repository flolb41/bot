#!/usr/bin/env bash
# Mises à jour de sécurité Debian automatiques sur le Pi, SANS redémarrage
# automatique (un redémarrage manuel relance le bot via systemd --user +
# linger). À lancer UNE fois : sudo bash ~/bot/scripts/install_os_updates.sh
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "À lancer avec sudo : sudo bash $0" >&2
    exit 1
fi
export DEBIAN_FRONTEND=noninteractive

apt-get update -q
apt-get install -y -q unattended-upgrades

cat > /etc/apt/apt.conf.d/20auto-upgrades <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
APT::Periodic::AutocleanInterval "7";
EOF

# Surcharge de la configuration par défaut (50unattended-upgrades conserve
# ses origines « security ») : jamais de reboot automatique.
cat > /etc/apt/apt.conf.d/52unattended-upgrades-bot <<'EOF'
Unattended-Upgrade::Automatic-Reboot "false";
Unattended-Upgrade::Remove-Unused-Dependencies "true";
Unattended-Upgrade::Remove-Unused-Kernel-Packages "true";
EOF

systemctl enable --now unattended-upgrades.service apt-daily.timer apt-daily-upgrade.timer
echo "--- Vérification (dry-run) ---"
unattended-upgrade --dry-run 2>&1 | tail -n 5 || true
systemctl list-timers apt-daily.timer apt-daily-upgrade.timer --no-pager
