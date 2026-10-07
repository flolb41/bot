"""Moteur de détection d'arbitrage inter-DEX (bot de trading, section "microtrades").

GARDE-FOUS DE PHASE (lire avant de modifier) :
    - Ce module ne fait QUE détecter des écarts de prix et ESTIMER un profit net
      (frais DEX + slippage + gas). Il n'envoie JAMAIS de transaction : il n'y a,
      à ce stade, littéralement aucun code d'exécution on-chain ici.
    - `would_execute` est un flag informatif ("si on exécutait, est-ce que ce
      serait rentable ?"), pas une autorisation. `mode` reste "simulation" tant
      qu'aucun moteur d'exécution live n'a été développé ET validé séparément
      par l'utilisateur après revue des résultats de simulation (Phase 2/3,
      non commencée).
    - Objectif de cette phase : accumuler des données réelles (fréquence des
      écarts rentables après coûts, sur quelles paires/DEX) pour juger si la
      stratégie vaut la peine d'être automatisée avec du capital réel, AVANT
      d'y toucher.
"""
from __future__ import annotations

import logging
from itertools import combinations

from app.database import Database
from app.trading import dex_sources, fees
from app.wallet.pricing import get_price_usd

logger = logging.getLogger(__name__)

_DEFAULT_SEED_TOKENS = [
    {"symbol": "WETH", "address": "0x4200000000000000000000000000000000000006", "coingecko_id": "ethereum"},
    {"symbol": "USDC", "address": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", "coingecko_id": "usd-coin"},
]
_DEFAULT_DEX_WHITELIST = ["uniswap", "aerodrome", "sushiswap", "pancakeswap"]


def _normalize_pair(raw: dict, chain_id: str) -> dict | None:
    """Normalise une entrée brute DEX Screener. Retourne None si incomplète/hors
    scope (chaîne différente ou données manquantes)."""
    if raw.get("chainId") != chain_id:
        return None
    base = raw.get("baseToken") or {}
    quote = raw.get("quoteToken") or {}
    base_addr = (base.get("address") or "").lower()
    quote_addr = (quote.get("address") or "").lower()
    if not base_addr or not quote_addr:
        return None
    try:
        price_usd = float(raw["priceUsd"])
        price_native = float(raw["priceNative"])
    except (KeyError, TypeError, ValueError):
        return None
    if price_native == 0:
        return None
    liquidity_usd = ((raw.get("liquidity") or {}).get("usd"))
    try:
        liquidity_usd = float(liquidity_usd) if liquidity_usd is not None else None
    except (TypeError, ValueError):
        liquidity_usd = None

    return {
        "dex_id": (raw.get("dexId") or "").lower(),
        "pair_address": raw.get("pairAddress"),
        "base_address": base_addr,
        "base_symbol": base.get("symbol", "?"),
        "quote_address": quote_addr,
        "quote_symbol": quote.get("symbol", "?"),
        "price_usd_base": price_usd,
        "price_native": price_native,
        "liquidity_usd": liquidity_usd,
    }


def _token_usd_price(pair: dict, token_address: str) -> float | None:
    """Prix USD de `token_address` dérivé d'une pool normalisée, que ce token
    soit le base ou le quote de cette pool (voir doc du module)."""
    token_address = token_address.lower()
    if pair["base_address"] == token_address:
        return pair["price_usd_base"]
    if pair["quote_address"] == token_address:
        return pair["price_usd_base"] / pair["price_native"]
    return None


def _collect_pairs(chain_id: str, seed_tokens: list[dict], dex_whitelist: set[str],
                    min_liquidity_usd: float) -> dict[str, list[dict]]:
    """Récupère et regroupe les pools par paire canonique (couple d'adresses
    triées), tous DEX whitelistés confondus, pour chaque token "seed"."""
    groups: dict[str, list[dict]] = {}
    for seed in seed_tokens:
        raw_pairs = dex_sources.fetch_token_pairs(chain_id, seed["address"])
        for raw in raw_pairs:
            pair = _normalize_pair(raw, chain_id)
            if pair is None:
                continue
            if pair["dex_id"] not in dex_whitelist:
                continue
            if pair["liquidity_usd"] is None or pair["liquidity_usd"] < min_liquidity_usd:
                continue
            key = "_".join(sorted([pair["base_address"], pair["quote_address"]]))
            groups.setdefault(key, []).append(pair)
    return groups


def _best_spread(pairs: list[dict], token_address: str) -> dict | None:
    """Parmi les pools d'une même paire canonique (DEX différents), trouve le
    plus grand écart de prix USD pour `token_address` entre deux DEX distincts."""
    priced = []
    for pair in pairs:
        usd_price = _token_usd_price(pair, token_address)
        if usd_price is not None and usd_price > 0:
            priced.append((usd_price, pair))
    if len(priced) < 2:
        return None

    best: dict | None = None
    for (price_a, pair_a), (price_b, pair_b) in combinations(priced, 2):
        if pair_a["dex_id"] == pair_b["dex_id"]:
            continue
        low, low_pair = (price_a, pair_a) if price_a <= price_b else (price_b, pair_b)
        high, high_pair = (price_b, pair_b) if price_a <= price_b else (price_a, pair_a)
        if low <= 0:
            continue
        spread_pct = (high - low) / low * 100
        if best is None or spread_pct > best["spread_pct"]:
            best = {
                "spread_pct": spread_pct,
                "buy_dex": low_pair["dex_id"], "buy_price_usd": low, "buy_liquidity_usd": low_pair["liquidity_usd"],
                "sell_dex": high_pair["dex_id"], "sell_price_usd": high, "sell_liquidity_usd": high_pair["liquidity_usd"],
            }
    return best


def _profitability(spread: dict, trade_size_usd: float, slippage_buffer_pct: float,
                    gas_cost_usd: float | None) -> dict:
    fee_pct = fees.dex_fee_pct(spread["buy_dex"]) + fees.dex_fee_pct(spread["sell_dex"])
    gross_profit_usd = trade_size_usd * spread["spread_pct"] / 100
    fees_usd = trade_size_usd * fee_pct / 100
    slippage_buffer_usd = trade_size_usd * slippage_buffer_pct / 100

    net_profit_usd = None
    would_execute = False
    if gas_cost_usd is not None:
        net_profit_usd = gross_profit_usd - fees_usd - slippage_buffer_usd - gas_cost_usd

    return {
        "gross_profit_usd": gross_profit_usd,
        "fees_usd": fees_usd,
        "slippage_buffer_usd": slippage_buffer_usd,
        "gas_cost_usd": gas_cost_usd,
        "net_profit_usd": net_profit_usd,
        "would_execute": would_execute,  # décidé par l'appelant (seuil configurable)
    }


def run_arbitrage_scan(db: Database, config: dict | None = None) -> list[dict]:
    """Scanne les paires inter-DEX sur les tokens "seed" configurés, journalise
    tout écart significatif en base (table arbitrage_signals), et retourne les
    signaux détectés durant ce cycle. N'exécute jamais de transaction (voir
    garde-fous en tête de fichier)."""
    trading_cfg = (config or {}).get("trading", {}) or {}
    chain_id = trading_cfg.get("chain", "base")
    dex_whitelist = set(trading_cfg.get("dex_whitelist") or _DEFAULT_DEX_WHITELIST)
    seed_tokens = trading_cfg.get("seed_tokens") or _DEFAULT_SEED_TOKENS
    min_liquidity_usd = float(trading_cfg.get("min_liquidity_usd", 10_000))
    max_trade_size_usd = float(trading_cfg.get("max_trade_size_usd", 200))
    liquidity_safety_fraction = float(trading_cfg.get("liquidity_safety_fraction", 0.02))
    min_spread_pct_to_log = float(trading_cfg.get("min_spread_pct_to_log", 0.10))
    min_net_profit_usd = float(trading_cfg.get("min_net_profit_usd", 0.50))

    groups = _collect_pairs(chain_id, seed_tokens, dex_whitelist, min_liquidity_usd)
    if not groups:
        logger.info("Arbitrage scan : aucune pool éligible trouvée (liquidité/DEX whitelist).")
        return []

    eth_usd_price = get_price_usd("ethereum")
    gas_cost_usd = fees.estimate_gas_cost_usd(eth_usd_price)
    if gas_cost_usd is None:
        logger.warning(
            "Arbitrage scan : coût de gas Base indisponible (RPC ou prix ETH) — "
            "aucun signal ne sera marqué 'would_execute' ce cycle (prudence)."
        )

    signals: list[dict] = []
    for pair_key, pairs in groups.items():
        token_a, token_b = pair_key.split("_")
        for token_address in (token_a, token_b):
            spread = _best_spread(pairs, token_address)
            if spread is None or spread["spread_pct"] < min_spread_pct_to_log:
                continue

            trade_size_usd = min(
                max_trade_size_usd,
                liquidity_safety_fraction * min(spread["buy_liquidity_usd"], spread["sell_liquidity_usd"]),
            )
            if trade_size_usd <= 0:
                continue

            profit = _profitability(
                spread, trade_size_usd,
                slippage_buffer_pct=fees.DEFAULT_SLIPPAGE_BUFFER_PCT,
                gas_cost_usd=gas_cost_usd,
            )
            would_execute = (
                profit["net_profit_usd"] is not None
                and profit["net_profit_usd"] >= min_net_profit_usd
            )

            # Symboles indicatifs (le même token peut apparaître avec des libellés
            # légèrement différents selon la pool ; on prend le premier trouvé).
            token_symbol = next((p["base_symbol"] for p in pairs if p["base_address"] == token_address),
                                 next((p["quote_symbol"] for p in pairs if p["quote_address"] == token_address), "?"))
            other_address = token_b if token_address == token_a else token_a
            other_symbol = next((p["base_symbol"] for p in pairs if p["base_address"] == other_address),
                                 next((p["quote_symbol"] for p in pairs if p["quote_address"] == other_address), "?"))

            signal = {
                "chain": chain_id,
                "token_symbol": token_symbol,
                "token_address": token_address,
                "quote_symbol": other_symbol,
                "quote_address": other_address,
                "buy_dex": spread["buy_dex"],
                "buy_price_usd": spread["buy_price_usd"],
                "buy_liquidity_usd": spread["buy_liquidity_usd"],
                "sell_dex": spread["sell_dex"],
                "sell_price_usd": spread["sell_price_usd"],
                "sell_liquidity_usd": spread["sell_liquidity_usd"],
                "spread_pct": spread["spread_pct"],
                "trade_size_usd": trade_size_usd,
                "gross_profit_usd": profit["gross_profit_usd"],
                "fees_usd": profit["fees_usd"],
                "slippage_buffer_usd": profit["slippage_buffer_usd"],
                "gas_cost_usd": profit["gas_cost_usd"],
                "net_profit_usd": profit["net_profit_usd"],
                "would_execute": would_execute,
                # Phase 1 : simulation uniquement, aucun moteur d'exécution live
                # n'existe dans ce commit — voir garde-fous en tête de fichier.
                "mode": "simulation",
                "executed": False,
            }
            db.add_arbitrage_signal(signal)
            signals.append(signal)

    logger.info("Arbitrage scan terminé : %d signal(aux) détecté(s), %d rentable(s) après coûts.",
                len(signals), sum(1 for s in signals if s["would_execute"]))
    return signals
