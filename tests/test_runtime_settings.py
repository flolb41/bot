from pathlib import Path
import time

import pytest
from fastapi.testclient import TestClient

from app.database import Database
from app.dashboard import create_app
from app.trading.runtime_settings import (
    apply_runtime_settings,
    get_runtime_settings,
    save_runtime_settings,
    validate_runtime_settings,
)


def _settings(**overrides):
    return {
        "min_net_profit_usd": 0.01,
        "min_net_profit_pct": 0.5,
        "flashloan_min_net_profit_usd": 0.25,
        "min_liquidity_usd": 10_000.0,
        "flashloan_enabled": True,
        **overrides,
    }


def test_runtime_settings_are_bounded_and_saved_in_database(tmp_path: Path):
    db = Database(tmp_path / "runtime.db")
    original = {"trading": {"min_net_profit_usd": 0.01, "flashloan_enabled": False}}

    saved = save_runtime_settings(db, _settings(min_net_profit_usd=0.05))
    effective = apply_runtime_settings(db, original)

    assert saved["min_net_profit_usd"] == 0.05
    assert effective["trading"]["min_net_profit_usd"] == 0.05
    assert effective["trading"]["flashloan_enabled"] is True
    assert original["trading"]["min_net_profit_usd"] == 0.01


@pytest.mark.parametrize("overrides", [
    {"min_net_profit_usd": -0.001},
    {"flashloan_min_net_profit_usd": -0.001},
    {"min_net_profit_usd": 5.01},
    {"min_net_profit_pct": float("nan")},
    {"min_liquidity_usd": 9999},
    {"unexpected": 1},
])
def test_runtime_settings_reject_invalid_values(overrides):
    with pytest.raises(ValueError):
        validate_runtime_settings(_settings(**overrides))


def test_persisted_zero_profit_thresholds_fall_back_to_static_floor(tmp_path: Path):
    db = Database(tmp_path / "runtime.db")
    db.set_state(
        "runtime_trading_settings",
        '{"flashloan_enabled":true,"flashloan_min_net_profit_usd":0.0,'
        '"min_liquidity_usd":10000.0,"min_net_profit_pct":0.0,'
        '"min_net_profit_usd":0.0}',
    )
    config = {
        "trading": {
            "min_net_profit_usd": 0.01,
            "min_net_profit_pct": 0.5,
            "flashloan_min_net_profit_usd": 0.25,
            "flashloan_enabled": False,
        },
    }

    settings = get_runtime_settings(db, config)

    assert settings["min_net_profit_usd"] == 0.01
    assert settings["min_net_profit_pct"] == 0.5
    assert settings["flashloan_min_net_profit_usd"] == 0.25
    assert settings["flashloan_enabled"] is False


def test_runtime_settings_api_requires_token_and_same_origin(tmp_path: Path, monkeypatch):
    db = Database(tmp_path / "dashboard.db")
    monkeypatch.setenv("DASHBOARD_CONTROL_TOKEN", "test-control-token")
    client = TestClient(create_app(db))
    origin = {"Origin": "http://testserver"}

    missing = client.put("/api/runtime-settings", json=_settings(), headers=origin)
    assert missing.status_code == 401

    wrong_origin = client.put(
        "/api/runtime-settings", json=_settings(),
        headers={**origin, "X-Dashboard-Control-Token": "test-control-token",
                 "Origin": "http://evil.example"},
    )
    assert wrong_origin.status_code == 403

    rejected = client.put(
        "/api/runtime-settings",
        json=_settings(min_net_profit_usd=0.0, flashloan_min_net_profit_usd=0.0),
        headers={**origin, "X-Dashboard-Control-Token": "test-control-token"},
    )
    assert rejected.status_code == 422
    rejected_below_floor = client.put(
        "/api/runtime-settings",
        json=_settings(min_net_profit_pct=0.25),
        headers={**origin, "X-Dashboard-Control-Token": "test-control-token"},
    )
    assert rejected_below_floor.status_code == 422

    saved = client.put(
        "/api/runtime-settings",
        json=_settings(),
        headers={**origin, "X-Dashboard-Control-Token": "test-control-token"},
    )
    assert saved.status_code == 200
    assert saved.json()["settings"]["min_net_profit_usd"] == 0.01
    assert saved.json()["settings"]["min_net_profit_pct"] == 0.5
    assert saved.json()["settings"]["flashloan_min_net_profit_usd"] == 0.25
    assert db.get_state("runtime_trading_settings") is not None


def test_dashboard_stop_control_sets_killswitch_and_audits(tmp_path: Path, monkeypatch):
    db = Database(tmp_path / "dashboard.db")
    monkeypatch.setenv("DASHBOARD_CONTROL_TOKEN", "test-control-token")
    client = TestClient(create_app(db))

    response = client.post(
        "/api/control/stop",
        headers={
            "Origin": "http://testserver",
            "X-Dashboard-Control-Token": "test-control-token",
        },
    )

    assert response.status_code == 200
    assert db.get_state("killswitch") == "1"
    assert db.list_notifications(limit=1)[0]["title"] == "Killswitch activé depuis le dashboard"


def test_dashboard_is_split_into_tabs_and_reports_runtime_controls(tmp_path: Path, monkeypatch):
    db = Database(tmp_path / "dashboard.db")
    monkeypatch.delenv("DASHBOARD_CONTROL_TOKEN", raising=False)
    page = TestClient(create_app(db)).get("/").text

    assert page.count('class="tab-panel') == 4
    assert "Vue d’ensemble" in page
    assert "Réglages de stratégie live" in page
    assert "DASHBOARD_CONTROL_TOKEN" in page
    assert "arrêt d’urgence" in page.lower()


def test_health_api_uses_scheduler_scan_timestamp(tmp_path: Path):
    db = Database(tmp_path / "health.db")
    db.set_state("last_scan_completed_at", str(time.time()))
    db.set_state("last_scan_duration_seconds", "12.5")

    response = TestClient(create_app(db)).get("/api/health")

    assert response.status_code == 200
    assert response.json()["status"] == "healthy"
    assert response.json()["last_scan_age_seconds"] <= 2
    assert response.json()["last_scan_duration_seconds"] == "12.5"
