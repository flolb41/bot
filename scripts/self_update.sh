#!/usr/bin/env bash
# Mise à jour automatique du bot depuis GitHub (branche main), exécutée toutes
# les 2 min par bot-self-update.timer (voir README « Mise à jour automatique »).
#
# Cycle : fetch -> nouveau commit ? -> CI verte ? -> sauvegarde SQLite ->
# killswitch -> arrêt du service -> git reset --hard -> pip (si requirements
# changés) -> compileall / pip check / import -> réglages runtime -> démarrage
# -> /api/health ; en cas d'échec, retour au commit précédent et notification.
set -euo pipefail

BOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$BOT_DIR"
PYTHON="$BOT_DIR/.venv/bin/python"
PIP="$BOT_DIR/.venv/bin/pip"
SERVICE="trading-bot-user.service"
# Services annexes (venv/process séparés) redémarrés avec la mise à jour s'ils
# étaient actifs ; leur venv est réinstallé si leur fichier de dépendances change.
PERPS_SERVICE="perps-paper.service"
PERPS_VENV="$BOT_DIR/.venv-perps"
BRANCH="${SELF_UPDATE_BRANCH:-main}"
# Nom du check GitHub Actions exigé (vide = pas de contrôle CI) ; doit
# correspondre au job « tests » de .github/workflows/ci.yml.
REQUIRED_CHECK="${SELF_UPDATE_REQUIRED_CHECK-tests}"
HEALTH_URL="http://127.0.0.1:8000/api/health"
HEALTH_TIMEOUT_SECONDS="${SELF_UPDATE_HEALTH_TIMEOUT_SECONDS:-90}"
CI_PENDING_ALERT_SECONDS=1800
LOG_FILE="$BOT_DIR/logs/self_update.log"
STATE_DIR="$BOT_DIR/.self-update"
UPDATING_MARKER="$BOT_DIR/.updating"   # lu par scripts/watchdog.sh
mkdir -p "$BOT_DIR/logs" "$STATE_DIR"

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') $1" | tee -a "$LOG_FILE"
}

notify() {  # titre message [niveau]
    "$PYTHON" - "$1" "$2" "${3:-info}" <<'PY' || true
import sys
from app.database import Database
Database().add_notification(sys.argv[1], sys.argv[2], level=sys.argv[3])
PY
}

notify_once() {  # clé titre message [niveau] : une seule notification par clé
    local key_file="$STATE_DIR/notified-$1"
    [ -f "$key_file" ] && return 0
    notify "$2" "$3" "${4:-info}"
    touch "$key_file"
}

killswitch_get() {
    "$PYTHON" -c 'from app.database import Database; print(Database().get_state("killswitch", "0"))'
}

killswitch_set() {
    "$PYTHON" -c "from app.database import Database; Database().set_state('killswitch', '$1')"
}

cross_dex_flag() {
    "$PYTHON" -c 'from app.config import load_config; print(load_config().get("trading", {}).get("cross_dex_flashloan_enabled"))' 2>/dev/null || echo "?"
}

# --- Exclusion mutuelle entre ticks du timer -----------------------------------
exec 9>"$BOT_DIR/.self-update.lock"
if ! flock -n 9; then
    exit 0
fi

if [ ! -d "$BOT_DIR/.git" ]; then
    log "ERREUR : $BOT_DIR n'est pas un clone git — exécuter scripts/install_self_update.sh."
    exit 1
fi

git fetch -q origin "$BRANCH"
LOCAL="$(git rev-parse HEAD)"
REMOTE="$(git rev-parse "origin/$BRANCH")"
if [ "$LOCAL" = "$REMOTE" ]; then
    exit 0
fi

if grep -qx "$REMOTE" "$STATE_DIR/rejected" 2>/dev/null; then
    # Déjà tenté et annulé (rollback) : ne pas relancer le service toutes les
    # 2 min sur un commit cassé ; un nouveau push débloque naturellement.
    exit 0
fi

if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
    log "ERREUR : fichiers suivis modifiés localement dans $BOT_DIR — mise à jour refusée tant que l'arbre n'est pas propre."
    notify_once "dirty-$REMOTE" "⛔ Mise à jour bloquée" \
        "Des fichiers suivis par git ont été modifiés directement sur le Pi ; le commit ${REMOTE:0:8} n'est pas appliqué. Voir 'git status' dans ~/bot." warning
    exit 1
fi

# --- Gate CI : n'appliquer qu'un commit dont les tests GitHub sont verts ---
if [ -n "$REQUIRED_CHECK" ]; then
    REPO="$(git remote get-url origin | sed -E 's#^(git@github\.com:|https://github\.com/)##; s#\.git$##')"
    set +e
    CI_OUTPUT="$("$PYTHON" "$BOT_DIR/scripts/ci_status.py" "$REPO" "$REMOTE" "$REQUIRED_CHECK" 2>&1)"
    CI_RC=$?
    set -e
    case "$CI_RC" in
        0) ;;
        2)
            FIRST_SEEN_FILE="$STATE_DIR/first-seen-$REMOTE"
            [ -f "$FIRST_SEEN_FILE" ] || date +%s > "$FIRST_SEEN_FILE"
            if [ $(( $(date +%s) - $(cat "$FIRST_SEEN_FILE") )) -gt "$CI_PENDING_ALERT_SECONDS" ]; then
                notify_once "ci-pending-$REMOTE" "⏳ Mise à jour en attente de la CI" \
                    "Le commit ${REMOTE:0:8} attend depuis plus de 30 min un résultat du check « $REQUIRED_CHECK » (workflow absent ou bloqué ?)." warning
            fi
            exit 0 ;;
        3)
            log "CI en échec pour ${REMOTE:0:8} — mise à jour refusée. $CI_OUTPUT"
            notify_once "ci-failed-$REMOTE" "⛔ Mise à jour refusée : tests en échec" \
                "Le commit ${REMOTE:0:8} a des tests GitHub Actions en échec ; le Pi reste sur ${LOCAL:0:8}." warning
            exit 0 ;;
        *)
            log "Statut CI indisponible pour ${REMOTE:0:8} (code $CI_RC) — nouvelle tentative au prochain tick. $CI_OUTPUT"
            exit 0 ;;
    esac
fi

COMMIT_COUNT="$(git rev-list --count "$LOCAL..$REMOTE" 2>/dev/null || echo "?")"
log "Mise à jour ${LOCAL:0:8} -> ${REMOTE:0:8} ($COMMIT_COUNT commit(s))."
touch "$UPDATING_MARKER"
trap 'rm -f "$UPDATING_MARKER"' EXIT

# --- Sauvegarde (même méthode que scripts/deploy.sh) ---------------------------
STAMP="$(date '+%Y%m%d_%H%M%S')"
BACKUP="$BOT_DIR/deploy-backups/auto-$STAMP"
mkdir -p "$BACKUP"
"$PYTHON" - "$BACKUP/reward_hunter.db" <<'PY'
import sqlite3
import sys

from app.config import load_config

db_path = load_config().get("database", {}).get("path", "data/reward_hunter.db")
with sqlite3.connect(db_path, timeout=30) as source, sqlite3.connect(sys.argv[1]) as destination:
    source.backup(destination)
    integrity = destination.execute("PRAGMA integrity_check").fetchone()
    if not integrity or integrity[0] != "ok":
        raise RuntimeError(f"Backup SQLite invalide: {integrity!r}")
PY
cp -p config/config.yaml "$BACKUP/config.yaml"
echo "$LOCAL" > "$BACKUP/commit.txt"

CROSS_DEX_BEFORE="$(cross_dex_flag)"
PREVIOUS_KILLSWITCH="$(killswitch_get)"
PERPS_WAS_ACTIVE=0
if systemctl --user is-active --quiet "$PERPS_SERVICE" 2>/dev/null; then
    PERPS_WAS_ACTIVE=1
    systemctl --user stop "$PERPS_SERVICE"
fi
killswitch_set 1
systemctl --user stop "$SERVICE"

apply_commit() {  # commit_cible commit_de_reference
    git reset -q --hard "$1"
    chmod +x scripts/*.sh 2>/dev/null || true
    if ! git diff --quiet "$2" "$1" -- requirements.txt; then
        log "requirements.txt modifié entre ${2:0:8} et ${1:0:8} — pip install."
        "$PIP" install -q --extra-index-url https://www.piwheels.org/simple -r requirements.txt
    fi
    if [ -x "$PERPS_VENV/bin/pip" ] && ! git diff --quiet "$2" "$1" -- requirements-perps.txt; then
        log "requirements-perps.txt modifié entre ${2:0:8} et ${1:0:8} — pip install dans .venv-perps."
        "$PERPS_VENV/bin/pip" install -q --extra-index-url https://www.piwheels.org/simple -r requirements-perps.txt
    fi
    # Passage simulation -> cross-DEX live : décision explicite exigée (même
    # garde-fou que deploy.sh --enable-cross-dex-live), via un marqueur posé
    # une fois à la main sur le Pi.
    if [ "$CROSS_DEX_BEFORE" != "True" ] && [ "$(cross_dex_flag)" = "True" ] \
            && [ ! -f "$BOT_DIR/.allow-cross-dex-live" ]; then
        log "Le commit ${1:0:8} active trading.cross_dex_flashloan_enabled sans marqueur .allow-cross-dex-live — refus."
        return 5
    fi
    "$PYTHON" -m compileall -q app main.py perps_main.py
    "$PIP" check >/dev/null
    "$PYTHON" -c 'import main, app.scheduler, app.dashboard, app.trading.executor'
    if [ -x "$PERPS_VENV/bin/python" ]; then
        "$PERPS_VENV/bin/python" -c 'import perps_main, app.perps.service, app.perps.dashboard'
    fi
    "$PYTHON" - <<'PY'
from app.config import load_config
from app.database import Database
from app.trading.runtime_settings import get_runtime_settings, save_runtime_settings

db = Database()
save_runtime_settings(db, get_runtime_settings(db, load_config()))
PY
}

start_and_wait_healthy() {
    systemctl --user start "$SERVICE"
    for _ in $(seq 1 "$HEALTH_TIMEOUT_SECONDS"); do
        # HTTP 200 ne suffit pas : la base doit répondre (le statut peut rester
        # « stale » jusqu'au premier scan complet, ce n'est pas un échec).
        if curl -fsS --max-time 5 "$HEALTH_URL" 2>/dev/null | grep -q '"database_ok":true'; then
            return 0
        fi
        sleep 1
    done
    return 1
}

restart_perps_if_needed() {
    [ "$PERPS_WAS_ACTIVE" -eq 1 ] || return 0
    systemctl --user start "$PERPS_SERVICE" || true
    sleep 5
    if systemctl --user is-active --quiet "$PERPS_SERVICE"; then
        log "$PERPS_SERVICE redémarré."
    else
        log "AVERTISSEMENT : $PERPS_SERVICE ne redémarre pas (voir logs/perps.log) — le bot d'arbitrage n'est pas concerné."
        notify "⚠️ Service perps papier arrêté" \
            "perps-paper.service n'a pas redémarré après la mise à jour ${REMOTE:0:8} ; voir logs/perps.log." warning
    fi
}

set +e
apply_commit "$REMOTE" "$LOCAL" && start_and_wait_healthy
RC=$?
set -e

if [ "$RC" -ne 0 ]; then
    log "ÉCHEC de la mise à jour vers ${REMOTE:0:8} (code $RC) — retour à ${LOCAL:0:8}."
    echo "$REMOTE" >> "$STATE_DIR/rejected"
    systemctl --user stop "$SERVICE" || true
    set +e
    apply_commit "$LOCAL" "$REMOTE" && start_and_wait_healthy
    ROLLBACK_RC=$?
    set -e
    restart_perps_if_needed
    if [ "$ROLLBACK_RC" -eq 0 ]; then
        killswitch_set "$PREVIOUS_KILLSWITCH"
        log "Retour à ${LOCAL:0:8} réussi ; killswitch restauré à $PREVIOUS_KILLSWITCH."
        notify "⛔ Mise à jour automatique annulée" \
            "Le commit ${REMOTE:0:8} n'a pas démarré sainement (code $RC) ; retour à ${LOCAL:0:8}, killswitch restauré à $PREVIOUS_KILLSWITCH. Sauvegarde : $BACKUP." warning
        exit 1
    fi
    log "ROLLBACK EN ÉCHEC (code $ROLLBACK_RC) — killswitch laissé actif, intervention manuelle requise."
    notify "🛑 Mise à jour automatique : rollback en échec" \
        "Ni ${REMOTE:0:8} ni ${LOCAL:0:8} ne démarrent sainement ; killswitch laissé actif. Sauvegarde : $BACKUP." alert
    exit 1
fi

killswitch_set "$PREVIOUS_KILLSWITCH"
restart_perps_if_needed
log "Mise à jour appliquée : ${REMOTE:0:8} (killswitch restauré à $PREVIOUS_KILLSWITCH)."
notify "⬆️ Mise à jour automatique appliquée" \
    "${LOCAL:0:8} -> ${REMOTE:0:8} ($COMMIT_COUNT commit(s)) : $(git log -1 --format=%s "$REMOTE"). Service redémarré sain, killswitch restauré à $PREVIOUS_KILLSWITCH." info
