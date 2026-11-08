#!/usr/bin/env bash
# Déploie le bot Base et redémarre uniquement son service systemd utilisateur.
# Usage : ./scripts/deploy.sh [user@host] [--enable-cross-dex-live]
set -euo pipefail

TARGET="${1:-florian@192.168.1.158}"
ENABLE_CROSS_DEX_LIVE="false"
if [ "${2:-}" = "--enable-cross-dex-live" ]; then
  ENABLE_CROSS_DEX_LIVE="true"
elif [ -n "${2:-}" ]; then
  echo "Option inconnue: $2" >&2
  exit 2
fi
REMOTE_ARCHIVE="bot/.deploy/update-$(date '+%Y%m%d_%H%M%S')-$$.tar.gz"
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ARCHIVE="$(mktemp)"
trap 'rm -f "$ARCHIVE"' EXIT
cd "$PROJECT_DIR"

tar czf "$ARCHIVE" \
  --exclude=.git --exclude=.venv --exclude=tests --exclude=__pycache__ --exclude=.pytest_cache \
  --exclude=data --exclude=logs --exclude=.env --exclude=.deploy --exclude=deploy-backups \
  --exclude=scripts/deploy_profit_improvements_tmp.py --exclude=scripts/deploy_triangular_fixes.py \
  --exclude=requirements-dev.txt --exclude=pyproject.toml --exclude=.gitattributes --exclude=.gitignore \
  .

ssh "$TARGET" "mkdir -p ~/bot/.deploy"
scp "$ARCHIVE" "$TARGET:$REMOTE_ARCHIVE"

ssh "$TARGET" "bash -s -- '$REMOTE_ARCHIVE' '$ENABLE_CROSS_DEX_LIVE'" <<'REMOTE'
set -euo pipefail
ARCHIVE_RELATIVE="$1"
ENABLE_CROSS_DEX_LIVE="$2"
REMOTE_DIR="$HOME/bot"
SERVICE="trading-bot-user.service"
case "$ENABLE_CROSS_DEX_LIVE" in
    true|false) ;;
    *) echo "Option cross-DEX invalide." >&2; exit 2 ;;
esac
case "$SERVICE" in
    trading-bot-user.service|trading-dashboard.service) ;;
    *) echo "Service cible non autorisé: $SERVICE" >&2; exit 1 ;;
esac
cd "$REMOTE_DIR"
if [ -d "$REMOTE_DIR/.git" ]; then
    echo "Ce Pi est géré par git + bot-self-update.timer (scripts/install_self_update.sh) :" >&2
    echo "pousser sur la branche main au lieu de deploy.sh, sinon la prochaine mise à jour automatique écraserait ce déploiement." >&2
    exit 1
fi
if ! systemctl --user is-active --quiet "$SERVICE"; then
    echo "Le service $SERVICE n'est pas actif; aucun déploiement effectué." >&2
    exit 1
fi

TIMESTAMP="$(date '+%Y%m%d_%H%M%S')"
BACKUP="$REMOTE_DIR/deploy-backups/$TIMESTAMP"
mkdir -p "$BACKUP/files"
cp -a app config scripts main.py README.md "$BACKUP/files/"
cp -p requirements.txt "$BACKUP/files/"
"$REMOTE_DIR/.venv/bin/python" - "$BACKUP/reward_hunter.db" <<'PY'
import sqlite3
import sys

with sqlite3.connect("data/reward_hunter.db", timeout=30) as source:
    with sqlite3.connect(sys.argv[1]) as destination:
        source.backup(destination)
        integrity = destination.execute("PRAGMA integrity_check").fetchone()
        if not integrity or integrity[0] != "ok":
            raise RuntimeError(f"Backup SQLite invalide: {integrity!r}")
PY
cp -p config/config.yaml "$BACKUP/config.yaml"
if [ -f "$REMOTE_DIR/data/reward_hunter_archive.db" ]; then
    cp -p "$REMOTE_DIR/data/reward_hunter_archive.db" "$BACKUP/reward_hunter_archive.db"
fi
PREVIOUS_KILLSWITCH="$("$REMOTE_DIR/.venv/bin/python" -c \
  "from app.database import Database; print(Database().get_state('killswitch', '0'))")"
"$REMOTE_DIR/.venv/bin/python" -c \
  "from app.database import Database; Database().set_state('killswitch', '1')"
systemctl --user stop "$SERVICE"

tar -xzf "$HOME/$ARCHIVE_RELATIVE" -C "$REMOTE_DIR"
rm -f "$HOME/$ARCHIVE_RELATIVE"
sed -i 's/\r$//' "$REMOTE_DIR"/scripts/*.sh "$REMOTE_DIR"/scripts/*.service \
  "$REMOTE_DIR"/config/*.yaml "$REMOTE_DIR"/.env.example
chmod +x "$REMOTE_DIR"/scripts/*.sh

if [ ! -f "$REMOTE_DIR/.env" ]; then
    echo "Fichier .env absent; déploiement interrompu après sauvegarde." >&2
    exit 1
fi
"$REMOTE_DIR/.venv/bin/python" - <<'PY'
from pathlib import Path
import secrets

path = Path(".env")
lines = path.read_text(encoding="utf-8").splitlines()
existing = [line for line in lines if line.startswith("DASHBOARD_CONTROL_TOKEN=")]
if len(existing) != 1 or not existing[0].split("=", 1)[1].strip():
    lines = [line for line in lines if not line.startswith("DASHBOARD_CONTROL_TOKEN=")]
    lines.append(f"DASHBOARD_CONTROL_TOKEN={secrets.token_hex(32)}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
PY
chmod 600 "$REMOTE_DIR/.env"
"$REMOTE_DIR/.venv/bin/pip" install \
  --extra-index-url https://www.piwheels.org/simple \
  -r "$REMOTE_DIR/requirements.txt"
"$REMOTE_DIR/.venv/bin/python" -m compileall -q "$REMOTE_DIR/app"
"$REMOTE_DIR/.venv/bin/pip" check
ENABLE_CROSS_DEX_LIVE="$ENABLE_CROSS_DEX_LIVE" "$REMOTE_DIR/.venv/bin/python" - <<'PY'
import os
from app.config import load_config

enabled = load_config().get("trading", {}).get("cross_dex_flashloan_enabled")
opted_in = os.environ["ENABLE_CROSS_DEX_LIVE"] == "true"
if enabled is not opted_in:
    if enabled is True:
        raise SystemExit(
            "Déploiement interrompu : relancer avec --enable-cross-dex-live "
            "pour confirmer la voie de trading cross-DEX live."
        )
    raise SystemExit(
        "Déploiement interrompu : le flag --enable-cross-dex-live exige "
        "trading.cross_dex_flashloan_enabled: true."
    )
PY
"$REMOTE_DIR/.venv/bin/python" - <<'PY'
from app.config import load_config
from app.database import Database
from app.trading.runtime_settings import get_runtime_settings, save_runtime_settings

db = Database()
effective = get_runtime_settings(db, load_config())
save_runtime_settings(db, effective)
PY
systemctl --user start "$SERVICE"
systemctl --user is-active --quiet "$SERVICE"
PREVIOUS_KILLSWITCH="$PREVIOUS_KILLSWITCH" "$REMOTE_DIR/.venv/bin/python" - <<'PY'
import os
from app.database import Database

Database().set_state("killswitch", os.environ["PREVIOUS_KILLSWITCH"])
PY
HEALTHY=0
for _ in $(seq 1 30); do
    if curl -fsS --max-time 2 http://127.0.0.1:8000/api/health >/dev/null; then
        HEALTHY=1
        break
    fi
    sleep 1
done
if [ "$HEALTHY" -ne 1 ]; then
    echo "Le dashboard n'est pas devenu disponible dans le délai imparti." >&2
    exit 1
fi
curl -fsS http://127.0.0.1:8000/api/runtime-settings >/dev/null
rmdir "$REMOTE_DIR/.deploy" 2>/dev/null || true
echo "Déploiement terminé; sauvegarde conservée dans $BACKUP"
REMOTE
