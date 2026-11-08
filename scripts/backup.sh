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

if [ ! -x "$PROJECT_DIR/.venv/bin/python" ]; then
    echo "Interpréteur Python du bot introuvable: $PROJECT_DIR/.venv/bin/python" >&2
    exit 1
fi

"$PROJECT_DIR/.venv/bin/python" - "$DB_PATH" "$DEST" <<'PY'
import sqlite3
import sys

source_path, destination_path = sys.argv[1:]
with sqlite3.connect(source_path, timeout=30) as source:
    with sqlite3.connect(destination_path) as destination:
        source.backup(destination)
        result = destination.execute("PRAGMA integrity_check").fetchone()
        if not result or result[0] != "ok":
            raise RuntimeError(f"Backup SQLite invalide: {result!r}")
PY

echo ">> Sauvegarde créée : $DEST"

find "$BACKUP_DIR" -name "reward_hunter_*.db" -mtime "+$RETENTION_DAYS" -delete
echo ">> Anciennes sauvegardes (> $RETENTION_DAYS j) supprimées."
