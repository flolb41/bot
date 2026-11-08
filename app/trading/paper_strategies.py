"""Paper-only research strategies evaluated from the shared DEX market scan."""
from __future__ import annotations

import logging
import math
import time

from app.database import Database
from app.trading import fees
from app.trading.executor import EXECUTABLE_QUOTE_TOKENS
from app.trading.routers import EXECUTABLE_DEXES

logger = logging.getLogger("app.trading.paper_strategies")

_STRATEGIES = {
    "liquid_momentum": {
        "min_liquidity_usd": 100_000.0,
        "min_volume_24h_usd": 500_000.0,
        "max_fdv_usd": None,
    },
    "microcap_momentum": {
        "min_liquidity_usd": 25_000.0,
        "min_volume_24h_usd": 100_000.0,
        "max_fdv_usd": 50_000_000.0,
    },
}


def _positive_config(config: dict, name: str, default: float) -> float:
    values = (config or {}).get("paper_research") or config or {}
    try:
        value = float(values.get(name, default))
    except (TypeError, ValueError):
        return default
    return value if math.isfinite(value) and value > 0 else default


def _market_candidates(groups: dict[str, list[dict]]) -> dict[tuple[str, str], dict]:
    best: dict[tuple[str, str], dict] = {}
    for pairs in groups.values():
        for pair in pairs:
            token = (pair.get("base_address") or "").lower()
            quote = (pair.get("quote_address") or "").lower()
            dex = pair.get("dex_id")
            price = pair.get("price_usd_base")
            liquidity = pair.get("liquidity_usd")
            volume = pair.get("volume_24h_usd")
            if (
                not token or token in EXECUTABLE_QUOTE_TOKENS
                or quote not in EXECUTABLE_QUOTE_TOKENS
                or dex not in EXECUTABLE_DEXES
                or price is None or price <= 0
                or liquidity is None or liquidity <= 0
            ):
                continue
            key = (token, quote)
            candidate = {
                "token_address": token,
                "token_symbol": pair.get("base_symbol") or "?",
                "quote_address": quote,
                "dex": dex,
                "price_usd": float(price),
                "liquidity_usd": float(liquidity),
                "volume_24h_usd": max(0.0, float(volume or 0)),
                "fdv_usd": pair.get("fdv_usd"),
            }
            previous = best.get(key)
            if previous is None or candidate["liquidity_usd"] > previous["liquidity_usd"]:
                best[key] = candidate
    return best


def _eligible(candidate: dict, strategy: dict) -> bool:
    if candidate["liquidity_usd"] < strategy["min_liquidity_usd"]:
        return False
    if candidate["volume_24h_usd"] < strategy["min_volume_24h_usd"]:
        return False
    max_fdv = strategy["max_fdv_usd"]
    if max_fdv is not None:
        fdv = candidate["fdv_usd"]
        if fdv is None or fdv <= 0 or fdv > max_fdv:
            return False
    return True


def _record_observation(db: Database, strategy: str, candidate: dict, now: float) -> None:
    db.execute(
        "INSERT INTO paper_market_observations "
        "(strategy, token_address, token_symbol, quote_address, dex, price_usd, liquidity_usd, "
        "volume_24h_usd, fdv_usd, observed_at, minute_bucket) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(strategy, token_address, quote_address, minute_bucket) DO UPDATE SET "
        "token_symbol=excluded.token_symbol, dex=excluded.dex, price_usd=excluded.price_usd, "
        "liquidity_usd=excluded.liquidity_usd, volume_24h_usd=excluded.volume_24h_usd, "
        "fdv_usd=excluded.fdv_usd, observed_at=excluded.observed_at",
        (
            strategy, candidate["token_address"], candidate["token_symbol"], candidate["quote_address"],
            candidate["dex"], candidate["price_usd"], candidate["liquidity_usd"],
            candidate["volume_24h_usd"], candidate["fdv_usd"], now, int(now // 60),
        ),
    )


def _close_paper_trade(db: Database, trade: dict, candidate: dict, now: float, reason: str) -> None:
    entry_value = float(trade["stake_usd"])
    exit_value = entry_value * candidate["price_usd"] / float(trade["entry_price_usd"])
    gross_pnl = exit_value - entry_value
    fee_cost = (entry_value + exit_value) * float(trade["fee_pct"]) / 100
    slippage_cost = (entry_value + exit_value) * float(trade["slippage_pct"]) / 100
    net_pnl = gross_pnl - fee_cost - slippage_cost - float(trade["roundtrip_gas_usd"])
    db.execute(
        "UPDATE paper_strategy_trades SET status='closed', exit_at=?, exit_price_usd=?, "
        "net_pnl_usd=?, exit_reason=? WHERE id=? AND status='open'",
        (now, candidate["price_usd"], net_pnl, reason, trade["id"]),
    )


def _evaluate_candidate(
    db: Database, name: str, candidate: dict, config: dict, now: float,
    gas_cost_usd: float | None,
) -> None:
    cfg = (config or {}).get("paper_research") or {}
    strategy_cfg = (cfg.get(name) or {})
    rules = {
        **_STRATEGIES[name],
        "min_liquidity_usd": _positive_config(
            strategy_cfg, "min_liquidity_usd", _STRATEGIES[name]["min_liquidity_usd"],
        ),
        "min_volume_24h_usd": _positive_config(
            strategy_cfg, "min_volume_24h_usd", _STRATEGIES[name]["min_volume_24h_usd"],
        ),
        "max_fdv_usd": (
            _positive_config(strategy_cfg, "max_fdv_usd", _STRATEGIES[name]["max_fdv_usd"])
            if _STRATEGIES[name]["max_fdv_usd"] is not None else None
        ),
    }
    eligible = _eligible(candidate, rules)
    open_trade = db.fetch_one(
        "SELECT * FROM paper_strategy_trades WHERE strategy=? AND token_address=? "
        "AND quote_address=? AND status='open' ORDER BY id DESC LIMIT 1",
        (name, candidate["token_address"], candidate["quote_address"]),
    )
    if not eligible and open_trade is None:
        return

    _record_observation(db, name, candidate, now)
    if open_trade is not None:
        change_pct = (
            candidate["price_usd"] / float(open_trade["entry_price_usd"]) - 1
        ) * 100
        held_minutes = (now - float(open_trade["entry_at"])) / 60
        take_profit = _positive_config(strategy_cfg, "take_profit_pct", 2.0)
        stop_loss = _positive_config(strategy_cfg, "stop_loss_pct", 1.5)
        max_hold_minutes = _positive_config(strategy_cfg, "max_hold_minutes", 60.0)
        reason = None
        if change_pct >= take_profit:
            reason = "take_profit"
        elif change_pct <= -stop_loss:
            reason = "stop_loss"
        elif held_minutes >= max_hold_minutes:
            reason = "max_hold"
        if reason:
            _close_paper_trade(db, open_trade, candidate, now, reason)
        return

    lookback_minutes = int(_positive_config(strategy_cfg, "lookback_minutes", 15.0))
    max_age_minutes = int(_positive_config(strategy_cfg, "max_reference_age_minutes", 20.0))
    reference = db.fetch_one(
        "SELECT price_usd, observed_at FROM paper_market_observations "
        "WHERE strategy=? AND token_address=? AND quote_address=? AND observed_at<=? "
        "AND observed_at>=? ORDER BY observed_at DESC LIMIT 1",
        (
            name, candidate["token_address"], candidate["quote_address"],
            now - lookback_minutes * 60, now - max_age_minutes * 60,
        ),
    )
    if reference is None or not eligible or gas_cost_usd is None:
        return
    change_pct = (candidate["price_usd"] / float(reference["price_usd"]) - 1) * 100
    entry_trigger = _positive_config(strategy_cfg, "entry_return_pct", 1.0)
    max_entry_return = _positive_config(strategy_cfg, "max_entry_return_pct", 5.0)
    if not entry_trigger <= change_pct <= max_entry_return:
        return

    fee_pct = fees.dex_fee_pct(candidate["dex"])
    stake_usd = _positive_config(strategy_cfg, "stake_usd", 5.0)
    try:
        # `OR IGNORE` : l'index unique partiel `idx_paper_one_open_trade` porte
        # déjà l'invariant « un seul trade ouvert par (stratégie, token,
        # quote) ». Le test `open_trade is None` ci-dessus ne protège pas
        # d'une ouverture concurrente (deux cycles de scan en parallèle le
        # 08/11/2026) : au lieu d'une IntegrityError bruyante, l'insertion
        # redondante est simplement ignorée.
        with db.connection() as conn:
            inserted = conn.execute(
                "INSERT OR IGNORE INTO paper_strategy_trades "
                "(strategy, token_address, token_symbol, quote_address, dex, entry_at, entry_price_usd, "
                "stake_usd, fee_pct, slippage_pct, roundtrip_gas_usd, entry_liquidity_usd, "
                "entry_volume_24h_usd, fdv_usd, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open')",
                (
                    name, candidate["token_address"], candidate["token_symbol"], candidate["quote_address"],
                    candidate["dex"], now, candidate["price_usd"], stake_usd, fee_pct,
                    fees.DEFAULT_SLIPPAGE_BUFFER_PCT, gas_cost_usd, candidate["liquidity_usd"],
                    candidate["volume_24h_usd"], candidate["fdv_usd"],
                ),
            ).rowcount
        if not inserted:
            logger.info(
                "Paper trade %s déjà ouvert pour %s — ouverture concurrente ignorée.",
                name, candidate["token_symbol"],
            )
    except Exception:
        logger.exception("Impossible d'ouvrir le paper trade %s pour %s.",
                         name, candidate["token_symbol"])


def run_paper_experiments(db: Database, config: dict, groups: dict[str, list[dict]]) -> None:
    """Track liquid momentum and microcap momentum without creating live orders."""
    cfg = (config or {}).get("paper_research") or {}
    if not cfg.get("enabled", True):
        return
    candidates = _market_candidates(groups)
    if not candidates:
        return
    now = time.time()
    gas_cost_usd = None
    try:
        from app.wallet.pricing import get_price_usd

        gas_cost_usd = fees.estimate_gas_cost_usd(
            get_price_usd("ethereum"),
            gas_units=int(cfg.get("roundtrip_gas_units", fees.ESTIMATED_GAS_UNITS_ROUNDTRIP)),
        )
    except Exception:
        logger.exception("Impossible d'estimer les frais paper du momentum.")

    for candidate in candidates.values():
        for strategy in _STRATEGIES:
            _evaluate_candidate(db, strategy, candidate, config, now, gas_cost_usd)

    cutoff = now - 30 * 24 * 60 * 60
    db.execute("DELETE FROM paper_market_observations WHERE observed_at < ?", (cutoff,))


def paper_strategy_stats(db: Database) -> list[dict]:
    rows = db.fetch_all(
        "SELECT strategy, COUNT(*) AS total_trades, "
        "SUM(CASE WHEN status='open' THEN 1 ELSE 0 END) AS open_trades, "
        "SUM(CASE WHEN status='closed' THEN 1 ELSE 0 END) AS closed_trades, "
        "SUM(CASE WHEN status='closed' AND net_pnl_usd > 0 THEN 1 ELSE 0 END) AS winners, "
        "SUM(CASE WHEN status='closed' THEN net_pnl_usd ELSE 0 END) AS realized_pnl_usd "
        "FROM paper_strategy_trades GROUP BY strategy ORDER BY strategy"
    )
    by_strategy = {row["strategy"]: row for row in rows}
    return [
        by_strategy.get(name, {
            "strategy": name, "total_trades": 0, "open_trades": 0, "closed_trades": 0,
            "winners": 0, "realized_pnl_usd": 0.0,
        })
        for name in _STRATEGIES
    ]


def list_paper_strategy_trades(db: Database, limit: int = 30) -> list[dict]:
    return db.fetch_all(
        "SELECT * FROM paper_strategy_trades ORDER BY COALESCE(exit_at, entry_at) DESC, id DESC LIMIT ?",
        (limit,),
    )
