"""Moteur d'EXÉCUTION LIVE de l'arbitrage inter-DEX — Phase 2.

LIS EN ENTIER AVANT DE MODIFIER CE FICHIER : il manipule de vraies transactions
avec le wallet MetaMask PRINCIPAL de l'utilisateur (voir `app/wallet/signer.py`
`import_wallet_from_private_key` et `app/trading/guardrails.py`).

─────────────────────────────────────────────────────────────────────────────
CONDITIONS QUI DOIVENT TOUTES ÊTRE RÉUNIES AVANT QU'UNE SEULE TRANSACTION
RÉELLE PUISSE ÊTRE ENVOYÉE (voir `app.trading.guardrails.is_trading_live`) :
─────────────────────────────────────────────────────────────────────────────
1. `trading.enabled: true` dans config.yaml
2. `TRADING_ENABLED=true` dans l'environnement (.env du Pi, jamais Git)
3. `TRADING_LIVE_CONFIRMED=<phrase exacte>` dans l'environnement
4. Un keystore existe à `WALLET_TRADING_KEYSTORE_PATH` (créé une seule fois,
   manuellement, via `python main.py import-trading-wallet` exécuté EN SSH
   DIRECTEMENT SUR LE PI — jamais via un script/commande distante)
5. Le killswitch global n'est pas actif

Tant qu'une seule de ces conditions manque (c'est le cas par défaut), ce
module se comporte exactement comme avant : aucune transaction n'est tentée,
tout reste en mode simulation (voir `app/trading/arbitrage.py`).

─────────────────────────────────────────────────────────────────────────────
PÉRIMÈTRE VOLONTAIREMENT RESTREINT (choix de sécurité explicites)
─────────────────────────────────────────────────────────────────────────────
- DEX exécutables : uniquement uniswap, aerodrome, sushiswap, pancakeswap,
  baseswap, alien-base (voir `app.trading.routers.EXECUTABLE_DEXES`). PancakeSwap utilise ici
  uniquement son Router v2 classique (`swapExactTokensForTokens`, vérifié sur
  deux sources indépendantes — voir `app/trading/routers.py`) ; son "Smart
  Router"/Universal Router (encodage multi-route) n'est jamais utilisé.
- Quote tokens exécutables : USDC et WETH (`EXECUTABLE_QUOTE_TOKENS`). Un
  round-trip part et termine dans LE MÊME quote token (jamais un mélange) —
  pour ne jamais finir avec un "reste" de valeur imprévisible si la 2e jambe
  échoue. WETH étant volatile (contrairement à l'USDC), son solde est
  reconverti en $ via `get_quote_trading_balance_usd` (prix CoinGecko) pour
  le dimensionnement dynamique des trades (voir `app/trading/arbitrage.py`).
- Risque non-atomique assumé : 2 transactions séparées (achat puis vente), pas
  un contrat atomique. Avant la 2e jambe, le prix est revérifié en direct ; si
  l'écart s'est refermé, le round-trip est annulé et le wallet garde le token
  acheté (pas de perte "forcée", juste un round-trip interrompu — à revendre
  manuellement ou au prochain cycle si le marché redevient favorable).
"""
from __future__ import annotations

import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor

from app.database import Database
from app.trading import dex_sources, fees
from app.trading.guardrails import (
    TradingRefused,
    is_trading_live,
    send_trading_transaction,
    slippage_pct,
)
from app.trading.rpc import call_with_rate_limit_retry, is_connected_with_retry, make_w3, rpc_urls
from app.trading.token_risk import quote_depth_floor, token_slippage_pct, token_trade_cap
from app.trading.routers import (
    AERODROME_POOL_ABI,
    AERODROME_ROUTER_ABI,
    AERODROME_SLIPSTREAM_FACTORIES,
    AERODROME_SLIPSTREAM_FACTORY_ABI,
    AERODROME_SLIPSTREAM_POOL_ABI,
    AERODROME_SLIPSTREAM_ROUTER_ABI,
    ERC20_ABI,
    EXECUTABLE_DEXES,
    PANCAKESWAP_V3_FACTORY_ABI,
    PANCAKESWAP_V3_FACTORY_ADDRESS,
    PANCAKESWAP_V3_FEE_TIERS,
    PANCAKESWAP_V3_POOL_ABI,
    PANCAKESWAP_V3_ROUTER_ABI,
    PANCAKESWAP_V3_ROUTER_ADDRESS,
    ROUTER_ADDRESSES,
    TRIANGULAR_V2_EXECUTABLE_DEXES,
    UNISWAP_V2_FACTORY_ABI,
    UNISWAP_V2_PAIR_ABI,
    UNISWAP_V2_ROUTER_ABI,
    UNISWAP_V2_ROUTER_FACTORY_ABI,
    UNISWAP_V3_FACTORY_ABI,
    UNISWAP_V3_FACTORY_ADDRESS,
    UNISWAP_V3_FEE_TIERS,
    UNISWAP_V3_POOL_ABI,
    UNISWAP_V3_ROUTER_ABI,
)

logger = logging.getLogger("app.trading.executor")

# USDC, WETH et BRETT sur Base — tokens "quote" exécutables. Un round-trip
# part et termine dans l'un de ces trois tokens ; le quote utilisé est porté
# explicitement par le signal (voir `_resolve_aerodrome_route`/`_resolve_pancakeswap_route`).
USDC_ADDRESS = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
WETH_ADDRESS = "0x4200000000000000000000000000000000000006"
BRETT_ADDRESS = "0x532f27101965dd16442e59d40670faf5ebb142e4"
EXECUTABLE_QUOTE_TOKENS = frozenset({USDC_ADDRESS, WETH_ADDRESS, BRETT_ADDRESS})

# Forks UniswapV2 classiques (ABI `swapExactTokensForTokens` standard,
# `factory()`/`getPair()` pour résoudre la pool unique) — voir `_build_swap_call`
# et `_onchain_pool_price_usd` ci-dessous.
UNISWAP_V2_FORK_DEXES = frozenset({"sushiswap", "pancakeswap", "baseswap", "alien-base", "swapbased"})

# Pour convertir le solde réel de chaque quote token en $ (dimensionnement
# dynamique des trades) : None = stablecoin (1 unité ≈ 1 $), sinon l'id
# CoinGecko à passer à `app.wallet.pricing.get_price_usd`.
_QUOTE_TOKEN_COINGECKO_ID: dict[str, str | None] = {
    USDC_ADDRESS: None,
    WETH_ADDRESS: "ethereum",
    BRETT_ADDRESS: "based-brett",
}

_DECIMALS_CACHE: dict[str, int] = {}

# Cache TTL (secondes) pour les résolutions de route/pool "la plus liquide"
# (`_best_v3_style_pool`, `_resolve_aerodrome_route`, `_resolve_pancakeswap_route`)
# — ces résolutions nécessitent jusqu'à 2 appels RPC PAR fee tier testé (jusqu'à
# 8 pour Uniswap/PancakeSwap V3 à 4 fee tiers), coût ajouté le 08/10 en
# remplaçant "premier fee tier existant" par "fee tier le plus liquide" (voir
# `_find_uniswap_v3_fee_tier`). Sans cache, LA MÊME paire était re-résolue
# plusieurs fois par cycle de scan (une fois par `_refine_spread_with_onchain_price`
# pour chaque côté du spread, puis à nouveau lors de la construction réelle du
# swap en cas de tentative d'exécution).
#
# Porté de 20s à 300s le 09/10 : avec un scan toutes les 60s, un TTL de 20s
# forçait une re-résolution COMPLÈTE à chaque cycle, soit des centaines
# d'appels RPC par minute — volume qui saturait les quotas des endpoints
# gratuits (429 en continu) et, via l'ancienne sémantique "erreur RPC = pool
# absente", corrompait le choix de pool (voir `_quote_reserve_in_pool`). La
# pool la plus profonde d'une paire ne change pas en 5 minutes.
_ROUTE_CACHE_TTL_SECONDS = 300.0
# Faits on-chain IMMUABLES (adresse d'une pool pour (factory, tokens, fee),
# adresse de factory d'un router, tick spacings d'une factory) : cachés bien
# plus longtemps. Seule une pool nouvellement créée pourrait être manquée
# pendant ce délai — négligeable pour les tokens seed suivis.
_IMMUTABLE_CACHE_TTL_SECONDS = 6 * 3600.0
_ROUTE_CACHE_MISSING = object()
_route_cache: dict[tuple, tuple[float, object]] = {}


def _route_cache_get(key: tuple) -> object:
    entry = _route_cache.get(key)
    if entry is None:
        return _ROUTE_CACHE_MISSING
    expires_at, value = entry
    if time.monotonic() > expires_at:
        _route_cache.pop(key, None)
        return _ROUTE_CACHE_MISSING
    return value


def _route_cache_set(key: tuple, value: object, ttl: float | None = None) -> None:
    _route_cache[key] = (time.monotonic() + (_ROUTE_CACHE_TTL_SECONDS if ttl is None else ttl), value)


def _cached_rpc_value(key: tuple, fn_call, ttl: float):
    """Lecture RPC mise en cache `ttl` secondes, avec retry sur 429. Une
    erreur RPC est PROPAGÉE (jamais cachée, jamais convertie en valeur par
    défaut) : voir `_quote_reserve_in_pool` pour la raison."""
    cached = _route_cache_get(key)
    if cached is not _ROUTE_CACHE_MISSING:
        return cached
    value = call_with_rate_limit_retry(fn_call)
    _route_cache_set(key, value, ttl=ttl)
    return value


def _pair_key(token_a: str, token_b: str) -> tuple[str, str]:
    """Clé de cache SYMÉTRIQUE pour une paire : la pool d'une paire ne dépend
    pas du sens du swap, inutile de la résoudre deux fois (achat puis vente)."""
    return tuple(sorted((token_a.lower(), token_b.lower())))  # type: ignore[return-value]


def _resolve_rpc_url() -> str:
    """Premier endpoint RPC configuré (voir `app.trading.rpc.rpc_urls`) —
    conservé pour les appelants qui attendent UNE URL (`send_trading_transaction`,
    logs) ; les connexions réelles passent par `make_w3` (rotation)."""
    return rpc_urls()[0]


def _is_connected_with_retry(w3) -> bool:
    """Alias de `app.trading.rpc.is_connected_with_retry` (conservé pour les
    imports existants, ex. `app.trading.flashloan`)."""
    return is_connected_with_retry(w3)


def _balance_of_with_retry(contract, address: str) -> int:
    """`contract.functions.balanceOf(address).call()` avec retry sur rate-limit
    RPC (429) — même raison que `_is_connected_with_retry` ci-dessus."""
    from web3 import Web3
    return int(call_with_rate_limit_retry(lambda: contract.functions.balanceOf(Web3.to_checksum_address(address)).call()))


def _newly_acquired_token_amount(balance_before: int, balance_after: int) -> int:
    return max(0, balance_after - balance_before)


def _get_decimals(w3, token_address: str) -> int:
    key = token_address.lower()
    if key not in _DECIMALS_CACHE:
        from web3 import Web3
        contract = w3.eth.contract(address=Web3.to_checksum_address(token_address), abi=ERC20_ABI)
        _DECIMALS_CACHE[key] = int(call_with_rate_limit_retry(lambda: contract.functions.decimals().call()))
    return _DECIMALS_CACHE[key]


def _quote_token_usd_price(quote_address: str) -> float | None:
    """Prix USD actuel du quote token `quote_address` : 1.0 pour un stablecoin
    (USDC), prix CoinGecko live pour un token volatile (WETH). Toutes les
    conversions USD <-> unités de quote token dans `execute_signal` passent
    par cette fonction — indispensable depuis l'ajout de WETH (sans elle, un
    budget de trade en $ serait traité comme un nombre d'ETH, soit ~2500x
    trop gros). Retourne `None` si indisponible OU si `quote_address` n'est pas
    un quote token connu (ni USDC ni WETH) : l'appelant doit alors refuser le
    trade par prudence plutôt que supposer un prix de 1$ pour un token
    volatile quelconque (bug corrigé le 2026-10-08 — `dict.get` renvoyait
    `None` aussi bien pour "c'est un stablecoin" que pour "token absent du
    dict", confondant les deux cas et traitant par erreur n'importe quel token
    inconnu comme un stablecoin à 1$)."""
    key = quote_address.lower()
    if key not in _QUOTE_TOKEN_COINGECKO_ID:
        return None
    coingecko_id = _QUOTE_TOKEN_COINGECKO_ID[key]
    if coingecko_id is None:
        return 1.0
    from app.wallet.pricing import get_price_usd
    return get_price_usd(coingecko_id)


def _actual_execution_metrics(
    profit_quote: float | None, quote_usd_price: float | None, gas_cost_wei: int | None,
) -> dict:
    gas_cost_usd = None
    realized_pnl_usd = None
    if gas_cost_wei is not None:
        from app.wallet.pricing import get_price_usd

        gas_cost_usd = fees.actual_gas_cost_usd(gas_cost_wei, get_price_usd("ethereum"))
        if profit_quote is not None:
            realized_pnl_usd = fees.realized_net_profit_usd(
                profit_quote, quote_usd_price, gas_cost_usd,
            )
    return {
        "realized_pnl_usd": realized_pnl_usd,
        "actual_profit_quote": profit_quote,
        "actual_gas_cost_wei": gas_cost_wei,
        "actual_gas_cost_usd": gas_cost_usd,
    }


# Marge tolérée entre un prix on-chain lu directement et un prix de référence
# (DexScreener, ou dernier prix connu) avant de considérer la lecture on-chain
# comme non fiable — voir `_onchain_price_is_plausible`.
def _net_token_transfer_from_receipts(
    token_address: str, wallet_address: str, receipts: list[dict | None],
) -> int | None:
    """Mesure les transferts du token dans ces transactions, sans le bruit d'autres mouvements du wallet."""
    from web3 import Web3

    token = token_address.lower()
    wallet = wallet_address.lower()
    transfer_topic = Web3.keccak(text="Transfer(address,address,uint256)")
    total = 0
    found_transfer = False
    for receipt in receipts:
        if receipt is None:
            continue
        for log in receipt.get("logs", []):
            topics = log.get("topics", [])
            if len(topics) < 3:
                continue
            topic_zero = topics[0]
            if isinstance(topic_zero, str):
                topic_zero = bytes.fromhex(topic_zero.removeprefix("0x"))
            if (
                (log.get("address") or "").lower() != token
                or bytes(topic_zero) != bytes(transfer_topic)
            ):
                continue
            sender_topic, recipient_topic = topics[1], topics[2]
            if isinstance(sender_topic, str):
                sender_topic = bytes.fromhex(sender_topic.removeprefix("0x"))
            if isinstance(recipient_topic, str):
                recipient_topic = bytes.fromhex(recipient_topic.removeprefix("0x"))
            sender = "0x" + bytes(sender_topic)[-20:].hex()
            recipient = "0x" + bytes(recipient_topic)[-20:].hex()
            amount = int(log.get("data", 0), 16) if isinstance(log.get("data"), str) else int.from_bytes(
                bytes(log.get("data", b"")), "big"
            )
            if sender == wallet:
                total -= amount
                found_transfer = True
            if recipient == wallet:
                total += amount
                found_transfer = True
    return total if found_transfer else None


_ONCHAIN_PRICE_MAX_PLAUSIBLE_RATIO = 10.0


def _onchain_price_is_plausible(onchain_price: float | None, reference_price_usd: float | None,
                                 max_ratio: float = _ONCHAIN_PRICE_MAX_PLAUSIBLE_RATIO) -> bool:
    """Garde-fou de plausibilité pour un prix lu directement via
    `_onchain_pool_price_usd` : un vrai écart de marché entre deux relectures
    rapprochées ne dépasse jamais un facteur `max_ratio` (10x par défaut, déjà
    très généreux) par rapport à un prix de référence connu (DexScreener au
    moment du scan, ou dernier prix d'achat enregistré). Un ratio plus extrême
    trahit presque toujours une lecture corrompue plutôt qu'un vrai mouvement
    de marché.

    Bug observé en prod le 2026-10-08 sur TOSHI/WETH et AERO/WETH (quote=WETH,
    donc `EXECUTABLE_QUOTE_TOKENS`, pas concerné par le fix du même jour sur
    `_quote_token_usd_price`) : `_onchain_pool_price_usd("uniswap", ...)`
    s'appuyait sur `_find_uniswap_v3_fee_tier`, qui retenait alors le PREMIER
    fee tier dont la pool existe on-chain, sans vérifier sa liquidité — pour
    ces deux tokens récemment ajoutés comme seed tokens, ce premier fee tier
    correspondait à une pool quasi vide/jamais tradée, dont le `sqrtPriceX96`
    ne reflète aucun prix de marché réel : résultat, un prix ~10^42 à 10^46
    fois le prix réel. Conséquence concrète au-delà de l'affichage corrompu :
    ce `net_profit_usd` factice gagnait SYSTÉMATIQUEMENT le tri par profit
    décroissant du scheduler (`app.scheduler.run_arbitrage_scan_job`),
    monopolisant la seule tentative d'exécution de chaque cycle sur un signal
    bidon (toujours refusé par la simulation `eth_call`, donc sans perte
    réelle de fonds) au détriment de vraies opportunités rentables qui
    n'étaient alors jamais tentées. `_find_uniswap_v3_fee_tier` a depuis été
    corrigé le 08/10 (second incident, DEGEN/WETH en flashloan cette fois,
    voir son propre docstring) pour retenir le fee tier le plus liquide — ce
    garde-fou de plausibilité reste néanmoins conservé par prudence."""
    if onchain_price is None or onchain_price <= 0:
        return False
    if reference_price_usd is None or reference_price_usd <= 0:
        # Pas de référence disponible : on ne peut pas juger, on fait confiance
        # (comportement historique, inchangé quand aucune référence n'existe).
        return True
    ratio = onchain_price / reference_price_usd
    return (1.0 / max_ratio) <= ratio <= max_ratio



def _ensure_allowance(db: Database, w3, *, token_address: str, owner: str, spender: str, amount: int,
                       count_against_daily_limit: bool = True) -> dict:
    """Vérifie l'allowance ERC20 courante ; envoie un `approve` SEULEMENT si
    insuffisante (évite une transaction superflue à chaque round-trip)."""
    from web3 import Web3
    token = w3.eth.contract(address=Web3.to_checksum_address(token_address), abi=ERC20_ABI)
    current = int(token.functions.allowance(Web3.to_checksum_address(owner), Web3.to_checksum_address(spender)).call())
    if current >= amount:
        return {"sent": False, "tx_hash": None, "error": None}
    return send_trading_transaction(
        db,
        rpc_url=_resolve_rpc_url(),
        contract_address=token_address,
        abi=ERC20_ABI,
        function_name="approve",
        args=(Web3.to_checksum_address(spender), amount),
        whitelist_target=spender,
        action_label=f"approve {token_address} -> {spender}",
        count_against_daily_limit=count_against_daily_limit,
    )


def get_quote_trading_balance_usd(quote_address: str, config: dict) -> float | None:
    """Retourne le solde réel (en $) du wallet de trading pour le token "quote"
    `quote_address` (doit être dans `EXECUTABLE_QUOTE_TOKENS`), ou `None` si le
    trading live n'est pas actif ou si la lecture échoue (RPC injoignable,
    keystore absent, prix indisponible, etc.) — jamais d'exception levée, pour
    que l'appelant (dimensionnement dynamique des trades) retombe proprement
    sur la taille de trade statique configurée en cas de souci.

    Ne nécessite PAS la clé privée elle-même (lecture seule) : le solde est
    interrogé via l'adresse publique issue du signer déchiffré, comme pour
    `recover_open_positions`."""
    quote_key = quote_address.lower()
    if quote_key not in _QUOTE_TOKEN_COINGECKO_ID:
        logger.warning("get_quote_trading_balance_usd: token '%s' non exécutable.", quote_address)
        return None
    if not is_trading_live(config):
        return None
    try:
        from web3 import Web3
        w3 = make_w3(timeout=15)
        if not _is_connected_with_retry(w3):
            return None

        from app.trading.guardrails import trading_keystore_path
        from app.wallet.signer import load_signer
        signer_account = load_signer(
            keystore_path=trading_keystore_path(), passphrase=os.environ.get("WALLET_TRADING_PASSPHRASE")
        )
        wallet_address = signer_account.address
        signer_account = None

        decimals = _get_decimals(w3, quote_key)
        contract = w3.eth.contract(address=Web3.to_checksum_address(quote_key), abi=ERC20_ABI)
        balance_units = _balance_of_with_retry(contract, wallet_address)
        balance_tokens = balance_units / (10 ** decimals)

        coingecko_id = _QUOTE_TOKEN_COINGECKO_ID[quote_key]
        if coingecko_id is None:  # stablecoin : 1 unité ≈ 1 $
            return balance_tokens
        from app.wallet.pricing import get_price_usd
        price_usd = get_price_usd(coingecko_id)
        if price_usd is None:
            logger.warning("get_quote_trading_balance_usd: prix indisponible pour %s.", coingecko_id)
            return None
        return balance_tokens * price_usd
    except Exception as exc:  # noqa: BLE001
        logger.warning("get_quote_trading_balance_usd: échec lecture solde réel (%s) : %s", quote_address, exc)
        return None


def get_usdc_trading_balance_usd(config: dict) -> float | None:
    """Alias rétrocompatible de `get_quote_trading_balance_usd` pour l'USDC
    (conservé car encore utilisé tel quel ailleurs/en tests)."""
    return get_quote_trading_balance_usd(USDC_ADDRESS, config)


def _find_uniswap_v3_fee_tier(w3, token_in: str, token_out: str) -> int:
    """Choisit le fee tier Uniswap V3 avec la plus grande réserve de
    `token_out` (voir `_best_v3_style_pool`, même méthode que PancakeSwap V3
    et Aerodrome Slipstream) — PLUS "le premier fee tier dont la pool existe",
    comportement corrigé le 08/10 après confirmation en conditions réelles
    d'un revert `InsufficientProfit` sur DEGEN/WETH (flashloan) : la pool
    choisie par l'ancienne méthode (premier fee tier trouvé, souvent 500) ne
    correspondait PAS à la pool la plus liquide utilisée pour chiffrer le
    signal (`app.trading.arbitrage._collect_pairs`, qui retient la pool de
    plus grande liquidité par DEX) — route d'exécution et prix/liquidité du
    signal portaient donc sur DEUX pools différentes du même DEX, un écart de
    prix/profondeur qui a fait perdre ~29$ en simulation (capital jamais
    engagé, bloqué par le garde-fou `InsufficientProfit` du contrat
    FlashArbitrage, voir contracts/FlashArbitrage.sol). Le docstring de
    `_collect_pairs` affirmait d'ailleurs déjà (à tort, avant ce correctif) que
    cette fonction retenait "le fee tier réellement liquide"."""
    result = _best_v3_style_pool(
        w3, UNISWAP_V3_FACTORY_ADDRESS, UNISWAP_V3_FACTORY_ABI, UNISWAP_V3_FEE_TIERS, token_in, token_out
    )
    if result is None:
        raise TradingRefused(
            f"Aucune pool Uniswap V3 trouvée pour {token_in}/{token_out} (fee tiers testés: {UNISWAP_V3_FEE_TIERS})."
        )
    fee_tier, _pool_address = result
    return fee_tier


def _quote_reserve_in_pool(w3, pool_address: str, quote_address: str) -> int:
    """Solde de `quote_address` détenu par `pool_address` -- métrique de
    profondeur pour une pool AMM CLASSIQUE (x*y=k : toute la réserve est
    active). Pour une pool à liquidité concentrée, utiliser
    `_v3_style_quote_depth` (le solde du contrat n'y mesure RIEN d'utile).

    Une erreur RPC est PROPAGÉE (après retry sur 429), jamais convertie en
    `None`/0. Bug corrigé le 09/10/2026 : l'ancienne version renvoyait `None`
    sur n'importe quelle exception, et les appelants écartaient alors la pool
    comme si elle n'existait pas — sous un RPC rate-limité (429 en continu),
    un 429 sur la lecture de la GROSSE pool faisait gagner une pool quasi vide,
    route ensuite mise en cache. Mieux vaut refuser la tentative (retentée au
    cycle suivant) que router sur une information fausse."""
    from web3 import Web3
    token = w3.eth.contract(address=Web3.to_checksum_address(quote_address), abi=ERC20_ABI)
    return int(call_with_rate_limit_retry(
        lambda: token.functions.balanceOf(Web3.to_checksum_address(pool_address)).call()
    ))


# Sélecteurs `slot0()` / `liquidity()` — communs à Uniswap V3, PancakeSwap V3
# et Aerodrome Slipstream (dont les `slot0` diffèrent par le NOMBRE de champs
# retournés : on ne décode que le premier mot, `sqrtPriceX96`, via un appel
# brut plutôt qu'un ABI par variante).
_SLOT0_SELECTOR = "0x3850c7bd"
_LIQUIDITY_SELECTOR = "0x1a686502"
_Q96 = 1 << 96


def _v3_style_quote_depth(w3, pool_address: str, token_address: str, quote_address: str) -> int:
    """Réserve VIRTUELLE de `quote_address` au prix courant d'une pool à
    liquidité concentrée (Uniswap V3 / PancakeSwap V3 / Aerodrome Slipstream) :
    `L·√P` si le quote est token1, `L/√P` sinon (avec L = `liquidity()`, la
    liquidité ACTIVE dans le tick courant, et √P = `slot0().sqrtPriceX96`).
    Par construction (x·y = L²), c'est exactement la réserve qu'aurait une
    pool V2 de même profondeur marginale : directement comparable à
    `_quote_reserve_in_pool` d'une pool classique, et nulle si aucune
    liquidité n'est en range.

    Bug corrigé le 09/10/2026 : la profondeur des pools V3 était mesurée par
    `balanceOf(pool)`, qui compte aussi les positions HORS range (et les
    tokens orphelins) — une pool PancakeSwap V3 BRETT/WETH au gros solde WETH
    mais ~sans liquidité active était ainsi préférée, et chaque simulation de
    flashloan y perdait 40 à 90 % (bloquée par `InsufficientProfit`, aucun
    fonds engagé, mais aucun trade possible)."""
    from web3 import Web3
    pool_cs = Web3.to_checksum_address(pool_address)
    raw_slot0 = call_with_rate_limit_retry(lambda: w3.eth.call({"to": pool_cs, "data": _SLOT0_SELECTOR}))
    raw_liquidity = call_with_rate_limit_retry(lambda: w3.eth.call({"to": pool_cs, "data": _LIQUIDITY_SELECTOR}))
    sqrt_price_x96 = int.from_bytes(bytes(raw_slot0)[:32], "big")
    liquidity = int.from_bytes(bytes(raw_liquidity)[:32], "big")
    if sqrt_price_x96 == 0 or liquidity == 0:
        return 0
    quote_is_token1 = int(quote_address, 16) > int(token_address, 16)
    if quote_is_token1:
        return liquidity * sqrt_price_x96 // _Q96
    return liquidity * _Q96 // sqrt_price_x96


def _best_v3_style_pool(w3, factory_address: str, factory_abi: list[dict], fee_tiers: tuple[int, ...],
                         token_address: str, quote_address: str) -> tuple[int, str] | None:
    """Cherche, parmi `fee_tiers`, la pool Uniswap-V3-style (`getPool(tokenA,
    tokenB, fee)`) avec la plus grande réserve de `quote_address` -- réutilisée
    pour PancakeSwap V3 (voir `_best_pancakeswap_v3_pool`) ET, depuis le 08/10,
    pour Uniswap V3 lui-même via `_find_uniswap_v3_fee_tier` (corrigé ce
    jour-là : l'ancienne logique "premier fee tier existant" pouvait router
    l'exécution vers une pool différente de celle utilisée pour chiffrer le
    signal, confirmé par un revert `InsufficientProfit` sur DEGEN/WETH en
    flashloan). Retourne (fee_tier, pool_address) ou None si aucune pool
    n'existe (adresse zéro CONFIRMÉE pour chaque fee tier). Une erreur RPC est
    propagée (voir `_quote_reserve_in_pool`).

    Résultat mis en cache (`_route_cache`, TTL `_ROUTE_CACHE_TTL_SECONDS`) ;
    les adresses de pool (`getPool`, immuables) sont cachées séparément et bien
    plus longtemps (`_IMMUTABLE_CACHE_TTL_SECONDS`) : une re-résolution ne
    coûte alors qu'UNE lecture de réserve par pool existante. Les `fee_tiers`
    étant indépendants les uns des autres, ils sont testés EN PARALLÈLE
    (`ThreadPoolExecutor`) — la rotation d'endpoints de `make_w3` répartit la
    rafale entre plusieurs RPC."""
    cache_key = ("v3_style_pool", factory_address.lower(), fee_tiers) + _pair_key(token_address, quote_address)
    cached = _route_cache_get(cache_key)
    if cached is not _ROUTE_CACHE_MISSING:
        return cached  # type: ignore[return-value]

    from web3 import Web3
    factory = w3.eth.contract(address=Web3.to_checksum_address(factory_address), abi=factory_abi)
    token_cs = Web3.to_checksum_address(token_address)
    quote_cs = Web3.to_checksum_address(quote_address)

    def _check_fee_tier(fee_tier: int) -> tuple[int, str, int] | None:
        pool_address = _cached_rpc_value(
            ("getPool", factory_address.lower(), fee_tier) + _pair_key(token_address, quote_address),
            lambda: factory.functions.getPool(token_cs, quote_cs, fee_tier).call(),
            _IMMUTABLE_CACHE_TTL_SECONDS,
        )
        if int(pool_address, 16) == 0:
            return None
        return (fee_tier, pool_address, _v3_style_quote_depth(w3, pool_address, token_address, quote_address))

    best: tuple[int, str] | None = None
    best_reserve = -1
    with ThreadPoolExecutor(max_workers=len(fee_tiers)) as executor_pool:
        for result in executor_pool.map(_check_fee_tier, fee_tiers):
            if result is None:
                continue
            fee_tier, pool_address, reserve = result
            if reserve <= best_reserve:
                continue
            best_reserve = reserve
            best = (fee_tier, pool_address)
    _route_cache_set(cache_key, best)
    return best


def _best_pancakeswap_v3_pool(w3, token_address: str, quote_address: str) -> tuple[int, str] | None:
    """Meilleure pool PancakeSwap V3 (plus grande réserve de `quote_address`)
    pour la paire donnée, toutes fee tiers confondues. Retourne
    (fee_tier, pool_address) ou None si aucune pool n'existe."""
    return _best_v3_style_pool(
        w3, PANCAKESWAP_V3_FACTORY_ADDRESS, PANCAKESWAP_V3_FACTORY_ABI, PANCAKESWAP_V3_FEE_TIERS,
        token_address, quote_address,
    )


def _best_aerodrome_slipstream_pool(w3, token_address: str, quote_address: str) -> tuple[str, int, str] | None:
    """Meilleure pool Aerodrome Slipstream (concentrated liquidity) pour la
    paire donnée, à travers les TROIS générations de factories concurrentes et
    simultanément actives (voir `routers.AERODROME_SLIPSTREAM_FACTORIES`) et
    tous leurs tick spacings activés (`tickSpacings()`, lu dynamiquement --
    PAS codé en dur, car potentiellement incomplet/évolutif). Choisie par plus
    grande réserve on-chain de `quote_address`. Retourne
    (router_address, tick_spacing, pool_address) ou None si aucune pool
    n'existe sur aucune des 3 factories.

    Résultat mis en cache (`_route_cache`, voir `_best_v3_style_pool`) : coût
    RPC similaire (jusqu'à 2 appels par tick spacing, sur 3 factories), rappelé
    plusieurs fois par cycle pour la même paire. Les 3 factories sont testées
    EN PARALLÈLE (même raison que `_best_v3_style_pool`)."""
    cache_key = ("aerodrome_slipstream_pool",) + _pair_key(token_address, quote_address)
    cached = _route_cache_get(cache_key)
    if cached is not _ROUTE_CACHE_MISSING:
        return cached  # type: ignore[return-value]

    from web3 import Web3
    token_cs = Web3.to_checksum_address(token_address)
    quote_cs = Web3.to_checksum_address(quote_address)

    def _check_factory(entry: tuple[str, str, str]) -> tuple[str, int, str, int] | None:
        _name, factory_address, router_address = entry
        factory = w3.eth.contract(
            address=Web3.to_checksum_address(factory_address), abi=AERODROME_SLIPSTREAM_FACTORY_ABI
        )
        tick_spacings = _cached_rpc_value(
            ("slipstream_tick_spacings", factory_address.lower()),
            lambda: list(factory.functions.tickSpacings().call()),
            _IMMUTABLE_CACHE_TTL_SECONDS,
        )
        local_best: tuple[str, int, str, int] | None = None
        local_best_reserve = -1
        for tick_spacing in tick_spacings:
            pool_address = _cached_rpc_value(
                ("slipstream_getPool", factory_address.lower(), tick_spacing) + _pair_key(token_address, quote_address),
                lambda ts=tick_spacing: factory.functions.getPool(token_cs, quote_cs, ts).call(),
                _IMMUTABLE_CACHE_TTL_SECONDS,
            )
            if int(pool_address, 16) == 0:
                continue
            reserve = _v3_style_quote_depth(w3, pool_address, token_address, quote_address)
            if reserve <= local_best_reserve:
                continue
            local_best_reserve = reserve
            local_best = (router_address, tick_spacing, pool_address, reserve)
        return local_best

    best: tuple[str, int, str] | None = None
    best_reserve = -1
    with ThreadPoolExecutor(max_workers=len(AERODROME_SLIPSTREAM_FACTORIES)) as executor_pool:
        for result in executor_pool.map(_check_factory, AERODROME_SLIPSTREAM_FACTORIES):
            if result is None:
                continue
            router_address, tick_spacing, pool_address, reserve = result
            if reserve <= best_reserve:
                continue
            best_reserve = reserve
            best = (router_address, tick_spacing, pool_address)
    _route_cache_set(cache_key, best)
    return best


def _resolve_aerodrome_route(w3, token_in: str, token_out: str, quote_address: str | None = None) -> dict | None:
    """Choisit, pour une jambe Aerodrome token_in<->token_out, entre le pool
    classique (stable=False, `defaultFactory`) et la meilleure pool Slipstream
    (`_best_aerodrome_slipstream_pool`, toutes factories/tick spacings
    confondues) -- comparaison par plus grande réserve on-chain du quote token.
    `quote_address`, quand fourni, désigne explicitement le token de cotation
    du round-trip (évite l'ambiguïté quand les deux côtés de la paire sont
    dans `EXECUTABLE_QUOTE_TOKENS`, ex. WETH/BRETT). Quand il est None, le
    quote est déduit de l'appartenance à `EXECUTABLE_QUOTE_TOKENS` (comportement
    historique). Utilisé À LA FOIS par `_build_swap_call` (construction de
    l'appel réel) ET `_onchain_pool_price_usd` (revérification de prix) — les
    deux doivent appeler EXACTEMENT cette fonction pour rester cohérents sur
    la pool choisie. Retourne un dict (`is_slipstream`, `router`, `pool`, +
    `default_factory` ou `tick_spacing` selon le cas), ou `None` si aucune
    pool n'existe nulle part pour cette paire.

    Résultat mis en cache (`_route_cache`, voir `_best_v3_style_pool`) : en
    plus de `_best_aerodrome_slipstream_pool` (déjà caché), les lectures
    "classic pool" ci-dessous (`defaultFactory`/`poolFor`/`balanceOf`) sont
    elles aussi rappelées plusieurs fois par cycle pour la même paire. Les
    deux branches (pool classique / `_best_aerodrome_slipstream_pool`) sont
    résolues EN PARALLÈLE (indépendantes l'une de l'autre)."""
    cache_key = ("aerodrome_route", (quote_address or "").lower()) + _pair_key(token_in, token_out)
    cached = _route_cache_get(cache_key)
    if cached is not _ROUTE_CACHE_MISSING:
        return cached  # type: ignore[return-value]

    from web3 import Web3
    token_in_cs = Web3.to_checksum_address(token_in)
    token_out_cs = Web3.to_checksum_address(token_out)
    if quote_address:
        quote_address = quote_address.lower()
        if token_in.lower() == quote_address:
            other_token = token_out
        elif token_out.lower() == quote_address:
            other_token = token_in
        else:
            # quote_address pas membre de la paire : repli sur l'inférence.
            if token_in.lower() in EXECUTABLE_QUOTE_TOKENS:
                quote_address, other_token = token_in, token_out
            else:
                quote_address, other_token = token_out, token_in
    elif token_in.lower() in EXECUTABLE_QUOTE_TOKENS:
        quote_address, other_token = token_in, token_out
    else:
        quote_address, other_token = token_out, token_in

    def _check_classic() -> tuple[str, str, int] | None:
        classic_router = w3.eth.contract(
            address=Web3.to_checksum_address(ROUTER_ADDRESSES["aerodrome"]), abi=AERODROME_ROUTER_ABI
        )
        factory_addr = _cached_rpc_value(
            ("aerodrome_default_factory",), lambda: classic_router.functions.defaultFactory().call(),
            _IMMUTABLE_CACHE_TTL_SECONDS,
        )
        pool_address = _cached_rpc_value(
            ("aerodrome_poolFor", factory_addr.lower()) + _pair_key(token_in, token_out),
            lambda: classic_router.functions.poolFor(token_in_cs, token_out_cs, False, factory_addr).call(),
            _IMMUTABLE_CACHE_TTL_SECONDS,
        )
        if int(pool_address, 16) == 0:
            return None
        return (factory_addr, pool_address, _quote_reserve_in_pool(w3, pool_address, quote_address))

    # Pool classique et meilleure pool Slipstream sont indépendantes l'une de
    # l'autre : résolues EN PARALLÈLE (même raison que `_best_v3_style_pool`).
    # Une erreur RPC dans l'une ou l'autre est propagée (`.result()` relance
    # l'exception) : jamais de choix de pool sur information partielle.
    with ThreadPoolExecutor(max_workers=2) as executor_pool:
        classic_future = executor_pool.submit(_check_classic)
        slipstream_future = executor_pool.submit(_best_aerodrome_slipstream_pool, w3, other_token, quote_address)
        classic_result = classic_future.result()
        slipstream = slipstream_future.result()

    classic_pool = None
    default_factory = None
    classic_reserve = -1
    if classic_result is not None:
        default_factory, classic_pool, classic_reserve = classic_result

    slipstream_reserve = -1
    if slipstream is not None:
        slipstream_reserve = _v3_style_quote_depth(w3, slipstream[2], other_token, quote_address)

    if slipstream is not None and slipstream_reserve > classic_reserve:
        router_address, tick_spacing, pool_address = slipstream
        result = {
            "is_slipstream": True, "router": router_address, "tick_spacing": tick_spacing, "pool": pool_address,
        }
    elif classic_pool is not None:
        result = {
            "is_slipstream": False, "router": ROUTER_ADDRESSES["aerodrome"],
            "default_factory": default_factory, "pool": classic_pool,
        }
    else:
        result = None
    _route_cache_set(cache_key, result)
    return result


def _resolve_pancakeswap_route(w3, token_in: str, token_out: str, quote_address: str | None = None) -> dict | None:
    """Même principe que `_resolve_aerodrome_route`, pour PancakeSwap v2
    (classique) vs PancakeSwap V3 (`_best_pancakeswap_v3_pool`). Retourne un
    dict (`is_v3`, `router`, `pool`, + `fee` si v3), ou `None` si aucune pool
    n'existe nulle part pour cette paire. `quote_address` désigne explicitement
    le token de cotation (voir `_resolve_aerodrome_route`).

    Résultat mis en cache (`_route_cache`, voir `_resolve_aerodrome_route`),
    même parallélisation des deux branches (classique / V3)."""
    cache_key = ("pancakeswap_route", (quote_address or "").lower()) + _pair_key(token_in, token_out)
    cached = _route_cache_get(cache_key)
    if cached is not _ROUTE_CACHE_MISSING:
        return cached  # type: ignore[return-value]

    from web3 import Web3
    token_in_cs = Web3.to_checksum_address(token_in)
    token_out_cs = Web3.to_checksum_address(token_out)
    if quote_address:
        quote_address = quote_address.lower()
        if token_in.lower() == quote_address:
            other_token = token_out
        elif token_out.lower() == quote_address:
            other_token = token_in
        else:
            if token_in.lower() in EXECUTABLE_QUOTE_TOKENS:
                quote_address, other_token = token_in, token_out
            else:
                quote_address, other_token = token_out, token_in
    elif token_in.lower() in EXECUTABLE_QUOTE_TOKENS:
        quote_address, other_token = token_in, token_out
    else:
        quote_address, other_token = token_out, token_in

    classic_pool = None
    classic_reserve = -1

    def _check_classic() -> tuple[str, int] | None:
        classic_router = w3.eth.contract(
            address=Web3.to_checksum_address(ROUTER_ADDRESSES["pancakeswap"]), abi=UNISWAP_V2_ROUTER_FACTORY_ABI
        )
        factory_addr = _cached_rpc_value(
            ("v2_factory", "pancakeswap"), lambda: classic_router.functions.factory().call(),
            _IMMUTABLE_CACHE_TTL_SECONDS,
        )
        factory = w3.eth.contract(address=Web3.to_checksum_address(factory_addr), abi=UNISWAP_V2_FACTORY_ABI)
        pool_address = _cached_rpc_value(
            ("v2_getPair", factory_addr.lower()) + _pair_key(token_in, token_out),
            lambda: factory.functions.getPair(token_in_cs, token_out_cs).call(),
            _IMMUTABLE_CACHE_TTL_SECONDS,
        )
        if int(pool_address, 16) == 0:
            return None
        return (pool_address, _quote_reserve_in_pool(w3, pool_address, quote_address))

    # Pool classique et meilleure pool V3 sont indépendantes l'une de l'autre :
    # résolues EN PARALLÈLE (même raison que `_best_v3_style_pool`). Une erreur
    # RPC est propagée (voir `_resolve_aerodrome_route`).
    with ThreadPoolExecutor(max_workers=2) as executor_pool:
        classic_future = executor_pool.submit(_check_classic)
        v3_future = executor_pool.submit(_best_pancakeswap_v3_pool, w3, other_token, quote_address)
        classic_result = classic_future.result()
        v3 = v3_future.result()
    if classic_result is not None:
        classic_pool, classic_reserve = classic_result

    v3_reserve = -1
    if v3 is not None:
        v3_reserve = _v3_style_quote_depth(w3, v3[1], other_token, quote_address)

    if v3 is not None and v3_reserve > classic_reserve:
        fee_tier, pool_address = v3
        result = {"is_v3": True, "router": PANCAKESWAP_V3_ROUTER_ADDRESS, "fee": fee_tier, "pool": pool_address}
    elif classic_pool is not None:
        result = {"is_v3": False, "router": ROUTER_ADDRESSES["pancakeswap"], "pool": classic_pool}
    else:
        result = None
    _route_cache_set(cache_key, result)
    return result


def _v2_fork_pool_confirmed_absent(w3, dex: str, token_in: str, token_out: str) -> bool:
    """Pour un fork UniswapV2 "simple" (sushiswap/baseswap/alien-base/
    swapbased, PAS aerodrome/pancakeswap qui ont leur propre résolution
    classique-vs-V3 ci-dessus) : `True` si on a pu interroger la factory de
    `dex` et confirmer qu'AUCUN pool direct `token_in`/`token_out` n'existe
    (`getPair` répond l'adresse zéro). `False` sinon (pool trouvé OU panne
    RPC/lecture impossible — comportement permissif existant ailleurs dans ce
    module : on ne bloque que sur une absence CONFIRMÉE, jamais sur un doute).

    Bug corrigé le 2026-10-08 : sans cette vérification, `_build_swap_call`
    et `flashloan._build_leg` construisaient aveuglément un appel
    `swapExactTokensForTokens` vers le router de `dex` en supposant qu'une
    paire directe existe — si ce n'est pas le cas (ex. AlienBase sans pool
    WETH/EURC, confirmé on-chain : `factory.getPair` renvoie l'adresse zéro),
    l'appel revert systématiquement avec `('execution reverted', 'no data')`
    (le call bas niveau vers une adresse sans bytecode ne renvoie aucune
    donnée décodable). Ce cas touchait TOUS les signaux où le DEX indiqué par
    DexScreener (prix utilisé pour chiffrer le signal, voir
    `app.trading.arbitrage._refine_spread_with_onchain_price`) n'a en réalité
    aucune pool directe exécutable sur CE DEX précis."""
    from web3 import Web3

    try:
        router = w3.eth.contract(
            address=Web3.to_checksum_address(ROUTER_ADDRESSES[dex]), abi=UNISWAP_V2_ROUTER_FACTORY_ABI
        )
        factory_address = _cached_rpc_value(
            ("v2_factory", dex), lambda: router.functions.factory().call(), _IMMUTABLE_CACHE_TTL_SECONDS,
        )
        factory = w3.eth.contract(address=Web3.to_checksum_address(factory_address), abi=UNISWAP_V2_FACTORY_ABI)
        pool_address = _cached_rpc_value(
            ("v2_getPair", factory_address.lower()) + _pair_key(token_in, token_out),
            lambda: factory.functions.getPair(
                Web3.to_checksum_address(token_in), Web3.to_checksum_address(token_out)
            ).call(),
            _IMMUTABLE_CACHE_TTL_SECONDS,
        )
        return int(pool_address, 16) == 0
    except Exception as exc:  # noqa: BLE001 - doute (panne RPC) : permissif, et surtout JAMAIS mis en cache
        logger.warning("Impossible de vérifier l'existence de la pool %s %s/%s : %s", dex, token_in, token_out, exc)
        return False


def _build_swap_call(dex: str, w3, *, token_in: str, token_out: str, amount_in: int,
                      min_amount_out: int, recipient: str, deadline: int,
                      quote_address: str | None = None) -> tuple[str, list[dict], str, tuple]:
    """Construit (contract_address, abi, function_name, args) pour le DEX demandé.
    Lève `TradingRefused` si le DEX n'est pas dans EXECUTABLE_DEXES.

    `quote_address` désigne explicitement le token de cotation du round-trip
    (USDC/WETH/BRETT) et est transmis à la résolution de route des DEX qui en
    ont besoin (aerodrome, pancakeswap) afin de choisir la bonne réserve de
    quote même quand les deux côtés de la paire sont exécutables."""
    from web3 import Web3
    if dex not in EXECUTABLE_DEXES:
        raise TradingRefused(f"Exécution non implémentée pour le DEX '{dex}' (voir EXECUTABLE_DEXES).")
    router_address = ROUTER_ADDRESSES[dex]
    token_in_cs = Web3.to_checksum_address(token_in)
    token_out_cs = Web3.to_checksum_address(token_out)
    recipient_cs = Web3.to_checksum_address(recipient)

    if dex == "uniswap":
        fee_tier = _find_uniswap_v3_fee_tier(w3, token_in, token_out)
        params = (token_in_cs, token_out_cs, fee_tier, recipient_cs, amount_in, min_amount_out, 0)
        return router_address, UNISWAP_V3_ROUTER_ABI, "exactInputSingle", (params,)

    if dex == "aerodrome":
        route = _resolve_aerodrome_route(w3, token_in, token_out, quote_address)
        if route is None:
            raise TradingRefused(f"Aucune pool Aerodrome (classique ou Slipstream) trouvée pour {token_in}/{token_out}.")
        if route["is_slipstream"]:
            params = (
                token_in_cs, token_out_cs, route["tick_spacing"], recipient_cs, deadline,
                amount_in, min_amount_out, 0,
            )
            return route["router"], AERODROME_SLIPSTREAM_ROUTER_ABI, "exactInputSingle", (params,)
        routes = [(token_in_cs, token_out_cs, False, route["default_factory"])]
        return route["router"], AERODROME_ROUTER_ABI, "swapExactTokensForTokens", (
            amount_in, min_amount_out, routes, recipient_cs, deadline,
        )

    if dex == "pancakeswap":
        route = _resolve_pancakeswap_route(w3, token_in, token_out, quote_address)
        if route is None:
            raise TradingRefused(f"Aucune pool PancakeSwap (v2 ou v3) trouvée pour {token_in}/{token_out}.")
        if route["is_v3"]:
            params = (
                token_in_cs, token_out_cs, route["fee"], recipient_cs, deadline,
                amount_in, min_amount_out, 0,
            )
            return route["router"], PANCAKESWAP_V3_ROUTER_ABI, "exactInputSingle", (params,)
        path = [token_in_cs, token_out_cs]
        return route["router"], UNISWAP_V2_ROUTER_ABI, "swapExactTokensForTokens", (
            amount_in, min_amount_out, path, recipient_cs, deadline,
        )

    # sushiswap / baseswap / alien-base / swapbased : ABI UniswapV2Router02
    # standard, chemin direct token_in->token_out (tous forks V2 classiques ;
    # pancakeswap géré au-dessus, bien qu'également membre de
    # UNISWAP_V2_FORK_DEXES pour les autres usages de cette constante).
    if dex in UNISWAP_V2_FORK_DEXES:
        if _v2_fork_pool_confirmed_absent(w3, dex, token_in, token_out):
            raise TradingRefused(f"Aucune pool {dex} trouvée pour {token_in}/{token_out}.")
        path = [token_in_cs, token_out_cs]
        return router_address, UNISWAP_V2_ROUTER_ABI, "swapExactTokensForTokens", (
            amount_in, min_amount_out, path, recipient_cs, deadline,
        )

    raise TradingRefused(f"Exécution non implémentée pour le DEX '{dex}' (voir EXECUTABLE_DEXES).")


def _build_swap_call_and_ensure_allowance(
    db: Database,
    w3,
    *,
    dex: str,
    token_in: str,
    token_out: str,
    amount_in: int,
    min_amount_out: int,
    recipient: str,
    deadline: int,
    quote_address: str,
    allowance_token_address: str,
    owner: str,
    allowance_amount: int,
    count_against_daily_limit: bool = True,
) -> tuple[str, list[dict], str, tuple, dict]:
    """Build the exact swap route, then approve its selected router as spender."""
    contract_address, abi, function_name, args = _build_swap_call(
        dex, w3, token_in=token_in, token_out=token_out,
        amount_in=amount_in, min_amount_out=min_amount_out,
        recipient=recipient, deadline=deadline, quote_address=quote_address,
    )
    approval = _ensure_allowance(
        db, w3, token_address=allowance_token_address, owner=owner,
        spender=contract_address, amount=allowance_amount,
        count_against_daily_limit=count_against_daily_limit,
    )
    return contract_address, abi, function_name, args, approval


def _build_triangular_swap_call(dex: str, w3, *, token_a: str, token_b: str, token_c: str,
                                 amount_in: int, min_amount_out: int, recipient: str,
                                 deadline: int) -> tuple[str, list[dict], str, tuple]:
    """Construit l'appel multi-hop UNIQUE token_a -> token_b -> token_c -> token_a
    (voir `app.trading.triangular`) : UNE SEULE transaction pour tout le cycle,
    atomique (si une jambe échoue, l'intégralité revert — aucun état
    intermédiaire possible, contrairement au round-trip classique 2-jambes de
    `_build_swap_call`/`execute_signal`). N'autorise que les routeurs V2 qui
    représentent directement les pools utilisées par le scan ; lève
    `TradingRefused` pour les DEX à routes ambiguës."""
    from web3 import Web3

    if dex not in TRIANGULAR_V2_EXECUTABLE_DEXES or dex not in EXECUTABLE_DEXES:
        raise TradingRefused(
            f"Exécution triangulaire wallet-funded non supportée pour '{dex}' : "
            "seuls les routeurs V2 correspondant exactement au scan sont autorisés."
        )
    router_address = ROUTER_ADDRESSES[dex]
    a_cs = Web3.to_checksum_address(token_a)
    b_cs = Web3.to_checksum_address(token_b)
    c_cs = Web3.to_checksum_address(token_c)
    recipient_cs = Web3.to_checksum_address(recipient)

    # Forks V2 classiques : chemin multi-hop natif à swapExactTokensForTokens
    # (même fonction/ABI que le round-trip 2-jambes, juste un path à 4 adresses
    # au lieu de 2 — aucune nouvelle primitive de routeur nécessaire).
    path = [a_cs, b_cs, c_cs, a_cs]
    return router_address, UNISWAP_V2_ROUTER_ABI, "swapExactTokensForTokens", (
        amount_in, min_amount_out, path, recipient_cs, deadline,
    )


def _quote_v2_triangular_output(w3, dex: str, path: list[str], amount_in: int) -> int:
    """Obtient le montant de sortie du chemin V2 exact, sans allowance ni écriture."""
    from web3 import Web3

    router = w3.eth.contract(
        address=Web3.to_checksum_address(ROUTER_ADDRESSES[dex]),
        abi=UNISWAP_V2_ROUTER_ABI,
    )
    checksum_path = [Web3.to_checksum_address(address) for address in path]
    amounts = call_with_rate_limit_retry(
        lambda: router.functions.getAmountsOut(amount_in, checksum_path).call()
    )
    if len(amounts) != len(checksum_path) or int(amounts[0]) != amount_in:
        raise TradingRefused(f"Réponse getAmountsOut invalide pour le cycle triangulaire {dex}.")
    output = int(amounts[-1])
    if output <= 0:
        raise TradingRefused(f"Le chemin triangulaire {dex} ne retourne aucun token de cotation.")
    return output


def execute_triangular_signal(db: Database, config: dict, signal: dict) -> dict:
    """Tente d'exécuter réellement un cycle triangulaire pour `signal` (dict au
    format retourné par `app.trading.triangular.run_triangular_scan`) : UNE
    SEULE transaction multi-hop token_a -> token_b -> token_c -> token_a.

    Retourne toujours un dict `{"executed": bool, "tx_hash": str|None,
    "error": str|None}`. Contrairement à `execute_signal` (2 transactions
    séparées), il n'y a ICI aucun état intermédiaire possible à gérer : soit
    la transaction entière passe (tous les 3 hops ont respecté leur
    `min_amount_out` implicite via le `min_amount_out` final du cycle), soit
    elle revert entièrement on-chain et le wallet n'a RIEN perdu d'autre que
    le gas de la tentative — pas de garde-fou de récupération de position
    équivalent à `recover_open_positions` nécessaire ici."""
    result = {"executed": False, "tx_hash": None, "error": None}

    if not is_trading_live(config):
        result["error"] = "Trading live désactivé (voir app.trading.guardrails.is_trading_live)."
        return result

    if signal["dex"] not in TRIANGULAR_V2_EXECUTABLE_DEXES or signal["dex"] not in EXECUTABLE_DEXES:
        result["error"] = (
            f"DEX non exécutable en triangulaire ({signal['dex']}) "
            f"— seuls {sorted(TRIANGULAR_V2_EXECUTABLE_DEXES & EXECUTABLE_DEXES)} sont implémentés."
        )
        return result

    if signal["token_a_address"].lower() not in EXECUTABLE_QUOTE_TOKENS:
        result["error"] = (
            f"Token de départ/arrivée {signal['token_a_symbol']} non exécutable — seuls USDC/WETH sont supportés."
        )
        return result

    # `would_execute_classic` (rentabilité au capital propre) si présent, sinon
    # repli sur `would_execute` (compat signaux plus anciens/tests) — ne jamais
    # se fier au flag combiné `would_execute` seul ici : un signal peut y être
    # marqué True uniquement grâce à sa rentabilité à l'échelle flashloan, ce
    # qui ne rend pas ce microtrade (capital propre) rentable pour autant.
    if not signal.get("would_execute_classic", signal.get("would_execute")):
        result["error"] = "Signal non marqué rentable au capital propre (would_execute_classic=False) — refus par prudence."
        return result

    try:
        from web3 import Web3
        w3 = make_w3(timeout=15)
        if not _is_connected_with_retry(w3):
            result["error"] = "RPC Base injoignable."
            return result

        from app.trading.guardrails import trading_keystore_path
        from app.wallet.signer import load_signer
        signer_account = load_signer(
            keystore_path=trading_keystore_path(), passphrase=os.environ.get("WALLET_TRADING_PASSPHRASE")
        )
        wallet_address = signer_account.address
        signer_account = None

        token_a = signal["token_a_address"]
        token_b = signal["token_b_address"]
        token_c = signal["token_c_address"]
        trade_size_usd = float(signal["trade_size_usd"])
        slip = slippage_pct() / 100.0
        deadline = int(time.time()) + 180

        token_a_decimals = _get_decimals(w3, token_a)

        quote_usd_price = _quote_token_usd_price(token_a)
        if not quote_usd_price or quote_usd_price <= 0:
            result["error"] = f"Prix USD de {signal['token_a_symbol']} indisponible — refus par prudence."
            return result

        # --- Garde-fou solde réel : ne jamais trader plus que ce qui est dispo ---
        token_a_contract = w3.eth.contract(address=Web3.to_checksum_address(token_a), abi=ERC20_ABI)
        token_a_balance_units = _balance_of_with_retry(token_a_contract, wallet_address)
        actual_gas_cost_wei = 0
        amount_in_tokens = trade_size_usd / quote_usd_price
        amount_in_units = int(amount_in_tokens * (10 ** token_a_decimals))
        if amount_in_units > token_a_balance_units:
            result["error"] = (
                f"Solde {signal['token_a_symbol']} insuffisant "
                f"({token_a_balance_units / 10**token_a_decimals:.6f} < {amount_in_tokens:.6f} requis "
                f"pour {trade_size_usd:.4f}$) — refus."
            )
            return result

        path = [token_a, token_b, token_c, token_a]
        quoted_amount_out_units = _quote_v2_triangular_output(
            w3, signal["dex"], path, amount_in_units,
        )
        min_amount_out_units = int(quoted_amount_out_units * (1 - slip))
        gas_cost_usd = signal.get("gas_cost_usd")
        if gas_cost_usd is None:
            result["error"] = "Coût de gas du cycle indisponible — refus par prudence, aucune approbation envoyée."
            return result
        projected_net_profit_usd = (
            (min_amount_out_units - amount_in_units) / (10 ** token_a_decimals) * quote_usd_price
            - float(gas_cost_usd)
        )
        trading_cfg = (config or {}).get("trading", {}) or {}
        profit_threshold_usd = max(
            float(trading_cfg.get("min_net_profit_usd", 0.0)),
            trade_size_usd * float(trading_cfg.get("min_net_profit_pct", 0.0)) / 100,
        )
        if not fees.is_net_profit_above_threshold(projected_net_profit_usd, profit_threshold_usd):
            result["error"] = (
                f"Devis on-chain du cycle {signal['dex']} non rentable après slippage et gas "
                f"({projected_net_profit_usd:.4f}$ < {profit_threshold_usd:.4f}$) — "
                "aucune approbation envoyée."
            )
            return result

        contract_address, abi, fn_name, args = _build_triangular_swap_call(
            signal["dex"], w3, token_a=token_a, token_b=token_b, token_c=token_c,
            amount_in=amount_in_units, min_amount_out=min_amount_out_units,
            recipient=wallet_address, deadline=deadline,
        )

        approve_result = _ensure_allowance(
            db, w3, token_address=token_a, owner=wallet_address,
            spender=ROUTER_ADDRESSES[signal["dex"]], amount=amount_in_units,
        )
        actual_gas_cost_wei += int(approve_result.get("gas_cost_wei") or 0)
        if approve_result["error"]:
            result["error"] = f"Approve échoué : {approve_result['error']}"
            if approve_result.get("tx_hash"):
                db.mark_triangular_signal_executed(
                    signal["id"], tx_hash=approve_result["tx_hash"], execution_error=result["error"],
                    **_actual_execution_metrics(None, quote_usd_price, actual_gas_cost_wei),
                )
            return result

        swap_result = send_trading_transaction(
            db, rpc_url=_resolve_rpc_url(), contract_address=contract_address, abi=abi,
            function_name=fn_name, args=args, whitelist_target=contract_address,
            action_label=(
                f"arbitrage triangulaire {signal['token_a_symbol']}->{signal['token_b_symbol']}->"
                f"{signal['token_c_symbol']}->{signal['token_a_symbol']} sur {signal['dex']}"
            ),
        )
        result["tx_hash"] = swap_result["tx_hash"]
        actual_gas_cost_wei += int(swap_result.get("gas_cost_wei") or 0)
        if swap_result["error"]:
            result["error"] = f"Cycle triangulaire échoué (revert complet, aucun fonds déplacé) : {swap_result['error']}"
            db.mark_triangular_signal_executed(
                signal["id"], tx_hash=result["tx_hash"], execution_error=result["error"],
                **_actual_execution_metrics(
                    0.0 if swap_result.get("gas_cost_wei") is not None else None,
                    quote_usd_price, actual_gas_cost_wei,
                ),
            )
            return result

        result["executed"] = True
        profit_units = _net_token_transfer_from_receipts(
            token_a, wallet_address, [swap_result.get("receipt")],
        )
        profit_quote = profit_units / (10 ** token_a_decimals) if profit_units is not None else None
        if profit_quote is None:
            logger.warning("Aucun transfert du token de cotation trouvé dans le reçu triangulaire %s.",
                           result["tx_hash"])
        db.mark_triangular_signal_executed(
            signal["id"], tx_hash=result["tx_hash"],
            **_actual_execution_metrics(profit_quote, quote_usd_price, actual_gas_cost_wei),
        )
        db.add_notification(
            title="✅ Cycle triangulaire exécuté",
            message=(
                f"{signal['token_a_symbol']}->{signal['token_b_symbol']}->{signal['token_c_symbol']}->"
                f"{signal['token_a_symbol']} sur {signal['dex']} — tx {result['tx_hash']}"
            ),
            level="info",
        )
        return result

    except TradingRefused as exc:
        result["error"] = f"Refusé par un garde-fou : {exc}"
        return result
    except Exception as exc:  # noqa: BLE001 - jamais de crash du scheduler pour une tentative de trade
        logger.exception("Erreur inattendue pendant execute_triangular_signal")
        result["error"] = f"Erreur inattendue : {exc}"
        return result


def _onchain_price_supported(dex: str) -> bool:
    """DEX dont `_onchain_pool_price_usd` sait lire la pool réellement routée."""
    return dex in UNISWAP_V2_FORK_DEXES or dex in ("aerodrome", "uniswap")


def _onchain_pool_price_usd(dex: str, token_address: str, quote_address: str) -> float | None:
    """Lit le prix de `token_address` (exprimé en `quote_address` puis converti
    en $) DIRECTEMENT depuis le pool on-chain réellement utilisé par
    `_build_swap_call` pour `dex` — lecture seule (aucune transaction), aucun
    risque. Supporté pour "aerodrome", "uniswap" (V3) ET tous les forks V2
    classiques (`UNISWAP_V2_FORK_DEXES`) depuis le 2026-10-08.

    Pour aerodrome/uniswap, ce sont les deux DEX pour lesquels DexScreener a
    démontré exposer plusieurs pools distincts sous le même dex_id
    (stable/volatile pour Aerodrome, plusieurs fee tiers pour Uniswap v3) — voir
    le fix `InsufficientOutputAmount`. Pour les forks V2 classiques, une seule
    pool canonique existe par paire/factory (pas d'ambiguïté), mais la lire
    directement reste plus FRAÎCHE qu'une revérification DexScreener (cache de
    quelques secondes à quelques minutes côté DexScreener) — gain de précision
    sur la revalidation de prix juste avant l'envoi d'une transaction.

    Depuis le 2026-10-08 également : "aerodrome" et "pancakeswap" choisissent
    dynamiquement entre leur pool classique (AMM) et leur(s) pool(s)
    concentrated-liquidity (Aerodrome Slipstream / PancakeSwap V3) — même
    ambiguïté DexScreener que ci-dessus, mais DexScreener n'expose ICI aucun
    `labels` permettant de distinguer les deux versions (vérifié
    empiriquement) ; `_resolve_aerodrome_route`/`_resolve_pancakeswap_route`
    choisissent par plus grande réserve on-chain, et DOIVENT être les mêmes
    fonctions appelées par `_build_swap_call`/`flashloan._build_leg` pour
    rester cohérentes sur la pool réellement routée.

    Retourne None si le DEX n'est pas supporté ici, ou si la lecture échoue
    pour n'importe quelle raison (RPC injoignable, pool inexistante...) — dans
    ce cas l'appelant retombe sur DexScreener plutôt que d'échouer."""
    if not _onchain_price_supported(dex):
        return None
    try:
        from web3 import Web3
        w3 = make_w3(timeout=10)
        if not _is_connected_with_retry(w3):
            return None

        token_cs = Web3.to_checksum_address(token_address)
        quote_cs = Web3.to_checksum_address(quote_address)

        if dex == "aerodrome":
            route = _resolve_aerodrome_route(w3, token_address, quote_address)
            if route is None:
                return None
            if route["is_slipstream"]:
                pool = w3.eth.contract(
                    address=Web3.to_checksum_address(route["pool"]), abi=AERODROME_SLIPSTREAM_POOL_ABI
                )
                token0 = pool.functions.token0().call()
                sqrt_price_x96 = pool.functions.slot0().call()[0]
                if sqrt_price_x96 == 0:
                    return None
                token_is_token0 = token0.lower() == token_cs.lower()
                decimals0 = _get_decimals(w3, token_address if token_is_token0 else quote_address)
                decimals1 = _get_decimals(w3, quote_address if token_is_token0 else token_address)
                raw_price_1_per_0 = (sqrt_price_x96 / (2 ** 96)) ** 2
                human_price_1_per_0 = raw_price_1_per_0 * (10 ** (decimals0 - decimals1))
                if human_price_1_per_0 <= 0:
                    return None
                price_in_quote = human_price_1_per_0 if token_is_token0 else (1.0 / human_price_1_per_0)
            else:
                pool = w3.eth.contract(address=Web3.to_checksum_address(route["pool"]), abi=AERODROME_POOL_ABI)
                token0 = pool.functions.token0().call()
                reserve0, reserve1, _ = pool.functions.getReserves().call()
                if reserve0 == 0 or reserve1 == 0:
                    return None
                decimals_token = _get_decimals(w3, token_address)
                decimals_quote = _get_decimals(w3, quote_address)
                token_is_token0 = token0.lower() == token_cs.lower()
                if token_is_token0:
                    # prix de token0 (notre token) exprimé en token1 (quote), ajusté décimales
                    price_in_quote = (reserve1 / (10 ** decimals_quote)) / (reserve0 / (10 ** decimals_token))
                else:
                    price_in_quote = (reserve0 / (10 ** decimals_quote)) / (reserve1 / (10 ** decimals_token))
        elif dex == "pancakeswap":
            route = _resolve_pancakeswap_route(w3, token_address, quote_address)
            if route is None:
                return None
            if route["is_v3"]:
                pool = w3.eth.contract(
                    address=Web3.to_checksum_address(route["pool"]), abi=PANCAKESWAP_V3_POOL_ABI
                )
                token0 = pool.functions.token0().call()
                sqrt_price_x96 = pool.functions.slot0().call()[0]
                if sqrt_price_x96 == 0:
                    return None
                token_is_token0 = token0.lower() == token_cs.lower()
                decimals0 = _get_decimals(w3, token_address if token_is_token0 else quote_address)
                decimals1 = _get_decimals(w3, quote_address if token_is_token0 else token_address)
                raw_price_1_per_0 = (sqrt_price_x96 / (2 ** 96)) ** 2
                human_price_1_per_0 = raw_price_1_per_0 * (10 ** (decimals0 - decimals1))
                if human_price_1_per_0 <= 0:
                    return None
                price_in_quote = human_price_1_per_0 if token_is_token0 else (1.0 / human_price_1_per_0)
            else:
                pool = w3.eth.contract(address=Web3.to_checksum_address(route["pool"]), abi=UNISWAP_V2_PAIR_ABI)
                token0 = pool.functions.token0().call()
                reserve0, reserve1, _ = pool.functions.getReserves().call()
                if reserve0 == 0 or reserve1 == 0:
                    return None
                decimals_token = _get_decimals(w3, token_address)
                decimals_quote = _get_decimals(w3, quote_address)
                token_is_token0 = token0.lower() == token_cs.lower()
                if token_is_token0:
                    price_in_quote = (reserve1 / (10 ** decimals_quote)) / (reserve0 / (10 ** decimals_token))
                else:
                    price_in_quote = (reserve0 / (10 ** decimals_quote)) / (reserve1 / (10 ** decimals_token))
        elif dex in UNISWAP_V2_FORK_DEXES:
            router = w3.eth.contract(
                address=Web3.to_checksum_address(ROUTER_ADDRESSES[dex]), abi=UNISWAP_V2_ROUTER_FACTORY_ABI
            )
            factory_address = router.functions.factory().call()
            factory = w3.eth.contract(address=Web3.to_checksum_address(factory_address), abi=UNISWAP_V2_FACTORY_ABI)
            pool_address = factory.functions.getPair(token_cs, quote_cs).call()
            if int(pool_address, 16) == 0:
                return None
            pool = w3.eth.contract(address=Web3.to_checksum_address(pool_address), abi=UNISWAP_V2_PAIR_ABI)
            token0 = pool.functions.token0().call()
            reserve0, reserve1, _ = pool.functions.getReserves().call()
            if reserve0 == 0 or reserve1 == 0:
                return None
            decimals_token = _get_decimals(w3, token_address)
            decimals_quote = _get_decimals(w3, quote_address)
            token_is_token0 = token0.lower() == token_cs.lower()
            if token_is_token0:
                price_in_quote = (reserve1 / (10 ** decimals_quote)) / (reserve0 / (10 ** decimals_token))
            else:
                price_in_quote = (reserve0 / (10 ** decimals_quote)) / (reserve1 / (10 ** decimals_token))
        else:  # uniswap (V3)
            factory = w3.eth.contract(
                address=Web3.to_checksum_address(UNISWAP_V3_FACTORY_ADDRESS), abi=UNISWAP_V3_FACTORY_ABI
            )
            fee_tier = _find_uniswap_v3_fee_tier(w3, token_address, quote_address)
            pool_address = factory.functions.getPool(token_cs, quote_cs, fee_tier).call()
            if int(pool_address, 16) == 0:
                return None
            pool = w3.eth.contract(address=Web3.to_checksum_address(pool_address), abi=UNISWAP_V3_POOL_ABI)
            token0 = pool.functions.token0().call()
            sqrt_price_x96 = pool.functions.slot0().call()[0]
            if sqrt_price_x96 == 0:
                return None
            token_is_token0 = token0.lower() == token_cs.lower()
            decimals0 = _get_decimals(w3, token_address if token_is_token0 else quote_address)
            decimals1 = _get_decimals(w3, quote_address if token_is_token0 else token_address)
            # prix brut (non ajusté décimales) de token1 par token0 : (sqrtP/2^96)^2
            raw_price_1_per_0 = (sqrt_price_x96 / (2 ** 96)) ** 2
            human_price_1_per_0 = raw_price_1_per_0 * (10 ** (decimals0 - decimals1))
            if human_price_1_per_0 <= 0:
                return None
            # prix de token0 en token1, ou son inverse selon quel token est notre `token_address`
            price_in_quote = human_price_1_per_0 if token_is_token0 else (1.0 / human_price_1_per_0)

        if price_in_quote <= 0:
            return None
        quote_price_usd = _quote_token_usd_price(quote_address)
        if quote_price_usd is None:
            return None
        return price_in_quote * quote_price_usd
    except Exception as exc:  # noqa: BLE001
        # INFO et non DEBUG : un échec silencieux ici a masqué pendant des
        # semaines l'ABI `slot0` PancakeSwap V3 erronée (voir routers.py).
        logger.info(
            "Lecture on-chain du prix échouée pour %s/%s sur %s : %s: %s",
            token_address, quote_address, dex, type(exc).__name__, str(exc)[:160],
        )
        return None


def _onchain_quote_depth_usd(
    dex: str,
    token_address: str,
    quote_address: str,
    quote_usd_price: float | None = None,
) -> float | None:
    """Profondeur RÉELLE (en USD) côté quote token de la pool que `dex`
    utiliserait pour `token_address`/`quote_address` — la même pool que celle
    choisie par `_build_swap_call`/`flashloan._build_leg` (résolutions de
    route cachées, donc coût RPC marginal) : réserve réelle pour une pool AMM
    classique (`_quote_reserve_in_pool`), réserve VIRTUELLE au prix courant
    pour une pool à liquidité concentrée (`_v3_style_quote_depth`).

    Sert au détecteur (`app.trading.arbitrage._refine_spread_with_onchain_price`)
    à écarter les signaux fantômes : DexScreener affiche pour une pool V3 une
    "liquidité" égale à la valeur de TOUS ses tokens (positions hors range
    comprises) et un prix qui n'est plus rafraîchi faute de trades — une pool
    PancakeSwap V3 BRETT/WETH ainsi listée à >10k$ n'avait en réalité presque
    aucune liquidité active : spread "rentable" permanent de plusieurs %,
    simulations à -40/-90 %, aucune transaction jamais possible (09/10/2026).

    Retourne None si indéterminable (DEX non supporté, RPC...) : l'appelant ne
    doit alors PAS écarter le signal sur ce seul critère.

    `quote_usd_price` permet à l'appelant cross-DEX de fournir le prix USD
    estimé d'un token intermédiaire, qui n'est pas nécessairement USDC/WETH.
    Sans cette valeur, seuls les quotes reconnus par `_quote_token_usd_price`
    restent évaluables."""
    try:
        from web3 import Web3
        w3 = make_w3(timeout=10)
        if dex == "aerodrome":
            route = _resolve_aerodrome_route(w3, token_address, quote_address)
            if route is None:
                return 0.0
            depth_units = (_v3_style_quote_depth(w3, route["pool"], token_address, quote_address)
                           if route["is_slipstream"] else _quote_reserve_in_pool(w3, route["pool"], quote_address))
        elif dex == "pancakeswap":
            route = _resolve_pancakeswap_route(w3, token_address, quote_address)
            if route is None:
                return 0.0
            depth_units = (_v3_style_quote_depth(w3, route["pool"], token_address, quote_address)
                           if route["is_v3"] else _quote_reserve_in_pool(w3, route["pool"], quote_address))
        elif dex == "uniswap":
            best = _best_v3_style_pool(
                w3, UNISWAP_V3_FACTORY_ADDRESS, UNISWAP_V3_FACTORY_ABI, UNISWAP_V3_FEE_TIERS, token_address, quote_address
            )
            if best is None:
                return 0.0
            depth_units = _v3_style_quote_depth(w3, best[1], token_address, quote_address)
        elif dex in UNISWAP_V2_FORK_DEXES:
            router = w3.eth.contract(
                address=Web3.to_checksum_address(ROUTER_ADDRESSES[dex]), abi=UNISWAP_V2_ROUTER_FACTORY_ABI
            )
            factory_address = _cached_rpc_value(
                ("v2_factory", dex), lambda: router.functions.factory().call(), _IMMUTABLE_CACHE_TTL_SECONDS,
            )
            factory = w3.eth.contract(address=Web3.to_checksum_address(factory_address), abi=UNISWAP_V2_FACTORY_ABI)
            pool_address = _cached_rpc_value(
                ("v2_getPair", factory_address.lower()) + _pair_key(token_address, quote_address),
                lambda: factory.functions.getPair(
                    Web3.to_checksum_address(token_address), Web3.to_checksum_address(quote_address)
                ).call(),
                _IMMUTABLE_CACHE_TTL_SECONDS,
            )
            if int(pool_address, 16) == 0:
                return 0.0
            depth_units = _quote_reserve_in_pool(w3, pool_address, quote_address)
        else:
            return None
        if quote_usd_price is None:
            quote_price_usd = _quote_token_usd_price(quote_address)
        else:
            quote_price_usd = quote_usd_price
        if quote_price_usd is None:
            return None
        return depth_units / (10 ** _get_decimals(w3, quote_address)) * quote_price_usd
    except Exception as exc:  # noqa: BLE001
        logger.debug("Profondeur on-chain indéterminable pour %s/%s sur %s: %s", token_address, quote_address, dex, exc)
        return None


def _refresh_price_usd(chain_id: str, token_address: str, quote_address: str, dex: str,
                        reference_price_usd: float | None = None) -> float | None:
    """Revérifie le prix courant sur `dex` juste avant d'agir (mitigation du
    risque non-atomique documenté en tête de fichier). Retourne None si
    indisponible (dans ce cas l'appelant doit refuser, pas supposer).

    Essaie d'abord une lecture ON-CHAIN directe du pool exact utilisé par
    `_build_swap_call` (voir `_onchain_pool_price_usd`) pour aerodrome/uniswap
    — plus fiable que DexScreener car elle cible précisément LE pool qui sera
    routé, sans ambiguïté. Si indisponible (DEX non supporté par cette lecture,
    RPC injoignable...), retombe sur DexScreener ci-dessous.

    `reference_price_usd`, si fourni (dernier prix connu pour ce token : prix
    DexScreener du signal, ou prix d'achat enregistré pour une position à
    recouvrer), sert à écarter une lecture on-chain implausible via
    `_onchain_price_is_plausible` AVANT de la faire confiance — voir sa
    docstring pour le bug concret évité (pool Uniswap V3 quasi vide pour un
    token récemment ajouté, prix ~10^42x réel, toujours gagnant du tri par
    profit du scheduler).

    Pour un DEX dont la pool est lisible on-chain (tous les DEX exécutables),
    AUCUN repli DexScreener : cette fonction sert à dimensionner
    `amountOutMinimum` et à valider l'écart juste avant d'envoyer une
    transaction, et un prix DexScreener peut dater de plusieurs minutes. Le
    08/11/2026 (ICNT/USDC), une lecture on-chain en échec (RPC 429) a été
    remplacée par un prix DexScreener 0,5 % sous le prix réel de la pool :
    `amountOutMinimum` inatteignable, achat reverté on-chain (« Too little
    received »), killswitch. Mieux vaut refuser la tentative (aucun capital
    engagé, cooldown court) que trader sur un prix périmé. Le repli
    DexScreener ci-dessous ne reste utilisé que pour un DEX sans lecture
    on-chain (`_onchain_pool_price_usd` non supporté) ; parmi ses pools
    partageant ce `dex_id`, on retient celui de plus grande liquidité."""
    onchain_price = _onchain_pool_price_usd(dex, token_address, quote_address)
    if onchain_price is not None:
        if _onchain_price_is_plausible(onchain_price, reference_price_usd):
            return onchain_price
        logger.warning(
            "Prix on-chain implausible pour %s/%s sur %s (%.6g, référence %.6g) — pas de repli DexScreener "
            "pour une décision d'exécution.",
            token_address, quote_address, dex, onchain_price, reference_price_usd,
        )
        return None
    if _onchain_price_supported(dex):
        logger.warning(
            "Prix on-chain indisponible pour %s/%s sur %s — tentative refusée plutôt que de se fier à un prix "
            "DexScreener potentiellement périmé.",
            token_address, quote_address, dex,
        )
        return None
    try:
        pairs = dex_sources.fetch_token_pairs(chain_id, token_address)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Revérification de prix échouée pour %s sur %s: %s", token_address, dex, exc)
        return None

    best_price: float | None = None
    best_liquidity = -1.0
    for raw in pairs or []:
        if raw.get("chainId") != chain_id or raw.get("dexId") != dex:
            continue
        base = raw.get("baseToken") or {}
        quote = raw.get("quoteToken") or {}
        price_usd = raw.get("priceUsd")
        price_native = raw.get("priceNative")
        if price_usd is None:
            continue
        try:
            price_usd = float(price_usd)
        except (TypeError, ValueError):
            continue

        if base.get("address", "").lower() == token_address.lower():
            candidate_price = price_usd
        elif quote.get("address", "").lower() == token_address.lower() and price_native:
            try:
                candidate_price = price_usd / float(price_native)
            except (TypeError, ValueError, ZeroDivisionError):
                continue
        else:
            continue

        try:
            liquidity_usd = float((raw.get("liquidity") or {}).get("usd") or -1)
        except (TypeError, ValueError):
            liquidity_usd = -1.0
        if liquidity_usd > best_liquidity:
            best_liquidity = liquidity_usd
            best_price = candidate_price
    return best_price


def _spread_pct(buy_price: float | None, sell_price: float | None) -> float:
    """Écart achat→vente en % ; 0 si l'un des prix est inutilisable."""
    if not buy_price or not sell_price or buy_price <= 0:
        return 0.0
    return (sell_price - buy_price) / buy_price * 100


def _minimum_spread_pct(signal: dict) -> float:
    """Écart minimal pour qu'un round-trip couvre au moins les frais des deux DEX."""
    return fees.dex_fee_pct(signal["buy_dex"]) + fees.dex_fee_pct(signal["sell_dex"])


def _pool_fee_pct(w3, dex: str, token_in: str, token_out: str, quote_address: str | None = None) -> float:
    """Frais de swap (%) de la pool que `_build_swap_call` routera réellement
    pour `dex` : fee tier réel pour Uniswap V3 et PancakeSwap V3 (résolutions
    de route déjà cachées, coût RPC marginal), constante par DEX de
    `fees.dex_fee_pct` sinon (forks V2, Aerodrome). Ne lève jamais : repli sur
    la constante si la route n'est pas déterminable ici."""
    try:
        if dex == "uniswap":
            return _find_uniswap_v3_fee_tier(w3, token_in, token_out) / 10_000
        if dex == "pancakeswap":
            route = _resolve_pancakeswap_route(w3, token_in, token_out, quote_address)
            if route is not None and route.get("is_v3"):
                return int(route["fee"]) / 10_000
    except Exception as exc:  # noqa: BLE001
        logger.info(
            "Frais réels de la pool %s %s/%s indisponibles (%s) — constante par DEX utilisée.",
            dex, token_in, token_out, exc,
        )
    return fees.dex_fee_pct(dex)


def _min_amount_out_units(expected_out_units: float, *, fee_pct: float, slippage_pct: float) -> int:
    """`amountOutMinimum` à partir de la sortie attendue AU PRIX SPOT (hors
    frais) : retire d'abord les frais de la pool, puis la tolérance de
    slippage. Sans le premier terme, les frais consommaient la tolérance —
    0,3 % de frais sur 0,5 % tolérés ne laissaient que 0,2 % pour l'impact de
    prix et la dérive entre simulation et inclusion, et une pool à 1 % de
    frais revertait systématiquement en « Too little received »."""
    return max(1, int(expected_out_units * (1 - fee_pct / 100) * (1 - slippage_pct / 100)))


def execute_signal(db: Database, config: dict, signal: dict) -> dict:
    """Tente d'exécuter réellement un round-trip d'arbitrage pour `signal`
    (dict au format retourné par `app.trading.arbitrage.run_arbitrage_scan`).

    Retourne toujours un dict `{"executed": bool, "tx_hash_buy": str|None,
    "tx_hash_sell": str|None, "error": str|None}`. Ne lève jamais d'exception
    pour un refus de garde-fou normal (catché et retourné comme `error`) ;
    seules des erreurs de programmation inattendues remonteraient.
    """
    result = {"executed": False, "tx_hash_buy": None, "tx_hash_sell": None, "error": None}

    if not is_trading_live(config):
        result["error"] = "Trading live désactivé (voir app.trading.guardrails.is_trading_live)."
        return result

    if signal["buy_dex"] not in EXECUTABLE_DEXES or signal["sell_dex"] not in EXECUTABLE_DEXES:
        result["error"] = (
            f"DEX non exécutable ({signal['buy_dex']}/{signal['sell_dex']}) "
            f"— seuls {sorted(EXECUTABLE_DEXES)} sont implémentés."
        )
        return result

    if signal["quote_address"].lower() not in EXECUTABLE_QUOTE_TOKENS:
        result["error"] = (
            f"Quote token {signal['quote_symbol']} non exécutable — seuls USDC/WETH sont supportés."
        )
        return result

    # Voir commentaire équivalent dans `execute_triangular_signal` ci-dessus.
    if not signal.get("would_execute_classic", signal.get("would_execute")):
        result["error"] = "Signal non marqué rentable au capital propre (would_execute_classic=False) — refus par prudence."
        return result

    try:
        from web3 import Web3
        w3 = make_w3(timeout=15)
        if not _is_connected_with_retry(w3):
            result["error"] = "RPC Base injoignable."
            return result

        from app.trading.guardrails import trading_keystore_path
        from app.wallet.signer import load_signer
        signer_account = load_signer(
            keystore_path=trading_keystore_path(), passphrase=os.environ.get("WALLET_TRADING_PASSPHRASE")
        )
        wallet_address = signer_account.address
        signer_account = None  # on n'a besoin que de l'adresse ici ; la signature se fait dans guardrails

        token_address = signal["token_address"]
        quote_address = signal["quote_address"]
        trade_size_usd = float(signal["trade_size_usd"])
        risk_slippage_pct = token_slippage_pct(config, token_address, quote_address)
        slip = (risk_slippage_pct if risk_slippage_pct is not None else slippage_pct()) / 100.0
        deadline = int(time.time()) + 180
        trade_cap_usd = token_trade_cap(config, token_address, quote_address, trade_size_usd)
        if trade_size_usd > trade_cap_usd:
            result["error"] = (
                f"Taille du signal ({trade_size_usd:.4f}$) supérieure au plafond de cette paire "
                f"({trade_cap_usd:.4f}$) — refus."
            )
            return result

        quote_decimals = _get_decimals(w3, quote_address)
        token_decimals = _get_decimals(w3, token_address)

        quote_usd_price = _quote_token_usd_price(quote_address)
        if not quote_usd_price or quote_usd_price <= 0:
            result["error"] = f"Prix USD de {signal['quote_symbol']} indisponible — refus par prudence."
            return result

        # --- Garde-fou solde réel : ne jamais trader plus que ce qui est dispo ---
        quote_contract = w3.eth.contract(address=Web3.to_checksum_address(quote_address), abi=ERC20_ABI)
        quote_balance_units = _balance_of_with_retry(quote_contract, wallet_address)
        actual_gas_cost_wei = 0
        amount_in_quote_tokens = trade_size_usd / quote_usd_price
        amount_in_units = int(amount_in_quote_tokens * (10 ** quote_decimals))
        if amount_in_units > quote_balance_units:
            result["error"] = (
                f"Solde {signal['quote_symbol']} insuffisant ({quote_balance_units / 10**quote_decimals:.6f} < "
                f"{amount_in_quote_tokens:.6f} requis pour {trade_size_usd:.4f}$) — refus."
            )
            return result

        if risk_slippage_pct is not None:
            minimum_depth = quote_depth_floor(
                config, token_address, quote_address,
                float(((config or {}).get("trading") or {}).get("min_liquidity_usd", 10_000)),
            )
            for dex in (signal["buy_dex"], signal["sell_dex"]):
                depth = _onchain_quote_depth_usd(dex, token_address, quote_address)
                if depth is None or depth < minimum_depth:
                    result["error"] = (
                        f"Profondeur on-chain BRETT indisponible ou sous {minimum_depth:.2f}$ "
                        f"sur {dex} — refus avant envoi."
                    )
                    return result

        # --- Revérification du prix d'achat en direct avant la jambe 1 ---
        # Même principe que la jambe 2 ci-dessous : le prix de détection peut
        # être périmé (DexScreener cache de quelques secondes à quelques
        # minutes). On utilise le prix on-chain frais du pool réellement routé
        # pour dimensionner min_amount_out, sinon la simulation reverte et le
        # cycle est inutilement mis en cooldown.
        fresh_buy_price = _refresh_price_usd(signal["chain"], token_address, quote_address,
                                              signal["buy_dex"], reference_price_usd=signal.get("buy_price_usd"))
        if fresh_buy_price is None:
            result["error"] = (
                "Impossible de revérifier le prix d'achat avant la jambe 1 — "
                "round-trip interrompu par prudence."
            )
            db.mark_arbitrage_signal_executed(signal["id"], execution_error=result["error"])
            return result

        # --- Revérification de l'ÉCART avant d'engager le moindre capital ---
        # Jusqu'ici seul le prix d'achat était rafraîchi avant la jambe 1 ; le
        # contrôle de l'écart n'intervenait qu'APRÈS l'achat, si bien qu'un
        # spread DexScreener fictif (ICNT/USDC 08/11/2026 : 4,59 % affiché,
        # 0,53 % réel) déclenchait quand même un achat, puis une revente de
        # récupération au cycle suivant — frais + gas perdus pour rien. Ici
        # aucune transaction n'a encore été envoyée : refus sans capital
        # engagé (cooldown court côté scheduler).
        fresh_sell_price = _refresh_price_usd(signal["chain"], token_address, quote_address, signal["sell_dex"],
                                               reference_price_usd=signal.get("sell_price_usd"))
        if fresh_sell_price is None:
            result["error"] = (
                "Impossible de revérifier le prix de vente avant la jambe 1 — "
                "round-trip refusé par prudence, aucune transaction envoyée."
            )
            return result
        pre_buy_spread_pct = _spread_pct(fresh_buy_price, fresh_sell_price)
        min_required_spread_pct = _minimum_spread_pct(signal)
        if pre_buy_spread_pct < min_required_spread_pct:
            result["error"] = (
                f"Écart refermé avant la jambe d'achat ({pre_buy_spread_pct:.3f}% < "
                f"{min_required_spread_pct:.3f}% de frais minimum) — aucune transaction envoyée."
            )
            return result

        # --- Jambe 1 : achat de token_address avec quote_address sur buy_dex ---
        expected_token_out_units = trade_size_usd / fresh_buy_price * (10 ** token_decimals)
        min_token_out_units = _min_amount_out_units(
            expected_token_out_units,
            fee_pct=_pool_fee_pct(w3, signal["buy_dex"], quote_address, token_address, quote_address),
            slippage_pct=slip * 100,
        )
        contract_address, abi, fn_name, args, approve_result = _build_swap_call_and_ensure_allowance(
            db, w3, dex=signal["buy_dex"], token_in=quote_address, token_out=token_address,
            amount_in=amount_in_units, min_amount_out=min_token_out_units,
            recipient=wallet_address, deadline=deadline, quote_address=quote_address,
            allowance_token_address=quote_address, owner=wallet_address,
            allowance_amount=amount_in_units,
        )
        actual_gas_cost_wei += int(approve_result.get("gas_cost_wei") or 0)
        if approve_result["error"]:
            result["error"] = f"Approve (jambe achat) échoué : {approve_result['error']}"
            if approve_result.get("tx_hash"):
                db.mark_arbitrage_signal_executed(
                    signal["id"], execution_error=result["error"],
                    **_actual_execution_metrics(None, quote_usd_price, actual_gas_cost_wei),
                )
            return result

        token_contract = w3.eth.contract(address=Web3.to_checksum_address(token_address), abi=ERC20_ABI)
        token_balance_before_units = _balance_of_with_retry(token_contract, wallet_address)
        buy_result = send_trading_transaction(
            db, rpc_url=_resolve_rpc_url(), contract_address=contract_address, abi=abi,
            function_name=fn_name, args=args, whitelist_target=contract_address,
            action_label=f"arbitrage achat {signal['token_symbol']} sur {signal['buy_dex']}",
        )
        result["tx_hash_buy"] = buy_result["tx_hash"]
        actual_gas_cost_wei += int(buy_result.get("gas_cost_wei") or 0)
        if buy_result["error"]:
            result["error"] = f"Jambe achat échouée : {buy_result['error']}"
            db.mark_arbitrage_signal_executed(signal["id"], tx_hash_buy=result["tx_hash_buy"],
                                               execution_error=result["error"],
                                               **_actual_execution_metrics(
                                                   0.0 if buy_result.get("gas_cost_wei") is not None else None,
                                                   quote_usd_price, actual_gas_cost_wei,
                                               ))
            return result

        # --- Solde net de token reçu par cet achat, hors inventaire préexistant ---
        # Quelques tentatives avec backoff : certains endpoints RPC publics sont
        # répartis sur plusieurs nœuds qui ne sont pas rigoureusement synchronisés
        # entre eux — lire le solde immédiatement après le receipt de la jambe
        # d'achat peut retomber sur un nœud légèrement en retard et renvoyer un
        # faux zéro, alors que la transaction a bien transféré les tokens (vu
        # a posteriori via les logs Transfer). Sans cette tolérance, le bot
        # annule la jambe de vente à tort et laisse le wallet exposé au token
        # acheté au lieu de boucler proprement en USDC.
        token_balance_units = 0
        for attempt in range(4):
            token_balance_after_units = _balance_of_with_retry(token_contract, wallet_address)
            token_balance_units = _newly_acquired_token_amount(
                token_balance_before_units, token_balance_after_units,
            )
            if token_balance_units > 0:
                break
            if attempt < 3:
                time.sleep(3)
        if token_balance_units <= 0:
            result["error"] = (
                "Jambe achat confirmée on-chain mais aucun nouveau solde de token attribuable à cet achat "
                "après plusieurs tentatives — vente annulée pour préserver les tokens déjà détenus."
            )
            db.mark_arbitrage_signal_executed(signal["id"], tx_hash_buy=result["tx_hash_buy"],
                                               execution_error=result["error"],
                                               **_actual_execution_metrics(None, quote_usd_price, actual_gas_cost_wei))
            return result

        # --- Revérification du prix avant la jambe 2 (mitigation non-atomique) ---
        fresh_sell_price = _refresh_price_usd(signal["chain"], token_address, quote_address, signal["sell_dex"],
                                               reference_price_usd=signal.get("sell_price_usd"))
        fresh_buy_price = _refresh_price_usd(signal["chain"], token_address, quote_address, signal["buy_dex"],
                                              reference_price_usd=signal.get("buy_price_usd"))
        if fresh_sell_price is None:
            result["error"] = (
                "Impossible de revérifier le prix de vente avant la 2e jambe — round-trip interrompu "
                "par prudence (le token acheté reste dans le wallet, revente manuelle possible)."
            )
            db.mark_arbitrage_signal_executed(signal["id"], tx_hash_buy=result["tx_hash_buy"],
                                               execution_error=result["error"],
                                               **_actual_execution_metrics(None, quote_usd_price, actual_gas_cost_wei))
            return result

        fresh_spread_pct = _spread_pct(fresh_buy_price, fresh_sell_price)
        min_required_spread_pct = _minimum_spread_pct(signal)
        if fresh_spread_pct < min_required_spread_pct:
            result["error"] = (
                f"Écart refermé avant la 2e jambe ({fresh_spread_pct:.3f}% < {min_required_spread_pct:.3f}% "
                "de frais minimum) — round-trip interrompu, le token acheté reste dans le wallet."
            )
            db.mark_arbitrage_signal_executed(signal["id"], tx_hash_buy=result["tx_hash_buy"],
                                               execution_error=result["error"],
                                               **_actual_execution_metrics(None, quote_usd_price, actual_gas_cost_wei))
            return result

        # --- Jambe 2 : vente de token_address contre quote_address sur sell_dex ---
        quote_usd_price_sell = _quote_token_usd_price(quote_address) or quote_usd_price
        expected_quote_out_usd = token_balance_units / (10 ** token_decimals) * fresh_sell_price
        expected_quote_out_tokens = expected_quote_out_usd / quote_usd_price_sell
        min_quote_out_units = _min_amount_out_units(
            expected_quote_out_tokens * (10 ** quote_decimals),
            fee_pct=_pool_fee_pct(w3, signal["sell_dex"], token_address, quote_address, quote_address),
            slippage_pct=slip * 100,
        )
        contract_address, abi, fn_name, args, approve_result = _build_swap_call_and_ensure_allowance(
            db, w3, dex=signal["sell_dex"], token_in=token_address, token_out=quote_address,
            amount_in=token_balance_units, min_amount_out=min_quote_out_units,
            recipient=wallet_address, deadline=deadline, quote_address=quote_address,
            allowance_token_address=token_address, owner=wallet_address,
            allowance_amount=token_balance_units,
        )
        actual_gas_cost_wei += int(approve_result.get("gas_cost_wei") or 0)
        if approve_result["error"]:
            result["error"] = f"Approve (jambe vente) échoué : {approve_result['error']}"
            db.mark_arbitrage_signal_executed(signal["id"], tx_hash_buy=result["tx_hash_buy"],
                                               execution_error=result["error"],
                                               **_actual_execution_metrics(None, quote_usd_price, actual_gas_cost_wei))
            return result

        sell_result = send_trading_transaction(
            db, rpc_url=_resolve_rpc_url(), contract_address=contract_address, abi=abi,
            function_name=fn_name, args=args, whitelist_target=contract_address,
            action_label=f"arbitrage vente {signal['token_symbol']} sur {signal['sell_dex']}",
        )
        result["tx_hash_sell"] = sell_result["tx_hash"]
        actual_gas_cost_wei += int(sell_result.get("gas_cost_wei") or 0)
        if sell_result["error"]:
            result["error"] = f"Jambe vente échouée : {sell_result['error']}"
            db.mark_arbitrage_signal_executed(signal["id"], tx_hash_buy=result["tx_hash_buy"],
                                               tx_hash_sell=result["tx_hash_sell"], execution_error=result["error"],
                                               **_actual_execution_metrics(
                                                   None,
                                                   quote_usd_price, actual_gas_cost_wei,
                                               ))
            return result

        result["executed"] = True
        profit_units = _net_token_transfer_from_receipts(
            quote_address, wallet_address,
            [buy_result.get("receipt"), sell_result.get("receipt")],
        )
        profit_quote = profit_units / (10 ** quote_decimals) if profit_units is not None else None
        if profit_quote is None:
            logger.warning(
                "Aucun transfert du token de cotation trouvé dans les reçus de l'arbitrage %s/%s.",
                signal["token_symbol"], signal["quote_symbol"],
            )
        actual_metrics = _actual_execution_metrics(profit_quote, quote_usd_price, actual_gas_cost_wei)
        db.mark_arbitrage_signal_executed(signal["id"], tx_hash_buy=result["tx_hash_buy"],
                                           tx_hash_sell=result["tx_hash_sell"], **actual_metrics)
        db.add_notification(
            title="✅ Round-trip d'arbitrage exécuté",
            message=(
                f"{signal['token_symbol']}/{signal['quote_symbol']} : achat {signal['buy_dex']} -> "
                f"vente {signal['sell_dex']} — tx achat {result['tx_hash_buy']}, tx vente {result['tx_hash_sell']}"
            ),
            level="info",
        )
        return result

    except TradingRefused as exc:
        result["error"] = f"Refusé par un garde-fou : {exc}"
        return result
    except Exception as exc:  # noqa: BLE001 - jamais de crash du scheduler pour une tentative de trade
        logger.exception("Erreur inattendue pendant execute_signal")
        result["error"] = f"Erreur inattendue : {exc}"
        return result


# En dessous de ce seuil (valeur estimée en USD), on ne tente pas de revendre :
# le gas coûterait plus cher que la poussière récupérée.
_MIN_RECOVERY_VALUE_USD = 0.50

# Valeur sentinelle (non-nulle, mais pas un vrai hash de tx) utilisée pour
# `tx_hash_sell` quand une position est jugée définitivement close SANS vente
# réelle (solde on-chain déjà nul). `Database.list_open_positions` filtre sur
# `tx_hash_sell IS NULL` : y laisser NULL ferait réapparaître indéfiniment la
# même ligne à chaque cycle (elle ne serait jamais exclue).
_NO_SALE_CLOSED_SENTINEL = "no-sale:balance-zero"


def _fetch_receipt(w3, tx_hash: str | None) -> dict | None:
    """Reçu d'une transaction déjà confirmée (hash persisté sans préfixe 0x)."""
    if not tx_hash:
        return None
    normalized = tx_hash if tx_hash.startswith("0x") else f"0x{tx_hash}"
    try:
        return call_with_rate_limit_retry(lambda: w3.eth.get_transaction_receipt(normalized))
    except Exception as exc:  # noqa: BLE001
        logger.warning("Reçu de la transaction %s indisponible : %s", normalized, exc)
        return None


def _recovery_execution_metrics(
    position: dict, wallet_address: str, buy_receipt: dict | None, sell_receipt: dict | None,
    quote_decimals: int, quote_usd_price: float | None, recovery_gas_cost_wei: int,
) -> dict:
    """P&L réellement imputable à un round-trip clôturé par `recover_open_positions`.

    La vente de récupération écoule TOUT le solde du token (inventaire
    préexistant compris, ICNT 08/11/2026 : 0,54$ achetés, ~3,28$ revendus) :
    seule la fraction correspondant aux unités acquises par la jambe d'achat
    est imputée au signal, au prorata des unités vendues. Sans reçu d'achat
    exploitable, le P&L reste non mesuré plutôt qu'estimé sur le solde entier
    (ce qui gonflerait artificiellement le profit)."""
    buy_gas_cost_wei = position.get("actual_gas_cost_wei")
    if buy_gas_cost_wei is None and buy_receipt is not None:
        buy_gas_cost_wei = fees.receipt_total_gas_cost_wei(buy_receipt)
    total_gas_cost_wei = int(buy_gas_cost_wei or 0) + int(recovery_gas_cost_wei or 0)

    token_address = position["token_address"]
    quote_address = position["quote_address"]
    acquired_units = _net_token_transfer_from_receipts(token_address, wallet_address, [buy_receipt])
    spent_quote_units = _net_token_transfer_from_receipts(quote_address, wallet_address, [buy_receipt])
    sold_units = _net_token_transfer_from_receipts(token_address, wallet_address, [sell_receipt])
    received_quote_units = _net_token_transfer_from_receipts(quote_address, wallet_address, [sell_receipt])
    profit_quote = None
    if (
        acquired_units is not None and acquired_units > 0
        and spent_quote_units is not None
        and sold_units is not None and sold_units < 0
        and received_quote_units is not None
    ):
        attributable_fraction = min(1.0, acquired_units / -sold_units)
        # `spent_quote_units` est négatif (le quote a quitté le wallet à l'achat).
        profit_units = received_quote_units * attributable_fraction + spent_quote_units
        profit_quote = profit_units / (10 ** quote_decimals)
    return _actual_execution_metrics(profit_quote, quote_usd_price, total_gas_cost_wei)


def recover_open_positions(db: Database, config: dict) -> list[dict]:
    """Termine les round-trips restés incomplets (voir `Database.list_open_positions`) :
    typiquement une jambe d'achat confirmée on-chain mais dont la jambe de vente a été
    annulée par prudence (écart refermé, anomalie de lecture de solde, etc.), laissant
    un token non-USDC dans le wallet de trading. Revend ce solde dès qu'un prix
    utilisable est disponible sur l'un des DEX exécutables — sans exiger l'écart
    d'origine (il n'y a plus de 2e jambe « rentable » à comparer, juste une position
    existante à clôturer proprement pour revenir en USDC).

    Ne lève jamais d'exception : chaque position est traitée indépendamment, un échec
    sur l'une n'empêche pas de traiter les suivantes."""
    results: list[dict] = []
    if not is_trading_live(config):
        return results

    open_positions = db.list_open_positions()
    if not open_positions:
        return results

    try:
        from web3 import Web3
        w3 = make_w3(timeout=15)
        if not _is_connected_with_retry(w3):
            return results

        from app.trading.guardrails import trading_keystore_path
        from app.wallet.signer import load_signer
        signer_account = load_signer(
            keystore_path=trading_keystore_path(), passphrase=os.environ.get("WALLET_TRADING_PASSPHRASE")
        )
        wallet_address = signer_account.address
        signer_account = None
    except Exception as exc:  # noqa: BLE001
        logger.warning("recover_open_positions: impossible d'initialiser le signer/RPC : %s", exc)
        return results

    for position in open_positions:
        outcome = {"position_id": position["id"], "token_symbol": position["token_symbol"],
                   "recovered": False, "tx_hash_sell": None, "error": None}
        try:
            token_address = position["token_address"]
            quote_address = position["quote_address"]

            token_contract = w3.eth.contract(address=Web3.to_checksum_address(token_address), abi=ERC20_ABI)
            token_balance_units = _balance_of_with_retry(token_contract, wallet_address)
            if token_balance_units <= 0:
                # Rien à récupérer (déjà revendu manuellement, ou poussière consommée) :
                # on referme réellement la ligne (sentinelle non-nulle) pour ne plus la
                # retenter à chaque cycle — laisser tx_hash_sell=None ne l'aurait pas
                # exclue de `list_open_positions` (déjà NULL avant cet appel).
                closure_note = "Position considérée close : solde on-chain nul au moment de la relance."
                original_error = position.get("execution_error")
                db.mark_arbitrage_signal_executed(
                    position["id"], tx_hash_buy=position["tx_hash_buy"], tx_hash_sell=_NO_SALE_CLOSED_SENTINEL,
                    execution_error=f"{original_error} {closure_note}" if original_error else closure_note,
                )
                logger.info("Position résiduelle %s (signal #%s) : %s", position["token_symbol"], position["id"], closure_note)
                continue

            token_decimals = _get_decimals(w3, token_address)
            quote_decimals = _get_decimals(w3, quote_address)
            quote_usd_price = _quote_token_usd_price(quote_address)
            if not quote_usd_price or quote_usd_price <= 0:
                outcome["error"] = f"Prix USD de {position['quote_symbol']} indisponible — nouvelle tentative au prochain cycle."
                results.append(outcome)
                continue

            # Classe tous les DEX exécutables par prix décroissant, puis tente la vente
            # sur chacun jusqu'au premier succès. Le prix affiché par `_refresh_price_usd`
            # n'est qu'une estimation (quote API/on-chain) : la transaction peut échouer
            # on-chain pour ce DEX précis (liquidité insuffisante pour honorer
            # min_amount_out, pool inexistant pour cette paire, etc.) sans que les autres
            # DEX exécutables soient affectés — d'où le repli au lieu d'abandonner.
            dex_prices = []
            for dex in EXECUTABLE_DEXES:
                price = _refresh_price_usd(position["chain"], token_address, quote_address, dex,
                                            reference_price_usd=position.get("buy_price_usd"))
                if price:
                    dex_prices.append((dex, price))
            if not dex_prices:
                outcome["error"] = "Aucun prix de vente disponible sur les DEX exécutables — nouvelle tentative au prochain cycle."
                results.append(outcome)
                continue
            dex_prices.sort(key=lambda dp: dp[1], reverse=True)

            best_price = dex_prices[0][1]
            estimated_value_usd = token_balance_units / (10 ** token_decimals) * best_price
            if estimated_value_usd < _MIN_RECOVERY_VALUE_USD:
                outcome["error"] = (
                    f"Solde résiduel trop faible (~{estimated_value_usd:.2f}$) — laissé tel quel "
                    "(le gas coûterait plus cher que la récupération)."
                )
                results.append(outcome)
                continue

            slip = slippage_pct() / 100.0
            deadline = int(time.time()) + 180
            last_error = None
            recovery_gas_cost_wei = 0
            logger.warning(
                "Récupération de position résiduelle %s (signal #%s) : ~%.2f$ à revendre, DEX candidats %s.",
                position["token_symbol"], position["id"], estimated_value_usd,
                ", ".join(dex for dex, _ in dex_prices),
            )
            for dex, price in dex_prices:
                min_quote_out_units = _min_amount_out_units(
                    token_balance_units / (10 ** token_decimals) * price / quote_usd_price * (10 ** quote_decimals),
                    fee_pct=_pool_fee_pct(w3, dex, token_address, quote_address, quote_address),
                    slippage_pct=slip * 100,
                )
                contract_address, abi, fn_name, args, approve_result = _build_swap_call_and_ensure_allowance(
                    db, w3, dex=dex, token_in=token_address, token_out=quote_address,
                    amount_in=token_balance_units, min_amount_out=min_quote_out_units,
                    recipient=wallet_address, deadline=deadline, quote_address=quote_address,
                    allowance_token_address=token_address, owner=wallet_address,
                    allowance_amount=token_balance_units,
                    count_against_daily_limit=False,
                )
                recovery_gas_cost_wei += int(approve_result.get("gas_cost_wei") or 0)
                if approve_result["error"]:
                    last_error = f"Approve échoué sur {dex} : {approve_result['error']}"
                    logger.warning("Récupération %s : %s", position["token_symbol"], last_error)
                    continue

                sell_result = send_trading_transaction(
                    db, rpc_url=_resolve_rpc_url(), contract_address=contract_address, abi=abi,
                    function_name=fn_name, args=args, whitelist_target=contract_address,
                    action_label=f"récupération position {position['token_symbol']} sur {dex}",
                    count_against_daily_limit=False,
                )
                recovery_gas_cost_wei += int(sell_result.get("gas_cost_wei") or 0)
                if sell_result["error"]:
                    last_error = f"Vente échouée sur {dex} : {sell_result['error']}"
                    logger.warning("Récupération %s : %s", position["token_symbol"], last_error)
                    continue

                outcome["tx_hash_sell"] = sell_result["tx_hash"]
                # Le round-trip reste un ÉCHEC (vente de récupération, pas la
                # 2e jambe prévue) : on conserve la raison d'origine au lieu de
                # l'écraser — sinon `live_trading_stats` le comptait comme un
                # arbitrage « completed » et le P&L restait non mesuré.
                closure_note = f"Position résiduelle revendue sur {dex} (tx {outcome['tx_hash_sell']})."
                original_error = position.get("execution_error")
                metrics = _recovery_execution_metrics(
                    position, wallet_address,
                    buy_receipt=_fetch_receipt(w3, position.get("tx_hash_buy")),
                    sell_receipt=sell_result.get("receipt"),
                    quote_decimals=quote_decimals, quote_usd_price=quote_usd_price,
                    recovery_gas_cost_wei=recovery_gas_cost_wei,
                )
                db.mark_arbitrage_signal_executed(
                    position["id"], tx_hash_buy=position["tx_hash_buy"], tx_hash_sell=outcome["tx_hash_sell"],
                    execution_error=f"{original_error} {closure_note}" if original_error else closure_note,
                    **metrics,
                )
                realized = metrics.get("realized_pnl_usd")
                logger.warning(
                    "Position résiduelle %s revendue sur %s (~%.2f$) — tx %s — P&L imputé au signal #%s : %s.",
                    position["token_symbol"], dex, estimated_value_usd, outcome["tx_hash_sell"], position["id"],
                    f"{realized:+.4f}$" if realized is not None else "non mesuré (reçu d'achat indisponible)",
                )
                db.add_notification(
                    title="✅ Position résiduelle clôturée",
                    message=(
                        f"{position['token_symbol']} revendu sur {dex} (~{estimated_value_usd:.2f}$) — "
                        f"tx {outcome['tx_hash_sell']}"
                    ),
                    level="info",
                )
                outcome["recovered"] = True
                break

            if not outcome["recovered"]:
                outcome["error"] = last_error or "Vente impossible sur tous les DEX exécutables."
            results.append(outcome)
        except TradingRefused as exc:
            outcome["error"] = f"Refusé par un garde-fou : {exc}"
            results.append(outcome)
        except Exception as exc:  # noqa: BLE001 - une position en échec ne doit pas bloquer les autres
            logger.exception("Erreur inattendue en récupérant une position ouverte")
            outcome["error"] = f"Erreur inattendue : {exc}"
            results.append(outcome)

    return results


def sell_token_balance(db: Database, config: dict, token_address: str, quote_address: str, *,
                       dry_run: bool = False) -> dict:
    """Revend TOUT le solde de `token_address` contre `quote_address` (USDC/WETH)
    sur le DEX exécutable offrant le meilleur prix on-chain — liquidation
    manuelle d'un inventaire (`main.py liquidate-inventory`), hors de tout
    signal d'arbitrage. Mêmes garde-fous que `recover_open_positions` :
    trading live requis, prix on-chain obligatoire, profondeur de pool
    vérifiée, `amountOutMinimum` net des frais de la pool, routeur whitelisté,
    killswitch. `dry_run=True` calcule et renvoie le plan sans rien envoyer.

    Retourne `{"sold": bool, "dry_run": bool, "dex": str|None, "balance": float,
    "estimated_value_usd": float, "received_quote": float|None, "tx_hash": str|None,
    "error": str|None}`."""
    result: dict = {
        "sold": False, "dry_run": dry_run, "dex": None, "balance": 0.0, "estimated_value_usd": 0.0,
        "received_quote": None, "tx_hash": None, "error": None,
    }
    if quote_address.lower() not in EXECUTABLE_QUOTE_TOKENS:
        result["error"] = f"Quote {quote_address} non exécutable — seuls {sorted(EXECUTABLE_QUOTE_TOKENS)} sont supportés."
        return result
    if not dry_run and not is_trading_live(config):
        result["error"] = "Trading live désactivé (voir app.trading.guardrails.is_trading_live) — utiliser dry_run."
        return result
    try:
        from web3 import Web3
        w3 = make_w3(timeout=15)
        if not _is_connected_with_retry(w3):
            result["error"] = "RPC Base injoignable."
            return result
        from app.trading.guardrails import trading_keystore_path
        from app.wallet.signer import load_signer
        signer_account = load_signer(
            keystore_path=trading_keystore_path(), passphrase=os.environ.get("WALLET_TRADING_PASSPHRASE")
        )
        wallet_address = signer_account.address
        signer_account = None

        token_contract = w3.eth.contract(address=Web3.to_checksum_address(token_address), abi=ERC20_ABI)
        balance_units = _balance_of_with_retry(token_contract, wallet_address)
        token_decimals = _get_decimals(w3, token_address)
        quote_decimals = _get_decimals(w3, quote_address)
        result["balance"] = balance_units / (10 ** token_decimals)
        if balance_units <= 0:
            result["error"] = "Solde nul : rien à vendre."
            return result
        quote_usd_price = _quote_token_usd_price(quote_address)
        if not quote_usd_price or quote_usd_price <= 0:
            result["error"] = "Prix USD du quote indisponible — refus par prudence."
            return result

        dex_prices = []
        for dex in EXECUTABLE_DEXES:
            price = _refresh_price_usd("base", token_address, quote_address, dex)
            if price:
                dex_prices.append((dex, price))
        if not dex_prices:
            result["error"] = "Aucun prix on-chain disponible sur les DEX exécutables."
            return result
        dex_prices.sort(key=lambda dp: dp[1], reverse=True)
        best_price = dex_prices[0][1]
        estimated_value_usd = result["balance"] * best_price
        result["estimated_value_usd"] = estimated_value_usd
        if estimated_value_usd < _MIN_RECOVERY_VALUE_USD:
            result["error"] = f"Valeur ~{estimated_value_usd:.2f}$ sous le minimum de {_MIN_RECOVERY_VALUE_USD:.2f}$ : le gas coûterait plus cher."
            return result

        slip = slippage_pct() / 100.0
        deadline = int(time.time()) + 180
        last_error = None
        for dex, price in dex_prices:
            depth_usd = _onchain_quote_depth_usd(dex, token_address, quote_address)
            # Au moins 20x la taille vendue en profondeur côté quote : évite les
            # pools fantômes (ex. Uniswap BRETT/USDC ≈ 3 $) et un impact de prix
            # que la tolérance de slippage ne couvrirait pas.
            if depth_usd is None or depth_usd < 20 * estimated_value_usd:
                last_error = f"{dex} : profondeur on-chain insuffisante ({depth_usd if depth_usd is None else round(depth_usd, 2)}$)."
                continue
            min_quote_out_units = _min_amount_out_units(
                balance_units / (10 ** token_decimals) * price / quote_usd_price * (10 ** quote_decimals),
                fee_pct=_pool_fee_pct(w3, dex, token_address, quote_address, quote_address),
                slippage_pct=slip * 100,
            )
            result["dex"] = dex
            result["plan"] = {
                "dex": dex, "price_usd": price, "depth_usd": depth_usd,
                "min_quote_out": min_quote_out_units / (10 ** quote_decimals),
                "alternatives": [(d, p) for d, p in dex_prices if d != dex],
            }
            if dry_run:
                return result
            contract_address, abi, fn_name, args, approve_result = _build_swap_call_and_ensure_allowance(
                db, w3, dex=dex, token_in=token_address, token_out=quote_address,
                amount_in=balance_units, min_amount_out=min_quote_out_units,
                recipient=wallet_address, deadline=deadline, quote_address=quote_address,
                allowance_token_address=token_address, owner=wallet_address,
                allowance_amount=balance_units, count_against_daily_limit=False,
            )
            if approve_result["error"]:
                last_error = f"Approve échoué sur {dex} : {approve_result['error']}"
                logger.warning("Liquidation %s : %s", token_address, last_error)
                continue
            sell_result = send_trading_transaction(
                db, rpc_url=_resolve_rpc_url(), contract_address=contract_address, abi=abi,
                function_name=fn_name, args=args, whitelist_target=contract_address,
                action_label=f"liquidation inventaire {token_address[:10]} sur {dex}",
                count_against_daily_limit=False,
            )
            if sell_result["error"]:
                last_error = f"Vente échouée sur {dex} : {sell_result['error']}"
                logger.warning("Liquidation %s : %s", token_address, last_error)
                continue
            result["sold"] = True
            result["tx_hash"] = sell_result["tx_hash"]
            received_units = _net_token_transfer_from_receipts(quote_address, wallet_address, [sell_result.get("receipt")])
            result["received_quote"] = received_units / (10 ** quote_decimals) if received_units is not None else None
            message = (
                f"Inventaire liquidé : {result['balance']:.6f} {token_address[:10]} → "
                f"{result['received_quote'] if result['received_quote'] is not None else '?'} quote sur {dex} "
                f"(~{estimated_value_usd:.2f}$) — tx {result['tx_hash']}"
            )
            logger.warning(message)
            db.add_notification(title="🧹 Inventaire liquidé", message=message, level="info")
            return result
        result["error"] = last_error or "Vente impossible sur tous les DEX exécutables."
        return result
    except TradingRefused as exc:
        result["error"] = f"Refusé par un garde-fou : {exc}"
        return result
    except Exception as exc:  # noqa: BLE001
        logger.exception("Erreur inattendue pendant sell_token_balance")
        result["error"] = f"Erreur inattendue : {exc}"
        return result
