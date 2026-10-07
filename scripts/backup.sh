#!/usr/bin/env bash
# Sauvegarde horodatée de la base SQLite (section 13, semaine 4 du TODO).
# À planifier via cron (ex: 0 3 * * * pour une sauvegarde quotidienne à 3h).
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DB_PATH="$PROJECT_DIR/data/reward_hunter.db"
BACKUP_DIR="$PROJECT_DIR/data/backups"
RETENTION_DAYS=30

mkdir -p "$BACKUP_DIR"

if [ ! -f "$DB_PATH" ]; then
    echo "Base introuvable: $DB_PATH" >&2
    exit 1
fi

TIMESTAMP="$(date '+%Y%m%d_%H%M%S')"
DEST="$BACKUP_DIR/reward_hunter_${TIMESTAMP}.db"

# sqlite3 .backup est plus sûr qu'une simple copie (évite les fichiers corrompus
# si une écriture est en cours), mais `cp` est utilisé en repli si sqlite3 est absent.
if command -v sqlite3 >/dev/null 2>&1; then
    sqlite3 "$DB_PATH" ".backup '$DEST'"
else
    cp "$DB_PATH" "$DEST"
fi

echo ">> Sauvegarde créée : $DEST"

find "$BACKUP_DIR" -name "reward_hunter_*.db" -mtime "+$RETENTION_DAYS" -delete
echo ">> Anciennes sauvegardes (> $RETENTION_DAYS j) supprimées."
