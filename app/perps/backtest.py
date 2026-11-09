"""Backtest de la stratégie tendance perps sur bougies Veranta (feed TradingView).

Pourquoi : le papier accumule ~3 trades par semaine ; il faudrait des mois
pour juger la stratégie. Les bougies (1 an en 1 h, 2 ans en 4 h, 4 ans en
journalier) donnent des milliers de barres tout de suite. Le simulateur
réutilise le modèle de coûts du moteur papier (spread contre le trader à
l'ouverture et à la clôture, frais d'ouverture/clôture, funding et rollover
par heure, liquidation à 85 % du collatéral) et les mêmes règles d'entrée/
sortie que `PaperEngine` — un test de parité le garantit en mode « clôture
seule ». Le mode intra-bougie (défaut) déclenche stop/objectif sur les
plus haut/plus bas, stop d'abord quand les deux sont touchés (prudent).

Limites : un seul marché par simulation (pas de plafond global de positions),
coûts constants sur la période, pas de glissement au-delà du spread coté.
"""
from __future__ import annotations

import itertools
import json
import logging
import math
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Awaitable, Callable

from app.perps.engine import gross_pnl_usd, liquidation_price
from app.perps.models import PairState, TrendConfig

logger = logging.getLogger("app.perps.backtest")

RESOLUTION_SECONDS = {"60": 3600, "240": 14400, "D": 86400}
DEFAULT_CACHE_DIR = Path("data/perps_candles")


@dataclass(frozen=True)
class Candle:
    time: float   # début de la bougie, secondes UTC
    open: float
    high: float
    low: float
    close: float


# ------------------------------------------------------------------ bougies
def feed_symbol(symbol: str) -> str:
    """`ETH/USD` -> `Crypto.ETH/USD` (symbole du shim TradingView Veranta)."""
    return symbol if "." in symbol else f"Crypto.{symbol}"


def candles_from_feed(raw: Any) -> list[Candle]:
    """Normalise la réponse du feed (liste de dicts, `time` en millisecondes)."""
    rows = raw.get("candles") if isinstance(raw, dict) else raw
    out: list[Candle] = []
    for row in rows or []:
        try:
            t = float(row["time"])
            candle = Candle(
                time=t / 1000 if t > 1e11 else t, open=float(row["open"]), high=float(row["high"]),
                low=float(row["low"]), close=float(row["close"]),
            )
        except (KeyError, TypeError, ValueError):
            continue
        if candle.low <= 0 or candle.close <= 0 or not all(math.isfinite(v) for v in (candle.open, candle.high, candle.low, candle.close)):
            continue
        out.append(candle)
    out.sort(key=lambda c: c.time)
    return out


def cache_path(symbol: str, resolution: str, cache_dir: Path = DEFAULT_CACHE_DIR) -> Path:
    return Path(cache_dir) / f"{symbol.replace('/', '-').replace('.', '_')}_{resolution}.json"


def read_cache(path: Path) -> list[Candle]:
    if not path.exists():
        return []
    try:
        return candles_from_feed(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return []


def write_cache(path: Path, candles: list[Candle]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = [{"time": c.time * 1000, "open": c.open, "high": c.high, "low": c.low, "close": c.close} for c in candles]
    path.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")


Fetcher = Callable[[str, str, int, int], Awaitable[Any]]


async def sdk_fetcher(symbol: str, resolution: str, start: int, end: int) -> Any:
    from veranta_sdk import AsyncVeranta  # venv perps uniquement

    async with AsyncVeranta() as client:
        return await client.markets.candles(symbol, resolution, start, end)


async def load_candles(symbol: str, resolution: str, days: float, *, now: float, cache_dir: Path = DEFAULT_CACHE_DIR,
                       fetcher: Fetcher | None = sdk_fetcher) -> list[Candle]:
    """Bougies clôturées sur [now − days, now], via le cache JSON complété par
    le feed (uniquement la partie manquante). `fetcher=None` = cache seul."""
    if resolution not in RESOLUTION_SECONDS:
        raise ValueError(f"Résolution inconnue {resolution!r} (attendu : {', '.join(RESOLUTION_SECONDS)}).")
    bar = RESOLUTION_SECONDS[resolution]
    start = now - days * 86400
    path = cache_path(symbol, resolution, cache_dir)
    cached = read_cache(path)
    merged = {c.time: c for c in cached}
    if fetcher is not None:
        ranges: list[tuple[int, int]] = []
        if not cached or cached[0].time > start + bar:
            ranges.append((int(start), int(now)))
        elif cached[-1].time + bar < now - bar:
            ranges.append((int(cached[-1].time), int(now)))
        for lo, hi in ranges:
            fetched = candles_from_feed(await fetcher(feed_symbol(symbol), resolution, lo, hi))
            logger.info("%s %s : %d bougies reçues du feed pour [%d, %d].", symbol, resolution, len(fetched), lo, hi)
            merged.update({c.time: c for c in fetched})
        if merged:
            write_cache(path, sorted(merged.values(), key=lambda c: c.time))
    # On ne garde que les bougies clôturées : la dernière du feed est en cours.
    return [c for c in sorted(merged.values(), key=lambda c: c.time) if start <= c.time and c.time + bar <= now]


# --------------------------------------------------------------- paramètres
@dataclass(frozen=True)
class CostModel:
    """Coûts constants sur la période (valeurs Veranta ETH/USD du 09/11/2026 par défaut)."""
    spread_pct: float = 0.03
    open_fee_pct: float = 0.045
    close_fee_pct: float = 0.045
    funding_long_pct_h: float = 0.0012     # long paie ~10,5 %/an
    funding_short_pct_h: float = -0.0012   # short encaisse ~10,5 %/an
    rollover_pct_h: float = 0.0

    @classmethod
    def from_pair_state(cls, state: PairState) -> "CostModel":
        return cls(
            spread_pct=state.spread_pct, open_fee_pct=state.open_fee_pct, close_fee_pct=state.close_fee_pct,
            funding_long_pct_h=state.funding_long_pct_h, funding_short_pct_h=state.funding_short_pct_h,
            rollover_pct_h=state.rollover_pct_h,
        )

    def funding_pct_h(self, side: str) -> float:
        return self.funding_long_pct_h if side == "long" else self.funding_short_pct_h

    def execution_price(self, index_price: float, side: str, *, opening: bool) -> float:
        buying = (side == "long") == opening
        return index_price * (1 + self.spread_pct / 100 if buying else 1 - self.spread_pct / 100)


@dataclass(frozen=True)
class TrendParams:
    lookback_minutes: int = 240
    confirm_minutes: int = 60
    entry_pct: float = 1.0
    stop_loss_pct: float = 1.5
    take_profit_pct: float = 3.0
    max_hold_hours: float = 24.0
    reentry_cooldown_minutes: int = 120
    daily_loss_limit_usd: float = 5.0
    notional_usd: float = 100.0
    leverage: float = 2.0

    @classmethod
    def from_trend_config(cls, cfg: TrendConfig) -> "TrendParams":
        return cls(
            lookback_minutes=cfg.lookback_minutes, confirm_minutes=cfg.confirm_minutes, entry_pct=cfg.entry_pct,
            stop_loss_pct=cfg.stop_loss_pct, take_profit_pct=cfg.take_profit_pct, max_hold_hours=cfg.max_hold_hours,
            reentry_cooldown_minutes=cfg.reentry_cooldown_minutes, daily_loss_limit_usd=cfg.daily_loss_limit_usd,
            notional_usd=cfg.notional_usd, leverage=cfg.leverage,
        )

    def with_overrides(self, **overrides: Any) -> "TrendParams":
        return replace(self, **overrides)


# --------------------------------------------------------------- simulation
@dataclass
class Trade:
    side: str
    opened_at: float
    closed_at: float
    entry_price: float
    exit_price: float
    notional_usd: float
    collateral_usd: float
    gross_pnl_usd: float
    fees_usd: float
    funding_usd: float
    rollover_usd: float
    net_pnl_usd: float
    exit_reason: str
    bars_held: int


@dataclass
class _Open:
    side: str
    opened_at: float
    entry_price: float
    notional: float
    collateral: float
    open_fee: float
    funding: float = 0.0
    rollover: float = 0.0
    bars: int = 0


@dataclass
class BacktestResult:
    symbol: str
    resolution: str
    params: TrendParams
    costs: CostModel
    start: float
    end: float
    bars: int
    trades: list[Trade] = field(default_factory=list)

    def metrics(self) -> dict[str, Any]:
        return compute_metrics(self.trades, self.bars, self.start, self.end)


def trend_signal(price: float, reference: float, confirm: float, entry_pct: float) -> str | None:
    """Même règle que `PaperEngine._trend_signal`."""
    if reference <= 0 or confirm <= 0:
        return None
    lookback_return = price / reference - 1
    confirm_return = price / confirm - 1
    threshold = entry_pct / 100
    if lookback_return >= threshold and confirm_return > 0:
        return "long"
    if lookback_return <= -threshold and confirm_return < 0:
        return "short"
    return None


def _bars_for(minutes: float, bar_seconds: float) -> int:
    return max(1, int(round(minutes * 60 / bar_seconds)))


def simulate_trend(candles: list[Candle], params: TrendParams, costs: CostModel, *, symbol: str = "?",
                   resolution: str = "?", intrabar: bool = True) -> BacktestResult:
    """Un tick par clôture de bougie, comme le moteur papier (accrual → sorties →
    entrées). `intrabar=False` n'évalue les sorties qu'au prix de clôture :
    c'est le comportement exact du moteur, utilisé par le test de parité."""
    if len(candles) < 2:
        return BacktestResult(symbol, resolution, params, costs, 0.0, 0.0, len(candles))
    bar_seconds = candles[1].time - candles[0].time
    lookback_bars = _bars_for(params.lookback_minutes, bar_seconds)
    confirm_bars = _bars_for(params.confirm_minutes, bar_seconds)
    bar_hours = bar_seconds / 3600
    result = BacktestResult(symbol, resolution, params, costs, candles[0].time, candles[-1].time + bar_seconds, len(candles))
    position: _Open | None = None
    last_closed_at: float | None = None
    day_pnl: dict[float, float] = {}

    def close(pos: _Open, now: float, index_exit: float, reason: str, *, liquidated: bool = False) -> None:
        nonlocal position, last_closed_at
        price = index_exit if liquidated else costs.execution_price(index_exit, pos.side, opening=False)
        close_fee = 0.0 if liquidated else pos.notional * costs.close_fee_pct / 100
        gross = gross_pnl_usd(pos.side, pos.entry_price, price, pos.notional)
        net = -pos.collateral if liquidated else gross - (pos.open_fee + close_fee + pos.funding + pos.rollover)
        result.trades.append(Trade(
            side=pos.side, opened_at=pos.opened_at, closed_at=now, entry_price=pos.entry_price, exit_price=price,
            notional_usd=pos.notional, collateral_usd=pos.collateral, gross_pnl_usd=gross,
            fees_usd=pos.open_fee + close_fee, funding_usd=pos.funding, rollover_usd=pos.rollover, net_pnl_usd=net,
            exit_reason=reason, bars_held=pos.bars,
        ))
        day = now - now % 86400
        day_pnl[day] = day_pnl.get(day, 0.0) + net
        position, last_closed_at = None, now

    for i, bar in enumerate(candles):
        now = bar.time + bar_seconds
        if position is not None:
            pos = position
            pos.bars += 1
            pos.funding += pos.notional * costs.funding_pct_h(pos.side) / 100 * bar_hours
            pos.rollover += pos.notional * costs.rollover_pct_h / 100 * bar_hours
            liq = liquidation_price(pos.side, pos.entry_price, pos.collateral, pos.notional,
                                    fees_usd=pos.open_fee + pos.funding + pos.rollover)
            sign = 1 if pos.side == "long" else -1
            stop_level = pos.entry_price * (1 - sign * params.stop_loss_pct / 100)
            tp_level = pos.entry_price * (1 + sign * params.take_profit_pct / 100)
            if intrabar:
                worst, best = (bar.low, bar.high) if pos.side == "long" else (bar.high, bar.low)
                if (worst <= liq) if pos.side == "long" else (worst >= liq):
                    close(pos, now, liq, "liquidation", liquidated=True)
                elif (worst <= stop_level) if pos.side == "long" else (worst >= stop_level):
                    # Stop d'abord si stop et objectif sont touchés ; un gap à
                    # l'ouverture au-delà du stop est exécuté au prix d'ouverture.
                    fill = min(stop_level, bar.open) if pos.side == "long" else max(stop_level, bar.open)
                    close(pos, now, fill, "stop_loss")
                elif (best >= tp_level) if pos.side == "long" else (best <= tp_level):
                    close(pos, now, tp_level, "take_profit")
            else:
                move = (bar.close / pos.entry_price - 1) * sign
                if (bar.close <= liq) if pos.side == "long" else (bar.close >= liq):
                    close(pos, now, liq, "liquidation", liquidated=True)
                elif move <= -params.stop_loss_pct / 100:
                    close(pos, now, bar.close, "stop_loss")
                elif move >= params.take_profit_pct / 100:
                    close(pos, now, bar.close, "take_profit")
            if position is not None and (now - pos.opened_at) / 3600 >= params.max_hold_hours:
                close(pos, now, bar.close, "duree_max")

        if position is None and i >= lookback_bars:
            if day_pnl.get(now - now % 86400, 0.0) <= -params.daily_loss_limit_usd:
                continue
            if last_closed_at is not None and now - last_closed_at < params.reentry_cooldown_minutes * 60:
                continue
            side = trend_signal(bar.close, candles[i - lookback_bars].close, candles[i - confirm_bars].close, params.entry_pct)
            if side is None:
                continue
            entry = costs.execution_price(bar.close, side, opening=True)
            position = _Open(
                side=side, opened_at=now, entry_price=entry, notional=params.notional_usd,
                collateral=params.notional_usd / params.leverage, open_fee=params.notional_usd * costs.open_fee_pct / 100,
            )
    return result


# ------------------------------------------------------------------ mesures
def compute_metrics(trades: list[Trade], bars: int, start: float, end: float) -> dict[str, Any]:
    nets = [t.net_pnl_usd for t in trades]
    wins = [n for n in nets if n > 0]
    losses = [n for n in nets if n <= 0]
    gross_profit = sum(wins)
    gross_loss = -sum(losses)
    peak = running = 0.0
    max_dd = 0.0
    for n in nets:
        running += n
        peak = max(peak, running)
        max_dd = max(max_dd, peak - running)
    by_reason: dict[str, int] = {}
    for t in trades:
        by_reason[t.exit_reason] = by_reason.get(t.exit_reason, 0) + 1
    months = max((end - start) / (30 * 86400), 1e-9)
    return {
        "trades": len(trades),
        "win_rate_pct": 100 * len(wins) / len(trades) if trades else None,
        "profit_factor": (gross_profit / gross_loss) if gross_loss > 0 else (math.inf if gross_profit > 0 else None),
        "net_pnl_usd": sum(nets),
        "net_per_month_usd": sum(nets) / months,
        "gross_pnl_usd": sum(t.gross_pnl_usd for t in trades),
        "fees_usd": sum(t.fees_usd for t in trades),
        "funding_usd": sum(t.funding_usd for t in trades),
        "rollover_usd": sum(t.rollover_usd for t in trades),
        "avg_trade_usd": sum(nets) / len(trades) if trades else None,
        "max_drawdown_usd": max_dd,
        "exposure_pct": 100 * sum(t.bars_held for t in trades) / bars if bars else None,
        "liquidations": by_reason.get("liquidation", 0),
        "exits": by_reason,
        "bars": bars,
        "months": months,
    }


# ------------------------------------------------------- grille / walk-forward
DEFAULT_GRID: dict[str, tuple[Any, ...]] = {
    "lookback_minutes": (240, 480, 1440),
    "entry_pct": (0.5, 1.0, 2.0, 3.0),
    "stop_loss_pct": (1.0, 1.5, 2.5, 4.0),
    "take_profit_pct": (2.0, 3.0, 5.0, 8.0),
}


def grid_combinations(grid: dict[str, tuple[Any, ...]] | None = None) -> list[dict[str, Any]]:
    grid = grid or DEFAULT_GRID
    keys = list(grid)
    return [dict(zip(keys, values)) for values in itertools.product(*(grid[k] for k in keys))]


def walk_forward(candles: list[Candle], base: TrendParams, costs: CostModel, *, symbol: str = "?", resolution: str = "?",
                 grid: dict[str, tuple[Any, ...]] | None = None, train_ratio: float = 0.7, min_trades: int = 10,
                 intrabar: bool = True, top: int = 10) -> dict[str, Any]:
    """Optimise sur les premiers `train_ratio` de l'historique, juge le reste.
    Le verdict qui compte est la ligne `best.test` : si elle est négative alors
    que l'entraînement était positif, la grille n'a fait que sur-apprendre."""
    if len(candles) < 10:
        raise ValueError("Pas assez de bougies pour un walk-forward.")
    bar_seconds = candles[1].time - candles[0].time
    split = int(len(candles) * train_ratio)
    combos = grid_combinations(grid)
    lookbacks = [base.lookback_minutes] + [c.get("lookback_minutes", base.lookback_minutes) for c in combos]
    warmup = max(_bars_for(minutes, bar_seconds) for minutes in lookbacks)
    train, test = candles[:split], candles[max(0, split - warmup):]
    test_start = candles[split].time

    def run(params: TrendParams, data: list[Candle], *, since: float | None = None) -> dict[str, Any]:
        res = simulate_trend(data, params, costs, symbol=symbol, resolution=resolution, intrabar=intrabar)
        if since is not None:   # ignore les trades ouverts pendant la période de chauffe
            res.trades = [t for t in res.trades if t.opened_at >= since]
            res.start, res.bars = since, len([c for c in data if c.time >= since])
        return res.metrics()

    baseline = {"params": asdict(base), "train": run(base, train), "test": run(base, test, since=test_start)}
    rows: list[dict[str, Any]] = []
    for combo in combos:
        params = base.with_overrides(**combo)
        train_metrics = run(params, train)
        rows.append({"params": combo, "train": train_metrics})
    eligible = [r for r in rows if r["train"]["trades"] >= min_trades]
    ranked = sorted(eligible or rows, key=lambda r: r["train"]["net_pnl_usd"], reverse=True)
    for row in ranked[:top]:
        row["test"] = run(base.with_overrides(**row["params"]), test, since=test_start)
    best = ranked[0] if ranked else None
    positive_oos = sum(1 for r in ranked[:top] if r.get("test", {}).get("net_pnl_usd", 0) > 0)
    return {
        "symbol": symbol, "resolution": resolution, "intrabar": intrabar, "bars": len(candles),
        "train_period": [candles[0].time, test_start], "test_period": [test_start, candles[-1].time + bar_seconds],
        "combinations": len(rows), "min_trades": min_trades,
        "baseline": baseline,
        "best": best,
        "top": ranked[:top],
        "top_positive_out_of_sample": f"{positive_oos}/{min(top, len(ranked))}",
    }


def verdict(report: dict[str, Any]) -> str:
    """Lecture humaine du walk-forward."""
    base_test = report["baseline"]["test"]
    best = report.get("best") or {}
    best_test = best.get("test") or {}
    lines = [
        f"{report['symbol']} {report['resolution']} — {report['bars']} bougies, {report['combinations']} combinaisons, "
        f"test hors échantillon {report['top_positive_out_of_sample']} positives parmi les meilleures à l'entraînement.",
        f"  Config actuelle : entraînement {report['baseline']['train']['net_pnl_usd']:+.2f} $ ({report['baseline']['train']['trades']} trades), "
        f"test {base_test['net_pnl_usd']:+.2f} $ ({base_test['trades']} trades, PF {_pf(base_test)}, DD max {base_test['max_drawdown_usd']:.2f} $).",
    ]
    if best:
        lines.append(
            f"  Meilleure à l'entraînement {best['params']} : {best['train']['net_pnl_usd']:+.2f} $ "
            f"({best['train']['trades']} trades) → test {best_test.get('net_pnl_usd', 0):+.2f} $ "
            f"({best_test.get('trades', 0)} trades, PF {_pf(best_test)})."
        )
    if base_test["net_pnl_usd"] <= 0 and best_test.get("net_pnl_usd", 0) <= 0:
        lines.append("  Verdict : aucune variante ne gagne hors échantillon — la stratégie n'a pas d'avantage mesurable ici.")
    elif best_test.get("net_pnl_usd", 0) > 0 and base_test["net_pnl_usd"] <= 0:
        lines.append("  Verdict : la config actuelle perd hors échantillon mais la variante optimisée tient — à confirmer sur un autre marché/résolution avant de changer la config.")
    else:
        lines.append("  Verdict : la config actuelle est positive hors échantillon — avantage plausible mais à relativiser (coûts constants, 1 marché).")
    return "\n".join(lines)


def _pf(metrics: dict[str, Any]) -> str:
    pf = metrics.get("profit_factor")
    if pf is None:
        return "n/a"
    return "∞" if pf == math.inf else f"{pf:.2f}"
