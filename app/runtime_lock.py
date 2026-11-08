"""Verrou mono-instance par base SQLite, partagé par les points d'entrée
(`main.py run`, `perps_main.py run`)."""
from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger("app.runtime_lock")

# Références conservées pour la durée du process : fermer le fichier relâcherait le verrou.
_HANDLES: dict[str, object] = {}


def acquire_single_instance_lock(db_path: str) -> bool:
    """Garantit qu'un seul process pilote une base SQLite donnée.

    Verrou `flock` exclusif sur `<base>.lock`, relâché automatiquement par le
    noyau à la mort du process (pas de verrou orphelin après un crash ou un
    `kill -9`). Le 08/11/2026, deux instances du bot ont tourné ~35 min en
    parallèle sur la même base (cycles de scan jumeaux dans app.log,
    IntegrityError sur les paper trades) : deux schedulers live sur un même
    wallet peuvent doubler des transactions. Retourne False si une autre
    instance détient déjà le verrou ; True sur un système sans `fcntl`
    (poste de dev Windows, aucun verrou possible)."""
    try:
        import fcntl
    except ImportError:
        return True
    lock_path = Path(f"{db_path}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(lock_path, "a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.seek(0)
        other_pid = handle.read().strip() or "inconnu"
        handle.close()
        logger.error(
            "Une autre instance (pid %s) pilote déjà %s — arrêt pour ne pas faire tourner deux "
            "schedulers/exécuteurs sur le même wallet.", other_pid, db_path,
        )
        return False
    handle.seek(0)
    handle.truncate()
    handle.write(str(os.getpid()))
    handle.flush()
    _HANDLES[str(lock_path)] = handle
    return True


def release_single_instance_locks() -> None:
    """Relâche les verrous détenus par ce process (tests)."""
    for handle in _HANDLES.values():
        try:
            handle.close()  # type: ignore[attr-defined]
        except OSError:
            pass
    _HANDLES.clear()
