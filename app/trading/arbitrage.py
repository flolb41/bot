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
from app.trading.flashloan import estimate_pair_signal_flashloan_preview, flashloan_min_net_profit_usd
from app.trading.pair_health import pair_health
from app.wallet.pricing import get_price_usd

logger = logging.getLogger(__name__)

_DEFAULT_SEED_TOKENS = [
    {"symbol": "WETH", "address": "0x4200000000000000000000000000000000000006", "coingecko_id": "ethereum"},
    {"symbol": "USDC", "address": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", "coingecko_id": "usd-coin"},
]
_DEFAULT_DEX_WHITELIST = ["uniswap", "aerodrome", "sushiswap", "pancakeswap", "baseswap", "alien-base"]


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
    triées), tous DEX whitelistés confondus, pour chaque token "seed".

    Déduplique par (paire canonique, dex_id) : un même `dex_id` DexScreener
    (ex. "aerodrome", "uniswap") peut recouvrir PLUSIEURS pools on-chain
    distincts pour le même couple de tokens (ex. Aerodrome stable vs volatile,
    pools d'une factory non-par-défaut, Uniswap v3 vs v4, plusieurs fee tiers
    v3...). Or l'exécution (`app.trading.executor._build_swap_call`) cible
    TOUJOURS une route précise et fixe par DEX (Aerodrome : stable=False +
    defaultFactory ; Uniswap : v3 + le fee tier réellement liquide trouvé via
    `_find_uniswap_v3_fee_tier`). Sans cette déduplication, un signal peut être
    dimensionné (prix, min_amount_out) sur un pool minoritaire/obsolète alors
    que l'exécution réelle passe par un AUTRE pool du même DEX au prix
    différent — cause confirmée des échecs `InsufficientOutputAmount` observés
    en conditions réelles sur AERO/aerodrome. Heuristique : le pool légitime et
    réellement routable est quasi toujours celui de plus grande liquidité pour
    ce couple de tokens sur ce DEX ; on ne garde que celui-là."""
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

    for key, pairs in groups.items():
        best_by_dex: dict[str, dict] = {}
        for pair in pairs:
            current_best = best_by_dex.get(pair["dex_id"])
            if current_best is None or pair["liquidity_usd"] > current_best["liquidity_usd"]:
                best_by_dex[pair["dex_id"]] = pair
        groups[key] = list(best_by_dex.values())
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


def _refine_spread_with_onchain_price(spread: dict, token_address: str, quote_address: str,
                                      min_quote_depth_usd: float | None = None) -> dict:
    """Affine `spread` (prix DexScreener, potentiellement périmés de quelques
    secondes à quelques minutes pour une paire peu liquide) avec le prix
    on-chain RÉEL de chaque pool concernée — la MÊME lecture que celle faite
    juste avant d'envoyer une transaction (voir
    `app.trading.executor._onchain_pool_price_usd` et
    `app.trading.flashloan.execute_pair_signal_via_flashloan`). Corrige le bug
    observé en conditions réelles : un signal affiché "rentable" sur la seule
    base DexScreener (ex. BRETT/WETH, spread ≈ 1 %) mais systématiquement
    refusé à l'exécution avec un profit réel quasi nul, car l'écart n'existait
    déjà plus on-chain au moment du scan.

    Ne modifie `spread` que si au moins un prix on-chain a pu être lu ;
    retombe silencieusement sur les prix DexScreener d'origine sinon (RPC
    indisponible, DEX non supporté par cette lecture directe...) — jamais
    d'échec dur ici, seulement une précision en moins.

    Réservé aux quote tokens exécutables (USDC/WETH, `EXECUTABLE_QUOTE_TOKENS`) :
    `_onchain_pool_price_usd` convertit via `_quote_token_usd_price`, qui ne
    connaît que ces deux tokens — un quote exotique (ex. AERO) y était
    auparavant silencieusement traité comme un stablecoin à 1$, produisant des
    spreads/prix totalement aberrants (observé en prod le 2026-10-08 sur
    WETH/AERO : spread à 10^44 %). Comme ces paires ne sont de toute façon
    jamais exécutables (quote non supporté), on retombe simplement sur le
    spread DexScreener d'origine sans tenter de lecture on-chain.

    `min_quote_depth_usd` (optionnel) : profondeur on-chain minimale, côté
    quote token, de la pool RÉELLEMENT routée sur chaque DEX (voir
    `executor._onchain_quote_depth_usd`). En dessous, le signal est écarté
    (spread ramené à 0) : la "liquidité" DexScreener d'une pool concentrée
    compte les positions hors range, et son prix n'est plus rafraîchi faute de
    trades — source de spreads "rentables" permanents mais fictifs
    (PancakeSwap V3 BRETT/WETH, 09/10/2026 : simulations à -40/-90 %)."""
    from app.trading.executor import (
        EXECUTABLE_QUOTE_TOKENS, UNISWAP_V2_FORK_DEXES, _onchain_pool_price_usd, _onchain_price_is_plausible,
        _onchain_quote_depth_usd, _v2_fork_pool_confirmed_absent,
    )
    from app.trading.rpc import make_w3

    if quote_address.lower() not in EXECUTABLE_QUOTE_TOKENS:
        return spread

    # Garde-fou dédié aux forks UniswapV2 "simples" (sushiswap/baseswap/
    # alien-base/swapbased) : si la factory de `buy_dex`/`sell_dex` n'a tout
    # simplement AUCUN pool direct pour ce couple de tokens, le signal ne
    # pourra JAMAIS s'exécuter (peu importe le prix) — mieux vaut l'écarter
    # ici (spread ramené à 0, donc filtré par le seuil `min_spread_pct_to_log`
    # juste après l'appel) que de le laisser déclencher un flashloan voué à
    # un revert `('execution reverted', 'no data')`. Bug confirmé en
    # production le 2026-10-08 : AlienBase listé par DexScreener comme
    # "buy_dex" pour EURC/WETH alors que `factory.getPair` renvoie l'adresse
    # zéro sur la factory réellement utilisée par `app.trading.routers`
    # (DexScreener agrège parfois des pools indirectes/d'autres factories).
    for dex_side in ("buy_dex", "sell_dex"):
        dex = spread[dex_side]
        if dex in UNISWAP_V2_FORK_DEXES and dex not in ("aerodrome", "pancakeswap"):
            # `_v2_fork_pool_confirmed_absent` est déjà permissif sur panne RPC :
            # pas de test de connexion préalable (un appel RPC de moins par signal).
            if _v2_fork_pool_confirmed_absent(make_w3(timeout=10), dex, token_address, quote_address):
                logger.warning(
                    "Signal écarté : aucune pool %s confirmée on-chain pour %s/%s (%s).",
                    dex, token_address, quote_address, dex_side,
                )
                refined = dict(spread)
                refined["spread_pct"] = 0.0
                return refined

    if min_quote_depth_usd is not None and min_quote_depth_usd > 0:
        for dex_side in ("buy_dex", "sell_dex"):
            dex = spread[dex_side]
            depth_usd = _onchain_quote_depth_usd(dex, token_address, quote_address)
            if depth_usd is not None and depth_usd < min_quote_depth_usd:
                logger.warning(
                    "Signal écarté : pool %s réellement routée pour %s/%s trop peu profonde on-chain "
                    "(≈%.0f$ côté quote < %.0f$ requis) — liquidité DexScreener non exploitable (%s).",
                    dex, token_address, quote_address, depth_usd, min_quote_depth_usd, dex_side,
                )
                refined = dict(spread)
                refined["spread_pct"] = 0.0
                return refined

    buy_price = _onchain_pool_price_usd(spread["buy_dex"], token_address, quote_address)
    sell_price = _onchain_pool_price_usd(spread["sell_dex"], token_address, quote_address)
    # Garde-fou de plausibilité (voir `_onchain_price_is_plausible`) : écarte
    # une lecture on-chain aberrante (ex. pool Uniswap V3 quasi vide pour un
    # token récemment ajouté) plutôt que de lui faire confiance aveuglément —
    # bug observé en prod le 2026-10-08 sur TOSHI/WETH et AERO/WETH (prix
    # ~10^42-10^46x la référence DexScreener, faisant gagner ces signaux
    # bidons à chaque tri par profit décroissant du scheduler).
    if buy_price is not None and not _onchain_price_is_plausible(buy_price, spread["buy_price_usd"]):
        logger.warning(
            "Prix on-chain implausible ignoré pour %s sur %s (%.6g vs référence DexScreener %.6g).",
            token_address, spread["buy_dex"], buy_price, spread["buy_price_usd"],
        )
        buy_price = None
    if sell_price is not None and not _onchain_price_is_plausible(sell_price, spread["sell_price_usd"]):
        logger.warning(
            "Prix on-chain implausible ignoré pour %s sur %s (%.6g vs référence DexScreener %.6g).",
            token_address, spread["sell_dex"], sell_price, spread["sell_price_usd"],
        )
        sell_price = None
    if buy_price is None and sell_price is None:
        return spread
    buy_price = buy_price if buy_price is not None else spread["buy_price_usd"]
    sell_price = sell_price if sell_price is not None else spread["sell_price_usd"]
    if buy_price <= 0 or sell_price <= 0:
        return spread
    refined = dict(spread)
    refined["buy_price_usd"] = buy_price
    refined["sell_price_usd"] = sell_price
    refined["spread_pct"] = (sell_price - buy_price) / buy_price * 100
    return refined


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


def collect_pairs_for_config(config: dict | None = None) -> dict[str, list[dict]]:
    """Point d'entrée public pour récupérer UNE SEULE FOIS les pools DexScreener
    (tous DEX whitelistés confondus, c'est-à-dire la whitelist COMPLETE de
    `trading.dex_whitelist` — un sur-ensemble de celle, plus restreinte, utilisée
    par `run_triangular_scan`) à partager entre `run_arbitrage_scan`,
    `run_triangular_scan` et `run_cross_dex_triangular_scan` au sein d'un même
    cycle de scheduler (voir `app.scheduler.run_arbitrage_scan_job`).

    Ces trois scans portaient chacun sur les MÊMES tokens seed / whitelist
    (restreinte ou non) / seuil de liquidité et interrogeaient DexScreener de
    façon totalement indépendante (3x `_collect_pairs`, donc 3x N appels
    DexScreener par cycle, N = nombre de tokens seed) pour des données qui ne
    varient pas assez en quelques secondes pour justifier une triple lecture.
    Confirmé en production : ce triple fetch (~3 x 13 appels, throttlés à 1.1s
    chacun dans `app.trading.dex_sources`) faisait déjà largement dépasser la
    durée d'un cycle au-delà de l'intervalle configuré (3 min), si bien que
    PRESQUE CHAQUE tick du scheduler était sauté ("maximum number of running
    instances reached") — réduire l'intervalle sans régler ce point n'aurait
    eu AUCUN effet. Partager un seul fetch par cycle divise par ~3 le temps et
    le volume d'appels de chaque cycle."""
    trading_cfg = (config or {}).get("trading", {}) or {}
    chain_id = trading_cfg.get("chain", "base")
    dex_whitelist = set(trading_cfg.get("dex_whitelist") or _DEFAULT_DEX_WHITELIST)
    seed_tokens = trading_cfg.get("seed_tokens") or _DEFAULT_SEED_TOKENS
    min_liquidity_usd = float(trading_cfg.get("min_liquidity_usd", 10_000))
    return _collect_pairs(chain_id, seed_tokens, dex_whitelist, min_liquidity_usd)


def run_arbitrage_scan(
    db: Database, config: dict | None = None, groups: dict[str, list[dict]] | None = None
) -> list[dict]:
    """Scanne les paires inter-DEX sur les tokens "seed" configurés, journalise
    tout écart significatif en base (table arbitrage_signals), et retourne les
    signaux détectés durant ce cycle. N'exécute jamais de transaction (voir
    garde-fous en tête de fichier).

    `groups` : pools déjà récupérées (voir `collect_pairs_for_config`), pour
    éviter un nouvel appel DexScreener si le scheduler les a déjà fetchées ce
    cycle. Si `None` (usage autonome, scripts, tests), fetch comme avant."""
    trading_cfg = (config or {}).get("trading", {}) or {}
    chain_id = trading_cfg.get("chain", "base")
    dex_whitelist = set(trading_cfg.get("dex_whitelist") or _DEFAULT_DEX_WHITELIST)
    seed_tokens = trading_cfg.get("seed_tokens") or _DEFAULT_SEED_TOKENS
    min_liquidity_usd = float(trading_cfg.get("min_liquidity_usd", 10_000))
    max_trade_size_usd = float(trading_cfg.get("max_trade_size_usd", 200))
    liquidity_safety_fraction = float(trading_cfg.get("liquidity_safety_fraction", 0.02))
    min_spread_pct_to_log = float(trading_cfg.get("min_spread_pct_to_log", 0.10))
    min_net_profit_usd = float(trading_cfg.get("min_net_profit_usd", 0.50))
    min_net_profit_pct = float(trading_cfg.get("min_net_profit_pct", 0.0))

    # Dimensionnement dynamique optionnel : au lieu d'une taille de trade fixe
    # (`max_trade_size_usd`), utiliser une fraction du capital RÉEL disponible
    # (solde on-chain du wallet de trading, dans LE quote token du signal
    # concerné — USDC ou WETH, voir `app.trading.executor.EXECUTABLE_QUOTE_TOKENS`)
    # si `trade_size_capital_fraction` est configuré. N'affecte jamais le coût
    # de gas (indépendant de la taille du trade — voir `fees.estimate_gas_cost_usd`,
    # qui ne prend pas trade_size en paramètre) : seul le montant tradé change,
    # pas le nombre/coût des transactions. Retombe sur `max_trade_size_usd`
    # (par quote token non exécutable, ou si le trading n'est pas live, ou si
    # la lecture du solde échoue — RPC injoignable, etc.).
    trade_size_capital_fraction = float(trading_cfg.get("trade_size_capital_fraction", 0) or 0)
    quote_capital_balances_usd: dict[str, float] = {}
    if trade_size_capital_fraction > 0:
        try:
            from app.trading.executor import EXECUTABLE_QUOTE_TOKENS, get_quote_trading_balance_usd
            for quote_address in EXECUTABLE_QUOTE_TOKENS:
                balance_usd = get_quote_trading_balance_usd(quote_address, config or {})
                if balance_usd is not None and balance_usd > 0:
                    quote_capital_balances_usd[quote_address] = balance_usd
        except Exception as exc:  # noqa: BLE001
            logger.warning("Dimensionnement dynamique : échec lecture des soldes réels, repli sur max_trade_size_usd : %s", exc)

    if groups is None:
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

            other_address = token_b if token_address == token_a else token_a
            # Affinage on-chain réservé aux candidats qui dépassent DÉJÀ le
            # seuil DexScreener ci-dessus (pas tous les pools scannés) — limite
            # le coût RPC ajouté à ~2 appels par signal potentiellement
            # rentable, pas par pool. Un signal dont l'écart s'évapore une fois
            # vérifié on-chain est reclassé immédiatement (voir docstring de
            # `_refine_spread_with_onchain_price`).
            spread = _refine_spread_with_onchain_price(
                spread, token_address, other_address,
                # Côté quote d'une pool V2 de `min_liquidity_usd` ≈ la moitié ;
                # même exigence appliquée à la profondeur RÉELLE on-chain.
                min_quote_depth_usd=min_liquidity_usd / 2,
            )
            if spread["spread_pct"] < min_spread_pct_to_log:
                continue

            quote_balance_usd = quote_capital_balances_usd.get(other_address.lower())
            trade_size_cap_usd = (
                trade_size_capital_fraction * quote_balance_usd
                if trade_size_capital_fraction > 0 and quote_balance_usd is not None
                else max_trade_size_usd
            )
            trade_size_usd = min(
                trade_size_cap_usd,
                liquidity_safety_fraction * min(spread["buy_liquidity_usd"], spread["sell_liquidity_usd"]),
            )
            if trade_size_usd <= 0:
                continue

            profit = _profitability(
                spread, trade_size_usd,
                slippage_buffer_pct=fees.DEFAULT_SLIPPAGE_BUFFER_PCT,
                gas_cost_usd=gas_cost_usd,
            )

            # Symboles indicatifs (le même token peut apparaître avec des libellés
            # légèrement différents selon la pool ; on prend le premier trouvé).
            token_symbol = next((p["base_symbol"] for p in pairs if p["base_address"] == token_address),
                                 next((p["quote_symbol"] for p in pairs if p["quote_address"] == token_address), "?"))
            other_symbol = next((p["base_symbol"] for p in pairs if p["base_address"] == other_address),
                                 next((p["quote_symbol"] for p in pairs if p["quote_address"] == other_address), "?"))

            # Historique d'exécution réelle de cette paire précise (buy_dex/sell_dex) :
            # seuil de profit relevé si échecs récents fréquents, exclusion
            # temporaire après une série d'échecs consécutifs — voir
            # `app.trading.pair_health`.
            health = pair_health(
                db, config or {}, token_symbol=token_symbol,
                buy_dex=spread["buy_dex"], sell_dex=spread["sell_dex"],
            )
            adaptive_min_net_profit_usd = min_net_profit_usd * health["threshold_multiplier"]
            would_execute_classic = (
                not health["disabled"]
                and profit["net_profit_usd"] is not None
                and profit["net_profit_usd"]
                >= max(adaptive_min_net_profit_usd, trade_size_usd * min_net_profit_pct / 100)
            )
            if health["disabled"]:
                logger.info("Arbitrage scan : %s", health["disabled_reason"])

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
                # Phase 1 : simulation uniquement, aucun moteur d'exécution live
                # n'existe dans ce commit — voir garde-fous en tête de fichier.
                "mode": "simulation",
                "executed": False,
            }
            # Aperçu du profit à l'échelle flashloan (notionnel bien plus grand
            # que `trade_size_usd` ci-dessus, borné par la liquidité et non par
            # le solde du wallet) — `None` si le signal n'est pas éligible
            # flashloan ou si le gas est indisponible, voir
            # `app.trading.flashloan.estimate_pair_signal_flashloan_preview`.
            # Calculé AVANT `would_execute` ci-dessous : un signal peut être non
            # rentable au micro-capital du wallet (quelques dollars, dominé par
            # le gas) mais tout de même rentable à l'échelle flashloan — sans
            # ce calcul préalable, le flashloan n'était jamais tenté car
            # `would_execute` ne considérait que `would_execute_classic`.
            flashloan_preview = estimate_pair_signal_flashloan_preview(config or {}, signal)
            would_execute_flashloan = (
                not health["disabled"]
                and flashloan_preview is not None
                and flashloan_preview["net_profit_usd"] >= flashloan_min_net_profit_usd(config or {})
            )
            signal["would_execute"] = would_execute_classic or would_execute_flashloan
            # Champs distincts (non persistés en base, consommés uniquement par
            # `app.scheduler` pour router vers la bonne voie d'exécution — voir
            # commentaire ci-dessus) : un signal rentable UNIQUEMENT via
            # flashloan ne doit jamais être exécuté via la voie classique
            # (capital propre), qui utiliserait sinon `trade_size_usd` alors
            # que ce microtrade précis n'est pas rentable à cette échelle.
            signal["would_execute_classic"] = would_execute_classic
            signal["would_execute_flashloan"] = would_execute_flashloan
            signal["flashloan_notional_usd"] = flashloan_preview["notional_usd"] if flashloan_preview else None
            signal["flashloan_net_profit_usd"] = flashloan_preview["net_profit_usd"] if flashloan_preview else None
            signal["id"] = db.add_arbitrage_signal(signal)
            signals.append(signal)

    logger.info("Arbitrage scan terminé : %d signal(aux) détecté(s), %d rentable(s) après coûts.",
                len(signals), sum(1 for s in signals if s["would_execute"]))
    return signals
