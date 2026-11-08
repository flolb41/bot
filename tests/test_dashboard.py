from pathlib import Path
import time

from app.database import Database
from app.dashboard import build_dashboard_html, create_app
from app.killswitch import stop
from fastapi.testclient import TestClient


def test_dashboard_shows_trading_status_and_not_reward_hunter_content(
    tmp_path: Path, monkeypatch,
):
    monkeypatch.delenv("TRADING_ENABLED", raising=False)
    monkeypatch.delenv("TRADING_LIVE_CONFIRMED", raising=False)
    db = Database(tmp_path / "trading-dashboard.db")
    stop(db)
    db.add_arbitrage_signal({
        "chain": "base", "token_symbol": "WETH", "token_address": "0xabc",
        "quote_symbol": "USDC", "quote_address": "0xdef",
        "buy_dex": "uniswap", "buy_price_usd": 2000, "buy_liquidity_usd": 50000,
        "sell_dex": "aerodrome", "sell_price_usd": 2020, "sell_liquidity_usd": 50000,
        "spread_pct": 1, "trade_size_usd": 10, "gross_profit_usd": 0.1,
        "fees_usd": 0.06, "slippage_buffer_usd": 0.02, "gas_cost_usd": 0.005,
        "net_profit_usd": 0.015, "would_execute": True,
    })
    db.add_triangular_signal({
        "chain": "base", "dex": "aerodrome",
        "token_a_symbol": "USDC", "token_a_address": "0xdef",
        "token_b_symbol": "WETH", "token_b_address": "0xabc",
        "token_c_symbol": "AERO", "token_c_address": "0x123",
        "cycle_multiplier": 1.01, "spread_pct": 1, "min_liquidity_usd": 50000,
        "trade_size_usd": 10, "gross_profit_usd": 0.1, "fees_usd": 0.06,
        "slippage_buffer_usd": 0.02, "gas_cost_usd": 0.005,
        "net_profit_usd": 0.015, "would_execute": True,
    })

    page = build_dashboard_html(db)

    assert "<h1>📊 Dashboard du bot de trading · Base</h1>" in page
    assert "Arbitrage inter-DEX" in page
    assert "Arbitrage triangulaire sur un DEX" in page
    assert "USDC → WETH → AERO → USDC" in page
    assert "P&amp;L hypothétique" in page
    assert "P&amp;L réalisé mesuré" in page
    assert "Gas on-chain mesuré" in page
    assert "P&amp;L réel" in page
    assert "Killswitch actif" in page
    assert 'role="tablist" aria-label="Sections du dashboard"' in page
    assert 'id="tab-button-trading" role="tab" aria-controls="tab-trading" aria-selected="false" tabindex="-1"' in page
    assert 'role="tabpanel" aria-labelledby="tab-button-trading"' in page
    assert "event.key==='ArrowRight'" in page
    assert "event.key==='Home'" in page
    assert "activateTab(location.hash.slice(1),false)" in page
    assert "/api/arbitrage" in page
    assert "Rentable estimé" in page
    assert "Rewards reçus" not in page
    assert "Crypto Reward Hunter" not in page


def test_dashboard_displays_cpu_temperature_and_refreshes_it(monkeypatch, tmp_path):
    monkeypatch.delenv("TRADING_ENABLED", raising=False)
    monkeypatch.delenv("TRADING_LIVE_CONFIRMED", raising=False)
    monkeypatch.setattr("app.dashboard.read_temperature_celsius", lambda: 56.7)
    db = Database(tmp_path / "temperature-dashboard.db")

    page = build_dashboard_html(db)
    response = TestClient(create_app(db)).get("/api/health")

    assert 'id="health-cpu-temp">56.7 °C' in page
    assert "d.cpu_temperature_celsius" in page
    assert response.status_code == 200
    assert response.json()["cpu_temperature_celsius"] == 56.7


def test_dashboard_reports_unavailable_cpu_temperature(monkeypatch, tmp_path):
    monkeypatch.delenv("TRADING_ENABLED", raising=False)
    monkeypatch.delenv("TRADING_LIVE_CONFIRMED", raising=False)
    monkeypatch.setattr("app.dashboard.read_temperature_celsius", lambda: None)
    db = Database(tmp_path / "temperature-dashboard.db")

    response = TestClient(create_app(db)).get("/api/health")

    assert response.status_code == 200
    assert response.json()["cpu_temperature_celsius"] is None


def test_health_counts_and_flags_unresolved_directional_positions(monkeypatch, tmp_path):
    monkeypatch.delenv("TRADING_ENABLED", raising=False)
    monkeypatch.delenv("TRADING_LIVE_CONFIRMED", raising=False)
    db = Database(tmp_path / "health-dashboard.db")
    db.execute(
        "INSERT INTO directional_live_trades "
        "(strategy, token_address, token_symbol, quote_address, dex, entry_at, "
        "entry_price_usd, stake_usd, status) "
        "VALUES ('liquid_momentum', ?, 'TEST', ?, 'aerodrome', ?, 1, 2, 'unresolved')",
        ("0x" + "1" * 40, "0x" + "2" * 40, time.time()),
    )

    response = TestClient(create_app(db)).get("/api/health")
    health = response.json()

    assert health["status"] == "degraded"
    assert health["open_positions"] == 1
    assert health["directional_open_positions"] == 1
    assert health["unresolved_positions"] == 1
    page = build_dashboard_html(db)
    assert "1 position(s) à contrôler, dont 1 non résolue(s)" in page
    assert "TEST/0x22222222" in page
