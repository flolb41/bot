"""Modèles du moteur perps papier : état d'une paire, paramètres des stratégies."""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

HOURS_PER_YEAR = 24 * 365


@dataclass(frozen=True)
class PairState:
    """Photographie d'une paire perp telle que vue par le protocole à un instant.

    Toutes les grandeurs « *_pct » sont en pour-cent. Le funding est exprimé par
    côté, en % du notionnel **par heure**, signe du point de vue du trader :
    positif = coût, négatif = revenu (convention Veranta : `fundingRate.long`
    et `fundingRate.short`). Le rollover (« margin fee ») est payé par les
    deux côtés."""
    symbol: str
    price: float
    spread_pct: float
    open_fee_pct: float
    close_fee_pct: float
    funding_long_pct_h: float
    funding_short_pct_h: float
    rollover_pct_h: float
    oi_long_usd: float
    oi_short_usd: float
    min_notional_usd: float
    max_leverage: float
    market_open: bool
    observed_at: float

    def funding_pct_h(self, side: str) -> float:
        return self.funding_long_pct_h if side == "long" else self.funding_short_pct_h

    def funding_income_apr_pct(self, side: str) -> float:
        """Revenu de funding annualisé pour `side` (positif = le trader encaisse)."""
        return -self.funding_pct_h(side) * HOURS_PER_YEAR

    def rollover_apr_pct(self) -> float:
        return self.rollover_pct_h * HOURS_PER_YEAR

    def net_carry_apr_pct(self, side: str) -> float:
        """Rendement net annualisé d'un portage : funding encaissé − rollover
        payé. Les alts Veranta ont 5–15 %/an de rollover : AERO à 11 % de
        funding est une perte nette, seuls ETH/BTC (rollover 0) portent."""
        return self.funding_income_apr_pct(side) - self.rollover_apr_pct()

    def execution_price(self, side: str, *, opening: bool) -> float:
        """Prix d'exécution modélisé : le spread joue toujours contre le trader,
        à l'ouverture comme à la clôture (hypothèse prudente)."""
        adverse = 1 + self.spread_pct / 100
        favourable = 1 - self.spread_pct / 100
        buying = (side == "long") == opening
        return self.price * (adverse if buying else favourable)


def _positive(value: Any, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) and number > 0 else default


def _non_negative(value: Any, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) and number >= 0 else default


@dataclass(frozen=True)
class CarryConfig:
    """Cash-and-carry : long spot (modélisé) + short perp, revenu = funding reçu.

    Économie : une rotation coûte ≈ 0,29 % du notionnel (frais perp aller-
    retour + jambe spot) alors que le funding rapporte ≈ 10 %/an → ~10 jours
    pour amortir l'entrée. D'où (1) un critère d'entrée sur le rendement NET
    (funding − rollover), (2) une exigence de persistance du funding sur les
    dernières heures, (3) un gain attendu sur la durée maximale ≥ un multiple
    des coûts, (4) une sortie sur funding négatif confirmée pendant des heures
    et non des minutes (deux rotations inutiles en 18 h le 09/11/2026)."""
    enabled: bool = True
    markets: tuple[str, ...] = ("ETH/USD", "BTC/USD")
    notional_usd: float = 100.0
    entry_min_apr_pct: float = 6.0
    entry_persistence_hours: float = 6.0
    entry_min_edge_ratio: float = 2.0
    exit_min_apr_pct: float = 0.0
    exit_confirm_hours: float = 12.0
    max_hold_hours: float = 30 * 24
    spot_cost_pct: float = 0.07   # frais DEX 0,05 % + glissement ~0,02 % par jambe spot
    spot_gas_usd: float = 0.02    # gas Base par transaction spot

    @classmethod
    def from_dict(cls, raw: dict | None) -> "CarryConfig":
        raw = raw or {}
        return cls(
            enabled=bool(raw.get("enabled", True)),
            markets=tuple(raw.get("markets") or cls.markets),
            notional_usd=_positive(raw.get("notional_usd"), cls.notional_usd),
            entry_min_apr_pct=_non_negative(raw.get("entry_min_apr_pct"), cls.entry_min_apr_pct),
            entry_persistence_hours=_non_negative(raw.get("entry_persistence_hours"), cls.entry_persistence_hours),
            entry_min_edge_ratio=_non_negative(raw.get("entry_min_edge_ratio"), cls.entry_min_edge_ratio),
            exit_min_apr_pct=float(raw.get("exit_min_apr_pct", cls.exit_min_apr_pct)),
            exit_confirm_hours=_non_negative(raw.get("exit_confirm_hours"), cls.exit_confirm_hours),
            max_hold_hours=_positive(raw.get("max_hold_hours"), cls.max_hold_hours),
            spot_cost_pct=_non_negative(raw.get("spot_cost_pct"), cls.spot_cost_pct),
            spot_gas_usd=_non_negative(raw.get("spot_gas_usd"), cls.spot_gas_usd),
        )

    def round_trip_costs_usd(self, notional_usd: float, open_fee_pct: float, close_fee_pct: float) -> float:
        """Coût complet d'une rotation : frais perp aller-retour + deux jambes spot."""
        perp = notional_usd * (open_fee_pct + close_fee_pct) / 100
        spot = 2 * (notional_usd * self.spot_cost_pct / 100 + self.spot_gas_usd)
        return perp + spot


@dataclass(frozen=True)
class TrendConfig:
    """Tendance basse fréquence, levier faible, stop/objectif/durée max."""
    enabled: bool = True
    markets: tuple[str, ...] = ("ETH/USD", "BTC/USD", "SOL/USD")
    notional_usd: float = 100.0
    leverage: float = 2.0
    lookback_minutes: int = 240
    confirm_minutes: int = 60
    entry_pct: float = 1.0
    stop_loss_pct: float = 1.5
    take_profit_pct: float = 3.0
    max_hold_hours: float = 24.0
    reentry_cooldown_minutes: int = 120
    max_open_positions: int = 3
    daily_loss_limit_usd: float = 5.0

    @classmethod
    def from_dict(cls, raw: dict | None) -> "TrendConfig":
        raw = raw or {}
        return cls(
            enabled=bool(raw.get("enabled", True)),
            markets=tuple(raw.get("markets") or cls.markets),
            notional_usd=_positive(raw.get("notional_usd"), cls.notional_usd),
            leverage=_positive(raw.get("leverage"), cls.leverage),
            lookback_minutes=int(_positive(raw.get("lookback_minutes"), cls.lookback_minutes)),
            confirm_minutes=int(_positive(raw.get("confirm_minutes"), cls.confirm_minutes)),
            entry_pct=_positive(raw.get("entry_pct"), cls.entry_pct),
            stop_loss_pct=_positive(raw.get("stop_loss_pct"), cls.stop_loss_pct),
            take_profit_pct=_positive(raw.get("take_profit_pct"), cls.take_profit_pct),
            max_hold_hours=_positive(raw.get("max_hold_hours"), cls.max_hold_hours),
            reentry_cooldown_minutes=int(_non_negative(raw.get("reentry_cooldown_minutes"), cls.reentry_cooldown_minutes)),
            max_open_positions=max(1, int(_positive(raw.get("max_open_positions"), cls.max_open_positions))),
            daily_loss_limit_usd=_positive(raw.get("daily_loss_limit_usd"), cls.daily_loss_limit_usd),
        )


@dataclass(frozen=True)
class FundingScanConfig:
    """Balayage du funding de TOUTES les paires Veranta (un appel `snapshot`)
    pour repérer où le portage paie le plus — et alerter sur les extrêmes."""
    enabled: bool = True
    interval_minutes: float = 30.0
    alert_min_net_apr_pct: float = 15.0
    alert_cooldown_hours: float = 12.0
    retention_days: float = 30.0
    # Paires dont la jambe spot existe sur Base (portage couvert possible) ;
    # les autres extrêmes ne sont que des biais directionnels.
    spot_hedgeable: tuple[str, ...] = (
        "ETH/USD", "BTC/USD", "SOL/USD", "AERO/USD", "LINK/USD", "DOGE/USD", "VIRTUAL/USD", "AAVE/USD",
        "MORPHO/USD", "ENA/USD", "ONDO/USD", "PENDLE/USD", "LDO/USD", "UNI/USD",
    )

    @classmethod
    def from_dict(cls, raw: dict | None) -> "FundingScanConfig":
        raw = raw or {}
        return cls(
            enabled=bool(raw.get("enabled", True)),
            interval_minutes=_positive(raw.get("interval_minutes"), cls.interval_minutes),
            alert_min_net_apr_pct=_non_negative(raw.get("alert_min_net_apr_pct"), cls.alert_min_net_apr_pct),
            alert_cooldown_hours=_non_negative(raw.get("alert_cooldown_hours"), cls.alert_cooldown_hours),
            retention_days=_positive(raw.get("retention_days"), cls.retention_days),
            spot_hedgeable=tuple(raw.get("spot_hedgeable") or cls.spot_hedgeable),
        )


@dataclass(frozen=True)
class TreasuryConfig:
    """Observation (papier) du rendement que le capital oisif pourrait toucher."""
    enabled: bool = True
    interval_minutes: float = 60.0
    wallet_address: str | None = None
    rpc_url: str | None = None
    aave_pool_address: str = "0xA238Dd80C259a72e81d7e4664a9801593F98d1c5"
    usdc_address: str = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"

    @classmethod
    def from_dict(cls, raw: dict | None) -> "TreasuryConfig":
        raw = raw or {}
        wallet = raw.get("wallet_address")
        return cls(
            enabled=bool(raw.get("enabled", True)),
            interval_minutes=_positive(raw.get("interval_minutes"), cls.interval_minutes),
            wallet_address=str(wallet) if wallet else None,
            rpc_url=str(raw["rpc_url"]) if raw.get("rpc_url") else None,
            aave_pool_address=str(raw.get("aave_pool_address", cls.aave_pool_address)),
            usdc_address=str(raw.get("usdc_address", cls.usdc_address)),
        )


@dataclass(frozen=True)
class PerpsConfig:
    enabled: bool = False
    mode: str = "paper"
    interval_minutes: float = 5.0
    database_path: str = "data/perps_paper.db"
    log_file: str = "logs/perps.log"
    log_level: str = "INFO"
    dashboard_enabled: bool = True
    dashboard_host: str = "0.0.0.0"
    dashboard_port: int = 8001
    venue: str = "veranta"
    markets: tuple[str, ...] = ("ETH/USD", "BTC/USD", "SOL/USD", "AERO/USD")
    observation_retention_days: float = 30.0
    carry: CarryConfig = field(default_factory=CarryConfig)
    trend: TrendConfig = field(default_factory=TrendConfig)
    funding_scan: FundingScanConfig = field(default_factory=FundingScanConfig)
    treasury: TreasuryConfig = field(default_factory=TreasuryConfig)

    @classmethod
    def from_config(cls, config: dict | None) -> "PerpsConfig":
        raw = (config or {}).get("perps") or {}
        dashboard = raw.get("dashboard") or {}
        carry = CarryConfig.from_dict(raw.get("funding_carry"))
        trend = TrendConfig.from_dict(raw.get("trend"))
        markets = tuple(raw.get("markets") or cls.markets)
        # Les marchés des stratégies doivent être observés : on les ajoute d'office.
        for extra in (*carry.markets, *trend.markets):
            if extra not in markets:
                markets = (*markets, extra)
        return cls(
            enabled=bool(raw.get("enabled", False)),
            mode=str(raw.get("mode", "paper")),
            interval_minutes=_positive(raw.get("interval_minutes"), cls.interval_minutes),
            database_path=str((raw.get("database") or {}).get("path", cls.database_path)),
            log_file=str((raw.get("logging") or {}).get("file", cls.log_file)),
            log_level=str((raw.get("logging") or {}).get("level", cls.log_level)),
            dashboard_enabled=bool(dashboard.get("enabled", True)),
            dashboard_host=str(dashboard.get("host", cls.dashboard_host)),
            dashboard_port=int(dashboard.get("port", cls.dashboard_port)),
            venue=str(raw.get("venue", cls.venue)),
            markets=markets,
            observation_retention_days=_positive(raw.get("observation_retention_days"), cls.observation_retention_days),
            carry=carry,
            trend=trend,
            funding_scan=FundingScanConfig.from_dict(raw.get("funding_scan")),
            treasury=TreasuryConfig.from_dict(raw.get("treasury")),
        )
