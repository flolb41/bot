"""Killswitch global (section 8 Phase 4 et section 12 du TODO).

Un simple flag en base (`system_state`) contrôlé par la commande Telegram
/stop. Tant que le killswitch est actif, le scheduler ne doit exécuter
aucune action (scan, notification de scan automatique, et a fortiori toute
interaction on-chain future des phases 3/4).
"""
from __future__ import annotations

from app.database import Database

STATE_KEY = "killswitch"


def is_stopped(db: Database) -> bool:
    return db.get_state(STATE_KEY, "0") == "1"


def stop(db: Database) -> None:
    db.set_state(STATE_KEY, "1")


def resume(db: Database) -> None:
    db.set_state(STATE_KEY, "0")
