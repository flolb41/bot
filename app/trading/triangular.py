"""Détection d'arbitrage TRIANGULAIRE : cycle à 3 jambes sur UN SEUL DEX
(ex: USDC -> WETH -> AERO -> USDC), par opposition à l'arbitrage classique
inter-DEX de `app/trading/arbitrage.py` (2 jambes, 2 DEX différents).

Intérêt par rapport à l'arbitrage classique :
    - Exécutable en UNE SEULE transaction multi-hop (voir
      `app.trading.executor._build_triangular_swap_call`) : atomique par
      construction (si une jambe échoue, toute la transaction revert), donc
      AUCUN risque de position résiduelle ouverte contrairement au round-trip
      classique (2 transactions séparées, voir le docstring de executor.py).
    - Nouvelle source d'opportunités indépendante des écarts inter-DEX :
      exploite les déséquilibres de réserves ENTRE pools d'un même DEX plutôt
      qu'entre deux DEX différents.

Garde-fous de phase : mêmes principes qu'`arbitrage.py` (détection + estimation
de profit net uniquement ici ; `would_execute` reste un flag informatif, voir
`app.trading.executor.execute_triangular_signal` pour l'exécution réelle sous
les mêmes conditions strictes que `is_trading_live`)."""
from __future__ import annotations

import logging

from app.database import Database
from app.trading import fees
from app.trading.arbitrage import _DEFAULT_DEX_WHITELIST, _DEFAULT_SEED_TOKENS, _collect_pairs
from app.trading.flashloan import estimate_triangular_signal_flashloan_preview
from app.trading.pair_health import triangular_health
from app.wallet.pricing import get_price_usd

logger = logging.getLogger(__name__)

# DEX supportant un multi-hop en UNE SEULE transaction (voir
# `executor._build_triangular_swap_call`) : tous les forks UniswapV2
# classiques (path=[A,B,C,A] natif à `swapExactTokensForTokens`) + Aerodrome
# (tableau `routes` multi-hop natif). Uniswap V3 est volontairement exclu :
# son encodage multi-hop (`exactInput`, chemin bytes packé avec les fee tiers)
# est nettement plus complexe à construire correctement — pas fait dans cette
# phase, risque jugé disproportionné par rapport au gain.
TRIANGULAR_CAPABLE_DEXES = frozenset({
    "aerodrome", "sushiswap", "pancakeswap", "baseswap", "alien-base", "swapbased",
})

# Gas un peu plus élevé qu'un aller-retour classique : 3 swaps dans UNE tx
# (au lieu de 2 swaps dans 2 tx séparées), estimation volontairement prudente
# (surestimée plutôt que sous-estimée, même principe que fees.py).
ESTIMATED_GAS_UNITS_TRIANGULAR = 420_000


def _pool_rate(pair: dict, from_token: str, to_token: str) -> float | None:
    """Taux de change tel que coté par CE pool précis (pas une moyenne USD
    inter-pools, qui donnerait toujours un produit de cycle égal à 1 et ne
    détecterait donc jamais rien) : combien d'unités de `to_token` reçues
    pour 1 unité de `from_token`, d'après `priceNative` de la pool."""
    if pair["base_address"] == from_token and pair["quote_address"] == to_token:
        return pair["price_native"]
    if pair["base_address"] == to_token and pair["quote_address"] == from_token:
        if pair["price_native"] == 0:
            return None
        return 1.0 / pair["price_native"]
    return None


def run_triangular_scan(db: Database, config: dict | None = None) -> list[dict]:
    """Scanne les cycles triangulaires sur les DEX multi-hop-compatibles
    (`TRIANGULAR_CAPABLE_DEXES`), journalise tout cycle significatif en base
    (table `triangular_signals`) et retourne les signaux détectés durant ce
    cycle. N'exécute jamais de transaction — voir `app.trading.executor`.

    Réutilise les pools déjà récupérés par `app.trading.arbitrage._collect_pairs`
    (même déduplication par pool de plus grande liquidité par (paire, dex) —
    aucun appel DexScreener supplémentaire)."""
    trading_cfg = (config or {}).get("trading", {}) or {}
    chain_id = trading_cfg.get("chain", "base")
    dex_whitelist = (set(trading_cfg.get("dex_whitelist") or _DEFAULT_DEX_WHITELIST)
                     & TRIANGULAR_CAPABLE_DEXES)
    seed_tokens = trading_cfg.get("seed_tokens") or _DEFAULT_SEED_TOKENS
    min_liquidity_usd = float(trading_cfg.get("min_liquidity_usd", 10_000))
    max_trade_size_usd = float(trading_cfg.get("max_trade_size_usd", 200))
    liquidity_safety_fraction = float(trading_cfg.get("liquidity_safety_fraction", 0.02))
    min_spread_pct_to_log = float(trading_cfg.get("min_spread_pct_to_log", 0.10))
    min_net_profit_usd = float(trading_cfg.get("min_net_profit_usd", 0.50))
    min_net_profit_pct = float(trading_cfg.get("min_net_profit_pct", 0.0))

    if not dex_whitelist:
        return []

    from app.trading.executor import EXECUTABLE_QUOTE_TOKENS, get_quote_trading_balance_usd

    # Dimensionnement dynamique optionnel, même logique que `arbitrage.py`
    # (voir son docstring) : une fraction du capital RÉEL disponible dans le
    # token de départ/arrivée du cycle (forcément USDC ou WETH).
    trade_size_capital_fraction = float(trading_cfg.get("trade_size_capital_fraction", 0) or 0)
    quote_capital_balances_usd: dict[str, float] = {}
    if trade_size_capital_fraction > 0:
        try:
            for quote_address in EXECUTABLE_QUOTE_TOKENS:
                balance_usd = get_quote_trading_balance_usd(quote_address, config or {})
                if balance_usd is not None and balance_usd > 0:
                    quote_capital_balances_usd[quote_address] = balance_usd
        except Exception as exc:  # noqa: BLE001
            logger.warning("Triangular scan : échec lecture des soldes réels, repli sur max_trade_size_usd : %s", exc)

    groups = _collect_pairs(chain_id, seed_tokens, dex_whitelist, min_liquidity_usd)
    if not groups:
        return []

    # Index par dex : {dex_id: {frozenset({addr_a, addr_b}): pair}}
    by_dex: dict[str, dict[frozenset, dict]] = {}
    for pairs in groups.values():
        for pair in pairs:
            by_dex.setdefault(pair["dex_id"], {})[
                frozenset({pair["base_address"], pair["quote_address"]})
            ] = pair

    eth_usd_price = get_price_usd("ethereum")
    gas_cost_usd = fees.estimate_gas_cost_usd(eth_usd_price, gas_units=ESTIMATED_GAS_UNITS_TRIANGULAR)
    if gas_cost_usd is None:
        logger.warning(
            "Triangular scan : coût de gas Base indisponible — "
            "aucun cycle ne sera marqué 'would_execute' ce cycle (prudence)."
        )

    seed_entries = [(t["symbol"], t["address"].lower()) for t in seed_tokens]
    signals: list[dict] = []

    for dex, pair_index in by_dex.items():
        for token_a_symbol, token_a_addr in seed_entries:
            # Le cycle part et revient dans ce token : doit être le capital
            # réellement détenu/exécutable (voir EXECUTABLE_QUOTE_TOKENS).
            if token_a_addr not in EXECUTABLE_QUOTE_TOKENS:
                continue
            for token_b_symbol, token_b_addr in seed_entries:
                if token_b_addr == token_a_addr:
                    continue
                pair_ab = pair_index.get(frozenset({token_a_addr, token_b_addr}))
                if pair_ab is None:
                    continue
                for token_c_symbol, token_c_addr in seed_entries:
                    if token_c_addr in (token_a_addr, token_b_addr):
                        continue
                    pair_bc = pair_index.get(frozenset({token_b_addr, token_c_addr}))
                    pair_ca = pair_index.get(frozenset({token_c_addr, token_a_addr}))
                    if pair_bc is None or pair_ca is None:
                        continue

                    rate_ab = _pool_rate(pair_ab, token_a_addr, token_b_addr)
                    rate_bc = _pool_rate(pair_bc, token_b_addr, token_c_addr)
                    rate_ca = _pool_rate(pair_ca, token_c_addr, token_a_addr)
                    if rate_ab is None or rate_bc is None or rate_ca is None:
                        continue
                    if rate_ab <= 0 or rate_bc <= 0 or rate_ca <= 0:
                        continue

                    cycle_multiplier = rate_ab * rate_bc * rate_ca
                    spread_pct = (cycle_multiplier - 1) * 100
                    if spread_pct < min_spread_pct_to_log:
                        continue

                    min_liquidity = min(
                        pair_ab["liquidity_usd"], pair_bc["liquidity_usd"], pair_ca["liquidity_usd"]
                    )

                    quote_balance_usd = quote_capital_balances_usd.get(token_a_addr)
                    trade_size_cap_usd = (
                        trade_size_capital_fraction * quote_balance_usd
                        if trade_size_capital_fraction > 0 and quote_balance_usd is not None
                        else max_trade_size_usd
                    )
                    trade_size_usd = min(trade_size_cap_usd, liquidity_safety_fraction * min_liquidity)
                    if trade_size_usd <= 0:
                        continue

                    fee_pct_total = 3 * fees.dex_fee_pct(dex)  # 3 swaps, même DEX
                    gross_profit_usd = trade_size_usd * spread_pct / 100
                    fees_usd = trade_size_usd * fee_pct_total / 100
                    slippage_buffer_usd = trade_size_usd * fees.DEFAULT_SLIPPAGE_BUFFER_PCT / 100

                    net_profit_usd = None
                    would_execute = False
                    health = triangular_health(
                        db, config or {}, dex=dex, token_a_symbol=token_a_symbol,
                        token_b_symbol=token_b_symbol, token_c_symbol=token_c_symbol,
                    )
                    if health["disabled"]:
                        logger.info("Triangular scan : %s", health["disabled_reason"])
                    if gas_cost_usd is not None:
                        net_profit_usd = gross_profit_usd - fees_usd - slippage_buffer_usd - gas_cost_usd
                        adaptive_min_net_profit_usd = min_net_profit_usd * health["threshold_multiplier"]
                        would_execute = not health["disabled"] and net_profit_usd >= max(
                            adaptive_min_net_profit_usd, trade_size_usd * min_net_profit_pct / 100
                        )

                    signal = {
                        "chain": chain_id,
                        "dex": dex,
                        "token_a_symbol": token_a_symbol, "token_a_address": token_a_addr,
                        "token_b_symbol": token_b_symbol, "token_b_address": token_b_addr,
                        "token_c_symbol": token_c_symbol, "token_c_address": token_c_addr,
                        "cycle_multiplier": cycle_multiplier,
                        "spread_pct": spread_pct,
                        "min_liquidity_usd": min_liquidity,
                        "trade_size_usd": trade_size_usd,
                        "gross_profit_usd": gross_profit_usd,
                        "fees_usd": fees_usd,
                        "slippage_buffer_usd": slippage_buffer_usd,
                        "gas_cost_usd": gas_cost_usd,
                        "net_profit_usd": net_profit_usd,
                        "would_execute": would_execute,
                        "mode": "simulation",
                        "executed": False,
                    }
                    # Aperçu du profit à l'échelle flashloan, même principe que
                    # `app.trading.arbitrage.run_arbitrage_scan` — voir
                    # `app.trading.flashloan.estimate_triangular_signal_flashloan_preview`.
                    flashloan_preview = estimate_triangular_signal_flashloan_preview(config or {}, signal)
                    signal["flashloan_notional_usd"] = flashloan_preview["notional_usd"] if flashloan_preview else None
                    signal["flashloan_net_profit_usd"] = (
                        flashloan_preview["net_profit_usd"] if flashloan_preview else None
                    )
                    signal["id"] = db.add_triangular_signal(signal)
                    signals.append(signal)

    if signals:
        logger.info(
            "Triangular scan terminé : %d cycle(s) détecté(s), %d rentable(s) après coûts.",
            len(signals), sum(1 for s in signals if s["would_execute"]),
        )
    return signals


def run_cross_dex_triangular_scan(db: Database, config: dict | None = None) -> list[dict]:
    """Détecte des cycles triangulaires dont les 3 jambes prennent chacune le
    MEILLEUR taux disponible tous DEX confondus, sans exiger qu'elles soient
    toutes cotées sur le même DEX (contrairement à `run_triangular_scan`, qui
    se limite à un seul DEX à la fois car c'est le seul cas exécutable de
    façon atomique en une transaction — voir `TRIANGULAR_CAPABLE_DEXES`).

    Repère donc des opportunités invisibles pour `run_triangular_scan` (ex :
    A→B mieux coté sur Aerodrome, B→C sur Uniswap V3, C→A sur Baseswap), mais
    PUREMENT INFORMATIONNEL : aucune exécution atomique en une seule
    transaction n'est possible sans redévelopper/redéployer
    `FlashArbitrage.sol` pour router chaque jambe vers un DEX différent (jugé
    hors scope de ce lot d'améliorations). Les cycles détectés sont
    journalisés en base (table `cross_dex_triangular_signals`, voir
    `Database.list_cross_dex_triangular_signals`) pour inspection manuelle —
    jamais exécutés ni proposés à l'exécution automatique."""
    trading_cfg = (config or {}).get("trading", {}) or {}
    chain_id = trading_cfg.get("chain", "base")
    dex_whitelist = set(trading_cfg.get("dex_whitelist") or _DEFAULT_DEX_WHITELIST)
    seed_tokens = trading_cfg.get("seed_tokens") or _DEFAULT_SEED_TOKENS
    min_liquidity_usd = float(trading_cfg.get("min_liquidity_usd", 10_000))
    # Seuil volontairement plus élevé que `min_spread_pct_to_log` (arbitrage
    # classique) : ces cycles ne sont pas exécutables, un seuil bas ne ferait
    # que noyer la base de données sans valeur actionnable immédiate.
    min_spread_pct_to_log = float(trading_cfg.get("cross_dex_min_spread_pct_to_log", 0.50))

    if not dex_whitelist:
        return []

    groups = _collect_pairs(chain_id, seed_tokens, dex_whitelist, min_liquidity_usd)
    if not groups:
        return []

    # Toutes les pools par paire de tokens, TOUS DEX confondus (contrairement
    # à `by_dex` dans `run_triangular_scan`, qui segmente strictement par DEX) :
    # {frozenset({addr_a, addr_b}): [pair, ...]}
    pools_by_token_pair: dict[frozenset, list[dict]] = {}
    for pairs in groups.values():
        for pair in pairs:
            pools_by_token_pair.setdefault(
                frozenset({pair["base_address"], pair["quote_address"]}), []
            ).append(pair)

    def _best_leg(from_addr: str, to_addr: str) -> tuple[float, dict] | None:
        """Meilleur taux ET pool d'origine pour cette jambe, tous DEX confondus."""
        best: tuple[float, dict] | None = None
        for pair in pools_by_token_pair.get(frozenset({from_addr, to_addr})) or []:
            rate = _pool_rate(pair, from_addr, to_addr)
            if rate is None or rate <= 0:
                continue
            if best is None or rate > best[0]:
                best = (rate, pair)
        return best

    seed_entries = [(t["symbol"], t["address"].lower()) for t in seed_tokens]
    signals: list[dict] = []

    for token_a_symbol, token_a_addr in seed_entries:
        for token_b_symbol, token_b_addr in seed_entries:
            if token_b_addr == token_a_addr:
                continue
            for token_c_symbol, token_c_addr in seed_entries:
                if token_c_addr in (token_a_addr, token_b_addr):
                    continue
                # Un cycle A→B→C→A est le MÊME cycle physique que B→C→A→B ou
                # C→A→B→C (simple rotation du point de départ) : ne garder que
                # la rotation où token_a est l'adresse la plus petite évite de
                # journaliser 3x le même cycle (les 2 sens de parcours restent
                # bien distincts, eux, puisque la boucle garde token_b/token_c
                # dans les deux ordres possibles).
                if token_a_addr > token_b_addr or token_a_addr > token_c_addr:
                    continue

                leg_ab = _best_leg(token_a_addr, token_b_addr)
                leg_bc = _best_leg(token_b_addr, token_c_addr)
                leg_ca = _best_leg(token_c_addr, token_a_addr)
                if leg_ab is None or leg_bc is None or leg_ca is None:
                    continue

                rate_ab, pair_ab = leg_ab
                rate_bc, pair_bc = leg_bc
                rate_ca, pair_ca = leg_ca

                # Les 3 jambes sur le même DEX : déjà couvert (et potentiellement
                # exécutable) par `run_triangular_scan`, pas la peine de dupliquer.
                if pair_ab["dex_id"] == pair_bc["dex_id"] == pair_ca["dex_id"]:
                    continue

                cycle_multiplier = rate_ab * rate_bc * rate_ca
                spread_pct = (cycle_multiplier - 1) * 100
                if spread_pct < min_spread_pct_to_log:
                    continue

                min_liquidity = min(
                    pair_ab["liquidity_usd"], pair_bc["liquidity_usd"], pair_ca["liquidity_usd"]
                )

                signal = {
                    "chain": chain_id,
                    "token_a_symbol": token_a_symbol, "token_a_address": token_a_addr,
                    "token_b_symbol": token_b_symbol, "token_b_address": token_b_addr,
                    "token_c_symbol": token_c_symbol, "token_c_address": token_c_addr,
                    "dex_ab": pair_ab["dex_id"], "dex_bc": pair_bc["dex_id"], "dex_ca": pair_ca["dex_id"],
                    "cycle_multiplier": cycle_multiplier,
                    "spread_pct": spread_pct,
                    "min_liquidity_usd": min_liquidity,
                }
                signal["id"] = db.add_cross_dex_triangular_signal(signal)
                signals.append(signal)

    if signals:
        logger.info(
            "Cross-DEX triangular scan (informationnel, non exécutable) : %d cycle(s) détecté(s).",
            len(signals),
        )
    return signals
