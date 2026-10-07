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
- DEX exécutables : uniquement uniswap, aerodrome, sushiswap, pancakeswap
  (voir `app.trading.routers.EXECUTABLE_DEXES`). PancakeSwap utilise ici
  uniquement son Router v2 classique (`swapExactTokensForTokens`, vérifié sur
  deux sources indépendantes — voir `app/trading/routers.py`) ; son "Smart
  Router"/Universal Router (encodage multi-route) n'est jamais utilisé.
- Quote token exécutable : uniquement USDC (`EXECUTABLE_QUOTE_TOKENS`). Un
  round-trip part et termine en USDC — jamais dans un token volatile — pour
  ne jamais finir avec un "reste" de valeur imprévisible si la 2e jambe échoue.
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

from app.database import Database
from app.trading import dex_sources, fees
from app.trading.guardrails import TradingRefused, is_trading_live, send_trading_transaction, slippage_pct
from app.trading.routers import (
    AERODROME_ROUTER_ABI,
    ERC20_ABI,
    EXECUTABLE_DEXES,
    ROUTER_ADDRESSES,
    UNISWAP_V2_ROUTER_ABI,
    UNISWAP_V3_FACTORY_ABI,
    UNISWAP_V3_FACTORY_ADDRESS,
    UNISWAP_V3_FEE_TIERS,
    UNISWAP_V3_ROUTER_ABI,
)

logger = logging.getLogger("app.trading.executor")

# USDC sur Base — seul token "quote" exécutable (voir docstring ci-dessus).
EXECUTABLE_QUOTE_TOKENS = frozenset({"0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"})

_DECIMALS_CACHE: dict[str, int] = {}


def _resolve_rpc_url() -> str:
    return os.environ.get("RPC_BASE") or "https://mainnet.base.org"


def _get_decimals(w3, token_address: str) -> int:
    key = token_address.lower()
    if key not in _DECIMALS_CACHE:
        from web3 import Web3
        contract = w3.eth.contract(address=Web3.to_checksum_address(token_address), abi=ERC20_ABI)
        _DECIMALS_CACHE[key] = int(contract.functions.decimals().call())
    return _DECIMALS_CACHE[key]


def _ensure_allowance(db: Database, w3, *, token_address: str, owner: str, spender: str, amount: int) -> dict:
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
    )


def _find_uniswap_v3_fee_tier(w3, token_in: str, token_out: str) -> int:
    from web3 import Web3
    factory = w3.eth.contract(address=Web3.to_checksum_address(UNISWAP_V3_FACTORY_ADDRESS), abi=UNISWAP_V3_FACTORY_ABI)
    for fee_tier in UNISWAP_V3_FEE_TIERS:
        pool = factory.functions.getPool(
            Web3.to_checksum_address(token_in), Web3.to_checksum_address(token_out), fee_tier
        ).call()
        if int(pool, 16) != 0:
            return fee_tier
    raise TradingRefused(
        f"Aucune pool Uniswap V3 trouvée pour {token_in}/{token_out} (fee tiers testés: {UNISWAP_V3_FEE_TIERS})."
    )


def _build_swap_call(dex: str, w3, *, token_in: str, token_out: str, amount_in: int,
                      min_amount_out: int, recipient: str, deadline: int) -> tuple[str, list[dict], str, tuple]:
    """Construit (contract_address, abi, function_name, args) pour le DEX demandé.
    Lève `TradingRefused` si le DEX n'est pas dans EXECUTABLE_DEXES."""
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
        router = w3.eth.contract(address=Web3.to_checksum_address(router_address), abi=AERODROME_ROUTER_ABI)
        default_factory = router.functions.defaultFactory().call()
        routes = [(token_in_cs, token_out_cs, False, default_factory)]
        return router_address, AERODROME_ROUTER_ABI, "swapExactTokensForTokens", (
            amount_in, min_amount_out, routes, recipient_cs, deadline,
        )

    # sushiswap / pancakeswap : ABI UniswapV2Router02 standard, chemin direct token_in->token_out.
    if dex in ("sushiswap", "pancakeswap"):
        path = [token_in_cs, token_out_cs]
        return router_address, UNISWAP_V2_ROUTER_ABI, "swapExactTokensForTokens", (
            amount_in, min_amount_out, path, recipient_cs, deadline,
        )

    raise TradingRefused(f"Exécution non implémentée pour le DEX '{dex}' (voir EXECUTABLE_DEXES).")


def _refresh_price_usd(chain_id: str, token_address: str, quote_address: str, dex: str) -> float | None:
    """Revérifie le prix courant sur `dex` juste avant d'agir (mitigation du
    risque non-atomique documenté en tête de fichier). Retourne None si
    indisponible (dans ce cas l'appelant doit refuser, pas supposer)."""
    try:
        pairs = dex_sources.fetch_token_pairs(chain_id, token_address)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Revérification de prix échouée pour %s sur %s: %s", token_address, dex, exc)
        return None
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
            return price_usd
        if quote.get("address", "").lower() == token_address.lower() and price_native:
            try:
                return price_usd / float(price_native)
            except (TypeError, ValueError, ZeroDivisionError):
                continue
    return None


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
            f"Quote token {signal['quote_symbol']} non exécutable — seul USDC est supporté pour l'instant."
        )
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
        slip = slippage_pct() / 100.0
        deadline = int(time.time()) + 180

        quote_decimals = _get_decimals(w3, quote_address)
        token_decimals = _get_decimals(w3, token_address)

        # --- Garde-fou solde réel : ne jamais trader plus que ce qui est dispo ---
        quote_contract = w3.eth.contract(address=Web3.to_checksum_address(quote_address), abi=ERC20_ABI)
        quote_balance_units = int(quote_contract.functions.balanceOf(Web3.to_checksum_address(wallet_address)).call())
        amount_in_units = int(trade_size_usd * (10 ** quote_decimals))
        if amount_in_units > quote_balance_units:
            result["error"] = (
                f"Solde USDC insuffisant ({quote_balance_units / 10**quote_decimals:.4f} < "
                f"{trade_size_usd:.4f} requis) — refus."
            )
            return result

        # --- Jambe 1 : achat de token_address avec quote_address sur buy_dex ---
        expected_token_out = trade_size_usd / signal["buy_price_usd"]
        min_token_out_units = int(expected_token_out * (1 - slip) * (10 ** token_decimals))

        approve_result = _ensure_allowance(
            db, w3, token_address=quote_address, owner=wallet_address,
            spender=ROUTER_ADDRESSES[signal["buy_dex"]], amount=amount_in_units,
        )
        if approve_result["error"]:
            result["error"] = f"Approve (jambe achat) échoué : {approve_result['error']}"
            return result

        contract_address, abi, fn_name, args = _build_swap_call(
            signal["buy_dex"], w3, token_in=quote_address, token_out=token_address,
            amount_in=amount_in_units, min_amount_out=min_token_out_units,
            recipient=wallet_address, deadline=deadline,
        )
        buy_result = send_trading_transaction(
            db, rpc_url=_resolve_rpc_url(), contract_address=contract_address, abi=abi,
            function_name=fn_name, args=args, whitelist_target=contract_address,
            action_label=f"arbitrage achat {signal['token_symbol']} sur {signal['buy_dex']}",
        )
        result["tx_hash_buy"] = buy_result["tx_hash"]
        if buy_result["error"]:
            result["error"] = f"Jambe achat échouée : {buy_result['error']}"
            db.mark_arbitrage_signal_executed(signal["id"], tx_hash_buy=result["tx_hash_buy"],
                                               execution_error=result["error"])
            return result

        # --- Solde réel de token_address reçu (robuste au slippage réel) ---
        # Quelques tentatives avec backoff : certains endpoints RPC publics sont
        # répartis sur plusieurs nœuds qui ne sont pas rigoureusement synchronisés
        # entre eux — lire le solde immédiatement après le receipt de la jambe
        # d'achat peut retomber sur un nœud légèrement en retard et renvoyer un
        # faux zéro, alors que la transaction a bien transféré les tokens (vu
        # a posteriori via les logs Transfer). Sans cette tolérance, le bot
        # annule la jambe de vente à tort et laisse le wallet exposé au token
        # acheté au lieu de boucler proprement en USDC.
        token_contract = w3.eth.contract(address=Web3.to_checksum_address(token_address), abi=ERC20_ABI)
        token_balance_units = 0
        for attempt in range(4):
            token_balance_units = int(
                token_contract.functions.balanceOf(Web3.to_checksum_address(wallet_address)).call()
            )
            if token_balance_units > 0:
                break
            if attempt < 3:
                time.sleep(3)
        if token_balance_units <= 0:
            result["error"] = (
                "Jambe achat confirmée on-chain mais solde du token reçu toujours nul après "
                "plusieurs tentatives — anomalie."
            )
            db.mark_arbitrage_signal_executed(signal["id"], tx_hash_buy=result["tx_hash_buy"],
                                               execution_error=result["error"])
            return result

        # --- Revérification du prix avant la jambe 2 (mitigation non-atomique) ---
        fresh_sell_price = _refresh_price_usd(signal["chain"], token_address, quote_address, signal["sell_dex"])
        fresh_buy_price = _refresh_price_usd(signal["chain"], token_address, quote_address, signal["buy_dex"])
        if fresh_sell_price is None:
            result["error"] = (
                "Impossible de revérifier le prix de vente avant la 2e jambe — round-trip interrompu "
                "par prudence (le token acheté reste dans le wallet, revente manuelle possible)."
            )
            db.mark_arbitrage_signal_executed(signal["id"], tx_hash_buy=result["tx_hash_buy"],
                                               execution_error=result["error"])
            return result

        fresh_spread_pct = (
            (fresh_sell_price - fresh_buy_price) / fresh_buy_price * 100 if fresh_buy_price else 0.0
        )
        min_required_spread_pct = fees.dex_fee_pct(signal["buy_dex"]) + fees.dex_fee_pct(signal["sell_dex"])
        if fresh_spread_pct < min_required_spread_pct:
            result["error"] = (
                f"Écart refermé avant la 2e jambe ({fresh_spread_pct:.3f}% < {min_required_spread_pct:.3f}% "
                "de frais minimum) — round-trip interrompu, le token acheté reste dans le wallet."
            )
            db.mark_arbitrage_signal_executed(signal["id"], tx_hash_buy=result["tx_hash_buy"],
                                               execution_error=result["error"])
            return result

        # --- Jambe 2 : vente de token_address contre quote_address sur sell_dex ---
        expected_quote_out = token_balance_units / (10 ** token_decimals) * fresh_sell_price
        min_quote_out_units = int(expected_quote_out * (1 - slip) * (10 ** quote_decimals))

        approve_result = _ensure_allowance(
            db, w3, token_address=token_address, owner=wallet_address,
            spender=ROUTER_ADDRESSES[signal["sell_dex"]], amount=token_balance_units,
        )
        if approve_result["error"]:
            result["error"] = f"Approve (jambe vente) échoué : {approve_result['error']}"
            db.mark_arbitrage_signal_executed(signal["id"], tx_hash_buy=result["tx_hash_buy"],
                                               execution_error=result["error"])
            return result

        contract_address, abi, fn_name, args = _build_swap_call(
            signal["sell_dex"], w3, token_in=token_address, token_out=quote_address,
            amount_in=token_balance_units, min_amount_out=min_quote_out_units,
            recipient=wallet_address, deadline=deadline,
        )
        sell_result = send_trading_transaction(
            db, rpc_url=_resolve_rpc_url(), contract_address=contract_address, abi=abi,
            function_name=fn_name, args=args, whitelist_target=contract_address,
            action_label=f"arbitrage vente {signal['token_symbol']} sur {signal['sell_dex']}",
        )
        result["tx_hash_sell"] = sell_result["tx_hash"]
        if sell_result["error"]:
            result["error"] = f"Jambe vente échouée : {sell_result['error']}"
            db.mark_arbitrage_signal_executed(signal["id"], tx_hash_buy=result["tx_hash_buy"],
                                               tx_hash_sell=result["tx_hash_sell"], execution_error=result["error"])
            return result

        result["executed"] = True
        db.mark_arbitrage_signal_executed(signal["id"], tx_hash_buy=result["tx_hash_buy"],
                                           tx_hash_sell=result["tx_hash_sell"])
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
        w3 = Web3(Web3.HTTPProvider(_resolve_rpc_url(), request_kwargs={"timeout": 15}))
        if not w3.is_connected():
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
            token_balance_units = int(
                token_contract.functions.balanceOf(Web3.to_checksum_address(wallet_address)).call()
            )
            if token_balance_units <= 0:
                # Rien à récupérer (déjà revendu manuellement, ou poussière consommée) :
                # on referme la ligne pour ne plus la retenter à chaque cycle.
                db.mark_arbitrage_signal_executed(
                    position["id"], tx_hash_buy=position["tx_hash_buy"], tx_hash_sell=None,
                    execution_error="Position considérée close : solde on-chain nul au moment de la relance.",
                )
                continue

            token_decimals = _get_decimals(w3, token_address)
            quote_decimals = _get_decimals(w3, quote_address)

            # Cherche le meilleur prix de vente disponible parmi les DEX exécutables
            # (pas forcément le sell_dex d'origine : le marché a pu bouger depuis).
            best_dex, best_price = None, 0.0
            for dex in EXECUTABLE_DEXES:
                price = _refresh_price_usd(position["chain"], token_address, quote_address, dex)
                if price and price > best_price:
                    best_dex, best_price = dex, price
            if best_dex is None:
                outcome["error"] = "Aucun prix de vente disponible sur les DEX exécutables — nouvelle tentative au prochain cycle."
                results.append(outcome)
                continue

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
            min_quote_out_units = int(
                token_balance_units / (10 ** token_decimals) * best_price * (1 - slip) * (10 ** quote_decimals)
            )

            approve_result = _ensure_allowance(
                db, w3, token_address=token_address, owner=wallet_address,
                spender=ROUTER_ADDRESSES[best_dex], amount=token_balance_units,
            )
            if approve_result["error"]:
                outcome["error"] = f"Approve échoué : {approve_result['error']}"
                results.append(outcome)
                continue

            contract_address, abi, fn_name, args = _build_swap_call(
                best_dex, w3, token_in=token_address, token_out=quote_address,
                amount_in=token_balance_units, min_amount_out=min_quote_out_units,
                recipient=wallet_address, deadline=deadline,
            )
            sell_result = send_trading_transaction(
                db, rpc_url=_resolve_rpc_url(), contract_address=contract_address, abi=abi,
                function_name=fn_name, args=args, whitelist_target=contract_address,
                action_label=f"récupération position {position['token_symbol']} sur {best_dex}",
            )
            outcome["tx_hash_sell"] = sell_result["tx_hash"]
            if sell_result["error"]:
                outcome["error"] = sell_result["error"]
                results.append(outcome)
                continue

            db.mark_arbitrage_signal_executed(
                position["id"], tx_hash_buy=position["tx_hash_buy"], tx_hash_sell=outcome["tx_hash_sell"],
            )
            db.add_notification(
                title="✅ Position résiduelle clôturée",
                message=(
                    f"{position['token_symbol']} revendu sur {best_dex} (~{estimated_value_usd:.2f}$) — "
                    f"tx {outcome['tx_hash_sell']}"
                ),
                level="info",
            )
            outcome["recovered"] = True
            results.append(outcome)
        except TradingRefused as exc:
            outcome["error"] = f"Refusé par un garde-fou : {exc}"
            results.append(outcome)
        except Exception as exc:  # noqa: BLE001 - une position en échec ne doit pas bloquer les autres
            logger.exception("Erreur inattendue en récupérant une position ouverte")
            outcome["error"] = f"Erreur inattendue : {exc}"
            results.append(outcome)

    return results
