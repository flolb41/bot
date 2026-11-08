"""Narrow, address-scoped risk overrides for explicitly approved token pairs."""
from __future__ import annotations

import math
from typing import Any


def token_risk_policy(
    config: dict, token_address: str, quote_address: str | None = None,
) -> dict[str, Any] | None:
    trading = (config or {}).get("trading") or {}
    policies = trading.get("low_liquidity_token_overrides") or {}
    for policy in policies.values():
        address = (policy.get("address") or "").lower()
        if quote_address is None and address == token_address.lower():
            return policy
        if quote_address is None:
            continue
        allowed_quotes = {
            value.lower() for value in policy.get("quote_addresses", [])
        }
        if (
            (address == token_address.lower() and quote_address.lower() in allowed_quotes)
            or (address == quote_address.lower() and token_address.lower() in allowed_quotes)
        ):
            return policy
    return None


def pool_liquidity_floor(
    config: dict, base_address: str, quote_address: str, default_usd: float,
) -> float:
    policy = token_risk_policy(config, base_address, quote_address)
    if policy is None:
        policy = token_risk_policy(config, quote_address, base_address)
    if policy is None:
        return default_usd
    return float(policy["min_pool_liquidity_usd"])


def quote_depth_floor(
    config: dict, token_address: str, quote_address: str, default_usd: float,
) -> float:
    policy = token_risk_policy(config, token_address, quote_address)
    if policy is not None:
        return float(policy["min_quote_depth_usd"])
    return default_usd / 2


def token_trade_cap(
    config: dict, token_address: str, quote_address: str, default_usd: float,
) -> float:
    policy = token_risk_policy(config, token_address, quote_address)
    if policy is None:
        return default_usd
    cap = float(policy["max_trade_size_usd"])
    if not math.isfinite(cap) or cap <= 0:
        raise ValueError("max_trade_size_usd must be finite and positive.")
    return min(default_usd, cap)


def token_slippage_pct(config: dict, token_address: str, quote_address: str) -> float | None:
    policy = token_risk_policy(config, token_address, quote_address)
    if policy is None:
        return None
    slippage = float(policy["max_slippage_pct"])
    if not math.isfinite(slippage) or not 0 < slippage < 100:
        raise ValueError("max_slippage_pct must be between 0 and 100.")
    return slippage


def notional_under_slippage(depth_usd: float, max_slippage_pct: float) -> float:
    if not math.isfinite(depth_usd) or depth_usd <= 0:
        return 0.0
    if not math.isfinite(max_slippage_pct) or not 0 < max_slippage_pct < 100:
        raise ValueError("max_slippage_pct must be between 0 and 100.")
    return depth_usd * 0.5 * max_slippage_pct / (100 - max_slippage_pct)


def token_flashloan_allowed(config: dict, token_address: str, quote_address: str) -> bool:
    policy = token_risk_policy(config, token_address, quote_address)
    return policy is None or bool(policy.get("flashloan_enabled", False))


def position_size_fraction(
    config: dict, token_address: str, quote_address: str, default_fraction: float = 0.8,
) -> float:
    """Fraction du capital quote à allouer pour une paire spécifique,
    override de `trade_size_capital_fraction` global.

    Clé de config : `"token_address:quote_address"` (adresses en minuscules).
    Retourne la fraction configurée si présente, sinon `default_fraction`.
    Jamais inférieure à 0 ni supérieure à 1."""
    if not (0 < default_fraction <= 1):
        raise ValueError("default_fraction must be between 0 and 1.")
    trading = (config or {}).get("trading") or {}
    per_pair = trading.get("position_size_fraction_per_pair") or {}
    key = f"{token_address.lower()}:{quote_address.lower()}"
    reverse_key = f"{quote_address.lower()}:{token_address.lower()}"
    raw = per_pair.get(key, per_pair.get(reverse_key))
    if raw is None:
        return default_fraction
    fraction = float(raw)
    if not math.isfinite(fraction) or not 0 < fraction <= 1:
        raise ValueError("position_size_fraction_per_pair values must be between 0 and 1.")
    return fraction


def token_slippage_pct_global(config: dict, token_address: str) -> float | None:
    """Slippage d'exécution toléré pour un token spécifique (en %), override de
    `TRADING_SLIPPAGE_PCT` global (0.5%).

    Clé de config : `"token_address"` (adresse en minuscules). Retourne None si
    non configuré pour ce token (le appelant doit alors utiliser la valeur
    globale)."""
    trading = (config or {}).get("trading") or {}
    per_token = trading.get("slippage_pct_per_token") or {}
    address = token_address.lower()
    raw = per_token.get(address)
    if raw is None:
        return None
    slippage = float(raw)
    if not math.isfinite(slippage) or not 0 < slippage < 100:
        raise ValueError("slippage_pct_per_token values must be between 0 and 100.")
    return slippage
