#!/usr/bin/env bash
# Vérifie le service utilisateur et son dashboard, puis le redémarre si nécessaire.
# À planifier via le cron de l'utilisateur (ex: */10 * * * *).
set -euo pipefail

SERVICE="trading-bot-user.service"
# /api/health et non /api/status : ce dernier agrège toute la table des
# signaux (~3 s au repos sur le Pi, davantage pendant un scan) et dépassait
# régulièrement le timeout, faisant redémarrer un service parfaitement sain.
DASHBOARD_URL="http://127.0.0.1:8000/api/health"
CURL_TIMEOUT_SECONDS=20
LOG_FILE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/logs/watchdog.log"
export XDG_RUNTIME_DIR="/run/user/$(id -u)"
export DBUS_SESSION_BUS_ADDRESS="unix:path=${XDG_RUNTIME_DIR}/bus"

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') $1" >> "$LOG_FILE"
}

# Mise à jour automatique en cours (scripts/self_update.sh arrête puis relance
# lui-même le service) : ne pas interférer. Un marqueur de plus de 20 min est
# considéré orphelin et ignoré.
UPDATING_MARKER="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/.updating"
if [ -f "$UPDATING_MARKER" ] && [ $(( $(date +%s) - $(stat -c %Y "$UPDATING_MARKER") )) -lt 1200 ]; then
    exit 0
fi

if ! systemctl --user is-active --quiet "$SERVICE"; then
    log "Service utilisateur $SERVICE inactif -> redémarrage."
    systemctl --user restart "$SERVICE"
    exit 0
fi

if ! curl -fsS --max-time "$CURL_TIMEOUT_SECONDS" "$DASHBOARD_URL" > /dev/null; then
    log "Dashboard injoignable -> redémarrage de $SERVICE."
    systemctl --user restart "$SERVICE"
fi
