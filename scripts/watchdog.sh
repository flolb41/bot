#!/usr/bin/env bash
# Watchdog simple (section 13, semaine 4 du TODO) : vérifie que le service tourne
# et qu'il répond, sinon le redémarre. À planifier via cron (ex: */10 * * * *).
set -euo pipefail

SERVICE="crypto-reward-hunter"
DASHBOARD_URL="http://127.0.0.1:8000/api/status"
LOG_FILE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/logs/watchdog.log"

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') $1" >> "$LOG_FILE"
}

if ! systemctl is-active --quiet "$SERVICE"; then
    log "Service $SERVICE inactif -> redémarrage."
    sudo systemctl restart "$SERVICE"
    exit 0
fi

if ! curl -fsS --max-time 5 "$DASHBOARD_URL" > /dev/null; then
    log "Dashboard injoignable -> redémarrage de $SERVICE."
    sudo systemctl restart "$SERVICE"
fi
