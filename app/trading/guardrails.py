"""Garde-fous dédiés au TRADING réel (Phase 2) — distincts de `app.wallet.autosign`.

Pourquoi un module séparé plutôt que de réutiliser `autosign.py` tel quel :
- Wallet différent : l'auto-claim (Phase 4 du TODO d'origine) utilise un wallet
  "autosigner" GÉNÉRÉ par le bot (`data/autosigner_keystore.json`), à faible
  enjeu. Le trading utilise le wallet MetaMask PRINCIPAL de l'utilisateur,
  IMPORTÉ (`data/trading_wallet_keystore.json`, voir `app/wallet/signer.py`
  `import_wallet_from_private_key`). Mélanger les deux exposerait tout le
  solde du wallet principal aux garde-fous (et bugs potentiels) conçus pour
  de simples claims à faible risque.
- Whitelist différente par nature : l'auto-claim whiteliste des CONTRATS
  arbitraires (fournis par le code d'un projet revu par un humain). Le trading
  whiteliste des ROUTERS DEX bien connus et fixes (voir `app/trading/routers.py`)
  — volontairement non modifiable par simple variable d'environnement, pour
  éviter qu'une erreur de configuration n'autorise un contrat arbitraire à
  dépenser les fonds du wallet principal.

Tant que `TRADING_ENABLED=false` (défaut) OU que `trading.enabled: false`
dans config.yaml OU que la confirmation explicite `TRADING_LIVE_CONFIRMED`
n'a pas la valeur exacte attendue, `is_trading_live()` retourne False et
aucune transaction réelle n'est jamais tentée (voir `app/trading/executor.py`).
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone

from app.database import Database
from app.killswitch import is_stopped, stop as killswitch_stop
from app.trading.routers import FLASHLOAN_CONTRACT_ADDRESSES, ROUTER_ADDRESSES
from app.wallet.signer import DEFAULT_KEYSTORE_PATH, load_signer

logger = logging.getLogger("app.trading.guardrails")

DEFAULT_TRADING_KEYSTORE_PATH = "data/trading_wallet_keystore.json"
LIVE_CONFIRMATION_PHRASE = "JE CONFIRME LE TRADING REEL AVEC MON WALLET PRINCIPAL"

# Nombre de tentatives + délai entre tentatives en cas de 429 (rate-limit) du
# RPC public gratuit (https://mainnet.base.org) lors de la simulation d'une
# transaction. Sans ce retry, un simple rate-limit transitoire est confondu
# avec un VRAI revert (manque de rentabilité), ce qui déclenche à tort le
# disjoncteur de sécurité (`app.trading.pair_health`, 3 échecs consécutifs →
# pause de 2h) alors que rien n'indique que le trade n'était pas rentable.
#
# Relevé en conditions réelles (endpoint Infura configuré) : les 429 ne sont
# PAS rares/isolés mais quasi-CONTINUS sous la charge actuelle (plusieurs par
# minute, sur des appels complètement différents — gas price, decimals, solde,
# connexion). 3 tentatives à 2s fixes (≈4s de patience totale) s'épuisaient
# avant la fin du rate-limit et laissaient échouer la tentative d'exécution
# quand même. Backoff exponentiel (2s, 4s, 8s, 16s, 32s) sur 6 tentatives
# (≈62s de patience totale) pour survivre aux rafales observées.
_RATE_LIMIT_MAX_ATTEMPTS = 6
_RATE_LIMIT_RETRY_DELAY_SECONDS = 2.0


def is_rate_limit_error(exc: Exception) -> bool:
    """Détecte un 429 Too Many Requests (ou équivalent) renvoyé par le RPC,
    par opposition à un VRAI revert de simulation (manque de rentabilité,
    slippage, etc.) — seule l'erreur réseau transitoire doit être retentée."""
    message = str(exc)
    return "429" in message or "too many requests" in message.lower()


def call_with_rate_limit_retry(fn_call):
    """Exécute `fn_call` (ex. `fn.call(...)`, `fn.estimate_gas(...)` ou
    `w3.is_connected()`) en retentant jusqu'à `_RATE_LIMIT_MAX_ATTEMPTS` fois
    avec un délai qui DOUBLE à chaque tentative (2s, 4s, 8s, ...) UNIQUEMENT si
    l'échec est un rate-limit RPC (429) — toute autre exception (revert réel)
    est immédiatement propagée sans retry.

    Nom PUBLIC (pas de préfixe `_`) : réutilisé par `app.trading.executor`
    (voir `execute_signal`/`execute_triangular_signal`), dont les vérifications
    de solde/connexion RPC avant envoi d'une transaction réelle subissaient le
    même 429 transitoire d'un RPC gratuit/limité (ex: Infura free tier) sans
    aucun retry — confirmé en conditions réelles responsable d'un échec
    QUASI-SYSTÉMATIQUE ("RPC Base injoignable" / "Erreur inattendue : 429 ...")
    de chaque tentative d'exécution réelle malgré des signaux rentables."""
    last_exc: Exception | None = None
    delay = _RATE_LIMIT_RETRY_DELAY_SECONDS
    for attempt in range(1, _RATE_LIMIT_MAX_ATTEMPTS + 1):
        try:
            return fn_call()
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if not is_rate_limit_error(exc) or attempt == _RATE_LIMIT_MAX_ATTEMPTS:
                raise
            logger.warning(
                "RPC rate-limited (429), nouvelle tentative %d/%d dans %.1fs : %s",
                attempt + 1, _RATE_LIMIT_MAX_ATTEMPTS, delay, exc,
            )
            time.sleep(delay)
            delay *= 2
    raise last_exc  # pragma: no cover - inatteignable (la boucle raise ou return toujours)


class TradingRefused(Exception):
    """Levée quand une transaction de trading est refusée par un garde-fou (pas une panne technique)."""


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    try:
        return float(raw) if raw not in (None, "") else default
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    try:
        return int(raw) if raw not in (None, "") else default
    except ValueError:
        return default


def trading_keystore_path() -> str:
    return os.environ.get("WALLET_TRADING_KEYSTORE_PATH", DEFAULT_TRADING_KEYSTORE_PATH)


def max_gas_price_gwei() -> float:
    # Base est une L2 où le gas est quasi toujours < 0.1 gwei : une limite à 2
    # gwei laisse une marge large tout en bloquant toute anomalie réseau.
    return _env_float("TRADING_MAX_GAS_PRICE_GWEI", 2.0)


def max_tx_per_day() -> int:
    """`TRADING_MAX_TX_PER_DAY=0` (ou négatif) désactive explicitement ce
    garde-fou (quota illimité) — voir l'appel dans `send_trading_transaction`."""
    return _env_int("TRADING_MAX_TX_PER_DAY", 5)


def slippage_pct() -> float:
    return _env_float("TRADING_SLIPPAGE_PCT", 0.5)


def is_trading_live(config: dict) -> bool:
    """Toutes les conditions suivantes doivent être réunies, sans exception :
    1. `trading.enabled: true` dans config.yaml (feature globalement activée)
    2. `TRADING_ENABLED=true` dans l'environnement (.env du Pi, jamais Git)
    3. `TRADING_LIVE_CONFIRMED` == la phrase exacte ci-dessus (évite qu'une
       activation accidentelle de (1)+(2) suffise à elle seule)
    4. Le killswitch global n'est pas actif
    """
    trading_cfg = config.get("trading", {}) if config else {}
    if not trading_cfg.get("enabled"):
        return False
    if not _env_bool("TRADING_ENABLED", False):
        return False
    if os.environ.get("TRADING_LIVE_CONFIRMED", "") != LIVE_CONFIRMATION_PHRASE:
        return False
    return True


def _notify(db: Database, title: str, message: str, level: str = "info") -> None:
    db.add_notification(title=title, message=message, level=level)


def _anomaly_stop(db: Database, reason: str) -> None:
    killswitch_stop(db)
    logger.error("Anomalie trading -> killswitch activé : %s", reason)
    _notify(db, "🛑 Trading : arrêt automatique", reason, level="alert")


def send_trading_transaction(
    db: Database,
    *,
    rpc_url: str,
    contract_address: str,
    abi: list[dict],
    function_name: str,
    args: tuple,
    whitelist_target: str,
    action_label: str,
    count_against_daily_limit: bool = True,
) -> dict:
    """Simule puis (si tout est vert) signe et diffuse UNE transaction de trading
    (approve OU swap — l'orchestration multi-étapes est gérée par
    `app/trading/executor.py`, pas ici).

    `whitelist_target` est l'adresse qui DOIT être un router whitelisté :
    - pour un `approve(spender, amount)`, c'est `spender` (le router autorisé
      à dépenser les tokens, PAS le token lui-même qui est `contract_address`),
    - pour un swap, c'est `contract_address` (le router appelé directement).
    Ce découplage évite qu'un `approve` soit refusé à tort (le token appelé
    n'est jamais lui-même dans la whitelist de routers) tout en garantissant
    qu'aucune transaction, de quelque nature, ne peut jamais bénéficier à une
    adresse hors de la whitelist fixe de `app/trading/routers.py`.

    `count_against_daily_limit=False` exempte CETTE transaction du plafond
    `TRADING_MAX_TX_PER_DAY` (tous les autres garde-fous — killswitch, gas
    price, whitelist — restent appliqués sans exception). Réservé aux
    transactions de CLÔTURE d'une position déjà ouverte (voir
    `app.trading.executor.recover_open_positions`) : le plafond quotidien vise
    à limiter la prise de nouveau risque, pas à bloquer le retour à USDC d'une
    exposition déjà existante — sans cette exemption, un wallet peut rester
    bloqué en token volatile jusqu'au lendemain une fois le quota atteint.

    Retourne toujours un dict `{"sent": bool, "tx_hash": str|None, "error": str|None}`.
    """
    # Whitelist fixe = routers DEX connus + contrat(s) FlashArbitrage déployé(s)
    # (voir app/trading/routers.py) — jamais étendue par variable d'environnement.
    whitelist = {addr.lower() for addr in ROUTER_ADDRESSES.values()} | {
        addr.lower() for addr in FLASHLOAN_CONTRACT_ADDRESSES
    }
    if whitelist_target.lower() not in whitelist:
        raise TradingRefused(
            f"{whitelist_target} absent de la whitelist fixe des routers DEX/contrats flashloan "
            f"({sorted(whitelist)}) — refusé."
        )

    if is_stopped(db):
        raise TradingRefused("Killswitch actif : aucune transaction de trading n'est envoyée.")

    from web3 import Web3

    w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 15}))
    try:
        connected = call_with_rate_limit_retry(lambda: w3.is_connected())
    except Exception:  # noqa: BLE001 - un vrai échec réseau persistant reste traité comme "injoignable"
        connected = False
    if not connected:
        return {"sent": False, "tx_hash": None, "error": "RPC injoignable"}

    signer_account = load_signer(
        keystore_path=trading_keystore_path(),
        passphrase=os.environ.get("WALLET_TRADING_PASSPHRASE"),
    )
    wallet_address = signer_account.address

    if count_against_daily_limit and max_tx_per_day() > 0 and db.today_executed_trades_count() >= max_tx_per_day():
        raise TradingRefused(f"Limite quotidienne de trades atteinte (TRADING_MAX_TX_PER_DAY={max_tx_per_day()}).")

    gas_price_wei = call_with_rate_limit_retry(lambda: w3.eth.gas_price)
    gas_price_gwei = float(Web3.from_wei(gas_price_wei, "gwei"))
    if gas_price_gwei > max_gas_price_gwei():
        raise TradingRefused(
            f"Gas price {gas_price_gwei:.3f} gwei > limite TRADING_MAX_GAS_PRICE_GWEI ({max_gas_price_gwei()})."
        )

    contract = w3.eth.contract(address=Web3.to_checksum_address(contract_address), abi=abi)
    fn = getattr(contract.functions, function_name)(*args)

    # --- Simulation obligatoire avant tout envoi -----------------------------
    # Retry automatique UNIQUEMENT sur rate-limit RPC (429, voir
    # `call_with_rate_limit_retry`) : le RPC public gratuit
    # (https://mainnet.base.org par défaut) renvoie parfois des 429 sous
    # charge, ce qui serait sinon confondu avec un vrai revert et déclencherait
    # à tort le disjoncteur de sécurité (pair_health) alors que la rentabilité
    # du trade n'a jamais été réellement testée.
    try:
        call_with_rate_limit_retry(lambda: fn.call({"from": wallet_address}))
        estimated_gas = call_with_rate_limit_retry(lambda: fn.estimate_gas({"from": wallet_address}))
    except Exception as exc:  # noqa: BLE001
        return {"sent": False, "tx_hash": None,
                "error": f"Simulation échouée pour {action_label or function_name} : {exc}"}

    try:
        tx = fn.build_transaction({
            "from": wallet_address,
            "gas": int(estimated_gas * 1.3),
            "gasPrice": gas_price_wei,
            "nonce": w3.eth.get_transaction_count(wallet_address),
            "chainId": w3.eth.chain_id,
        })
        signed = signer_account.sign_transaction(tx)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction).hex()
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
    except Exception as exc:  # noqa: BLE001
        _anomaly_stop(db, f"Échec d'envoi pour {action_label or function_name} : {exc}")
        return {"sent": False, "tx_hash": None, "error": str(exc)}
    finally:
        signer_account = None  # noqa: F841 - abandon de la référence à la clé déchiffrée au plus vite

    if receipt.get("status") != 1:
        _anomaly_stop(db, f"Transaction {tx_hash} ({action_label or function_name}) a échoué on-chain (status=0).")
        return {"sent": True, "tx_hash": tx_hash, "error": "Transaction échouée on-chain (status=0)"}

    _notify(db, "✅ Transaction de trading envoyée",
            f"{action_label or function_name} — tx {tx_hash}", level="info")
    return {"sent": True, "tx_hash": tx_hash, "error": None}
