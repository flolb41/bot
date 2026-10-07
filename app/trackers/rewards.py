"""Suivi des récompenses reçues (app/trackers/rewards.py, section 6 et 14 du TODO)."""
from __future__ import annotations

from app.database import Database


def total_received(db: Database, token: str | None = None) -> float:
    rewards = db.list_rewards(status="received")
    if token:
        rewards = [r for r in rewards if r["token"] == token]
    return sum(r["amount"] for r in rewards)


def pending_rewards(db: Database) -> list[dict]:
    return db.list_rewards(status="pending")


def rewards_summary(db: Database) -> dict:
    """Résumé utilisé par /rewards et le dashboard."""
    all_rewards = db.list_rewards()
    received = [r for r in all_rewards if r["status"] == "received"]
    pending = [r for r in all_rewards if r["status"] == "pending"]
    converted = [r for r in all_rewards if r["status"] == "converted"]

    by_token: dict[str, float] = {}
    for r in received + converted:
        by_token[r["token"]] = by_token.get(r["token"], 0.0) + r["amount"]

    return {
        "nb_received": len(received),
        "nb_pending": len(pending),
        "nb_converted": len(converted),
        "by_token": by_token,
        "total_gas_spent": sum(tx["gas_cost"] for tx in db.list_transactions()),
    }
