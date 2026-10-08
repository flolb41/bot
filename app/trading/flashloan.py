"""Exécution d'arbitrage financée par flashloan Aave V3 — contrat
`FlashArbitrage` déjà déployé RÉELLEMENT sur Base mainnet (adresse
`app.trading.routers.FLASH_ARBITRAGE_ADDRESS`, voir `contracts/README.md`
pour le tx hash de déploiement et la validation Remix VM effectuée avant ce
déploiement réel).

Pourquoi un module séparé plutôt qu'intégré à `executor.py` :
- Modèle de risque différent : le wallet ne fournit JAMAIS le capital tradé
  (Aave le prête directement AU CONTRAT, pas au wallet) — le wallet ne paie
  que le gas de la tentative. Toute la séquence (emprunt + swaps +
  remboursement) est UNE SEULE transaction atomique : soit elle réussit et
  transfère le profit net au wallet, soit elle `revert` intégralement (le
  contrat refuse lui-même de rembourser si le profit minimum n'est pas
  couvert, voir `contracts/FlashArbitrage.sol::executeOperation`). Aucun état
  intermédiaire possible, contrairement au round-trip classique à 2
  transactions de `executor.execute_signal` (pas d'`approve` wallet non plus :
  les fonds empruntés n'entrent jamais dans le wallet).
- Permet de trader un montant INDÉPENDANT du solde réel du wallet (objectif :
  dépasser la contrainte de capital qui limite `execute_signal`/
  `execute_triangular_signal` à une fraction du solde réel détenu).

DEX supportés : forks UniswapV2 classiques + Aerodrome + Uniswap V3 (single-hop
uniquement, `exactInputSingle` sur SwapRouter02 — voir `_build_leg` kind=2 et
`contracts/FlashArbitrage.sol::IUniswapV3SwapRouter02`). Le multi-hop V3
(`exactInput`, chemin bytes packé) reste hors scope : c'est pourquoi Uniswap V3
reste exclu de `app.trading.triangular.TRIANGULAR_CAPABLE_DEXES` (cycle à 3
jambes nécessiterait soit 3 appels single-hop distincts côté scanner — non
implémenté côté détection triangulaire — soit l'encodage multi-hop).

Activation : désactivée par défaut (`trading.flashloan_enabled: false` dans
config.yaml), EN PLUS de toutes les conditions de
`app.trading.guardrails.is_trading_live` (même garde-fou que le reste du
trading live) — décision explicite requise avant la première utilisation
réelle, même si l'unique risque d'une tentative ratée est le gas dépensé.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from app.database import Database
from app.trading.executor import (
    EXECUTABLE_QUOTE_TOKENS,
    _find_uniswap_v3_fee_tier,
    _get_decimals,
    _quote_token_usd_price,
    _refresh_price_usd,
    _resolve_aerodrome_route,
    _resolve_pancakeswap_route,
    _resolve_rpc_url,
)
from app.trading.fees import DEFAULT_SLIPPAGE_BUFFER_PCT, dex_fee_pct, estimate_gas_cost_usd
from app.trading.guardrails import TradingRefused, is_trading_live, send_trading_transaction
from app.trading.routers import EXECUTABLE_DEXES, FLASH_ARBITRAGE_ADDRESS, ROUTER_ADDRESSES

logger = logging.getLogger("app.trading.flashloan")

# Forks UniswapV2 classiques + Aerodrome + Uniswap V3 (single-hop, voir
# docstring ci-dessus) — par contraste, `triangular.TRIANGULAR_CAPABLE_DEXES`
# exclut toujours Uniswap V3 (scope différent, cycles à 3 jambes non
# supportés côté scanner triangulaire).
FLASHLOAN_CAPABLE_DEXES = frozenset({
    "uniswap", "aerodrome", "sushiswap", "pancakeswap", "baseswap", "alien-base", "swapbased",
})

_ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"

# Prime de flashloan Aave V3 standard (0,05 %, documentation Aave) : utilisée
# UNIQUEMENT pour l'estimation de rentabilité off-chain avant d'envoyer la
# transaction — le contrat calcule et exige le remboursement EXACT on-chain
# (`premium` fourni par le Pool à `executeOperation`), cette constante ne sert
# jamais à calculer le montant réellement remboursé.
AAVE_V3_FLASHLOAN_PREMIUM_PCT = 0.05

# Gas plus élevé qu'un swap classique : emprunt + N swaps + remboursement dans
# UNE SEULE transaction (surestimation volontaire, même principe prudent que
# `fees.ESTIMATED_GAS_UNITS_ROUNDTRIP`).
ESTIMATED_GAS_UNITS_FLASHLOAN_PAIR = 550_000
ESTIMATED_GAS_UNITS_FLASHLOAN_TRIANGULAR = 700_000

_ABI_CACHE: list[dict] | None = None


def _flash_arbitrage_abi() -> list[dict]:
    global _ABI_CACHE
    if _ABI_CACHE is None:
        artifact_path = Path(__file__).resolve().parents[2] / "contracts" / "FlashArbitrage.compiled.json"
        with artifact_path.open("r", encoding="utf-8") as f:
            _ABI_CACHE = json.load(f)["abi"]
    return _ABI_CACHE


def flashloan_enabled(config: dict) -> bool:
    return bool(((config or {}).get("trading") or {}).get("flashloan_enabled", False))


def flashloan_notional_usd(config: dict) -> float:
    return float(((config or {}).get("trading") or {}).get("flashloan_notional_usd", 1000))


def flashloan_min_net_profit_usd(config: dict) -> float:
    return float(((config or {}).get("trading") or {}).get("flashloan_min_net_profit_usd", 1.0))


def flashloan_liquidity_safety_fraction(config: dict) -> float:
    return float(((config or {}).get("trading") or {}).get("liquidity_safety_fraction", 0.02))


def pair_signal_is_flashloan_eligible(config: dict, signal: dict) -> bool:
    """Décision de ROUTAGE pour le scheduler (pas une garantie d'exécution :
    `execute_pair_signal_via_flashloan` revalide tout en interne, y compris
    une revérification de prix en direct). Un signal 'pair' est éligible si le
    flashloan est activé et que ses deux DEX supportent l'interface V2/Aerodrome
    attendue par le contrat, avec un quote token exécutable."""
    return (
        flashloan_enabled(config)
        and signal.get("buy_dex") in FLASHLOAN_CAPABLE_DEXES
        and signal.get("sell_dex") in FLASHLOAN_CAPABLE_DEXES
        and (signal.get("quote_address") or "").lower() in EXECUTABLE_QUOTE_TOKENS
    )


def triangular_signal_is_flashloan_eligible(config: dict, signal: dict) -> bool:
    """Décision de ROUTAGE pour le scheduler, même principe que ci-dessus."""
    return (
        flashloan_enabled(config)
        and signal.get("dex") in FLASHLOAN_CAPABLE_DEXES
        and (signal.get("token_a_address") or "").lower() in EXECUTABLE_QUOTE_TOKENS
    )


def estimate_pair_signal_flashloan_preview(config: dict, signal: dict) -> dict | None:
    """Aperçu (dashboard/API/Telegram UNIQUEMENT — aucune transaction envoyée) du
    notionnel et du profit net qu'aurait ce signal 'pair' s'il était exécuté via
    flashloan plutôt qu'en financement classique. Calculé à partir des données
    DÉJÀ mesurées par le scan (`spread_pct`, liquidités) — contrairement à
    `execute_pair_signal_via_flashloan`, qui revalide les prix en direct avant
    d'envoyer la vraie transaction, ceci est une ESTIMATION affichée en amont,
    pas une garantie de rentabilité au moment d'une éventuelle exécution.
    Retourne `None` si le signal n'est pas éligible flashloan, si la liquidité
    ne permet aucun notionnel positif, ou si le coût de gas Base est
    indisponible."""
    if not pair_signal_is_flashloan_eligible(config, signal):
        return None
    safety_fraction = flashloan_liquidity_safety_fraction(config)
    min_liquidity_usd = min(signal.get("buy_liquidity_usd") or 0.0, signal.get("sell_liquidity_usd") or 0.0)
    upper_bound_usd = min(flashloan_notional_usd(config), safety_fraction * min_liquidity_usd)
    if upper_bound_usd <= 0:
        return None
    profit_fn = lambda n: _pair_profit_before_gas_usd(  # noqa: E731
        n, spread_pct=signal.get("spread_pct") or 0.0, buy_dex=signal["buy_dex"], sell_dex=signal["sell_dex"],
        buy_liquidity_usd=signal.get("buy_liquidity_usd"), sell_liquidity_usd=signal.get("sell_liquidity_usd"),
    )
    notional_usd = _optimal_flashloan_notional_usd(upper_bound_usd, profit_fn)
    _, gas_cost_usd = _estimate_premium_and_gas_cost_usd(notional_usd, ESTIMATED_GAS_UNITS_FLASHLOAN_PAIR)
    if gas_cost_usd is None:
        return None
    net_profit_usd = profit_fn(notional_usd) - gas_cost_usd
    return {"notional_usd": notional_usd, "net_profit_usd": net_profit_usd}


def estimate_triangular_signal_flashloan_preview(config: dict, signal: dict) -> dict | None:
    """Équivalent triangulaire de `estimate_pair_signal_flashloan_preview` — même
    principe (aperçu affiché en amont, aucune transaction, aucune revérification
    de prix en direct)."""
    if not triangular_signal_is_flashloan_eligible(config, signal):
        return None
    safety_fraction = flashloan_liquidity_safety_fraction(config)
    min_liquidity_usd = signal.get("min_liquidity_usd") or 0.0
    upper_bound_usd = min(flashloan_notional_usd(config), safety_fraction * min_liquidity_usd)
    if upper_bound_usd <= 0:
        return None
    profit_fn = lambda n: _triangular_profit_before_gas_usd(  # noqa: E731
        n, spread_pct=signal.get("spread_pct") or 0.0, dex=signal["dex"], min_liquidity_usd=min_liquidity_usd,
    )
    notional_usd = _optimal_flashloan_notional_usd(upper_bound_usd, profit_fn)
    _, gas_cost_usd = _estimate_premium_and_gas_cost_usd(notional_usd, ESTIMATED_GAS_UNITS_FLASHLOAN_TRIANGULAR)
    if gas_cost_usd is None:
        return None
    net_profit_usd = profit_fn(notional_usd) - gas_cost_usd
    return {"notional_usd": notional_usd, "net_profit_usd": net_profit_usd}


def _build_leg(dex: str, w3, *, token_in: str, token_out: str) -> tuple:
    """Construit une jambe `(kind, router, tokenIn, tokenOut, aerodromeStable,
    aerodromeFactory, fee)` — ordre EXACT de la struct `FlashArbitrage.Leg`
    (voir contracts/FlashArbitrage.sol). `kind=0` pour un fork UniswapV2
    classique, `kind=1` pour Aerodrome classique (nécessite sa
    `defaultFactory` on-chain), `kind=2` pour Uniswap V3 OU PancakeSwap V3
    single-hop (`exactInputSingle`, ABI identique — nécessite le fee tier de
    la pool concernée), `kind=3` pour Aerodrome Slipstream single-hop
    (`exactInputSingle` avec `tickSpacing` au lieu de `fee`, voir
    `contracts/FlashArbitrage.sol::ISlipstreamSwapRouter` — le champ `fee`
    (uint24) de la struct Leg est réutilisé pour stocker le tickSpacing,
    toujours positif et petit, aucun changement de struct nécessaire).

    Pour "aerodrome"/"pancakeswap", le kind/router/fee sont déterminés par
    `executor._resolve_aerodrome_route`/`_resolve_pancakeswap_route` — LES
    MÊMES fonctions que celles utilisées par `executor._build_swap_call` et
    `_onchain_pool_price_usd`, pour rester cohérent sur la pool réellement
    routée (classique vs concentrated-liquidity)."""
    from web3 import Web3

    if dex not in FLASHLOAN_CAPABLE_DEXES or dex not in EXECUTABLE_DEXES:
        raise TradingRefused(f"DEX '{dex}' non supporté par le contrat FlashArbitrage.")
    router_cs = Web3.to_checksum_address(ROUTER_ADDRESSES[dex])
    token_in_cs = Web3.to_checksum_address(token_in)
    token_out_cs = Web3.to_checksum_address(token_out)
    if dex == "aerodrome":
        route = _resolve_aerodrome_route(w3, token_in, token_out)
        if route is None:
            raise TradingRefused(f"Aucune pool Aerodrome (classique ou Slipstream) trouvée pour {token_in}/{token_out}.")
        if route["is_slipstream"]:
            router_v3 = Web3.to_checksum_address(route["router"])
            return (3, router_v3, token_in_cs, token_out_cs, False, _ZERO_ADDRESS, route["tick_spacing"])
        return (1, router_cs, token_in_cs, token_out_cs, False, route["default_factory"], 0)
    if dex == "pancakeswap":
        route = _resolve_pancakeswap_route(w3, token_in, token_out)
        if route is None:
            raise TradingRefused(f"Aucune pool PancakeSwap (v2 ou v3) trouvée pour {token_in}/{token_out}.")
        if route["is_v3"]:
            router_v3 = Web3.to_checksum_address(route["router"])
            return (2, router_v3, token_in_cs, token_out_cs, False, _ZERO_ADDRESS, route["fee"])
        return (0, router_cs, token_in_cs, token_out_cs, False, _ZERO_ADDRESS, 0)
    if dex == "uniswap":
        fee_tier = _find_uniswap_v3_fee_tier(w3, token_in, token_out)
        return (2, router_cs, token_in_cs, token_out_cs, False, _ZERO_ADDRESS, fee_tier)
    return (0, router_cs, token_in_cs, token_out_cs, False, _ZERO_ADDRESS, 0)


def _amm_price_impact_pct(notional_usd: float, pool_liquidity_usd: float | None) -> float:
    """Estimation du slippage réel subi en swappant `notional_usd` dans un pool
    à produit constant (x*y=k) de profondeur totale `pool_liquidity_usd` (TVL
    approximatif, les deux côtés du pool confondus — on suppose ~moitié de
    cette valeur du côté du token swappé, approximation standard).

    Remplace le tampon fixe `DEFAULT_SLIPPAGE_BUFFER_PCT` (0,20 %, pensé pour
    les trades classiques à 3-5 $) : à l'échelle flashloan (ex: 1000 $), ce
    tampon fixe sous-estime massivement le coût réel sur des pools peu
    profonds — cas réel observé : BRETT/WETH sur PancakeSwap (~195k$ de TVL)
    affichait +5,67 $ de profit net estimé au tampon fixe, alors que la
    revérification on-chain juste avant l'envoi de la transaction trouvait un
    profit réel négatif (-7 à -8 $), le spread s'étant refermé entre-temps sur
    une paire fine et volatile. Ce modèle de slippage, appliqué en PLUS de la
    revérification de prix, rend l'estimation affichée cohérente avec ce que
    le marché impose réellement sur un pool de cette taille."""
    if not pool_liquidity_usd or pool_liquidity_usd <= 0:
        return 100.0  # Liquidité inconnue/nulle -> slippage jugé prohibitif.
    half_liquidity_usd = pool_liquidity_usd / 2
    return (notional_usd / (notional_usd + half_liquidity_usd)) * 100


def _estimate_premium_and_gas_cost_usd(notional_usd: float, gas_units: int) -> tuple[float, float | None]:
    from app.wallet.pricing import get_price_usd

    premium_usd = notional_usd * AAVE_V3_FLASHLOAN_PREMIUM_PCT / 100
    eth_usd_price = get_price_usd("ethereum")
    gas_cost_usd = estimate_gas_cost_usd(eth_usd_price, gas_units=gas_units)
    return premium_usd, gas_cost_usd


def _pair_profit_before_gas_usd(
    notional_usd: float, *, spread_pct: float, buy_dex: str, sell_dex: str,
    buy_liquidity_usd: float | None, sell_liquidity_usd: float | None,
) -> float:
    """Profit net (hors gas, qui ne dépend pas du notionnel) d'un signal 'pair'
    financé par flashloan à `notional_usd` — fonction CONCAVE de `notional_usd`
    (le gain brut et les frais DEX/prime Aave sont linéaires, le slippage AMM
    est convexe), utilisée à la fois pour calculer le profit affiché ET pour
    trouver le notionnel optimal via `_optimal_flashloan_notional_usd`."""
    gross_profit_usd = notional_usd * spread_pct / 100
    fees_usd = notional_usd * (dex_fee_pct(buy_dex) + dex_fee_pct(sell_dex)) / 100
    amm_impact_pct = _amm_price_impact_pct(notional_usd, buy_liquidity_usd) + _amm_price_impact_pct(
        notional_usd, sell_liquidity_usd
    )
    slippage_buffer_usd = notional_usd * max(DEFAULT_SLIPPAGE_BUFFER_PCT, amm_impact_pct) / 100
    premium_usd = notional_usd * AAVE_V3_FLASHLOAN_PREMIUM_PCT / 100
    return gross_profit_usd - fees_usd - slippage_buffer_usd - premium_usd


def _triangular_profit_before_gas_usd(
    notional_usd: float, *, spread_pct: float, dex: str, min_liquidity_usd: float | None,
) -> float:
    """Équivalent triangulaire de `_pair_profit_before_gas_usd` — même principe,
    3 jambes sur le même DEX, slippage AMM appliqué par prudence aux 3 jambes
    avec la liquidité minimale du cycle (seule connue, voir `run_triangular_scan`)."""
    fee_pct_total = 3 * dex_fee_pct(dex)
    gross_profit_usd = notional_usd * spread_pct / 100
    fees_usd = notional_usd * fee_pct_total / 100
    amm_impact_pct = 3 * _amm_price_impact_pct(notional_usd, min_liquidity_usd)
    slippage_buffer_usd = notional_usd * max(DEFAULT_SLIPPAGE_BUFFER_PCT, amm_impact_pct) / 100
    premium_usd = notional_usd * AAVE_V3_FLASHLOAN_PREMIUM_PCT / 100
    return gross_profit_usd - fees_usd - slippage_buffer_usd - premium_usd


def _optimal_flashloan_notional_usd(upper_bound_usd: float, profit_before_gas_fn) -> float:
    """Trouve, par recherche ternaire, le notionnel dans `[0, upper_bound_usd]`
    qui MAXIMISE `profit_before_gas_fn(notional_usd)`.

    Remplace l'ancien dimensionnement figé (toujours `min(flashloan_notional_usd,
    plafond de liquidité)`, qui gaspillait du profit sur les opportunités où ce
    montant fixe était trop PRUDENT, et en perdait trop sur celles où il était
    trop AGRESSIF vis-à-vis de la liquidité réelle du pool). `profit_before_gas_fn`
    est CONCAVE en `notional_usd` ici (gain brut/frais/prime linéaires, slippage
    AMM `x*y=k` convexe — voir `_pair_profit_before_gas_usd`/
    `_triangular_profit_before_gas_usd`), ce qui garantit la convergence de la
    recherche ternaire vers le maximum global en quelques dizaines d'itérations,
    sans dérivée à calculer analytiquement."""
    if upper_bound_usd <= 0:
        return 0.0
    lo, hi = 0.0, upper_bound_usd
    for _ in range(50):
        m1 = lo + (hi - lo) / 3
        m2 = hi - (hi - lo) / 3
        if profit_before_gas_fn(m1) < profit_before_gas_fn(m2):
            lo = m1
        else:
            hi = m2
    return (lo + hi) / 2


def execute_pair_signal_via_flashloan(db: Database, config: dict, signal: dict) -> dict:
    """Exécute un signal 'pair' (`app.trading.arbitrage`) en UNE SEULE
    transaction financée par flashloan Aave V3, au lieu du round-trip
    classique à 2 transactions auto-financées de `executor.execute_signal`.
    Retourne toujours `{"executed": bool, "tx_hash": str|None, "error": str|None}`
    (même forme que `executor.execute_triangular_signal`, consommée telle
    quelle par `app.scheduler.run_arbitrage_scan_job`)."""
    result: dict = {"executed": False, "tx_hash": None, "error": None}

    if not is_trading_live(config):
        result["error"] = "Trading live désactivé (voir app.trading.guardrails.is_trading_live)."
        return result
    if not flashloan_enabled(config):
        result["error"] = "Flashloan désactivé (trading.flashloan_enabled: false dans config.yaml)."
        return result
    if signal["buy_dex"] not in FLASHLOAN_CAPABLE_DEXES or signal["sell_dex"] not in FLASHLOAN_CAPABLE_DEXES:
        result["error"] = (
            f"DEX non supporté par le contrat flashloan ({signal['buy_dex']}/{signal['sell_dex']}) "
            f"— seuls {sorted(FLASHLOAN_CAPABLE_DEXES)} sont implémentés."
        )
        return result
    quote_address = signal["quote_address"]
    if quote_address.lower() not in EXECUTABLE_QUOTE_TOKENS:
        result["error"] = f"Quote token {signal['quote_symbol']} non exécutable — seuls USDC/WETH sont supportés."
        return result
    if not signal.get("would_execute"):
        result["error"] = "Signal non marqué rentable (would_execute=False) — refus par prudence."
        return result

    try:
        from web3 import Web3

        w3 = Web3(Web3.HTTPProvider(_resolve_rpc_url(), request_kwargs={"timeout": 15}))
        if not w3.is_connected():
            result["error"] = "RPC Base injoignable."
            return result

        token_address = signal["token_address"]
        quote_decimals = _get_decimals(w3, quote_address)
        quote_usd_price = _quote_token_usd_price(quote_address)
        if not quote_usd_price or quote_usd_price <= 0:
            result["error"] = f"Prix USD de {signal['quote_symbol']} indisponible — refus par prudence."
            return result

        # Dimensionnement : plafond du notionnel emprunté, INDÉPENDANT du solde
        # réel du wallet, borné par la liquidité des deux pools (même
        # garde-fou que la détection classique, voir `liquidity_safety_fraction`).
        safety_fraction = flashloan_liquidity_safety_fraction(config)
        min_liquidity_usd = min(signal["buy_liquidity_usd"], signal["sell_liquidity_usd"])
        upper_bound_usd = min(flashloan_notional_usd(config), safety_fraction * min_liquidity_usd)
        if upper_bound_usd <= 0:
            result["error"] = "Liquidité de pool insuffisante pour un notionnel flashloan positif — refus."
            return result

        # Revérification du prix en direct (le spread détecté peut s'être
        # refermé depuis le dernier scan) — même mitigation que `execute_signal`.
        fresh_buy_price = _refresh_price_usd(signal["chain"], token_address, quote_address, signal["buy_dex"])
        fresh_sell_price = _refresh_price_usd(signal["chain"], token_address, quote_address, signal["sell_dex"])
        if fresh_buy_price is None or fresh_sell_price is None:
            result["error"] = "Impossible de revérifier les prix avant la tentative flashloan — refus par prudence."
            return result
        fresh_spread_pct = (fresh_sell_price - fresh_buy_price) / fresh_buy_price * 100 if fresh_buy_price else 0.0

        # Notionnel optimisé (recherche ternaire) sur le spread FRAÎCHEMENT
        # revalidé, pas sur celui du scan — maximise le profit réel attendu
        # au lieu d'un montant figé en config (voir `_optimal_flashloan_notional_usd`).
        profit_fn = lambda n: _pair_profit_before_gas_usd(  # noqa: E731
            n, spread_pct=fresh_spread_pct, buy_dex=signal["buy_dex"], sell_dex=signal["sell_dex"],
            buy_liquidity_usd=signal["buy_liquidity_usd"], sell_liquidity_usd=signal["sell_liquidity_usd"],
        )
        notional_usd = _optimal_flashloan_notional_usd(upper_bound_usd, profit_fn)
        premium_usd, gas_cost_usd = _estimate_premium_and_gas_cost_usd(notional_usd, ESTIMATED_GAS_UNITS_FLASHLOAN_PAIR)
        if gas_cost_usd is None:
            result["error"] = "Coût de gas Base indisponible — refus par prudence."
            return result
        net_profit_usd = profit_fn(notional_usd) - gas_cost_usd
        min_required_usd = flashloan_min_net_profit_usd(config)
        if net_profit_usd < min_required_usd:
            result["error"] = (
                f"Profit net estimé après prime Aave+gas ({net_profit_usd:.4f}$) < seuil flashloan "
                f"({min_required_usd:.4f}$) — refus par prudence, aucune transaction envoyée."
            )
            return result

        amount_units = int((notional_usd / quote_usd_price) * (10 ** quote_decimals))
        min_profit_units = int((min_required_usd / quote_usd_price) * (10 ** quote_decimals))

        legs = [
            _build_leg(signal["buy_dex"], w3, token_in=quote_address, token_out=token_address),
            _build_leg(signal["sell_dex"], w3, token_in=token_address, token_out=quote_address),
        ]

        send_result = send_trading_transaction(
            db,
            rpc_url=_resolve_rpc_url(),
            contract_address=FLASH_ARBITRAGE_ADDRESS,
            abi=_flash_arbitrage_abi(),
            function_name="startArbitrage",
            args=(Web3.to_checksum_address(quote_address), amount_units, legs, min_profit_units),
            whitelist_target=FLASH_ARBITRAGE_ADDRESS,
            action_label=(
                f"flashloan arbitrage {signal['token_symbol']}/{signal['quote_symbol']} "
                f"({signal['buy_dex']}→{signal['sell_dex']}, notionnel≈{notional_usd:.2f}$)"
            ),
        )
        result["tx_hash"] = send_result["tx_hash"]
        if send_result["error"]:
            result["error"] = f"Flashloan échoué (revert complet, aucun fonds déplacé) : {send_result['error']}"
            db.mark_arbitrage_signal_executed(
                signal["id"], tx_hash_buy=result["tx_hash"], tx_hash_sell=result["tx_hash"],
                execution_error=result["error"],
            )
            return result

        result["executed"] = True
        db.mark_arbitrage_signal_executed(signal["id"], tx_hash_buy=result["tx_hash"], tx_hash_sell=result["tx_hash"])
        db.add_notification(
            title="⚡ Flashloan d'arbitrage exécuté",
            message=(
                f"{signal['token_symbol']}/{signal['quote_symbol']} : {signal['buy_dex']}→{signal['sell_dex']} "
                f"(notionnel≈{notional_usd:.2f}$, profit net estimé≈{net_profit_usd:.2f}$) — tx {result['tx_hash']}"
            ),
            level="info",
        )
        return result

    except TradingRefused as exc:
        result["error"] = f"Refusé par un garde-fou : {exc}"
        return result
    except Exception as exc:  # noqa: BLE001 - jamais de crash du scheduler pour une tentative de trade
        logger.exception("Erreur inattendue pendant execute_pair_signal_via_flashloan")
        result["error"] = f"Erreur inattendue : {exc}"
        return result


def execute_triangular_signal_via_flashloan(db: Database, config: dict, signal: dict) -> dict:
    """Équivalent flashloan de `executor.execute_triangular_signal` : un cycle
    triangulaire à 3 jambes sur le même DEX, financé par flashloan au lieu du
    capital propre du wallet. Retourne toujours
    `{"executed": bool, "tx_hash": str|None, "error": str|None}`."""
    result: dict = {"executed": False, "tx_hash": None, "error": None}

    if not is_trading_live(config):
        result["error"] = "Trading live désactivé (voir app.trading.guardrails.is_trading_live)."
        return result
    if not flashloan_enabled(config):
        result["error"] = "Flashloan désactivé (trading.flashloan_enabled: false dans config.yaml)."
        return result
    if signal["dex"] not in FLASHLOAN_CAPABLE_DEXES:
        result["error"] = (
            f"DEX non supporté par le contrat flashloan ({signal['dex']}) — seuls "
            f"{sorted(FLASHLOAN_CAPABLE_DEXES)} sont implémentés."
        )
        return result
    token_a = signal["token_a_address"]
    if token_a.lower() not in EXECUTABLE_QUOTE_TOKENS:
        result["error"] = f"Token de départ {signal['token_a_symbol']} non exécutable — seuls USDC/WETH sont supportés."
        return result
    if not signal.get("would_execute"):
        result["error"] = "Signal non marqué rentable (would_execute=False) — refus par prudence."
        return result

    try:
        from web3 import Web3

        w3 = Web3(Web3.HTTPProvider(_resolve_rpc_url(), request_kwargs={"timeout": 15}))
        if not w3.is_connected():
            result["error"] = "RPC Base injoignable."
            return result

        token_b = signal["token_b_address"]
        token_c = signal["token_c_address"]
        token_a_decimals = _get_decimals(w3, token_a)
        quote_usd_price = _quote_token_usd_price(token_a)
        if not quote_usd_price or quote_usd_price <= 0:
            result["error"] = f"Prix USD de {signal['token_a_symbol']} indisponible — refus par prudence."
            return result

        safety_fraction = flashloan_liquidity_safety_fraction(config)
        upper_bound_usd = min(flashloan_notional_usd(config), safety_fraction * signal["min_liquidity_usd"])
        if upper_bound_usd <= 0:
            result["error"] = "Liquidité de pool insuffisante pour un notionnel flashloan positif — refus."
            return result

        # Le spread mesuré à la détection (`cycle_multiplier`/`spread_pct`)
        # reste la meilleure estimation disponible pour un cycle à 3 jambes
        # sur un même DEX : pas de "prix frais" simple à revérifier ici sans
        # reconstruire tout le scan triangulaire. Le floor on-chain `minProfit`
        # protège contre un écart refermé entre détection et exécution.
        # Notionnel optimisé par recherche ternaire (voir
        # `_optimal_flashloan_notional_usd`) au lieu d'un montant figé en config.
        profit_fn = lambda n: _triangular_profit_before_gas_usd(  # noqa: E731
            n, spread_pct=signal["spread_pct"], dex=signal["dex"], min_liquidity_usd=signal["min_liquidity_usd"],
        )
        notional_usd = _optimal_flashloan_notional_usd(upper_bound_usd, profit_fn)
        premium_usd, gas_cost_usd = _estimate_premium_and_gas_cost_usd(
            notional_usd, ESTIMATED_GAS_UNITS_FLASHLOAN_TRIANGULAR
        )
        if gas_cost_usd is None:
            result["error"] = "Coût de gas Base indisponible — refus par prudence."
            return result
        net_profit_usd = profit_fn(notional_usd) - gas_cost_usd
        min_required_usd = flashloan_min_net_profit_usd(config)
        if net_profit_usd < min_required_usd:
            result["error"] = (
                f"Profit net estimé après prime Aave+gas ({net_profit_usd:.4f}$) < seuil flashloan "
                f"({min_required_usd:.4f}$) — refus par prudence, aucune transaction envoyée."
            )
            return result

        amount_units = int((notional_usd / quote_usd_price) * (10 ** token_a_decimals))
        min_profit_units = int((min_required_usd / quote_usd_price) * (10 ** token_a_decimals))

        legs = [
            _build_leg(signal["dex"], w3, token_in=token_a, token_out=token_b),
            _build_leg(signal["dex"], w3, token_in=token_b, token_out=token_c),
            _build_leg(signal["dex"], w3, token_in=token_c, token_out=token_a),
        ]

        send_result = send_trading_transaction(
            db,
            rpc_url=_resolve_rpc_url(),
            contract_address=FLASH_ARBITRAGE_ADDRESS,
            abi=_flash_arbitrage_abi(),
            function_name="startArbitrage",
            args=(Web3.to_checksum_address(token_a), amount_units, legs, min_profit_units),
            whitelist_target=FLASH_ARBITRAGE_ADDRESS,
            action_label=(
                f"flashloan triangulaire {signal['token_a_symbol']}→{signal['token_b_symbol']}→"
                f"{signal['token_c_symbol']}→{signal['token_a_symbol']} sur {signal['dex']} "
                f"(notionnel≈{notional_usd:.2f}$)"
            ),
        )
        result["tx_hash"] = send_result["tx_hash"]
        if send_result["error"]:
            result["error"] = f"Flashloan échoué (revert complet, aucun fonds déplacé) : {send_result['error']}"
            db.mark_triangular_signal_executed(signal["id"], tx_hash=result["tx_hash"], execution_error=result["error"])
            return result

        result["executed"] = True
        db.mark_triangular_signal_executed(signal["id"], tx_hash=result["tx_hash"])
        db.add_notification(
            title="⚡ Flashloan triangulaire exécuté",
            message=(
                f"{signal['token_a_symbol']}→{signal['token_b_symbol']}→{signal['token_c_symbol']}→"
                f"{signal['token_a_symbol']} sur {signal['dex']} (notionnel≈{notional_usd:.2f}$, "
                f"profit net estimé≈{net_profit_usd:.2f}$) — tx {result['tx_hash']}"
            ),
            level="info",
        )
        return result

    except TradingRefused as exc:
        result["error"] = f"Refusé par un garde-fou : {exc}"
        return result
    except Exception as exc:  # noqa: BLE001
        logger.exception("Erreur inattendue pendant execute_triangular_signal_via_flashloan")
        result["error"] = f"Erreur inattendue : {exc}"
        return result
