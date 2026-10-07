import tempfile
from pathlib import Path

import pytest

from app.database import Database


@pytest.fixture
def db(tmp_path: Path) -> Database:
    return Database(tmp_path / "test.db")


def test_schema_creates_all_tables(db: Database):
    tables = {row["name"] for row in db.fetch_all(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    expected = {"projects", "tasks", "wallets", "rewards", "transactions",
                "points_history", "system_state"}
    assert expected.issubset(tables)


def test_upsert_project_insert_then_update(db: Database):
    project = {
        "id": "demo", "name": "Demo", "token": "DEM", "network": "testnet",
        "url": "https://demo", "official_url": "https://demo", "type": "testnet",
        "status": "actif", "requires_kyc": 0, "requires_capital": 0,
        "estimated_cost": 0.0, "notes": "",
    }
    db.upsert_project(project)
    assert db.get_project("demo")["name"] == "Demo"

    project["name"] = "Demo Updated"
    db.upsert_project(project)
    rows = db.fetch_all("SELECT * FROM projects WHERE id = ?", ("demo",))
    assert len(rows) == 1
    assert rows[0]["name"] == "Demo Updated"


def test_update_project_scan_result_detects_change(db: Database):
    db.upsert_project({"id": "p1", "name": "P1"})
    changed_first = db.update_project_scan_result("p1", "hash-a")
    assert changed_first is False  # pas de hash précédent -> pas de "changement"

    changed_second = db.update_project_scan_result("p1", "hash-b")
    assert changed_second is True

    changed_same = db.update_project_scan_result("p1", "hash-b")
    assert changed_same is False


def test_killswitch_state(db: Database):
    from app.killswitch import is_stopped, resume, stop

    assert is_stopped(db) is False
    stop(db)
    assert is_stopped(db) is True
    resume(db)
    assert is_stopped(db) is False


def test_rewards_and_transactions(db: Database):
    db.upsert_project({"id": "p1", "name": "P1"})
    db.add_reward({"project_id": "p1", "token": "USDC", "amount": 5.0, "status": "received"})
    db.add_transaction({"network": "testnet", "tx_hash": "0xabc", "gas_cost": 0.001})

    assert len(db.list_rewards(status="received")) == 1
    assert len(db.list_transactions()) == 1
